# demo — the harness and the end-to-end session

Two pieces, both test/demo machinery only — the server package
(`src/world_mcp`) never imports anything here (a test pins that):

- **`core_host.py`** — a builder-ide core, wired and seeded, behind its
  HTTP door. world-mcp is a pure client of `core.api`, but a core started
  bare has no vocabulary, no recipe store and no node registry (the
  recipe seam is opt-in until boot-time wiring lands upstream —
  builder-ide INTEGRATION §6), so this module opens the core the way
  builder-ide's own tests do, seeds the Fjellvik world (plus the south
  span the demo works on and the `bridge.rope` coming-soon gap), and
  serves it with uvicorn. It is the *only* file in this repo that
  imports builder-ide.

  Standalone, for trying the server by hand:

  ```sh
  .venv/bin/python demo/core_host.py            # temp $BUILDER_HOME, port 8000
  .venv/bin/python demo/core_host.py --port 8010 ~/builder-home
  ```

- **`agent_session.py`** — the end-to-end demo: a scripted agent session
  (no real LLM) that reads the context packet, files "make these
  sturdier" with a selection, hears the core's one-line confirmation,
  says "go", checks the ledger names the author — then seals one typed
  prediction and receives the `world.dirty` push. The world-mcp server
  runs as a stdio subprocess, the way a local agent runs it.

  ```sh
  .venv/bin/python demo/agent_session.py
  ```

The tests (`../tests`) boot the same host per test, so one harness
serves two callers.
