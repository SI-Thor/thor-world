# `compiler/` — the world-stream recipe→glTF compiler

The deterministic half of the wire: a **recipe** (ours, JSON) plus a source
scene (an Infinigen 2.0 glTF export) compile to a **world file** — glTF 2.0
with real LOD chains, `EXT_mesh_gpu_instancing`, `KHR_mesh_quantization`
and `EXT_meshopt_compression` on every bufferView, provenance in
`asset.extras`, sorted timestamp-free JSON ⇒ byte-identical rebuilds.
Design and decisions: `../docs/design.md`. Numbers: `../docs/measurements.md`.

## Layout

| path | what |
|---|---|
| `worldc.mjs` | the compiler (single file, Node ≥ 18, dep: `meshoptimizer` only) |
| `package.json` | `npm install` gives you `meshoptimizer@1.3` (MIT) |
| `tools/export_scene.py` | runs on the **Thor**: generates an Infinigen 2.0 scene through the v2 venv's own CLI path and exports glTF from the same resident bpy process (writes only into its `--outdir`) |
| `test/make_fixture.mjs` | deterministic synthetic source scene (boxes + rock) for smoke tests |

## Commands

```bash
npm install

# 1. derive the canonical v1 recipe from a source glTF
node worldc.mjs recipe --source source.glb --world livingroom \
  --meta generator=room_livingroom_rand --meta seed=42 \
  --meta engine=infinigen@2.0.0a3+bpy4.2 --out v1.json

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

## Honest limits (first slice)

- Triangulated, static meshes only (no skins/animations/cameras — an
  Infinigen room has none on the wire).
- `instance.add` to a brand-new template would need a *fragment GLB*
  (`templates_added[]` in the delta schema reserves it) — **designed, not
  implemented in this slice**; the demo delta is a transform edit, and the
  op the viewer handles today is `instance.update`/`remove` plus `add` of
  an entry whose template already exists client-side.
- Texture stage (KTX2/Basis) is designed, not implemented: this scene has
  no textures, and `toktx` needs a source build on the Thor (Casey's OK).
