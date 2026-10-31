"""Concurrent reads: the packet fans out, the core holds.

world-mcp used to take the core's doors strictly one at a time: the core
served its store-reading GETs from one SQLite connection across the
threadpool, and two reads in the same tick could collide into a 500 —
drawn on about one run in three while building this server (design note
§8.5). The core closed that race at the source — every read of the shared
connection now takes the one lock the writes held (builder-ide core-doors
180ee04, chosen by measurement over per-thread connections) — so the
server composes the packet's six reads concurrently again.

These tests keep the hammer on the seam, from both sides: the server
answering a volley of context packets through a real MCP session, and the
core doors taking a raw concurrent volley of exactly the reads the packet
composes. A regression on either side — the core's lock, or a race this
server grows of its own — fails here loudly.
"""

from __future__ import annotations

import asyncio

import httpx

from conftest import tool_body

#: Packets in flight in the hammer. Each packet fans into six door reads
#: (five JSON GETs plus the SSE backlog), so 12 packets is 72 reads at the
#: core — well past the "two GETs in one tick" that used to 500.
PACKETS = 12

#: The store-reading doors the context packet composes, hammered raw.
READ_DOORS = ["/v1/slate", "/v1/budget", "/v1/vocab", "/v1/branches",
              "/v1/work-orders"]

PANEL = {"Authorization": "Bearer dev-token-panel"}


def test_the_server_answers_a_volley_of_concurrent_packets(mcp):
    results = mcp.call_all("get_context", [{} for _ in range(PACKETS)])
    assert len(results) == PACKETS
    for result in results:
        body = tool_body(result)  # asserts the call was no error
        assert body["project"]["branch"] == "br_main"


def test_the_core_takes_a_raw_concurrent_volley_without_a_500(core_server):
    """The door set of one packet, fired concurrently and directly — the
    shape that answered 20–37 % 500s (``sqlite3.InterfaceError`` in
    ``count_events``) before the core took the lock."""
    url, _core = core_server

    async def volley() -> list[int]:
        limits = httpx.Limits(max_connections=64)
        async with httpx.AsyncClient(base_url=url, limits=limits) as http:
            calls = []
            for _ in range(PACKETS):
                for door in READ_DOORS:
                    calls.append(http.get(door, headers=PANEL))
            codes = await asyncio.gather(*calls, return_exceptions=True)
        return [c if isinstance(c, Exception) else c.status_code
                for c in codes]

    import anyio

    codes = anyio.run(volley)
    bad = [c for c in codes if not isinstance(c, int) or c >= 500]
    assert not bad, f"{len(bad)}/{len(codes)} concurrent reads failed: {bad[:5]}"
    assert all(c == 200 for c in codes if isinstance(c, int))
