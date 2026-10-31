"""The HTTP client of the core's own doors.

This is the whole contact surface between world-mcp and builder-ide: JSON
over the API of ``core.api`` (core-spec §4.2) plus the SSE event stream
(§4.3). Nothing here imports the core — the core is the single writer that
owns the ledger, and a second writer process would break §1.0, so this
server is a client and only a client.

Two ways to read the log:

* :meth:`CoreClient.read_events` — a one-shot backlog read: open the SSE
  stream with ``since_seq``/``types``, collect what the ledger already
  holds, close. Rewind is free (the feed is the log, §4.3).
* :meth:`CoreClient.watch_events` — a long-lived async generator that
  yields events as they land and reconnects with a resuming cursor. One
  connection per server process serves every subscriber.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator
from uuid import uuid4

import anyio
import httpx

from .config import Settings

log = logging.getLogger(__name__)

MAX_BACKLOG = 500
RECONNECT_SECONDS = 1.0
RETRY_SECONDS = 0.25


class CoreError(RuntimeError):
    """An error the core's door returned, verbatim.

    The core's refusals are product moments ("params outside the word's
    schema are refused at the door"), so the message is carried through to
    the agent untouched, with the code and any extra fields riding along.
    """

    def __init__(self, status: int, payload: dict | None = None) -> None:
        err = (payload or {}).get("error") or {}
        self.status = status
        self.code = err.get("code", "core_error")
        self.message = err.get("message", f"core returned {status}")
        self.extra = {k: v for k, v in err.items() if k not in ("code", "message")}
        super().__init__(f"{self.code}: {self.message}")

    @property
    def is_refusal(self) -> bool:
        return self.code == "refused"


class CoreClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(base_url=settings.core_url, timeout=30.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ JSON doors

    def _headers(self, principal: str, idem_key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.settings.token(principal)}"}
        if idem_key:
            headers["Idempotency-Key"] = idem_key
        return headers

    async def get(self, path: str, *, principal: str = "panel",
                  params: dict | None = None) -> Any:
        r = await self._send("GET", path, principal=principal, params=params)
        if r.status_code >= 400:
            raise CoreError(r.status_code, _json_or_none(r))
        return r.json()

    async def post(self, path: str, body: dict | None = None, *,
                   principal: str, idem_key: str | None = None) -> Any:
        key = idem_key or f"world-mcp:{uuid4().hex}"
        r = await self._send("POST", path, principal=principal,
                             json=body or {}, idem_key=key)
        if r.status_code >= 400:
            raise CoreError(r.status_code, _json_or_none(r))
        return r.json()

    async def _send(self, method: str, path: str, *, principal: str,
                    idem_key: str | None = None, **kwargs) -> httpx.Response:
        """One door call, with one transport-level retry.

        A refusal is never retried (it is information, not a fault), but a
        dropped connection under load is worth one more try — and always a
        safe one: every POST here carries an Idempotency-Key, so the core
        answers the retry from the first write, never with a second one.
        """
        headers = self._headers(principal, idem_key)
        try:
            return await self._http.request(method, path, headers=headers,
                                            **kwargs)
        except httpx.TransportError:
            await _sleep(RETRY_SECONDS)
            return await self._http.request(method, path, headers=headers,
                                            **kwargs)

    # ------------------------------------------------------------ the stream

    async def health(self) -> dict:
        """``/healthz`` — one door that says the core is alive."""
        return await self.get("/healthz")

    async def read_events(self, types: list[str], *, since_seq: int = 0,
                          limit: int = MAX_BACKLOG) -> list[dict]:
        """A one-shot backlog read over ``GET /v1/events``.

        The ledger answers the first batch immediately; we collect frames
        until the stream goes quiet (the log is drained up to ``limit``) or
        the timeout passes, then close. A filter the log has nothing for
        simply returns an empty list after the timeout.

        Best-effort by design: a door refusal (4xx) is loud, but a dropped
        stream or a bad frame returns what was collected — the read feeds
        history halves and cursors, which must degrade, not fail.
        """
        events: list[dict] = []
        try:
            async with self._http.stream(
                "GET", "/v1/events",
                params={"since_seq": since_seq, "types": ",".join(types)},
                headers=self._headers("panel"),
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise CoreError(response.status_code, _loads_or_none(body))
                lines = response.aiter_lines()
                frame: dict | None = None
                quiet = self.settings.sse_quiet_seconds
                deadline = anyio.current_time() + self.settings.sse_read_timeout
                while len(events) < limit:
                    remaining = deadline - anyio.current_time()
                    if remaining <= 0:
                        break
                    # once data has been seen, a quiet gap means the backlog is
                    # drained; until then, wait out the whole budget for the
                    # first batch
                    budget = min(quiet if (frame is not None or events)
                                 else remaining, remaining)
                    with anyio.move_on_after(budget) as scope:
                        try:
                            line = await lines.__anext__()
                        except StopAsyncIteration:
                            break
                    if scope.cancelled_caught:
                        if frame is not None or events:
                            break  # drained: nothing new within the quiet gap
                        continue  # still waiting for the first batch
                    if line.startswith("data:"):
                        try:
                            payload = json.loads(line[5:].strip() or "null")
                        except json.JSONDecodeError:
                            frame = None
                            continue  # one bad frame is skipped, not fatal
                        if isinstance(payload, dict):
                            frame = payload
                    elif not line.strip() and frame is not None:
                        events.append(frame)
                        frame = None
                    elif line.startswith(":") and frame is None and events:
                        break  # a heartbeat with nothing pending: drained
        except CoreError:
            raise
        except Exception as exc:  # noqa: BLE001 - a drop degrades the read
            log.warning("event backlog read dropped after %d event(s): %s",
                        len(events), exc)
        return events

    async def watch_events(self, types: list[str], *,
                           since_seq: int = 0) -> AsyncIterator[dict]:
        """Yield events as the core's log grows; reconnect with a cursor.

        One connection per server process: the watcher fans notifications
        out to every subscribed MCP session from this single upstream
        stream, which is the whole reason a Thor-hosted core stays cheap to
        watch from many agents.
        """
        cursor = since_seq
        while True:
            try:
                async with self._http.stream(
                    "GET", "/v1/events",
                    params={"since_seq": cursor, "types": ",".join(types)},
                    headers=self._headers("panel"),
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "replace")
                        raise CoreError(response.status_code, _loads_or_none(body))
                    frame: dict | None = None
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            payload = json.loads(line[5:].strip() or "null")
                            if isinstance(payload, dict):
                                frame = payload
                        elif not line.strip() and frame is not None:
                            cursor = max(cursor, int(frame.get("seq", cursor)))
                            yield frame
                            frame = None
            except Exception as exc:  # noqa: BLE001 - reconnect on any drop
                # cancellation is a BaseException and propagates untouched
                log.warning("event stream dropped (%s); reconnecting from seq %s",
                            exc, cursor)
            await _sleep(RECONNECT_SECONDS)


def _json_or_none(r: httpx.Response) -> dict | None:
    return _loads_or_none(r.text)


def _loads_or_none(text: str) -> dict | None:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


async def _sleep(seconds: float) -> None:
    await anyio.sleep(seconds)
