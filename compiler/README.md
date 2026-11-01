# `compiler/` — the world-stream recipe→glTF compiler

The deterministic half of the wire: a **recipe** (ours, JSON) plus a source
scene (an Infinigen 2.0 glTF export) compile to a **world file** — glTF 2.0
with real LOD chains, `EXT_mesh_gpu_instancing`, `KHR_mesh_quantization`
and `EXT_meshopt_compression` on every bufferView, provenance in
`asset.extras`, sorted timestamp-free JSON ⇒ byte-identical rebuilds.
Design and decisions: `../docs/stream-design.md`. Numbers: `../docs/measurements.md`.

## Layout

| path | what |
|---|---|
| `worldc.mjs` | the compiler (single file, Node ≥ 18, dep: `meshoptimizer` only) |
| `package.json` | `npm install` gives you `meshoptimizer@1.3` (MIT) |
| `tools/export_scene.py` | runs on the **Thor**: generates an Infinigen 2.0 scene through the v2 venv's own CLI path and exports glTF from the same resident bpy process (writes only into its `--outdir`) |
| `tools/bake_colour.py` | runs on the **Thor** (under `flock ~/lm-wt/heavy.lock`, ≥ 25 GB free): the round-2 colour stage — `flatten` (32² bakes → per-slot averaged constants), `bake` (512² base/rough/normal PNGs + textured export + Cycles beauty render), `uvcheck`; unwraps fresh **xatlas** UV atlases (the generator's own UVMaps overlap — `uvcheck` measures it) with a geometry-keyed cache |
| `tools/recon_colour.py` | read-only scene survey: UV layers, which Principled inputs are constants vs linked graphs, Cycles devices |
| `tools/run-colour.sh` | the whole measured colour pipeline in one lock session (flatten ×2 for determinism, bake ×2 GPU + 1 CPU for drift, PNG-vs-WebP, wheel probes) |
| `tools/shoot-coloured.sh` | ProArt side: coloured source → recipe → build → delta → live viewer screenshots |
| `test/make_fixture.mjs` | deterministic synthetic source scene (boxes + rock) for smoke tests |
| `test/make_textured_fixture.mjs` | deterministic TEXTURED source scene (hand-rolled PNG encoder) for the texture-path smoke test |

## Commands

```bash
npm install

# 1. derive the canonical v1 recipe from a source glTF
node worldc.mjs recipe --source source.glb --world livingroom \
  --meta generator=room_livingroom_rand --meta seed=42 \
  --meta engine=infinigen@2.0.0a3+bpy4.2 --out v1.json

# 1b. optionally: split huge templates spatially so LOD simplification
#     parallelises (round-2 serial-tail lever; see docs/measurements.md §9)
node worldc.mjs recipe --source source.glb --world livingroom \
  --chunk-above 150000 --chunk-target 400000 --out v1.json

# 2. one parameter edit → v2 (author + note land in the edit trail)
node worldc.mjs edit --in v1.json --id Armchair_0021 --move 0.9,0,0.4 \
  --author Architect --note "moved to open a path" --out v2.json

# 3. compile (world@N.glb + manifest); --jobs N fans templates across workers
node worldc.mjs build --recipe v1.json --out build/ --jobs 14

# 4. the since_version delta (op list, only changed entries)
node worldc.mjs delta --old v1.json --new v2.json --out build/

# determinism: build twice in-process, byte-compare
node worldc.mjs verify --recipe v1.json
```

## Invariants (why the code looks the way it does)

- **Assembly order is content order.** Templates are processed in hash
  order and serialized in that order regardless of which worker finished
  first — parallelism cannot change the bytes (verified 1 ≡ 4 workers).
- **No wall-clock anywhere near the GLB.** Phase timings live in the
  manifest beside the file, never inside it.
- **meshoptimizer's JS `compactMesh` mutates its argument** — every index
  array handed to it is a copy, and simplification always runs against the
  level-0 compacted vertex set (`C`-space; see the comments in
  `processTemplate`).
- **Quantized attribute strides are padded to 4-byte multiples** (i16×3 →
  8 B, i8×3 → 4 B): meshopt's encoder requires it, so attributes are
  interleaved `x,y,z,pad` and the viewer de-interleaves.
- The recipe is validated against the source by sha256 — a rebuild against
  a different export refuses rather than silently re-baking.
- **Chunked templates are still one node.** The split lives entirely
  inside the compiler: chunks simplify in parallel with `LockBorder` (so
  neighbouring chunks' LODs stay mated), and the LODs stitch back into the
  template's meshes before assembly — recipes, deltas, manifests and the
  viewer never see the chunks.
- **Textures enter the template identity by content hash.** A material's
  `baseColorTexture` is carried as the image's sha256 (not an index), so
  template dedup, deltas and rebuilds are content-stable; image bytes ride
  in plain (uncompressed) 4-byte-aligned bufferViews.

## Honest limits (round 2)

- Triangulated, static meshes only (no skins/animations/cameras — an
  Infinigen room has none on the wire).
- `instance.add` to a brand-new template would need a *fragment GLB*
  (`templates_added[]` in the delta schema reserves it) — **designed, not
  implemented**; the demo delta is a transform edit.
- Colour ships in two measured tiers: flatten (deterministic constants)
  and base-colour textures (content-hashed, not byte-reproducible — the
  bake pixels jitter ±1 LSB on this box; measurements.md §10). Only
  base-colour textures ride the wire; roughness/metal/normal bakes are
  measured artifacts, and the KTX2 transcode stage is still designed, not
  built (a `toktx` source build awaits Casey's OK).
