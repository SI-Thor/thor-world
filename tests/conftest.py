"""Test fixtures: a real core behind its HTTP door, and MCP clients at it.

Every test gets its own ``$BUILDER_HOME`` (a temp directory with a real
SQLite ledger) served by a real uvicorn server — the same rule
builder-ide's own tests follow, so no test shares a ledger and none touches
a developer's home.

The MCP clients come in three transports, each proving a different thing:

* ``stdio_session`` — the server as a *subprocess*, the way a local agent
  (Claude Code, zcode) actually runs it. Tool and resource tests use this
  one: if the package can't boot from a clean process, nothing else
  matters.
* ``http_server`` — the streamable-HTTP transport, the way the ProArt's
  agents reach a Thor-hosted server.
* in-memory — never used for the suite's claims; the subprocess is cheap
  enough and far more honest.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from core_host import serve_core


@pytest.fixture
def home(tmp_path) -> Path:
    return tmp_path / "builder-home"


@pytest.fixture
def core_server(home):
    """A wired, seeded core behind real uvicorn. Yields (url, core)."""
    with serve_core(home) as pair:
        yield pair


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _AsyncClient:
    """An MCP client session owned by one background loop thread.

    The session's context managers must be entered and exited by the same
    task (anyio cancel scopes demand it), so the whole session lives in
    one coroutine on a dedicated loop and tests submit work to it.
    """

    def __init__(self, open_session):
        import asyncio
        import threading

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever,
                                        daemon=True)
        self._ready = threading.Event()
        self._holder: dict = {}
        self._error: BaseException | None = None
        self._open_session = open_session
        self._thread.start()
        self._main = asyncio.run_coroutine_threadsafe(
            self._session_main(), self._loop)
        if not self._ready.wait(60):  # pragma: no cover - a slow environment
            self.close()
            raise RuntimeError("the MCP session never came up")

    async def _session_main(self):
        import asyncio

        stop = asyncio.Event()
        try:
            async with self._open_session() as session:
                initialize = getattr(session, "initialize", None)
                if initialize is not None:
                    await initialize()
                self._holder["session"] = session
                self._holder["stop"] = stop
                self._ready.set()
                await stop.wait()
        except BaseException as exc:  # pragma: no cover - surfaced at close
            self._error = exc
            self._ready.set()

    def _run(self, coro, timeout=60):
        import asyncio

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    def call(self, name, arguments=None):
        session = self._holder["session"]
        return self._run(session.call_tool(name, arguments or {}))

    def call_all(self, name, argsets):
        """Several calls in flight on one session, answers as a list.

        One MCP session multiplexes requests by id, so this is the shape a
        chatty agent (or several agents behind one server) actually makes —
        the concurrent-read hammer's muzzle.
        """
        import asyncio

        session = self._holder["session"]

        async def together():
            return list(await asyncio.gather(
                *(session.call_tool(name, args) for args in argsets)))

        return self._run(together())

    def read(self, uri):
        session = self._holder["session"]
        return self._run(session.read_resource(uri))

    def list_tools(self):
        return self._run(self._holder["session"].list_tools())

    def list_resources(self):
        return self._run(self._holder["session"].list_resources())

    def list_resource_templates(self):
        return self._run(self._holder["session"].list_resource_templates())

    def raw(self):
        """The live ClientSession, for callers driving it directly on the
        loop (the subscription test)."""
        return self._holder["session"], self._loop

    def close(self):
        stop = self._holder.get("stop")
        if stop is not None:
            # Event.set() is synchronous on 3.10+; schedule it on the loop
            self._loop.call_soon_threadsafe(stop.set)
        self._main.result(30)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=15)
        if self._error:  # pragma: no cover - a session that died mid-test
            raise self._error


@contextmanager
def _mcp_stdio(core_url: str):
    """The server as a subprocess over stdio — an agent's eye view."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "world_mcp", "serve", "--stdio"],
        env={"BUILDER_CORE_URL": core_url},
    )

    class _Stdio:
        """stdio_client's streams, then a ClientSession over them."""

        async def __aenter__(self):
            self._cm = stdio_client(params)
            streams = await self._cm.__aenter__()
            self._session_cm = ClientSession(*streams)
            return await self._session_cm.__aenter__()

        async def __aexit__(self, *exc):
            await self._session_cm.__aexit__(*exc)
            await self._cm.__aexit__(*exc)

    client = _AsyncClient(_Stdio)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def mcp(core_server):
    """An MCP client at the world server, over stdio (a subprocess)."""
    url, _core = core_server
    with _mcp_stdio(url) as client:
        yield client


@contextmanager
def _mcp_http(core_url: str):
    """The server over streamable HTTP, the ProArt's transport.

    The client is the SDK's high-level ``Client`` in its default ``auto``
    mode: it probes ``server/discover`` and opens the session in the
    2026-07-28 protocol era — the era where ``subscriptions/listen`` exists.
    """
    import threading

    import uvicorn

    from mcp.client.client import Client

    from world_mcp.config import Settings
    from world_mcp.server import build_server

    settings = Settings(core_url=core_url,
                        owner_token="dev-token-casey-world-mcp",
                        character_token="dev-token-props-world-mcp",
                        panel_token="dev-token-panel")
    app = build_server(settings).streamable_http_app()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1",
                                           port=port, log_level="error",
                                           lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:  # pragma: no cover - a broken environment
        raise RuntimeError("uvicorn did not start")

    class _Http:
        async def __aenter__(self):
            self._client = Client(f"http://127.0.0.1:{port}/mcp")
            return await self._client.__aenter__()

        async def __aexit__(self, *exc):
            await self._client.__aexit__(*exc)

    client = _AsyncClient(_Http)
    try:
        yield client
    finally:
        client.close()
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=5)


@pytest.fixture
def mcp_http(core_server):
    url, _core = core_server
    with _mcp_http(url) as client:
        yield client


def tool_body(result) -> dict:
    """The JSON a tool returned (tools speak one text block of JSON)."""
    assert not result.is_error, result.content[0].text if result.content else result
    return json.loads(result.content[0].text)


def resource_body(result) -> dict:
    return json.loads(result.contents[0].text)
