"""The end-to-end demo: a scripted agent session at the world (no real LLM).

An agent — any agent, here a script standing in for a cloud LLM — is asked,
in the owner's words, to make a bridge sturdier. It works only through the
MCP server's doors: it reads the context packet, files a terse work order
with a selection, hears the core's one-line confirmation, says "go", and the
ledger shows who did what. Then, to show the world answering back, it makes
one typed vocabulary edit and receives the ``world.dirty`` push.

Run it:

    .venv/bin/python demo/agent_session.py

Everything is local and disposable: the core boots on a temp $BUILDER_HOME
(``core_host.py``), the world-mcp server runs as a stdio subprocess — the
way a local agent (Claude Code, zcode, mcode) actually runs it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # for core_host

from core_host import serve_core_async  # noqa: E402

#: The scripted intent behind "make these sturdier". A real cloud LLM would
#: write this from the context packet; the shape is the core's own contract
#: (spec §1.3: no acceptance check, no queue).
STURDIER_INTENT = {
    "title": "Sturdier south span",
    "goal": "Raise the south span load factor to 2.0, keeping the sway",
    "spec_sentence": "reinforce the south span so the analytic load check "
                     "passes at 2x",
    "acceptance": [{"kind": "constraint", "spec": {
        "path": "structure.south_span.params.load_factor", "op": ">=",
        "value": 2.0, "method": "analytic_member_forces"}}],
    "estimate": {"build_verify": {"gpu_minutes": 20, "wall_minutes": 40},
                 "generate_test": {"gpu_minutes": 10, "wall_minutes": 20}},
}


def say(beat: str, line: str = "") -> None:
    print(f"\n== {beat} " + "=" * (66 - len(beat)))
    if line:
        print(line)


async def agent_session(core_url: str, host_core) -> None:
    from mcp.client.client import Client
    from mcp.client.stdio import StdioServerParameters
    from mcp.client.subscriptions import ResourceUpdated
    from mcp.types import TextContent

    def text(result) -> str:
        return "".join(
            c.text for c in result.content if isinstance(c, TextContent))

    def body(result) -> dict:
        import json

        assert not result.is_error, text(result)
        return json.loads(text(result))

    # The world-mcp server as a subprocess — the way a local agent runs it.
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "world_mcp", "serve", "--stdio"],
        env={"BUILDER_CORE_URL": core_url},
    )
    async with Client(params) as agent:
        say("the packet", "the agent reads world://context before acting — "
                          "the situation explains the few words to come.")
        pkt = body_of(await agent.read_resource("world://context"))
        print(f"project                   : branch {pkt['project']['branch']} "
              f"@ {pkt['project']['head']}")
        print(f"budget left tonight       : "
              f"{pkt['project']['budget_left_tonight']}")
        print(f"vocabulary gaps           : "
              f"{pkt['project']['vocabulary_gaps']}")
        print("view                      : nothing selected yet — the "
              "selection rides with the owner's words")

        say("the terse request",
            'the owner says "make these sturdier" with a selection; '
            "the agent files it through the door and the core compiles it.")
        filed = body(await agent.call_tool("file_work_order", {
            "request": "make these sturdier",
            "selection": ["span_south"],
            "intent": STURDIER_INTENT,
        }))
        draft = filed["draft"]
        print(f"work order   : {draft['id']} ({draft['state']})")
        print(f"targets      : {draft['targets']}  "
              "(resolved through the node registry)")
        print(filed["confirm_line"])

        say('the owner says "go"',
            "one word back through confirm_work_order; idempotent by key.")
        confirmed = body(await agent.call_tool("confirm_work_order", {
            "work_order": draft["id"], "answer": "go",
            "idem_key": "demo:sturdier:go"}))
        wo = confirmed["work_order"]
        print(f"work order   : {wo['id']} -> {wo['state']}")
        print(f"recorded_as  : {confirmed['recorded_as']} "
              f"(agent: {confirmed['agent']} — the row's own answer)")

        say("the ledger", "everything the agent did is in the core's audit "
                          "log, with its author — checked at the log itself.")
        # The demo host holds the core; this read is the proof, not the
        # tool's say-so.
        for ev in host_core.store.events_since(
                0, types=["wo.filed", "wo.confirmed"]):
            print(f"  seq {ev.seq:>3}  {ev.type:<14} "
                  f"actor={ev.actor.id} agent={ev.actor.agent}")

        say("the world answers back",
            "one typed vocabulary edit — sealed prediction first — and the "
            "agent hears world.dirty without asking.")
        async with agent.listen(
                resource_subscriptions=["world://dirty"]) as dirty:
            edit = body(await agent.call_tool("propose_recipe_edit", {
                "word": "props.harbor.crates", "node": "crates_01",
                "params": {"count": 6, "style": "weathered",
                           "from_library": 0, "seed": 771},
                "prediction": {
                    "claim": "six weathered crates will read as weathered "
                             "timber",
                    "claim_type": "material_read", "confidence": "guess",
                    "basis": "0 observations",
                    "rung": 2, "resolution": 1024, "seed": 771},
            }))
            sealed = edit["sealed_prediction"]
            print(f"sealed       : {sealed['seal_hash'][:24]}… "
                  f"(the bet exists before its render does)")
            print(f"edit         : committed={edit['committed']} "
                  f"version={edit['version']} "
                  f"recorded_as={edit['recorded_as']}")

            async with asyncio.timeout(15):
                async for event in dirty:
                    if isinstance(event, ResourceUpdated):
                        print(f"pushed       : resource updated — "
                              f"{event.uri} (the recipe moved)")
                        break
        recipe = body_of(await agent.read_resource("world://recipe"))
        head = next(b["head"] for b in recipe["branches"]
                    if b["id"] == "br_main")
        print(f"main head    : {head}")

    say("done", "the agent never wrote code into the world; every write "
               "went through a typed door and the ledger names its author.")


def body_of(resource) -> dict:
    import json

    return json.loads(resource.contents[0].text)


async def main() -> int:
    import tempfile

    say("boot", "a seeded builder-ide core on a temp $BUILDER_HOME, behind "
                "its HTTP door; world-mcp starts as a stdio subprocess.")
    async with serve_core_async(
            Path(tempfile.mkdtemp(prefix="world-mcp-demo-"))) as (url, core):
        print(f"core at {url}")
        await agent_session(url, core)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
