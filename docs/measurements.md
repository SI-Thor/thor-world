# measurements — every number with the box beside it

The standing rule: no number without the state of the box it was measured
on (load, what else was running, versions). All Thor numbers come from
`thor-lan:~/lm-wt/world-stream-scratch/` (logs and artifacts stay there).
Measured 2026-10-31. One box event colours the day and is recorded where
it matters: **the Thor rebooted ~13:29** (uptime 7 min observed at 13:36),
killing the two stuck exporter runs and the first NoApply fallback — and
auto-restarting the seed sweep on its own (tmux `last-mile`, 13:32). The
measurements below were taken after the reboot unless marked.

## 0. The boxes

**ProArt** (the viewer box): RTX 4050 6 GB, WSL2 (kernel 6.18
microsoft-standard), Node v24.21.0, meshoptimizer 1.3.0 (npm, WASM),
Chromium 156.0.8078.4 (Playwright build, headless). No other significant
load during the local builds.

**Thor** (`ssh thor-lan`): NVIDIA AGX Thor, aarch64, 14 cores, 122 GB
unified memory, driver 595.78, CUDA 13.2, ffmpeg n8.0.1-17 (Ubuntu Jetson
build). **Background load**: before the reboot the box carried the v2
seed sweep + a lab5 `run_solve` job (load average drifted 27→66 during
the morning); after the reboot the sweep restarted itself and the
compiles below ran at load ~11–12 with 112 GB mem available. Nothing was
started or stopped to flatter a number.

## 1. Source scene: the Infinigen 2.0 living room

`compiler/tools/export_scene.py` on the Thor (v2 venv, resident bpy 4.2):
generate → save `.blend` → export glTF in one process, `--cpu_threads 4`.
Generator `room_livingroom_rand`, seed 42, two independent runs
(`export1`, `export2`), on the *busy* pre-reboot box:

| | export1 | export2 |
|---|---|---|
| generation (`room_livingroom_rand`) | 15.894 s | 15.594 s |
| `.blend` bytes | 145,717,009 | 145,717,009 |
| `.blend` sha256 | `d6f836b4…` | `56e7b2c6…` |

- The **`.blend` is not byte-deterministic** (same size, different
  bytes — Blender embeds session state). Fine: the `.blend` is not the
  wire; the glTF export is, and §1.2 is its verdict.
- The scene: 119 objects, 117 meshes, 93 materials, **2,054,732
  triangles** (n-gons included).

### 1.1 The exporter finding (recorded, because it costs days if rediscovered)

The bpy 4.2 glTF exporter **stalls exponentially on Infinigen 2.0's
procedural materials**: `io_scene_gltf2`'s material gatherer
(`gltf2_blender_search_node_tree.__search_from_socket`) re-walks shared
node-group subgraphs with no visited set (the file carries a literal
`TODO: cache these searches`), and Infinigen's deep nested graphs
explode it. Evidence: `faulthandler.dump_traceback_later` stacks show
40+ recursion frames of `__search_from_socket`; three separate runs
(`export_apply` true and false, busy and idle box) burned >2 CPU-hours
each on a scene that opens in 0.3 s — and the original exporter logs sat
silent after the single-vertex primitives, right where material
gathering begins. Unlinking Principled inputs does **not** defuse it
(the graph walk enters through node groups).

The working path: export with `export_materials='NONE'`
(`export_flat.py` in the scratch dir) — **2.79 s / 3.4 s wall** for the
90,827,084-byte `source.glb`. Cycles node graphs do not survive glTF
anyway (design.md §1.5); the slice's wire carries geometry + flat
materials, and colour comes back with the texture stage (open question
for Casey: bake/flatten colours in the warm-scene server, or accept grey
until KTX2 lands).

### 1.2 Export determinism (the claim design.md makes, now actually measured)

| | export1 | export2 |
|---|---|---|
| `source.glb` bytes | 90,827,084 | 90,827,084 |
| `source.glb` sha256 | `96e84e21…` | `96e84e21…` |

**Byte-identical.** Same seed → identical GLB, even though the `.blend`s
differ — the exporter's output is a pure function of evaluated mesh
data, which is what the wire pins its sha256 to.

## 2. The fixture compile (correctness + determinism at the small scale)

Source: `compiler/test/fixture_source.glb` — 168,080 B, 6,528 tris LOD0
(subdivided jittered crate ×3 instances + rock ×1; the fixture generator
itself is deterministic: same bytes on both boxes, sha256 `b99e961c…`).

Compiled with `worldc.mjs` (meshoptimizer 1.3.0):

| | ProArt | Thor |
|---|---|---|
| `world@1.glb` bytes | 76,540 | 76,540 |
| sha256 | `0da3ae36…` | `0da3ae36…` |
| build wall | 27/10 ms (j1, in-process ×2), 93/47 ms (j4) | 187 ms (j4) |
| templates / instances | 2 / 4 | 2 / 4 |

- **Cross-machine determinism holds**: ProArt x86 and Thor aarch64
  produce byte-identical `world@1.glb`; in-process double-builds agree at
  jobs 1 and 4 (`worldc verify`).
- The compiled file is *smaller than its uncompressed source* (76.5 KB
  vs 168 KB) while carrying **four real LODs per template** —
  quantization + meshopt pay for the whole LOD chain and then some.
- LOD chains (from the manifest, triangles read back off the built
  meshes): crate 1728 → 1102 → 467 → 173 (worldError 0 → 0.0090 →
  0.0264 → 0.0342 m); rock 4800 → 2740 → 1199 → 479.

## 3. The fixture delta (one parameter edit)

`worldc edit --id Crate_0002 --move 0.9,0,0.4` → `worldc delta`:

- delta `world@2.since1.json`: **608 B, 1 op** (`instance.update`), vs
  76,540 B full — **0.79 % of the full file**.
- Byte-identical on both machines (sha256 `4773c59f…`) — the delta is a
  pure function of the two recipes.
- Provenance rides in the delta (author, note, both recipe sha256s) —
  what the viewer's ledger card renders.

## 4. The living-room compile (the real scene, on the Thor)

Recipe: 110 entries derived from `source.glb`
(`worldc recipe --meta generator=room_livingroom_rand seed=42
engine=infinigen@2.0.0a3+bpy4.2`). Box: Thor, load ~11 (sweep running),
112 GB mem available.

| | Thor `--jobs 1` | Thor `--jobs 14` | ProArt `--jobs 1` |
|---|---|---|---|
| wall clock | **6.74 s** | 8.06 s | 10.63 s |
| peak rss | 992 MB | 1.39 GB | — |
| `world@1.glb` sha256 | `8e57f1a4…` | `8e57f1a4…` | `8e57f1a4…` |

- **Deterministic across job counts and machines**: the Thor's 1-job,
  14-job and the ProArt's 1-job builds are byte-for-byte the same
  43,843,672-byte file.
- **jobs 14 is *slower* than jobs 1** — and that is the honest, useful
  result: the scene has 55 templates but one 1.77 M-triangle room shell,
  so per-template parallelism has a serial tail (one worker gets the
  shell, the rest finish in milliseconds). The lever against the shell
  is *intra-template* parallelism — exactly the Warp simplification
  experiment (design.md §2.2), which now has a measured reason to exist.
- The compiled world is **2.07× smaller than its source** (43.8 MB vs
  90.8 MB) while carrying **3,514,274 triangles across all LODs**
  (1,979,464 at LOD0) — the whole LOD chain costs less than nothing.
- Instancing: 55 templates / 110 instances (2× dedup on average; the
  most-reused templates carry 9/7/7/7/7 instances).
- Honest chain stops: the shell simplifies 1,769,472 → 884,736 →
  590,674 and stops (the 0.1-ratio pass made no real progress);
  furniture goes four deep (e.g. 104 → 51 → 26 → 14, 1212 → 605 →
  303 → 229).
- Phase timings (jobs 14 run): parse 328 ms, LOD+quantize+encode
  7,126 ms, assemble+serialize 225 ms — simplification of the shell *is*
  the compile; that is the kernel the Warp port targets.

## 5. The living-room delta (one parameter edit)

`worldc edit --id _table_coffee_legged_rand --move 0.9,0,0.4 --author
Architect --note "moved to open a path"` → `worldc delta`:

- **629 B, 1 op** vs the 43,843,672 B full file — **0.0014 %**.
- Byte-identical across machines (sha256 `e1532f7d…`).
- Applied live in the viewer (§6): the table slides, the ledger card
  names who and why, nothing else moves.

## 6. The viewer (ProArt, headless Chromium)

Headless Chromium 156 on WSL2: **WebGPU has no adapter** ("No available
adapters" — no Vulkan in this headless/WSL2 combination), and the
`WebGPURenderer` fell back to **WebGL2 (SwiftShader)** exactly as
designed; the HUD badge reports the truth (`backend: WebGL2`).

Two demo loops were driven end-to-end in that browser and screenshot
(`docs/screenshots/`):

- **fixture** (2 templates · 4 instances): load `world@1.glb` → the
  608 B delta lands on disk while the page watches → SSE
  `world.dirty {branch, head}` → `GET /api/scene?since_version=1` → the
  crate moves, version chip flips, ledger card: *"Architect — moved to
  open a path · v1 → v2 · 1 op(s), 1 applied live"*.
- **living room** (55 templates · 110 instances, the real scene): same
  loop with the 629 B delta; the coffee table visibly slides
  (`livingroom-v1.png` → `livingroom-v2.png`). Live LOD mix in the HUD
  (26/8/9/12 at the screenshot instant) — the chains are real and the
  swaps happen.

Three defects were found and fixed on the first live runs (instance
decode, renderer sizing, demo-mode announce) — see the viewer commits.
The chain-broken contract was exercised too: `?since_version=7` →
`{full: true, reason: "chain-broken"}` (the client's cue to re-stream).

WebGPU on a desktop (non-headless, non-WSL2) Chrome remains a
two-minute check for Casey; the fallback path is what ships today.

## 7. NVENC filmstrip encode (Thor, dedicated encoder block)

90 frames of 512×512 NV12 (the v2 warm-scene camera outputs), 30 fps,
2 Mb/s target, `ffmpeg -benchmark`, real time (`rtime`), measured **on
the busy pre-reboot box** (load 58–66):

| encoder | wall | throughput | bytes | note |
|---|---|---|---|---|
| `h264_nvenc` | 0.415 s | ~217 fps | 843,607 | |
| `hevc_nvenc` | 0.230 s | ~391 fps | 756,972 | 0.90× the h264 size |
| `av1_nvenc` | — | — | 261 (mux stub) | fails: `error -22 (Invalid argument)`, no frames |

- At load 60 the encodes stay sub-second — the encoder block really is
  its own hardware, uncontended by the saturated SMs/CPU.
- **Re-runs are byte-identical**: today's redo files match the first
  measurement's bytes exactly — fixed-settings NVENC output is stable,
  which the filmstrip cache can rely on.
- Caveat (recorded, not hidden): this ffmpeg is a minimal Jetson build —
  no concat demuxer, no `libx264` (so no CPU-encoder comparison on this
  box), and `av1_nvenc` produced no frames at these settings on this
  driver. Input was raw NV12 from a numpy RGB→NV12 bridge; in production
  the render hands NV12/RGBA straight to the encoder, GPU-side, and that
  pass disappears.

## 8. What is still designed, not measured

- GPU (Warp) simplification, zero-copy export — design + falsifier in
  design.md §2.2/§2.4; §4 now supplies the CPU floor (6.74 s, shell-
  dominated) and §1 the export-cost seam the zero-copy path removes.
- Fragment-GLB deltas (geometry-adding `instance.add`) — schema
  reserved, not exercised.

---

# Round 2 — 2026-10-31 afternoon (colour, and the serial tail)

Box state for everything below: the Thor rebooted again ~13:29 (the
morning's event was still the same boot — uptime 47 min observed at
14:17); the sweep self-restarted as before and load sat at ~5–11.
`~/lm-wt/heavy.lock` is now honoured by other seats too — an fp8/TRELLIS
experiment and a CUDA extension build each held it while the colour runs
queued; every queue wait is part of the honest wall-clock below.

## 9. The serial tail, split (chunked LOD simplification)

The round-1 finding was that `--jobs 14` (8.06 s) lost to `--jobs 1`
(6.74 s) because one 1,769,472-triangle room-shell template serialized
the compile. Round 2 adds the lever: `worldc recipe --chunk-above N
--chunk-target M` splits a template above the threshold into contiguous
spatial chunks (recursive median split of triangle centroids along the
longest bbox axis — a pure function of the geometry), simplifies each
chunk on its own worker with meshoptimizer's `LockBorder` (the cut edges
are open edges; locked border vertices mean neighbouring chunks' LODs
still mate — no cracks), and stitches the per-chunk LODs back into one
template. A chunk whose own chain stalls repeats its coarsest LOD while
siblings progress; the template chain stops when a stitched level shrinks
less than 2 %.

Living room, same box and load class as §4 (Thor, load ~6–7, sweep
running):

| recipe | jobs 1 | jobs 14 | `world@1.glb` | triangles, all LODs | shell chain |
|---|---|---|---|---|---|
| unchunked (§4) | **6.74 s** | 8.06 s | 43,843,672 B | 3,514,274 | 1,769,472 → 884,736 → 590,674, stop |
| chunk-target 200k | 12.63 s | 6.06 s | 80,915,564 B | 4,396,124 | 1,769,472 → 1,041,422 → 709,646 → 606,192 |
| chunk-target 400k | 10.60 s | **4.98 s** | 80,559,660 B | 4,213,650 | 1,769,472 → 898,276 → 677,092 → 599,418 |

- **Parallelism now pays**: jobs 14 went from 20 % *slower* than jobs 1
  to **2.1× faster**, and the best wall dropped 8.06 → 4.98 s (1.62×)
  against the same source and policy space.
- **The price is bytes**: locked borders retain triangles, so the file
  grows 43.8 → 80.6 MB (+84 %) and all-LOD triangles 3.51 M → 4.21 M.
  Fewer, bigger chunks (400k) reduce both the border overhead and the
  wall — chunk400 dominates chunk200 on every axis here.
- **Determinism holds**: `--jobs 1` ≡ `--jobs 14` byte-for-byte on the
  Thor for both chunk sizes, and the chunk-400k build is byte-identical
  across machines — Thor aarch64 (jobs 14) ≡ ProArt x86 (jobs 6), sha256
  `f0b4e5d6…`. The fixture's un-chunked sha is unchanged (`0da3ae36…`) —
  the feature is opt-in per recipe and touches nothing else.
- Phase timings (chunk400, jobs 14): parse 330 ms, LOD+quantize+encode
  4,304 ms, assemble+serialize 41 ms — the tail is now eight ~220k-tri
  chunk simplifies fanned across workers instead of one 1.77 M-tri
  monolith.
- The chunked world renders in the viewer unchanged (manifest shape is
  untouched): 55 templates · 110 instances, live LOD mix 24/9/8/14 at the
  screenshot instant — the shell's four-level chain is actually used.
- What chunking does *not* fix: per-chunk `LockBorder` costs reduction
  efficiency (LOD1 keeps 898k vs the monolith's 884k — close), and the
  single-threaded compile got slower (6.74 → 10.60 s) — the lever buys
  parallel wall, not total work. The Warp/clustered simplifier (§2.2)
  remains the answer for total-work reduction.
