# `docs/` — design and measurements

| file | what |
|---|---|
| `design.md` | the wire (recipe → deterministic glTF; LODs, instancing, meshopt-vs-Draco, KTX2, deltas), where each stage runs on the Thor (CPU today, Warp/NVENC/zero-copy), the viewer, dependencies + licences, and the measured-vs-designed honesty table |
| `measurements.md` | every number with the box's state beside it (the standing rule) |

Everything cross-references builder-ide: `docs/research/maquette.md`
(the five defects this design answers), `docs/design/core-spec.md` §4.2/§4.3
(`world.dirty`, `/v1/world/scene`, `since_version`), §6.2 item 4 (the
compiler milestone this slice lands), and `core/engine/` (the seam the
production compiler plugs into).
