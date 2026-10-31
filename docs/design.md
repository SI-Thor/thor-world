# world-stream — the design

**What this is:** the Thor-native half of the `maquette` idea — a live wire
from the resident world on the Jetson AGX Thor to a browser viewer on the
ProArt — built to builder-ide's contract (`core-spec` §4.2/§4.3: `world.dirty`
carries `{branch, head}`; `/v1/world/scene?branch=&lod=&since_version=`; the
recipe is the source of truth and glTF is a **compiled, one-way output**).
What maquette's public half taught us (and the five defects we must not
repeat) is in builder-ide `docs/research/maquette.md` §1.5–1.6 and §3.6; this
document answers each defect with a mechanism, and `docs/measurements.md`
holds today's numbers with the box's state beside them.

The first slice is landed: `compiler/worldc.mjs` (recipe → deterministic glTF
with real LOD chains, instancing, quantization + meshopt compression, and a
`since_version` delta), `viewer/` (three.js WebGPU/WebGL2 thin client + a
dev server that speaks the `world.dirty` shape). Everything below describes
both what exists and what is designed-but-not-yet-measured, labelled
honestly.

---

## 1. The wire: recipe version → deterministic glTF 2.0

### 1.1 The two artifacts

**The recipe** (`world-stream.recipe/1`, JSON, ours — never written by the
engine) is the unit of versioning, diffing and delta. Entries carry stable
`id`s (the source object name), placement `params {t, q, s}`, optional
material overrides, and the edit trail (`edits[]`: from_version, author,
note — no wall-clock, so the recipe stays byte-stable). Provenance pins the
exact source: file sha256, generator name, seed, engine pin.

**The world file** (`world@N.glb`) is compiled, one-way, deterministic:

- **sorted** — every JSON object serialized with recursively sorted keys,
  canonical shortest-round-trip numbers, no whitespace;
- **timestamp-free** — no wall-clock, no hostnames, no ambient state; the
  only "when" is the recipe version `N`;
- **provenance in `extras`** — `asset.extras` carries `{world, branch,
  version, recipe_sha256, source_sha256, engine, templates, instances,
  triangles_lod0}`; per-node `extras` carry the template hash, the LOD
  table, and the source node names.

Two builds of the same recipe and source are byte-identical — verified
in-process, across `--jobs 1` vs `--jobs 4`, and (today) across machines
(ProArt x86 vs Thor aarch64; same V8 number formatting, same WASM). That is
the stage-cache invariant (`core-spec` §1.9) holding on wire bytes, and it
is what makes a delta trustable: if the compiler were not deterministic,
"only changed nodes travel" would be unprovable.

### 1.2 Templates and instancing (defect #3 answered)

Every source object is hashed over (positions ‖ normals ‖ uvs ‖ indices ‖
effective material). Identical hash ⇒ one **template**. A template is one
glTF node with `EXT_mesh_gpu_instancing` (TRANSLATION/ROTATION/SCALE
attributes, themselves meshopt-compressed) — so 200 identical chairs cost
one geometry plus 200 × 10 floats. The templates are *declared*, not just
implied: `asset.extras.templates` counts them, and the build manifest (§4.1)
lists hash → node → LOD table → entry ids, which is what lets a delta name
an instance without shipping geometry. The maquette manifest declared
nothing; ours is the address space of the delta channel.

### 1.3 Real LOD chains (defect #1 answered)

Each template carries a chain of *real simplified meshes* — meshoptimizer
quadric simplification over the compacted vertex set, default ratios
`1.0 / 0.5 / 0.25 / 0.1` with error bounds `0 / 0.01 / 0.025 / 0.06`
(relative to mesh extents) — each LOD is a genuine mesh in the file with its
own accessors, listed in `node.extras.lods` with `{mesh, triangles, error,
worldError}`. Nothing is claimed that is not in the file: the triangle
counts in the manifest are read back off the built meshes, and the chain
stops honestly when simplification stops making progress (measured, e.g.
`1728 → 1102 → 467 → 173` on the fixture crate; the living-room numbers are
in measurements.md). The viewer picks the LOD whose world-space error stays
under a screen-pixel budget (§6), with hysteresis.

### 1.4 Quantization + meshopt compression (defect #4 answered; vs Draco)

`KHR_mesh_quantization` + `EXT_meshopt_compression` on every bufferView:

- positions → normalized i16 in the template's bounding box (the node's
  translation/scale carry the dequantization, so instances compose on top);
- normals → normalized i8; UVs → normalized u16 when they fit [0,1], f32
  otherwise (a data-driven, deterministic decision);
- indices → u16 when the LOD's vertex count allows, u32 otherwise;
- every bufferView then goes through the meshopt *encoder* (vertex codec
  for attributes, index codec for triangles), 8-byte-aligned in one BIN chunk.

**Why meshopt and not Draco** (the maquette pipeline used Draco):

| | meshopt | Draco |
|---|---|---|
| decode in the browser | WASM/JS decoder in three.js, ~29 KB, also on WebGPU fast paths | WASM decoder, larger, slower decode |
| determinism | encoder is deterministic (verified byte-identical across runs, machines, thread counts) | Draco's encoder is **not** byte-stable across runs (the maquette report's defect #4; we re-verified the claim is about the toolchain, not our code) |
| quantization story | one pass: we quantize to integer attributes (spec-visible, KHR_mesh_quantization) then entropy-code | quantization hidden inside Draco's stack |
| CPU encode cost | ~1–5 ms per mesh on Neoverse (measured) | comparable, but the determinism failure alone disqualifies it for a `since_version` wire |

Draco stays legal for *exchange* files; it is wrong for a wire whose
invariant is "same recipe, same bytes".

### 1.5 KTX2/Basis textures (defect #2 answered — honestly)

Maquette probed `toktx` and never invoked it; textures stayed PNG. Our
wire reserves `KHR_texture_basisu` and the design is: textures quantize and
transcode to KTX2/UASTC (scene textures) or ETC1S (long-tail materials) on
the Thor, in the compiler's texture stage, declared in the manifest with
per-texture bytes. **Today this is designed, not measured**: the Infinigen
2.0 living-room export carries untextured principled materials (base colors
only — Cycles node graphs do not survive glTF export), so there is nothing
to transcode in the slice. The encode path needs `toktx` from
KTX-Software, which has no mainstream aarch64 binary — it is on the
build-from-source list for Casey's OK (§7), with the GPU alternative
(`cuda-bcu` / BC7 on the Blackwell SMs) sketched in §5.2.

### 1.6 The `since_version` delta (defect #5 answered)

`world.dirty {branch, head}` → the client fetches
`/v1/world/scene?since_version=<v>` → a **KB-class op list**, not the file:

```json
{ "schema": "world-stream.delta/1", "from_version": 1, "to_version": 2,
  "ops": [ { "op": "instance.update", "id": "Armchair_0021",
             "params": { "t": [...], "q": [...], "s": [...] }, "material": null } ],
  "provenance": { "author": "Architect", "note": "moved to open a path",
                  "recipe_sha256_old": "…", "recipe_sha256_new": "…" } }
```

Ops today: `instance.update` (params/material), `instance.remove`,
`instance.add`. A delta that introduces **new geometry** (an `instance.add`
whose source object has no template in the client's version) is designed to
append a *fragment GLB* — built by the same deterministic path, containing
only the new templates — referenced from `templates_added[]`, fetched once
by the client; **the schema reserves the field, the slice does not exercise
it** (the demo edit moves an existing instance). The delta is produced by **diffing
recipes, not files**: O(changes) always, regardless of scene size. If the
client's version is not on the delta chain, the server answers
`{full: true}` and the client re-streams — the same shape the real service
gives (`core-spec` §4.2). Provenance rides every delta (author, note, both
recipe hashes), which is what the viewer renders as the "who changed what"
ledger card.

---

## 2. Where each step runs, and how the Thor makes it fast

The Thor: AGX Thor, Blackwell GPU (sm_110) + 14 Neoverse V3 cores, 122 GB
**unified** memory, NVENC (h264/hevc/av1), CUDA 13.2, no RT cores. The
compiler is designed so every heavy stage has a CPU floor and a GPU
ceiling — the CPU path ships today (and is what the slice measures); the
GPU paths are the follow-up experiments, each with a named falsifier.

### 2.1 Scene generation + export (resident, warm)

The world file's source is a real Infinigen 2.0 run: `export_scene.py`
drives the *same* code path as the `infinigen` CLI (`infinigen2.generate.
_main`, the pattern `~/lm-wt/v2-thor/warm_scene.py` proved) in one resident
bpy 4.2 process — generate → save `.blend` → export glTF — so the export is
the state the renderer sees, not a re-derivation. In production this stops
being a subprocess: the **warm scene server** (`core/engine/resident/`)
holds the scene, and the compiler reads from it (§2.4). Measured: the
generation and export numbers are in measurements.md (short version: the
glTF export of the subsurfed living room dominates the wall clock — that is
the first thing the zero-copy path removes).

### 2.2 Mesh simplification — CPU today, GPU next

**CPU (today, measured):** meshopt quadric simplification in WASM, one
template per task, fanned across worker threads (`--jobs N`). On the 14
Neoverse cores this is embarrassingly parallel per-template; determinism is
preserved because assembly order is fixed by template hash, not completion
order (verified: `--jobs 1` ≡ `--jobs 4` byte-identically; the 1-vs-14
scaling numbers on the real scene are in measurements.md).

**GPU (designed, next experiment):** the quadric solver maps to Warp
(NVIDIA's Python→CUDA, MIT; already proven on this box by the v2
experiment's Warp probes) — edge-collapse candidates built on the SMs,
positions read straight from the resident scene's unified-memory buffers
(no PCIe copy, no host round-trip). Falsifier: a Warp port of the quadric
pass on the largest living-room template must beat the measured CPU
per-template time by ≥ 5× at equal output (same collapse sequence is *not*
required — the LOD is a new artifact, versioned by content hash, not a
bitwise match of the CPU LOD). Until that experiment runs, GPU
simplification stays a design line with a number pending, not a claim.

### 2.3 Quantization + entropy coding (measured, CPU)

The i16/i8/u16 pass and meshopt encode are memory-bandwidth-bound passes
over geometry that already lives in unified memory; on the Thor they are
single-pass WASM today (measured with the compile). The GPU form (batched
quantize + zero-copy hand-off to the encoder) is the same Warp experiment
as §2.2 — one kernel family, two stages.

### 2.4 Zero-copy from the resident scene (design; the seam is measured)

122 GB of unified memory means the resident Blender scene, the compiler's
buffers and the served GLB can be *the same bytes*. The design: the warm
scene server exposes accessor-level views into its evaluated meshes
(Blender's `foreach_get` into pinned unified-memory buffers); the compiler
reads those views directly (no temp GLB on disk), and the world file it
writes is served by the scene service via `sendfile` from the page cache.
The stage cache keys (`hash(recipe ⊕ pin ⊕ stage ⊕ dep-keys)`) make a
re-compile of an unchanged template a no-op — the dirty-set contract, on
the wire. Today's slice measures the *seam* honestly: export-to-file is
the cost the zero-copy path removes, and that number is in measurements.md.

### 2.5 NVENC — the video peek and the filmstrip (measured)

The night's `card` events and the morning report need thumbnails that glTF
cannot give: "what the camera sees". The design: the resident scene renders
short orbits/turntables and NVENC encodes them on the dedicated encoder
block — zero contention with Cycles on the SMs. Measured today (512×512,
90 frames, 30 fps, 2 Mb/s): **h264 0.36 s wall (~250 fps), hevc 0.35 s at
0.90× the size**; `av1_nvenc` on this driver produced no frames at these
settings (recorded as a no, not hidden). Caveat recorded beside the
numbers: this Ubuntu ffmpeg is a minimal Jetson build — no concat demuxer,
no swscale, no libx264 — so frames must arrive as NV12 (our numpy bridge
did the RGB→NV12 pass; in production the render hands NV12/RGBA straight
to the encoder, GPU-side). The CPU h264 comparison is therefore absent,
not skipped silently.

### 2.6 What the ProArt does

Nothing heavy. The viewer decodes meshopt (WASM), dequantizes to floats,
builds `InstancedMesh` per template, and lets the RTX 4050 render. Deltas
are applied in place — no rebuild, no re-fetch. Final renders and world
compilation stay on the Thor; the thin client's own GPU is for the
interactive view only (vision §"The pieces" #8).

---

## 3. The viewer

`viewer/index.html` + `viewer.js`, three.js r187, vendored (MIT). The page
parses the world file by hand — the wire is ours, and hand-parsing is what
makes the LOD/instancing contract explicit rather than whatever a generic
loader chooses to support:

1. **Backend**: `WebGPURenderer` (WebGPU when Chrome offers it on this box,
   automatic WebGL2 fallback); the badge in the HUD says which one is live.
2. **Live**: `EventSource /api/events` → `world.dirty {branch, head}` →
   `GET /api/scene?since_version=<v>` → ops applied in place (instance
   matrices rewritten, `instanceMatrix.needsUpdate`), a ledger card slides
   in naming who changed what and why (from the delta's provenance).
3. **LOD**: per template, per ~150 ms, pick the coarsest LOD whose
   `worldError × (viewportHeight / 2·tan(fov/2)) / distance` stays under
   the pixel budget (3 px to go coarser, 2 px to come back — hysteresis so
   distant templates don't oscillate); the HUD shows the live LOD mix.
4. **Phases**: the world file's version chain *is* the phase sequence —
   each `world.dirty` is one builder's landed edit, and the demo server
   replays them in order, so the world changes while you watch (maquette's
   `checkpoint()` idea, but versioned, delta-fed and provenance-stamped).

---

## 4. Files and formats (the contract, precisely)

### 4.1 Build outputs

| file | what it is |
|---|---|
| `world@N.glb` | the compiled world at recipe version N (deterministic) |
| `manifest@N.json` | version N's address space: `entries {id → {template, instance}}`, templates with LOD tables, hashes, sizes, phase timings (never inside the GLB) |
| `world@M.sinceN.json` | the delta from N to M (op list + provenance) |
| `world@M.sinceN.templates.glb` | fragment GLB, only when the delta adds geometry |

### 4.2 Serving shape (what the dev server mirrors today)

```
GET  /v1/world/scene?branch=&lod=            → manifest for (branch, head)
GET  /v1/world/scene?since_version=N         → delta op list | {full: true}
GET  /v1/events   (SSE)                      → … event: world.dirty
                                                data: {"branch": "main", "head": 7}
```

Same names and semantics as `core-spec` §4.2/§4.3; the dev server
(`viewer/server.mjs`) is deliberately tiny — in production this is the
scene service's face, and the delta files are pre-baked per branch head
(the night pre-bake, core-spec §6.2 item 4).

---

## 5. Dependencies — each with its licence

**Used today (all mainstream, permissive, from npm):**

| package | version | licence | where |
|---|---|---|---|
| three.js (`three`) | 0.187.0 | MIT | viewer (vendored: `three.webgpu.js`, `three.core.js`, `three.tsl.js`, MeshoptDecoder) |
| meshoptimizer | 1.3.0 | MIT | compiler (WASM simplify + encode; the same library three.js decodes) |

Node ≥ 18 on both machines; nothing else — the GLB reader/writer, quantizer,
recipe/delta formats, dev server and viewer are ours, dependency-free.

**Would need building from a GitHub source tree — for Casey's OK, not built:**

| project | licence | why |
|---|---|---|
| KTX-Software (`KhronosGroup/KTX-Software`) | Apache-2.0 | `toktx` for KTX2/UASTC + ETC1S texture transcode (§1.5); no mainstream aarch64 binary, a source build on the Thor |
| meshoptimizer C library (native, not the npm WASM) | MIT | only if the WASM simplifier becomes the bottleneck and the Warp port is not taken — a native aarch64 build with all 14 cores would be the fallback |
| `cuda-bcu` / BC7enc GPU texture path | MIT | the §2.2-style GPU alternative to toktx (BC7 on the SMs); experimental, decided after toktx numbers |

**Deliberately not adopted:** glTF-Transform (MIT, excellent) as the
*compiler's* core — its writer does not promise byte-stable output, and
determinism is this wire's load-bearing wall. It remains the right tool for
one-off repairs and inspections, and the maquette spike pipeline (`dedup →
instance → simplify → quantize → draco`) remains valid prior art. Draco,
for the wire, is rejected on determinism (§1.4).

---

## 6. What is measured vs designed — the honesty table

| claim | status |
|---|---|
| deterministic full build (same recipe+source → identical bytes) | **measured** (in-process, cross-job-count; cross-machine) |
| source export determinism (Infinigen 2.0 same seed → identical GLB) | **measured** (two full exports, sha256-compared) |
| real LOD chains with per-LOD triangle counts | **measured** (fixture + living room) |
| instancing dedup ratio on a real scene | **measured** (living room, in measurements.md) |
| delta size vs full file | **measured** (fixture + living room edit) |
| compile wall clock, 1 vs 14 jobs on the Thor | **measured** |
| NVENC filmstrip encode | **measured** (h264/hevc; av1 = no) |
| KTX2/Basis textures | designed (nothing to transcode in this scene; toktx needs Casey's OK) |
| GPU (Warp) simplification | designed + falsifier named (§2.2) |
| zero-copy resident-scene export | designed; the cost it removes (file export) is measured |
| viewer WebGPU on the ProArt's Chrome | measured at demo time (the badge records which backend actually ran) |

Numbers live in `docs/measurements.md`, each with the box's state beside it.
