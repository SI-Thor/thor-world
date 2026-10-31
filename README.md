# thor-world

The live wire between a world generated on the Jetson AGX Thor and the people and agents who use it. Two halves:

| Component | What it is | Where | Status |
|---|---|---|---|
| **world-mcp** (`src/`, `tests/`, `demo/`) | An MCP server over builder-ide's core: the only way agents (Claude Code, zcode, mcode, cloud tile authors) work on a world, through the core's typed doors. Every action is recorded with its author. | Thor or ProArt | 30/30 tests against a live core; the "make these sturdier" demo runs end to end ([design](docs/design.md)) |
| **world-stream** (`compiler/`, `viewer/`) | A deterministic recipe-to-glTF compiler (real LOD chains, GPU instancing, quantization + meshopt, provenance), a `since_version` delta channel over SSE, and a three.js viewer (WebGPU, with a WebGL2 fallback). | Compiler on the Thor; viewer in Chrome on the ProArt | Byte-identical builds across the Thor and an x86 ProArt; a 629 B delta for one moved table; 3.51 M triangles over all LODs in 43.8 MB, compiled in 6.7 s ([design](docs/stream-design.md), [numbers](docs/measurements.md)) |

It fills the role of the private companion repo of the `maquette` Infinigen fork (an MCP server plus a browser viewer). It is our own design, built for the Thor; see builder-ide's [`docs/research/maquette.md`](https://github.com/SI-Thor/builder-ide/blob/main/docs/research/maquette.md) for what we learned from that fork's public half.

**Open for the owner:**
- the colour pipeline: Infinigen materials stall the stock glTF exporter, so today's world is grey;
- a source build of `toktx` (KTX-Software, Apache-2.0) for KTX2 textures;
- Warp simplification for the room-shell serial tail;
- a two-minute WebGPU check in desktop Chrome.

Licence: MIT. Part of [builder-ide](https://github.com/SI-Thor/builder-ide); world engine: [infinigen-thor](https://github.com/SI-Thor/infinigen-thor).

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

Environment: `BUILDER_CORE_URL`, the three token classes
(`WORLD_MCP_OWNER_TOKEN` / `WORLD_MCP_CHARACTER_TOKEN` /
`WORLD_MCP_PANEL_TOKEN`, defaulting to a fresh `$BUILDER_HOME`'s dev
tokens) and `WORLD_MCP_ACTOR` (the agent's own name). A standalone seeded
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


## world-stream

See [`compiler/README.md`](compiler/README.md) (recipe → world file → delta) and [`viewer/README.md`](viewer/README.md) (the dev server and the live viewer). Measurements with the box state beside every number are in [`docs/measurements.md`](docs/measurements.md); screenshots are in `docs/screenshots/`.
