#!/usr/bin/env node
// worldc — the world-stream recipe→glTF compiler.
//
// A recipe (ours, JSON) + a source scene (an Infinigen 2.0 glTF export) compile
// to a deterministic glTF 2.0 "world file":
//
//   - templates: identical (geometry+material) objects deduplicated by hash,
//     placed with EXT_mesh_gpu_instancing — declared in node.extras + manifest
//   - real LOD chains: several *simplified meshes* per template (meshoptimizer
//     quadric simplification), each real geometry in the file, listed in
//     node.extras.lods with triangle counts and world-space error
//   - KHR_mesh_quantization: positions → normalized i16 in the template's
//     bounding box, normals → normalized i8, UVs → normalized u16
//   - EXT_meshopt_compression on every bufferView (vertices, indices, instances)
//   - provenance in asset.extras (branch, version, recipe sha256, source sha256,
//     engine pin) — no timestamps, sorted JSON keys ⇒ byte-identical rebuilds
//
// Subcommands:
//   recipe  derive the canonical v1 recipe from a source glTF
//   edit    apply one parameter edit to a recipe (bumps the version)
//   build   compile a recipe to world@N.glb + manifest.json
//   delta   diff two recipes to a since_version delta (only changed nodes)
//   verify  build twice in-process and byte-compare (determinism check)
//
// Node ≥ 18; single dependency: meshoptimizer (npm, MIT).

import { createHash } from 'node:crypto';
import { performance } from 'node:perf_hooks';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { basename, dirname, resolve } from 'node:path';
import { Worker } from 'node:worker_threads';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { MeshoptEncoder, MeshoptSimplifier } from 'meshoptimizer';
// workers resolve bare specifiers from the process CWD — pin the package by
// this module's own location so builds work from any directory
const MESHOPT_SPEC = pathToFileURL(createRequire(import.meta.url).resolve('meshoptimizer')).href;
const COMPILER = 'world-stream/0.1';
const SCHEMA_RECIPE = 'world-stream.recipe/1';
const SCHEMA_DELTA = 'world-stream.delta/1';
const DEFAULT_LODS = { ratios: [1.0, 0.5, 0.25, 0.1], errors: [0.0, 0.01, 0.025, 0.06], min_triangles: 8 };

// ---------------------------------------------------------------- utilities

const sha256 = (buf) => createHash('sha256').update(buf).digest('hex');
const bytesOf = (ta) => new Uint8Array(ta.buffer, ta.byteOffset, ta.byteLength);

function canonicalJson(value) {
  // Deterministic JSON text: recursively key-sorted, no whitespace, JS
  // shortest-round-trip numbers. Same input ⇒ same bytes on any machine.
  const out = [];
  const walk = (v) => {
    if (v === null || typeof v === 'string' || typeof v === 'boolean') out.push(JSON.stringify(v));
    else if (typeof v === 'number') {
      if (!Number.isFinite(v)) throw new Error('non-finite number reached serialization');
      out.push(String(v));
    } else if (Array.isArray(v)) {
      out.push('[');
      for (let i = 0; i < v.length; i++) { if (i) out.push(','); walk(v[i]); }
      out.push(']');
    } else if (typeof v === 'object') {
      out.push('{');
      const keys = Object.keys(v).sort();
      for (let i = 0; i < keys.length; i++) {
        if (i) out.push(',');
        out.push(JSON.stringify(keys[i]), ':');
        walk(v[keys[i]]);
      }
      out.push('}');
    } else throw new Error(`unserializable value: ${typeof v}`);
  };
  walk(value);
  return out.join('');
}

function writeGlb(json, binary) {
  const jsonBytes = Buffer.from(canonicalJson(json), 'utf8');
  const jsonPad = (4 - (jsonBytes.length % 4)) % 4;
  const binPad = (4 - (binary.length % 4)) % 4;
  const jsonLen = jsonBytes.length + jsonPad;
  const binLen = binary.length + binPad;
  const glb = Buffer.alloc(12 + 8 + jsonLen + 8 + binLen);
  glb.writeUInt32LE(0x46546c67, 0);  // magic "glTF"
  glb.writeUInt32LE(2, 4);           // version
  glb.writeUInt32LE(glb.length, 8);  // total length
  glb.writeUInt32LE(jsonLen, 12);
  glb.writeUInt32LE(0x4e4f534a, 16); // chunkType "JSON"
  jsonBytes.copy(glb, 20);
  for (let i = 0; i < jsonPad; i++) glb[20 + jsonBytes.length + i] = 0x20; // pad with spaces
  const binHeader = 20 + jsonLen;
  glb.writeUInt32LE(binLen, binHeader);
  glb.writeUInt32LE(0x004e4942, binHeader + 4); // chunkType "BIN"
  binary.copy(glb, binHeader + 8);
  return glb;
}

function readGlb(path) {
  const buf = readFileSync(path);
  if (buf.readUInt32LE(0) !== 0x46546c67) throw new Error(`${path}: not a GLB`);
  const jsonLen = buf.readUInt32LE(12);
  if (buf.readUInt32LE(16) !== 0x4e4f534a) throw new Error(`${path}: first chunk is not JSON`);
  const json = JSON.parse(buf.toString('utf8', 20, 20 + jsonLen));
  let bin = Buffer.alloc(0);
  let off = 20 + jsonLen;
  while (off + 8 <= buf.length) {
    const len = buf.readUInt32LE(off);
    if (buf.readUInt32LE(off + 4) === 0x004e4942) { bin = buf.subarray(off + 8, off + 8 + len); break; }
    off += 8 + len;
  }
  return { json, bin, bytes: buf.length, sha256: sha256(buf) };
}

// glTF accessor decode — the component types Blender's exporter emits.
const COMPONENT = { 5120: Int8Array, 5121: Uint8Array, 5122: Int16Array, 5123: Uint16Array, 5125: Uint32Array, 5126: Float32Array };
const NUM_COMPONENTS = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4, MAT4: 16 };

function readAccessor(gltf, bin, accessorIndex, { asFloat = false } = {}) {
  const acc = gltf.accessors[accessorIndex];
  const bv = gltf.bufferViews[acc.bufferView];
  const Ta = COMPONENT[acc.componentType];
  const n = NUM_COMPONENTS[acc.type];
  const base = (bv.byteOffset || 0) + (acc.byteOffset || 0);
  const stride = bv.byteStride || n * Ta.BYTES_PER_ELEMENT;
  if (stride % Ta.BYTES_PER_ELEMENT !== 0) throw new Error('unaligned accessor stride');
  const count = acc.count;
  const view = new Ta(bin.buffer, bin.byteOffset + base, Math.floor((bin.length - base) / Ta.BYTES_PER_ELEMENT));
  if (stride === n * Ta.BYTES_PER_ELEMENT) {
    const slice = view.subarray(0, count * n);
    return asFloat ? Float32Array.from(slice) : slice.slice();
  }
  const out = new (asFloat ? Float32Array : Ta)(count * n);
  for (let i = 0; i < count; i++)
    for (let c = 0; c < n; c++) out[i * n + c] = view[(i * stride) / Ta.BYTES_PER_ELEMENT + c];
  return out;
}

// ------------------------------------------------------------ linear algebra

function mat4Identity() { const m = new Float64Array(16); m[0] = m[5] = m[10] = m[15] = 1; return m; }

function mat4Multiply(a, b) { // a ∘ b (column-major, like glTF)
  const out = new Float64Array(16);
  for (let c = 0; c < 4; c++)
    for (let r = 0; r < 4; r++)
      out[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
  return out;
}

function mat4FromTrs(t, q, s) {
  const [x, y, z, w] = q;
  const m = new Float64Array(16);
  m[0] = (1 - 2 * y * y - 2 * z * z) * s[0]; m[1] = (2 * x * y + 2 * z * w) * s[0]; m[2] = (2 * x * z - 2 * y * w) * s[0];
  m[4] = (2 * x * y - 2 * z * w) * s[1]; m[5] = (1 - 2 * x * x - 2 * z * z) * s[1]; m[6] = (2 * y * z + 2 * x * w) * s[1];
  m[8] = (2 * x * z + 2 * y * w) * s[2]; m[9] = (2 * y * z - 2 * x * w) * s[2]; m[10] = (1 - 2 * x * x - 2 * y * y) * s[2];
  m[12] = t[0]; m[13] = t[1]; m[14] = t[2]; m[15] = 1;
  return m;
}

function mat4Decompose(m) { // → {t, q, s}; assumes no shear (glTF static exports)
  const t = [m[12], m[13], m[14]];
  const sx = Math.hypot(m[0], m[1], m[2]);
  const sy = Math.hypot(m[4], m[5], m[6]);
  const sz = Math.hypot(m[8], m[9], m[10]);
  const s = [sx || 1, sy || 1, sz || 1];
  const r = [
    m[0] / (sx || 1), m[1] / (sx || 1), m[2] / (sx || 1),
    m[4] / (sy || 1), m[5] / (sy || 1), m[6] / (sy || 1),
    m[8] / (sz || 1), m[9] / (sz || 1), m[10] / (sz || 1),
  ];
  const trace = r[0] + r[4] + r[8];
  let q;
  if (trace > 0) {
    const root = Math.sqrt(trace + 1) * 2;
    q = [(r[7] - r[5]) / root, (r[2] - r[6]) / root, (r[3] - r[1]) / root, 0.25 * root];
  } else if (r[0] > r[4] && r[0] > r[8]) {
    const root = Math.sqrt(1 + r[0] - r[4] - r[8]) * 2;
    q = [0.25 * root, (r[1] + r[3]) / root, (r[2] + r[6]) / root, (r[7] - r[5]) / root];
  } else if (r[4] > r[8]) {
    const root = Math.sqrt(1 + r[4] - r[0] - r[8]) * 2;
    q = [(r[1] + r[3]) / root, 0.25 * root, (r[5] + r[7]) / root, (r[2] - r[6]) / root];
  } else {
    const root = Math.sqrt(1 + r[8] - r[0] - r[4]) * 2;
    q = [(r[2] + r[6]) / root, (r[5] + r[7]) / root, 0.25 * root, (r[3] - r[1]) / root];
  }
  const norm = Math.hypot(...q) || 1;
  q = q.map((v) => v / norm);
  if (q[3] < 0) q = q.map((v) => -v); // canonical sign: w ≥ 0
  return { t: t.map((v) => +v.toFixed(9)), q: q.map((v) => +v.toFixed(9)), s: s.map((v) => +v.toFixed(9)) };
}

// ----------------------------------------------------------- scene harvest

function harvestSource(gltf, bin) {
  // Every mesh primitive in the default scene: geometry, effective material,
  // world matrix. One item per (node, primitive). Images referenced by
  // base-colour textures are harvested too (content-hashed — two materials
  // pointing at identical bytes share one image on the wire).
  const items = [];
  const images = new Map(); // image index → {sha256, mimeType, bytes}
  const imageOf = (gltfMaterial) => {
    const texIndex = gltfMaterial?.pbrMetallicRoughness?.baseColorTexture?.index;
    if (texIndex === undefined) return null;
    const texture = gltf.textures?.[texIndex];
    if (!texture || texture.source === undefined) return null;
    const img = gltf.images?.[texture.source];
    if (!img || img.bufferView === undefined) return null;
    if (!images.has(texture.source)) {
      const bv = gltf.bufferViews[img.bufferView];
      const bytes = bin.subarray(bv.byteOffset || 0, (bv.byteOffset || 0) + bv.byteLength);
      images.set(texture.source, { sha256: sha256(bytes), mimeType: img.mimeType ?? 'image/png', bytes });
    }
    return images.get(texture.source).sha256;
  };
  const sceneIndex = gltf.scene ?? 0;
  const roots = gltf.scenes?.[sceneIndex]?.nodes ?? [];
  const visit = (nodeIndex, parent) => {
    const node = gltf.nodes[nodeIndex];
    let local = mat4Identity();
    if (node.matrix) local = Float64Array.from(node.matrix);
    else local = mat4FromTrs(node.translation ?? [0, 0, 0], node.rotation ?? [0, 0, 0, 1], node.scale ?? [1, 1, 1]);
    const world = parent === null ? local : mat4Multiply(parent, local);
    if (node.mesh !== undefined) {
      const mesh = gltf.meshes[node.mesh];
      mesh.primitives.forEach((prim, p) => {
        if (prim.mode !== undefined && prim.mode !== 4) return; // triangles only
        const positions = readAccessor(gltf, bin, prim.attributes.POSITION, { asFloat: true });
        const normals = prim.attributes.NORMAL !== undefined
          ? readAccessor(gltf, bin, prim.attributes.NORMAL, { asFloat: true }) : null;
        const uvs = prim.attributes.TEXCOORD_0 !== undefined
          ? readAccessor(gltf, bin, prim.attributes.TEXCOORD_0, { asFloat: true }) : null;
        const indices = prim.indices !== undefined
          ? (() => {
            const raw = readAccessor(gltf, bin, prim.indices);
            const out = new Uint32Array(raw.length);
            for (let i = 0; i < raw.length; i++) out[i] = raw[i];
            return out;
          })()
          : Uint32Array.from({ length: positions.length / 3 }, (_, i) => i);
        const material = effectiveMaterial(gltf, prim.material, (m) => imageOf(m));
        const h = createHash('sha256');
        h.update(Buffer.from(positions.buffer, positions.byteOffset, positions.byteLength));
        if (normals) h.update(Buffer.from(normals.buffer, normals.byteOffset, normals.byteLength));
        if (uvs) h.update(Buffer.from(uvs.buffer, uvs.byteOffset, uvs.byteLength));
        h.update(Buffer.from(indices.buffer, indices.byteOffset, indices.byteLength));
        h.update(canonicalJson(material));
        items.push({
          id: (node.name ?? `node_${nodeIndex}`) + (mesh.primitives.length > 1 ? `.p${p}` : ''),
          world,
          positions, normals, uvs, indices, material,
          geometryHash: h.digest('hex'),
          triangles: indices.length / 3,
        });
      });
    }
    for (const child of node.children ?? []) visit(child, world);
  };
    for (const root of roots) visit(root, null);
    return { items, images };
  }

function effectiveMaterial(gltf, materialIndex, imageOf = null) {
  if (materialIndex === undefined || materialIndex === null) {
    return { baseColorFactor: [0.8, 0.8, 0.8, 1], metallicFactor: 0, roughnessFactor: 0.9, doubleSided: false };
  }
  const mat = gltf.materials[materialIndex];
  const pbr = mat.pbrMetallicRoughness ?? {};
  const texSha = imageOf ? imageOf(mat) : null;
  const material = {
    baseColorFactor: (pbr.baseColorFactor ?? [0.8, 0.8, 0.8, 1]).map((v) => +v.toFixed(6)),
    metallicFactor: +(pbr.metallicFactor ?? 1).toFixed(6),
    roughnessFactor: +(pbr.roughnessFactor ?? 1).toFixed(6),
    doubleSided: !!mat.doubleSided,
  };
  if (texSha !== null) material.baseColorTexture = texSha; // content hash, not an index
  return material;
}

// ------------------------------------------------------------------ compile

// Spatial chunking (the serial-tail lever): a template far above the target
// triangle count is split into contiguous chunks by recursive median split of
// triangle centroids along the longest bbox axis — a pure function of the
// geometry, so every job count and machine splits identically. Each chunk
// simplifies on its own worker with 'LockBorder': the cut edges are open
// edges, and locked border vertices mean adjacent chunks' LODs still mate
// (no cracks) at the cost of some reduction near the cuts.
function splitSpatial(indices, positions, targetTriangles) {
  const nTri = indices.length / 3;
  const centroids = new Float64Array(nTri * 3);
  for (let t = 0; t < nTri; t++) {
    for (let c = 0; c < 3; c++) {
      centroids[t * 3 + c] = (positions[indices[t * 3] * 3 + c]
        + positions[indices[t * 3 + 1] * 3 + c]
        + positions[indices[t * 3 + 2] * 3 + c]) / 3;
    }
  }
  const chunks = [];
  const rec = (tris) => {
    if (tris.length <= targetTriangles || tris.length < 8) { chunks.push(tris); return; }
    const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
    for (const t of tris)
      for (let c = 0; c < 3; c++) {
        if (centroids[t * 3 + c] < min[c]) min[c] = centroids[t * 3 + c];
        if (centroids[t * 3 + c] > max[c]) max[c] = centroids[t * 3 + c];
      }
    let axis = 0;
    if (max[1] - min[1] > max[axis] - min[axis]) axis = 1;
    if (max[2] - min[2] > max[axis] - min[axis]) axis = 2;
    const sorted = Uint32Array.from(tris).sort((a, b) => {
      const d = centroids[a * 3 + axis] - centroids[b * 3 + axis];
      return d !== 0 ? (d < 0 ? -1 : 1) : a - b;
    });
    const mid = Math.floor(sorted.length / 2);
    rec(sorted.subarray(0, mid));
    rec(sorted.subarray(mid));
  };
  const all = new Uint32Array(nTri);
  for (let t = 0; t < nTri; t++) all[t] = t;
  rec(all);
  return chunks.map((tris) => {
    const out = new Uint32Array(tris.length * 3);
    let o = 0;
    for (const t of tris) { out[o++] = indices[t * 3]; out[o++] = indices[t * 3 + 1]; out[o++] = indices[t * 3 + 2]; }
    return out;
  });
}

// The quantization frame for a chunked template: one bbox over every vertex
// its triangles reference — the same set the un-chunked path compacts to —
// plus the template-wide UV-range decision (every chunk must quantize UVs
// the same way or the stitched buffers do not compose).
function templateFrame(positions, indices, uvs) {
  const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < indices.length; i++) {
    const v = indices[i] * 3;
    for (let c = 0; c < 3; c++) {
      if (positions[v + c] < min[c]) min[c] = positions[v + c];
      if (positions[v + c] > max[c]) max[c] = positions[v + c];
    }
  }
  let uvIsFloat = false;
  if (uvs) {
    for (let i = 0; i < uvs.length; i++) {
      if (uvs[i] < -1e-6 || uvs[i] > 1 + 1e-6) { uvIsFloat = true; break; }
    }
  }
  return {
    center: min.map((m, c) => (m + max[c]) / 2),
    half: min.map((m, c) => Math.max((max[c] - m) / 2, 1e-9)),
    uvIsFloat,
  };
}

async function compile(recipe, sourcePath, { jobs = 1, phaseTimings = null } = {}) {
  const tTotal = performance.now();
  const t0 = performance.now();
  const { json: gltf, bin, sha256: sourceSha, bytes: sourceBytes } = readGlb(sourcePath);
  if (recipe.source?.sha256 && recipe.source.sha256 !== sourceSha) {
    throw new Error(`source sha256 mismatch: recipe pins ${recipe.source.sha256}, file is ${sourceSha}`);
  }
  const harvested = harvestSource(gltf, bin);
  const harvest = harvested.items;
  const sourceImages = harvested.images;
  const byId = new Map(harvest.map((item) => [item.id, item]));
  const lodPolicy = recipe.lod_policy ?? DEFAULT_LODS;
  if (phaseTimings) phaseTimings.parse_source = performance.now() - t0;

  // Group recipe entries into templates (geometry+material identity).
  const t1 = performance.now();
  const templates = new Map();
  const dropped = [];
  for (const entry of recipe.entries) {
    const item = byId.get(entry.src);
    if (!item) throw new Error(`recipe entry ${entry.id}: no source node "${entry.src}"`);
    if (item.triangles < 1) { dropped.push(entry.id); continue; }
    const material = entry.material ?? item.material;
    const hash = material === item.material ? item.geometryHash
      : sha256(Buffer.from(item.geometryHash + canonicalJson(material)));
    if (!templates.has(hash)) templates.set(hash, { hash, material, source: item, entries: [] });
    templates.get(hash).entries.push(entry);
  }
  for (const tpl of templates.values()) tpl.entries.sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const sortedTemplates = [...templates.values()].sort((a, b) => (a.hash < b.hash ? -1 : 1));
  if (phaseTimings) phaseTimings.group_templates = performance.now() - t1;

  // Per-template work: LOD chains, quantization, meshopt encoding. Templates
  // far above the chunk policy's triangle threshold are split spatially so
  // the simplification fans out (the round-1 serial tail); chunk results are
  // stitched back per template before assembly.
  const t2 = performance.now();
  const chunkPolicy = lodPolicy.chunk ?? null;
  const work = [];
  const templateOfWork = [];
  sortedTemplates.forEach((tpl, ti) => {
    const src = tpl.source;
    if (chunkPolicy && src.indices.length / 3 > chunkPolicy.above_triangles) {
      const chunks = splitSpatial(src.indices, src.positions, chunkPolicy.target_triangles);
      const frame = templateFrame(src.positions, src.indices, src.uvs);
      chunks.forEach((indices) => {
        work.push({
          hash: tpl.hash, chunked: true, frame,
          positions: src.positions, normals: src.normals, uvs: src.uvs,
          indices, instances: [], policy: lodPolicy,
        });
        templateOfWork.push(ti);
      });
    } else {
      work.push({
        hash: tpl.hash,
        positions: src.positions, normals: src.normals, uvs: src.uvs,
        indices: src.indices,
        instances: tpl.entries.map((e) => e.params),
        policy: lodPolicy,
      });
      templateOfWork.push(ti);
    }
  });
  const results = jobs > 1 ? await runInWorkers(work, jobs) : work.map(processTemplate);
  const templateResults = [];
  {
    const byTemplate = new Map();
    work.forEach((item, i) => {
      const ti = templateOfWork[i];
      if (item.chunked) {
        if (!byTemplate.has(ti)) byTemplate.set(ti, []);
        byTemplate.get(ti).push(results[i]);
      } else {
        templateResults[ti] = results[i];
      }
    });
    for (const [ti, chunks] of byTemplate) templateResults[ti] = stitchChunks(sortedTemplates[ti], chunks);
  }
  if (phaseTimings) phaseTimings.lod_quantize_encode = performance.now() - t2;

  // Assemble the glTF document — deterministic order: templates sorted by hash.
  const t3 = performance.now();
  const binaryChunks = [];
  let binLen = 0;
  const bufferViews = [];
  const accessors = [];
  const meshes = [];
  const nodes = [];
  const materialsOut = [];
  const materialIndex = new Map();
  const usedImages = new Map(); // image sha → {index in images[], bytes, mimeType}
  const textures = [];          // one texture per image (single repeat sampler)

  const pushCompressed = (encoded, decodedByteLength, mode, stride) => {
    const viewIndex = bufferViews.length;
    bufferViews.push({ byteLength: decodedByteLength }); // decoded size; no own buffer
    const copy = Buffer.from(encoded.buffer, encoded.byteOffset, encoded.byteLength);
    binaryChunks.push(copy);
    const pad = (8 - (copy.length % 8)) % 8;
    if (pad) binaryChunks.push(Buffer.alloc(pad));
    bufferViews[viewIndex].extensions = {
      EXT_meshopt_compression: { buffer: 0, byteOffset: binLen, byteLength: copy.length, byteStride: stride, mode, filter: 'NONE' },
    };
    binLen += copy.length + pad;
    return viewIndex;
  };

  // image bytes (PNG/JPEG) ride uncompressed in their own 4-byte-aligned views
  const pushRaw = (bytes) => {
    const viewIndex = bufferViews.length;
    bufferViews.push({ byteOffset: binLen, byteLength: bytes.length });
    const copy = Buffer.from(bytes);
    binaryChunks.push(copy);
    const pad = (4 - (copy.length % 4)) % 4;
    if (pad) binaryChunks.push(Buffer.alloc(pad));
    binLen += copy.length + pad;
    return viewIndex;
  };

  const pushAccessor = (viewIndex, componentType, type, count, normalized, minMax = null) => {
    const acc = { bufferView: viewIndex, componentType, type, count };
    if (normalized) acc.normalized = true;
    if (minMax) { acc.min = minMax.min; acc.max = minMax.max; }
    accessors.push(acc);
    return accessors.length - 1;
  };

  const materialFor = (material) => {
    const key = canonicalJson(material);
    if (!materialIndex.has(key)) {
      let baseColorTexture;
      if (material.baseColorTexture) {
        if (!usedImages.has(material.baseColorTexture)) {
          const img = sourceImages instanceof Map
            ? [...sourceImages.values()].find((x) => x.sha256 === material.baseColorTexture)
            : null;
          if (!img) throw new Error(`material references unknown image ${material.baseColorTexture.slice(0, 12)}…`);
          usedImages.set(material.baseColorTexture, {
            index: usedImages.size, bytes: img.bytes, mimeType: img.mimeType,
          });
          textures.push({ sampler: 0, source: usedImages.size - 1 });
        }
        baseColorTexture = { index: usedImages.get(material.baseColorTexture).index };
      }
      materialIndex.set(key, materialsOut.length);
      materialsOut.push({
        name: `mat_${materialsOut.length}`,
        pbrMetallicRoughness: {
          baseColorFactor: material.baseColorFactor,
          ...(baseColorTexture ? { baseColorTexture } : {}),
          metallicFactor: material.metallicFactor,
          roughnessFactor: material.roughnessFactor,
        },
        doubleSided: material.doubleSided,
      });
    }
    return materialIndex.get(key);
  };

  const manifestTemplates = [];
  let trianglesLod0 = 0;
  let encodedBytes = 0;
  for (let ti = 0; ti < sortedTemplates.length; ti++) {
    const tpl = sortedTemplates[ti];
    const res = templateResults[ti];
    const matIndex = materialFor(tpl.material);
    const lodTable = [];
    for (const lod of res.lods) {
      const attributes = {};
      const posView = pushCompressed(lod.positionView, lod.vertexCount * 8, 'ATTRIBUTES', 8);
      attributes.POSITION = pushAccessor(posView, 5122, 'VEC3', lod.vertexCount, true,
        { min: lod.positionMin.map((v) => v / 32767), max: lod.positionMax.map((v) => v / 32767) });
      if (lod.normalView !== null) {
        const nrmView = pushCompressed(lod.normalView, lod.vertexCount * 4, 'ATTRIBUTES', 4);
        attributes.NORMAL = pushAccessor(nrmView, 5120, 'VEC3', lod.vertexCount, true);
      }
      if (lod.uvView !== null) {
        const uvStride = lod.uvIsFloat ? 8 : 4;
        const uvView = pushCompressed(lod.uvView, lod.vertexCount * uvStride, 'ATTRIBUTES', uvStride);
        attributes.TEXCOORD_0 = pushAccessor(uvView, lod.uvIsFloat ? 5126 : 5123, 'VEC2', lod.vertexCount, !lod.uvIsFloat);
      }
      const indexWidth = lod.indicesAre32 ? 4 : 2;
      const idxView = pushCompressed(lod.indicesView, lod.indexCount * indexWidth, 'TRIANGLES', indexWidth);
      const indicesAcc = pushAccessor(idxView, lod.indicesAre32 ? 5125 : 5123, 'SCALAR', lod.indexCount, false);
      meshes.push({
        name: `tpl_${tpl.hash.slice(0, 12)}_lod${lod.level}`,
        primitives: [{ attributes, indices: indicesAcc, material: matIndex }],
        extras: { worldTemplate: tpl.hash, lod: lod.level, triangles: lod.indexCount / 3 },
      });
      lodTable.push({ mesh: meshes.length - 1, triangles: lod.indexCount / 3, error: lod.error, worldError: lod.worldError, level: lod.level });
      if (lod.level === 0) trianglesLod0 += lod.indexCount / 3;
    }
    const inst = res.instances;
    const tView = pushCompressed(MeshoptEncoder.encodeVertexBuffer(bytesOf(inst.translation), inst.count, 12), inst.count * 12, 'ATTRIBUTES', 12);
    const rView = pushCompressed(MeshoptEncoder.encodeVertexBuffer(bytesOf(inst.rotation), inst.count, 16), inst.count * 16, 'ATTRIBUTES', 16);
    const sView = pushCompressed(MeshoptEncoder.encodeVertexBuffer(bytesOf(inst.scale), inst.count, 12), inst.count * 12, 'ATTRIBUTES', 12);
    nodes.push({
      name: `tpl_${tpl.hash.slice(0, 12)}`,
      mesh: lodTable[0].mesh,
      translation: res.dequant.center,
      scale: res.dequant.half,
      extensions: { EXT_mesh_gpu_instancing: { attributes: {
        TRANSLATION: pushAccessor(tView, 5126, 'VEC3', inst.count, false),
        ROTATION: pushAccessor(rView, 5126, 'VEC4', inst.count, false),
        SCALE: pushAccessor(sView, 5126, 'VEC3', inst.count, false),
      } } },
      extras: { worldTemplate: tpl.hash, lods: lodTable, sourceNodes: tpl.entries.map((e) => e.id) },
    });
    manifestTemplates.push({ hash: tpl.hash, node: nodes.length - 1, lods: lodTable, entryIds: tpl.entries.map((e) => e.id) });
  }
  // image bytes must land in the BIN chunk before it is concatenated
  const imageViews = textures.length
    ? [...usedImages.values()].map((img) => ({ bufferView: pushRaw(img.bytes), mimeType: img.mimeType }))
    : null;
  encodedBytes = binLen;
  const binChunk = Buffer.concat(binaryChunks);
  const recipeSha = sha256(Buffer.from(canonicalJson(recipe)));
  const doc = {
    asset: {
      version: '2.0',
      generator: COMPILER,
      extras: {
        world: recipe.world, branch: recipe.branch, version: recipe.version,
        recipe_sha256: recipeSha, source_sha256: sourceSha,
        source_kind: recipe.source?.kind ?? 'unknown',
        engine: recipe.source?.engine ?? null,
        templates: sortedTemplates.length,
        instances: recipe.entries.length - dropped.length,
        triangles_lod0: trianglesLod0,
        determinism: 'sorted keys, no timestamps, canonical numbers',
      },
    },
    scene: 0,
    scenes: [{ name: recipe.world, nodes: nodes.map((_, i) => i) }],
    extensionsUsed: ['EXT_mesh_gpu_instancing', 'EXT_meshopt_compression', 'KHR_mesh_quantization'],
    extensionsRequired: ['EXT_meshopt_compression', 'KHR_mesh_quantization'],
    ...(materialsOut.length ? { materials: materialsOut } : {}),
    ...(imageViews ? { images: imageViews, samplers: [{ wrapS: 10497, wrapT: 10497 }], textures } : {}),
    ...(meshes.length ? { meshes } : {}),
    nodes,
    accessors,
    bufferViews,
    buffers: [{ byteLength: binChunk.length }],
  };
  const glb = writeGlb(doc, binChunk);
  if (phaseTimings) {
    phaseTimings.assemble_serialize = performance.now() - t3;
    phaseTimings.total = performance.now() - tTotal;
  }

  const entriesMap = {};
  for (const tpl of sortedTemplates) tpl.entries.forEach((e, i) => { entriesMap[e.id] = { template: tpl.hash, instance: i }; });
  const manifest = {
    schema: 'world-stream.manifest/1',
    world: recipe.world, branch: recipe.branch, version: recipe.version,
    recipe_sha256: recipeSha, source_sha256: sourceSha,
    file: `world@${recipe.version}.glb`,
    glb_sha256: sha256(glb), glb_bytes: glb.length,
    bin_bytes: encodedBytes, source_bytes: sourceBytes,
    templates: manifestTemplates,
    entries: entriesMap,
    dropped,
    triangles_lod0: trianglesLod0,
    compiler: COMPILER,
  };
  return { glb, manifest, phaseTimings };
}

// meshopt-encode one processed LOD's buffers. Runs inside workers (normal
// templates) and on the main thread (stitched chunked templates) — hence a
// top-level function embedded into the worker source too.
function encodeLodViews(lod) {
  lod.positionView = MeshoptEncoder.encodeVertexBuffer(bytesOf(lod.qPos), lod.vertexCount, 8);
  lod.normalView = lod.qNrm ? MeshoptEncoder.encodeVertexBuffer(bytesOf(lod.qNrm), lod.vertexCount, 4) : null;
  lod.uvView = lod.qUv ? MeshoptEncoder.encodeVertexBuffer(bytesOf(lod.qUv), lod.vertexCount, lod.uvIsFloat ? 8 : 4) : null;
  lod.indicesView = MeshoptEncoder.encodeIndexBuffer(lod.indexBytes, lod.indexCount, lod.indicesAre32 ? 4 : 2);
  lod.qPos = null; lod.qNrm = null; lod.qUv = null; lod.indexBytes = null; // keep the wire small
  return lod;
}

// Fold a chunked template's per-chunk LODs back into one template result:
// level-by-level concatenation with per-chunk index offsets (the shared
// quantization frame makes the buffers composable). A chunk whose own chain
// stalled repeats its coarsest LOD while others still progress; the template
// chain stops when a stitched level no longer shrinks (honest, at the level
// that matters — the one the viewer sees).
function stitchChunks(tpl, chunkResults) {
  const depth = Math.max(...chunkResults.map((r) => r.lods.length));
  const lods = [];
  for (let level = 0; level < depth; level++) {
    let vertexCount = 0; let indexCount = 0;
    for (const r of chunkResults) {
      const l = r.lods[Math.min(level, r.lods.length - 1)];
      vertexCount += l.vertexCount; indexCount += l.indexCount;
    }
    if (level > 0 && indexCount / 3 >= lods[level - 1].triangles * 0.98) break;
    const first = chunkResults[0].lods[Math.min(level, chunkResults[0].lods.length - 1)];
    const hasNrm = chunkResults.every((r) => r.lods[Math.min(level, r.lods.length - 1)].qNrm !== null);
    const hasUv = chunkResults.every((r) => r.lods[Math.min(level, r.lods.length - 1)].qUv !== null);
    if (hasUv && chunkResults.some((r) => r.lods[Math.min(level, r.lods.length - 1)].uvIsFloat !== first.uvIsFloat)) {
      throw new Error('chunked template: mixed uv quantization across chunks');
    }
    const indicesAre32 = vertexCount - 1 > 65535;
    const qPos = new Int16Array(vertexCount * 4);
    const qNrm = hasNrm ? new Int8Array(vertexCount * 4) : null;
    const qUv = hasUv ? new Float32Array(0) : null;
    const uvIsFloat = first.uvIsFloat;
    const realQUv = hasUv ? (uvIsFloat ? new Float32Array(vertexCount * 2) : new Uint16Array(vertexCount * 2)) : null;
    const indexBytes = indicesAre32 ? new Uint32Array(indexCount) : new Uint16Array(indexCount);
    const min = [32767, 32767, 32767], max = [-32767, -32767, -32767];
    let worldError = 0; let vOff = 0; let iOff = 0;
    for (const r of chunkResults) {
      const l = r.lods[Math.min(level, r.lods.length - 1)];
      qPos.set(l.qPos, vOff * 4);
      if (qNrm && l.qNrm) qNrm.set(l.qNrm, vOff * 4);
      if (realQUv && l.qUv) realQUv.set(l.qUv, vOff * 2);
      for (let i = 0; i < l.indexBytes.length; i++) indexBytes[iOff + i] = l.indexBytes[i] + vOff;
      for (let c = 0; c < 3; c++) {
        if (l.positionMin[c] < min[c]) min[c] = l.positionMin[c];
        if (l.positionMax[c] > max[c]) max[c] = l.positionMax[c];
      }
      worldError = Math.max(worldError, l.worldError);
      vOff += l.vertexCount; iOff += l.indexBytes.length;
    }
    lods.push(encodeLodViews({
      level, triangles: indexCount / 3, error: 0, worldError,
      vertexCount, indexCount, positionMin: min, positionMax: max,
      uvIsFloat, indicesAre32, qPos, qNrm, qUv: realQUv, indexBytes,
    }));
  }
  const count = tpl.entries.length;
  const translation = new Float32Array(count * 3);
  const rotation = new Float32Array(count * 4);
  const scale = new Float32Array(count * 3);
  tpl.entries.forEach((e, i) => {
    translation.set(e.params.t ?? [0, 0, 0], i * 3);
    rotation.set(e.params.q ?? [0, 0, 0, 1], i * 4);
    scale.set(e.params.s ?? [1, 1, 1], i * 3);
  });
  return {
    hash: tpl.hash, lods,
    instances: { count, translation, rotation, scale },
    dequant: chunkResults[0].dequant,
  };
}

// Per-template LOD + quantize + encode. Pure function of the work item —
// deterministic, and safe to run in any worker thread.
//
// meshoptimizer's JS compactMesh() mutates the index array it is given (it
// writes the remapped indices back), so every array handed to it is a copy,
// and simplification always runs against the *compacted* vertex set.
function processTemplate(item) {
  const { positions, indices, policy } = item;
  const hasNormals = !!item.normals;
  const hasUvs = !!item.uvs;

  // ---- compact level 0 once: drop vertices no triangle references
  const base = indices.slice();
  const [remap0, vertexCount] = MeshoptSimplifier.compactMesh(base); // base is now compacted
  const C = new Float32Array(vertexCount * 3);
  const CN = hasNormals ? new Float32Array(vertexCount * 3) : null;
  const CU = hasUvs ? new Float32Array(vertexCount * 2) : null;
  for (let v = 0, srcVerts = positions.length / 3; v < srcVerts; v++) {
    const nv = remap0[v];
    if (nv === 0xffffffff) continue;
    C[nv * 3] = positions[v * 3]; C[nv * 3 + 1] = positions[v * 3 + 1]; C[nv * 3 + 2] = positions[v * 3 + 2];
    if (CN) { CN[nv * 3] = item.normals[v * 3]; CN[nv * 3 + 1] = item.normals[v * 3 + 1]; CN[nv * 3 + 2] = item.normals[v * 3 + 2]; }
    if (CU) { CU[nv * 2] = item.uvs[v * 2]; CU[nv * 2 + 1] = item.uvs[v * 2 + 1]; }
  }

  // Template-local bounding box (shared by every LOD: one node dequant). A
  // chunked item receives the template-wide frame explicitly — every chunk
  // of a template must quantize against the same box to stitch cleanly.
  let center, half;
  if (item.frame) {
    center = item.frame.center; half = item.frame.half;
  } else {
    const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
    for (let i = 0; i < C.length; i += 3)
      for (let c = 0; c < 3; c++) {
        if (C[i + c] < min[c]) min[c] = C[i + c];
        if (C[i + c] > max[c]) max[c] = C[i + c];
      }
    center = min.map((m, c) => (m + max[c]) / 2);
    half = min.map((m, c) => Math.max((max[c] - m) / 2, 1e-9));
  }
  const scaleExtent = MeshoptSimplifier.getScale(C, 3); // stride in floats
  const simplifyFlags = item.chunked ? ['LockBorder'] : [];

  // quantized attribute strides must be 4-byte multiples for meshopt encoding,
  // so VEC3 i16 → 8 bytes (xyz+pad) and VEC3 i8 → 4 bytes (xyz+pad)
  const quantizeLod = (lodIndices, remap, unique, srcVertexCount, level) => {
    const qPos = new Int16Array(unique * 4);
    const posMinI = [32767, 32767, 32767], posMaxI = [-32767, -32767, -32767];
    for (let v = 0; v < srcVertexCount; v++) {
      const nv = remap[v];
      if (nv === 0xffffffff) continue;
      for (let c = 0; c < 3; c++) {
        const q = Math.max(-32767, Math.min(32767, Math.round(((C[v * 3 + c] - center[c]) / half[c]) * 32767)));
        qPos[nv * 4 + c] = q;
        if (q < posMinI[c]) posMinI[c] = q;
        if (q > posMaxI[c]) posMaxI[c] = q;
      }
    }
    let qNrm = null;
    if (CN) {
      qNrm = new Int8Array(unique * 4);
      for (let v = 0; v < srcVertexCount; v++) {
        const nv = remap[v];
        if (nv === 0xffffffff) continue;
        for (let c = 0; c < 3; c++)
          qNrm[nv * 4 + c] = Math.max(-127, Math.min(127, Math.round(CN[v * 3 + c] * 127)));
      }
    }
    let qUv = null; let uvIsFloat = false;
    if (CU) {
      if (item.frame) uvIsFloat = item.frame.uvIsFloat; // template-wide decision
      else {
        let inRange = true;
        for (let i = 0; i < CU.length; i++) if (CU[i] < -1e-6 || CU[i] > 1 + 1e-6) { inRange = false; break; }
        uvIsFloat = !inRange;
      }
      qUv = uvIsFloat ? new Float32Array(unique * 2) : new Uint16Array(unique * 2);
      for (let v = 0; v < srcVertexCount; v++) {
        const nv = remap[v];
        if (nv === 0xffffffff) continue;
        for (let c = 0; c < 2; c++)
          qUv[nv * 2 + c] = uvIsFloat ? CU[v * 2 + c]
            : Math.max(0, Math.min(65535, Math.round(CU[v * 2 + c] * 65535)));
      }
    }
    const indicesAre32 = unique - 1 > 65535;
    let indexBytes;
    if (indicesAre32) indexBytes = lodIndices;
    else {
      indexBytes = new Uint16Array(lodIndices.length);
      for (let i = 0; i < lodIndices.length; i++) indexBytes[i] = lodIndices[i];
    }
    return {
      level,
      triangles: lodIndices.length / 3,
      error: 0, // filled by the caller for level > 0
      worldError: 0,
      vertexCount: unique,
      indexCount: lodIndices.length,
      positionMin: posMinI, positionMax: posMaxI,
      uvIsFloat, indicesAre32,
      qPos, qNrm, qUv, indexBytes,
    };
  };

  const identityRemap = (n) => {
    const r = new Uint32Array(n);
    for (let i = 0; i < n; i++) r[i] = i;
    return r;
  };

  const lods = [];
  let prevIndices = base; // always in C-space (level-0 compacted numbering)
  for (let level = 0; level < policy.ratios.length; level++) {
    if (level === 0) {
      const lod = quantizeLod(prevIndices, identityRemap(vertexCount), vertexCount, vertexCount, 0);
      lods.push(lod);
      continue;
    }
    const prev = lods[lods.length - 1];
    if (prev.triangles <= policy.min_triangles) break;
    const target = Math.max(3, Math.round((base.length * policy.ratios[level]) / 3) * 3);
    const [raw, error] = MeshoptSimplifier.simplify(
      prevIndices, C, 3, target, policy.errors[level], simplifyFlags,
    );
    if (raw.length >= prevIndices.length * 0.95) break; // no real progress ⇒ stop the chain
    // compact this LOD's vertices (drop those the coarse mesh no longer uses)
    const compact = raw.slice(); // compactMesh mutates its argument
    const [r2, u2] = MeshoptSimplifier.compactMesh(compact);
    const lod = quantizeLod(compact, r2, u2, vertexCount, level); // attributes picked from C
    lod.error = error;
    lod.worldError = +(error * scaleExtent).toFixed(9);
    lods.push(lod);
    prevIndices = raw; // stay in C-space for the next simplification
  }

  // meshopt-encode every bufferView payload (planar vertices, indices);
  // chunked items return raw buffers — the main thread stitches first
  for (const lod of lods) if (!item.chunked) encodeLodViews(lod);
  if (item.chunked) {
    return { hash: item.hash, chunked: true, lods, dequant: { center, half } };
  }
  const count = item.instances.length;
  const translation = new Float32Array(count * 3);
  const rotation = new Float32Array(count * 4);
  const scale = new Float32Array(count * 3);
  item.instances.forEach((p, i) => {
    translation.set(p.t ?? [0, 0, 0], i * 3);
    rotation.set(p.q ?? [0, 0, 0, 1], i * 4);
    scale.set(p.s ?? [1, 1, 1], i * 3);
  });
  return { hash: item.hash, lods, instances: { count, translation, rotation, scale }, dequant: { center, half } };
}


// ------------------------------------------------------------- worker pool

const WORKER_SOURCE = `
const { parentPort } = require('node:worker_threads');
const bytesOf = (ta) => new Uint8Array(ta.buffer, ta.byteOffset, ta.byteLength);
(async () => {
  const { MeshoptEncoder, MeshoptSimplifier } = await import(${JSON.stringify(MESHOPT_SPEC)});
  await Promise.all([MeshoptEncoder.ready, MeshoptSimplifier.ready]);
  ${encodeLodViews.toString()}
  ${processTemplate.toString()}
  parentPort.on('message', (item) => parentPort.postMessage(processTemplate(item)));
  parentPort.postMessage('__ready');
})().catch((e) => { console.error(e); process.exit(1); });
`;

async function runInWorkers(work, jobs) {
  return new Promise((resolveAll, rejectAll) => {
    const results = new Array(work.length);
    let next = 0; let doneCount = 0; let settled = false;
    const workers = [];
    const pump = (w) => {
      if (settled) return;
      if (next >= work.length) return;
      const i = next++;
      w.once('message', (res) => {
        if (settled) return;
        results[i] = res;
        if (++doneCount === work.length) {
          settled = true;
          for (const x of workers) x.terminate();
          resolveAll(results);
        } else pump(w);
      });
      w.postMessage(work[i]);
    };
    const n = Math.min(jobs, work.length);
    for (let i = 0; i < n; i++) {
      const w = new Worker(WORKER_SOURCE, { eval: true });
      w.on('error', (e) => {
        if (settled) return;
        settled = true;
        for (const x of workers) x.terminate();
        rejectAll(e);
      });
      w.once('message', () => pump(w)); // '__ready' → start pumping
      workers.push(w);
    }
  });
}

// ------------------------------------------------------------------ deltas

function diffRecipes(oldRecipe, newRecipe) {
  if (oldRecipe.world !== newRecipe.world) throw new Error('delta across worlds is refused');
  const oldById = new Map(oldRecipe.entries.map((e) => [e.id, e]));
  const newById = new Map(newRecipe.entries.map((e) => [e.id, e]));
  const ops = [];
  const changed = [];
  for (const [id, ne] of newById) {
    const oe = oldById.get(id);
    if (!oe) {
      ops.push({ op: 'instance.add', id, src: ne.src, params: ne.params, material: ne.material ?? null });
      changed.push(id);
    } else if (canonicalJson(oe.params) !== canonicalJson(ne.params)
      || canonicalJson(oe.material ?? null) !== canonicalJson(ne.material ?? null)) {
      ops.push({ op: 'instance.update', id, src: ne.src, params: ne.params, material: ne.material ?? null });
      changed.push(id);
    }
  }
  for (const [id] of oldById) {
    if (!newById.has(id)) { ops.push({ op: 'instance.remove', id }); changed.push(id); }
  }
  ops.sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  return { ops, changed };
}

// -------------------------------------------------------------------- CLI

function parseFloatList(text, n, label) {
  const v = text.split(',').map((s) => parseFloat(s.trim()));
  if (v.length !== n || v.some((x) => !Number.isFinite(x))) throw new Error(`${label}: expected ${n} comma-separated numbers`);
  return v;
}

function usage() {
  console.log(`worldc — the world-stream recipe→glTF compiler

  node worldc.mjs recipe  --source scene.glb [--world NAME] [--branch B] [--version N] [--meta k=v ...]
                          [--chunk-above N] [--chunk-target M] --out recipe.json
  node worldc.mjs edit    --in recipe.json --id ENTRY (--move dx,dy,dz | --rotate-y DEG | --scale F | --tint r,g,b)
                          [--author NAME] [--note TEXT] --out recipe2.json
  node worldc.mjs build   --recipe recipe.json [--source scene.glb] --out DIR [--jobs N]
  node worldc.mjs delta   --old r1.json --new r2.json --out DIR
  node worldc.mjs verify  --recipe r.json [--source scene.glb] [--jobs N]   # build twice, byte-compare
`);
}

async function main() {
  const argv = process.argv.slice(2);
  const cmd = argv[0];
  const arg = (name, fallback = undefined) => {
    const i = argv.indexOf(`--${name}`);
    return i === -1 ? fallback : argv[i + 1];
  };
  if (!cmd || argv.includes('--help')) { usage(); process.exit(cmd ? 0 : 1); }

  await MeshoptEncoder.ready;
  await MeshoptSimplifier.ready;

  if (cmd === 'recipe') {
    const sourcePath = resolve(arg('source'));
    const { json: gltf, bin, sha256: sha } = readGlb(sourcePath);
    const items = harvestSource(gltf, bin).items.sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    const entries = items.map((it) => {
      const { t, q, s } = mat4Decompose(it.world);
      return { id: it.id, src: it.id, params: { t, q, s }, material: null };
    });
    const meta = {};
    for (let i = argv.indexOf('--meta'); i !== -1; i = argv.indexOf('--meta', i + 1)) {
      if (i + 1 >= argv.length) break;
      const [k, ...rest] = argv[i + 1].split('=');
      meta[k] = rest.join('=');
    }
    const lodPolicyIn = { ...DEFAULT_LODS };
    const above = arg('chunk-above');
    const target = arg('chunk-target');
    if (above !== undefined || target !== undefined) {
      lodPolicyIn.chunk = {
        above_triangles: parseInt(above ?? '150000', 10),
        target_triangles: parseInt(target ?? '200000', 10),
      };
    }
    const recipe = {
      schema: SCHEMA_RECIPE,
      world: arg('world', 'world'),
      branch: arg('branch', 'main'),
      version: parseInt(arg('version', '1'), 10),
      source: { kind: 'infinigen2-gltf-export', file: basename(sourcePath), sha256: sha, ...meta },
      lod_policy: lodPolicyIn,
      entries,
      edits: [],
    };
    writeFileSync(arg('out'), canonicalJson(recipe) + '\n');
    console.log(JSON.stringify({ out: arg('out'), entries: entries.length, source_sha256: sha }, null, 2));
    return;
  }

  if (cmd === 'edit') {
    const inPath = arg('in');
    const recipe = JSON.parse(readFileSync(inPath, 'utf8'));
    const id = arg('id');
    const entry = recipe.entries.find((e) => e.id === id);
    if (!entry) throw new Error(`no entry "${id}" in ${inPath}`);
    const notes = [];
    if (arg('move')) {
      const d = parseFloatList(arg('move'), 3, '--move');
      entry.params.t = entry.params.t.map((v, i) => +(v + d[i]).toFixed(9));
      notes.push(`moved by ${d.map((v) => v.toFixed(2)).join(',')} m`);
    }
    if (arg('rotate-y')) {
      const deg = parseFloat(arg('rotate-y'));
      const rad = (deg * Math.PI) / 180;
      const [x, y, z, w] = entry.params.q;
      const Y = Math.sin(rad / 2); const W = Math.cos(rad / 2);
      // Hamilton product q * (0, sin, 0, cos)
      entry.params.q = [
        +(x * W + w * 0 + (y * 0 - z * Y)).toFixed(9),
        +(w * Y + y * W + (z * 0 - x * 0)).toFixed(9),
        +(w * 0 + z * W + (x * Y - y * 0)).toFixed(9),
        +(w * W - (x * 0 + y * Y + z * 0)).toFixed(9),
      ];
      notes.push(`rotated ${deg}° about Y`);
    }
    if (arg('scale')) {
      const f = parseFloat(arg('scale'));
      entry.params.s = entry.params.s.map((v) => +(v * f).toFixed(9));
      notes.push(`scaled ×${f}`);
    }
    if (arg('tint')) {
      const c = parseFloatList(arg('tint'), 3, '--tint');
      entry.material = { baseColorFactor: [...c, 1], metallicFactor: 0, roughnessFactor: 0.8, doubleSided: false };
      notes.push(`tinted rgb(${c.join(',')})`);
    }
    recipe.version += 1;
    recipe.edits.push({
      from_version: recipe.version - 1, to_version: recipe.version,
      entry: id, author: arg('author', 'unknown'), note: arg('note', notes.join('; ') || 'edit'),
    });
    writeFileSync(arg('out'), canonicalJson(recipe) + '\n');
    console.log(JSON.stringify({ out: arg('out'), version: recipe.version, entry: id, edit: recipe.edits.at(-1) }, null, 2));
    return;
  }

  if (cmd === 'build') {
    const recipePath = resolve(arg('recipe'));
    const recipe = JSON.parse(readFileSync(recipePath, 'utf8'));
    const sourcePath = resolve(arg('source') ?? resolve(dirname(recipePath), recipe.source.file));
    const outDir = resolve(arg('out'));
    mkdirSync(outDir, { recursive: true });
    const jobs = parseInt(arg('jobs', '1'), 10);
    const timings = {};
    const { glb, manifest } = await compile(recipe, sourcePath, { jobs, phaseTimings: timings });
    writeFileSync(resolve(outDir, manifest.file), glb);
    manifest.timings_ms = Object.fromEntries(Object.entries(timings).map(([k, v]) => [k, Math.round(v)]));
    manifest.timings_ms.jobs = jobs;
    writeFileSync(resolve(outDir, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
    console.log(JSON.stringify({
      out: resolve(outDir, manifest.file), bytes: glb.length, sha256: manifest.glb_sha256,
      version: recipe.version, templates: manifest.templates.length,
      instances: Object.keys(manifest.entries).length,
      triangles_lod0: manifest.triangles_lod0,
      ms: Math.round(timings.total),
    }, null, 2));
    return;
  }

  if (cmd === 'delta') {
    const oldR = JSON.parse(readFileSync(resolve(arg('old')), 'utf8'));
    const newR = JSON.parse(readFileSync(resolve(arg('new')), 'utf8'));
    if (newR.version <= oldR.version) throw new Error('new recipe version must be greater than old');
    const { ops, changed } = diffRecipes(oldR, newR);
    const lastEdit = newR.edits?.at(-1) ?? null;
    const delta = {
      schema: SCHEMA_DELTA,
      world: newR.world, branch: newR.branch,
      from_version: oldR.version, to_version: newR.version,
      ops,
      templates_added: [], // a geometry-carrying delta appends a fragment GLB here (design.md)
      provenance: {
        author: lastEdit?.author ?? 'unknown',
        note: lastEdit?.note ?? '',
        entry: lastEdit?.entry ?? null,
        recipe_sha256_old: sha256(Buffer.from(canonicalJson(oldR))),
        recipe_sha256_new: sha256(Buffer.from(canonicalJson(newR))),
      },
      counters: { ops: ops.length, entries_changed: changed.length },
    };
    const outDir = resolve(arg('out'));
    mkdirSync(outDir, { recursive: true });
    const file = resolve(outDir, `world@${newR.version}.since${oldR.version}.json`);
    const text = canonicalJson(delta) + '\n';
    writeFileSync(file, text);
    console.log(JSON.stringify({ out: file, bytes: Buffer.byteLength(text), ops: ops.length, changed }, null, 2));
    return;
  }

  if (cmd === 'verify') {
    const recipePath = resolve(arg('recipe'));
    const recipe = JSON.parse(readFileSync(recipePath, 'utf8'));
    const sourcePath = resolve(arg('source') ?? resolve(dirname(recipePath), recipe.source.file));
    const jobs = parseInt(arg('jobs', '1'), 10);
    const a = await compile(recipe, sourcePath, { jobs, phaseTimings: {} });
    const b = await compile(recipe, sourcePath, { jobs, phaseTimings: {} });
    const same = a.glb.equals(b.glb);
    console.log(JSON.stringify({
      deterministic: same, sha256: sha256(a.glb), bytes: a.glb.length,
      a_ms: Math.round(a.phaseTimings.total), b_ms: Math.round(b.phaseTimings.total),
    }, null, 2));
    if (!same) process.exit(1);
    return;
  }

  usage();
  process.exit(1);
}

main().catch((err) => { console.error(err.stack || err); process.exit(1); });
