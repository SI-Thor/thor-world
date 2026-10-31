# thor-world (working name)

The live wire between a Thor-generated world and the people and agents who use it:

- **world-mcp**: an MCP server over builder-ide's core, so any agent (Claude Code, zcode, mcode, others) can read the world's context and act on it through the typed vocabulary.
- **world-stream + viewer**: a deterministic recipe-to-glTF compiler with real LODs, GPU-compressed textures and a `since_version` delta channel, built for the Jetson AGX Thor, and a browser viewer on the ProArt that watches the world change live.

It fills the role of the private companion repo of the `maquette` Infinigen fork (an MCP server plus a browser viewer). It is our own design, optimised for the Thor; see builder-ide's `docs/research/maquette.md` for what we learned from that fork's public half. Licence: MIT.

## world-mcp

An agent's only way into a builder-ide world. The server is a **pure client
of the core's HTTP API** — it never imports builder-ide, never touches the
ledger, and every write goes through one of the core's typed doors with the
token's actor recorded as its author (`docs/design.md` is the design note).

**Six resources:** `world://context` (the §1.4 context packet: selection,
recent history, conversation labels, project state, the budget left tonight,
the vocabulary and its gaps), `world://vocabulary`, `world://recipe`,
`world://renders` (and critic verdicts), `world://ledger` (the two tallies),
`world://dirty` (the `{branch, head, seq}` cursor).

**Eight tools:** `get_context`, `list_vocabulary`, `read_night_plan`,
`propose_recipe_edit` (seals the prediction first), `request_render`,
`file_work_order` (terse request + context → question XOR draft + the
one-line confirm), `confirm_work_order` / `answer_question`, `queue_genbatch`,
`subscribe_world_dirty`. No tool runs arbitrary code in the world; a core
refusal (a coming-soon word, params outside a schema) is surfaced verbatim.

**Running it** (from this directory; the core is builder-ide's, installed
read-only into this venv from its own tree):

```sh
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pip install -e /home/proart/projects/builder-ide   # read-only

.venv/bin/world-mcp check                 # probe the core once
BUILDER_CORE_URL=http://127.0.0.1:8000 \
    .venv/bin/world-mcp serve --stdio     # local agents (default)
BUILDER_CORE_URL=http://thor-lan:8000 \
    .venv/bin/world-mcp serve --http --host 0.0.0.0 --port 8808
                                          # the ProArt's agents reach the Thor
```

Environment: `BUILDER_CORE_URL` and the three token classes
(`WORLD_MCP_OWNER_TOKEN` / `WORLD_MCP_CHARACTER_TOKEN` /
`WORLD_MCP_PANEL_TOKEN` — the owner and character tokens are **per-agent
tokens**, whose `agent` claim lands on every audit row they write, so the
log itself answers "which agent acted for whom"; the defaults are a fresh
`$BUILDER_HOME`'s per-agent dev tokens). A standalone seeded
core for trying it: `.venv/bin/python demo/core_host.py`.

**Tests + demo:**

```sh
.venv/bin/python -m pytest      # every tool/resource at a live core
.venv/bin/python demo/agent_session.py   # the scripted end-to-end session
```

Each test boots a seeded core on a temp `$BUILDER_HOME` behind a real
uvicorn server and drives the server as a stdio subprocess through the
official MCP client; the HTTP transport and the `world.dirty` push have
their own tests.

