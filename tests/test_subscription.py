"""The push path: world.dirty reaches subscribed clients the moment the
world changes.

The transport is streamable HTTP (the ProArt's way in), the change is made
through the same MCP session, and the client holds a
``subscriptions/listen`` stream — the MCP-standard form of "subscribe to
world.dirty". One upstream SSE connection in the server fans the
notification out to every subscribed session.
"""

from __future__ import annotations

import asyncio

from conftest import tool_body
from mcp.client.subscriptions import ResourceUpdated


def test_world_dirty_pushes_to_a_subscribed_client(core_server):
    url, _core = core_server

    async def listen_for_dirty(client) -> list:
        """Hold the subscription open while the world changes."""
        got: list = []
        async with client.listen(
                resource_subscriptions=["world://dirty"]) as sub:
            # the change goes through the same client's own tool call: the
            # watcher still publishes to the bus, which fans out to the
            # listen stream — the loop is closed through the core's log,
            # not through the tool's return value
            result = await client.call_tool("propose_recipe_edit", {
                "word": "props.harbor.crates", "node": "crates_01",
                "params": {"count": 4, "style": "plain", "from_library": 0,
                           "seed": 9},
                "prediction": {"claim": "four plain crates read as plain",
                               "claim_type": "material_read",
                               "confidence": "guess", "rung": 1,
                               "resolution": 512, "seed": 9}})
            assert not result.is_error
            # the notification lands within seconds: one SSE hop
            # (core -> watcher -> bus -> listen stream)
            async with asyncio.timeout(15):
                async for event in sub:
                    got.append(event)
                    if any(isinstance(e, ResourceUpdated) for e in got):
                        break
        return got

    from conftest import _mcp_http

    with _mcp_http(url) as client:
        session, loop = client.raw()

        def drive():
            return asyncio.run_coroutine_threadsafe(
                listen_for_dirty(session), loop).result(60)

        events = drive()
    assert events, "no world.dirty notification reached the subscriber"
    assert any(isinstance(e, ResourceUpdated) and str(e.uri) == "world://dirty"
               for e in events), events


def test_the_http_transport_serves_tools(mcp_http):
    """The streamable-HTTP door (the ProArt's transport) serves the same
    toolset and answers calls."""
    body = tool_body(mcp_http.call("read_night_plan"))
    assert body["slate"]["bar"]["gpu_minutes"] == 360
    assert "local" in body["slate"]["money"]
