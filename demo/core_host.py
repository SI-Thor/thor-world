"""The core host: a builder-ide core, wired and seeded, behind its HTTP door.

world-mcp itself is a pure client of ``core.api`` — it never imports the
core. But a core started bare (`uvicorn core.api:app`) has no vocabulary,
no recipe and no node registry: the spine's own integration notes say the
recipe seam is opt-in until boot-time wiring lands upstream
(builder-ide INTEGRATION.md §6). This module is that host: it opens the
core the way builder-ide's own tests do (``RecipeStoreSeam`` over the one
ledger), seeds the Fjellvik world (plus the south span the demo works on),
publishes the node registry the compiler will own one day, and serves
``core.api:create_app`` with uvicorn.

It is used by the tests and the demo — never by the server package.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

# builder-ide imports — the harness side of the seam. The server package
# (src/world_mcp) must never import any of this; a test pins that rule.
from core.api import create_app
from core.config import Settings
from core.events import core_actor
from core.svc import Core
from core.svc.recipes import RecipeStoreSeam, schemas_from_vocabulary
from core.svc.vocab import VocabService
from core.vocab.seed import ENGINE_PIN, RECIPE_DOCUMENT
from core.vocab.words import default_entries

#: The south span of core-spec's own WO-14 example (§1.4.1's packet names
#: it; the shipped seed document stops at the harbour). Added here as seed
#: data — builder-ide itself is never touched.
SOUTH_SPAN_ENTRY = {
    "path": "structure.south_span",
    "kind": "structure",
    "generator": {
        "module": "infinigen_thor.structures",
        "func": "bridge",
        "variant": "rope",
        "rev": ENGINE_PIN["commit"],
    },
    "params": {"length_m": 14.0, "planks": 8, "sway": 0.22, "load_factor": 1.1},
    "seed": 99,
    "constraints": [],
    "assets": [],
    "origin": "builtin",
}

SOUTH_SPAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "length_m": {"type": "number", "minimum": 0.5, "maximum": 60.0},
        "planks": {"type": "integer", "minimum": 1, "maximum": 64},
        "sway": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "load_factor": {"type": "number", "minimum": 0.0, "maximum": 10.0},
    },
}

#: The rope-bridge word, pinned coming-soon the way WO-14 files it
#: (§1.5): the gap the packet names, and the promotion the order lands.
BRIDGE_ROPE_WORD = {
    "word": "bridge.rope",
    "kind": "structure",
    "owner_character": "architect",
    "status": "coming-soon",
    "params": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "length_m": {"type": "number", "minimum": 1.0, "maximum": 60.0},
            "planks": {"type": "integer", "minimum": 1, "maximum": 64},
            "sway": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        },
    },
    "acceptance_template": None,
    "taste_notes": [],
    "provenance": {"origin": "builtin"},
    "version": 1,
}

#: The world's nodes, as the compiler will publish them (§1.4.0):
#: node id ↔ entry path ↔ kind.
NODES = [
    {"node_id": "span_south", "entry_path": "structure.south_span",
     "kind": "structure", "assets": [], "part": "deck"},
    {"node_id": "pier_s2", "entry_path": "structure.south_span",
     "kind": "structure", "assets": [], "part": "pier"},
    {"node_id": "crates_01", "entry_path": "harbor.crates",
     "kind": "prop_set", "assets": [], "part": None},
]


def build_core(home: Path) -> Core:
    """Open the core and wire the whole world into it."""
    settings = Settings.for_home(home)
    settings.ensure_tokens_file()
    core = Core.open(settings)

    # 1. the vocabulary words (the three of the slice + the coming-soon gap)
    words = default_entries()
    vocab = VocabService(core)
    for entry in words:
        vocab.add_word(entry.model_dump(mode="json"), core_actor())
    vocab.add_word(dict(BRIDGE_ROPE_WORD), core_actor())

    # 2. the recipe store over the one ledger, validated by the vocabulary
    schemas = schemas_from_vocabulary(words)
    schemas.register("structure.south_span", SOUTH_SPAN_SCHEMA)
    core.recipes = RecipeStoreSeam(core, schemas=schemas)

    # 3. the recipe: Fjellvik plus the south span
    document = json.loads(json.dumps(RECIPE_DOCUMENT))  # deep copy of the seed
    document["entries"]["structure.south_span"] = json.loads(
        json.dumps(SOUTH_SPAN_ENTRY))
    root = core.recipes.seed_recipe(document)

    # 4. the branch the spine's own projections read (the recipe store's
    #    branch.created may fold in its own spelling; make sure the spine's
    #    shape is there too — conftest.seed_branch's spelling)
    if not [b for b in core.store.scan("branches") if b.get("purpose") == "main"]:
        core.store.append(core.store.new_event(
            "branch.created", core_actor(),
            {"entity_kind": "branch", "entity_id": "br_main"},
            {"id": "br_main", "name": "main", "head": root, "purpose": "main",
             "protected": 1},
        ))
    else:
        row = core.store.get_row("branches", {"id": "br_main"})
        if row and not row.get("payload", {}).get("head"):
            core.store.append(core.store.new_event(
                "branch.head.moved", core_actor(),
                {"entity_kind": "branch", "entity_id": "br_main"},
                {"from_head": None, "to": root, "via": "seed"},
            ))

    # 5. the node registry: the compiler's nodes.json artifact, published
    #    on a version.committed for the branch (conftest.seed_nodes' shape)
    desc = core.store.artifacts.put(
        json.dumps({"nodes": NODES}).encode("utf-8"), "nodes.json")
    core.store.append(core.store.new_event(
        "version.committed", core_actor(),
        {"entity_kind": "branch", "entity_id": "br_main"},
        {"branch": "br_main", "head": root},
        artifact=desc,
    ))

    # 6. the established history: Casey's taste rows and the Prop-maker's
    #    earlier scored bets (the melt starts somewhere)
    from core.vocab.seed import seed_audit_events

    for ev in seed_audit_events():
        core.store.append(ev)
    return core


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@contextmanager
def serve_core(home: Path):
    """A real uvicorn server over the wired core, on an ephemeral port.

    Some things — an SSE stream the MCP watcher holds open, exact
    authorization headers — are facts about sockets; an in-process
    transport does not reproduce them. Yields the base URL.
    """
    import uvicorn

    core = build_core(home)
    app = create_app(core.settings, core=core, run_boot=True)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:  # pragma: no cover - a broken environment
        raise RuntimeError("uvicorn did not start")
    try:
        yield f"http://127.0.0.1:{port}", core
    finally:
        # MCP clients watching /v1/events close their connections when the
        # world server shuts down, so uvicorn's graceful exit can complete
        # before the ledger closes. A stream that missed the memo (a watcher
        # reconnecting, say) is cut off forcibly — the store must close last.
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=5)
        core.store.close()


@asynccontextmanager
async def serve_core_async(home: Path):
    """The same server, for an async caller (the demo's agent session).

    The sync context manager runs in its own thread; a plain threading
    event hands it back on exit.
    """
    import threading

    import anyio

    stop = threading.Event()
    holder: dict = {}

    def run():
        with serve_core(home) as pair:
            holder["url"], holder["core"] = pair
            holder["ready"] = True
            stop.wait(120)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    while not holder.get("ready"):
        if not thread.is_alive():  # pragma: no cover - a broken environment
            raise RuntimeError("the core host thread died")
        await anyio.sleep(0.02)
    try:
        yield holder["url"], holder["core"]
    finally:
        stop.set()
        thread.join(timeout=10)


def main() -> int:
    """Serve the seeded core standalone: `python -m core_host [HOME]`."""
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("home", nargs="?", type=Path,
                        help="$BUILDER_HOME (default: a temp dir)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    home = args.home or Path(tempfile.mkdtemp(prefix="builder-home-"))
    core = build_core(home)
    print(f"$BUILDER_HOME={home}  core on http://127.0.0.1:{args.port} "
          f"(tokens: dev-token-casey-world-mcp / dev-token-props-world-mcp"
          f" / dev-token-panel)")

    import uvicorn

    uvicorn.run(create_app(core.settings, core=core, run_boot=False),
                host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
