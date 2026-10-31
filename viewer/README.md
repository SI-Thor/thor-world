# `viewer/` — the thin client and its dev server

The browser half: loads the compiled world file, renders it with three.js
(WebGPU when Chrome on this box offers it, WebGL2 otherwise), and keeps it
live over the `world.dirty` → `since_version` loop. Design: `../docs/design.md` §3.

## Layout

| path | what |
|---|---|
| `index.html` | the page (HUD: world/branch, backend badge, version, templates/instances, live LOD mix, fps; the world log with provenance cards) |
| `viewer.js` | hand-rolled glTF parse (meshopt decode + dequantize), one `InstancedMesh` per template, LOD swap by screen-space error with hysteresis, SSE subscribe, delta apply |
| `server.mjs` | tiny dev server: statics + `/api/scene[?since_version=]` + `/api/events` (SSE, `world.dirty {branch, head}`) |
| `vendor/` | three.js r187 (MIT) — `three.webgpu.js`, `three.core.js`, `three.tsl.js` — and three's `meshopt_decoder.module.js`; vendored so the demo needs no bundler |

## Run

```bash
# build/ holds world@N.glb + manifest@N.json (+ world@M.sinceN.json deltas)
node server.mjs [path/to/build] --port 8787
# → http://127.0.0.1:8787/

# demo mode (default): versions above the boot head "land" 6 s after your
# page subscribes — you watch the world change. Override with DEMO_DELAY_MS=…
# screenshot mode: ?autodemo=4000 applies the delta 4 s after load (no SSE wait)
```

## What it proves, and what it doesn't

- **Proves:** the compiled world file renders in a stock browser; a
  KB-class delta moves one named instance live, with provenance on screen;
  LOD chains are real (watch the "lod mix" chip change as you zoom); the
  WebGPU/WebGL2 badge tells the truth about which backend ran.
  Screenshots of both demo loops (fixture and the real Infinigen living
  room, delta applied live) are in `../docs/screenshots/` — driven and
  captured by headless Chromium; see `../docs/measurements.md` §6.
- **Doesn't (yet):** fragment-GLB deltas (`instance.add` with new
  geometry) are schema-reserved, not handled; textures are unexercised
  (the source scene has none — see measurements.md §1.1 for why, and
  what the colour pipeline needs); lights/cameras from the source scene
  are not imported (the page brings its own sun + hemisphere).
