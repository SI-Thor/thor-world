// make_fixture.mjs — a tiny synthetic source scene for compiler smoke tests.
//
// Builds a minimal GLB by hand (no Blender): one box mesh placed three times
// (different transforms — exercises dedup + instancing) and one bumpy "rock"
// mesh placed once, with two materials. Written next to this script.
//
// The box is deliberately non-trivial (subdivided, per-vertex jitter from a
// fixed arithmetic sequence — no Math.random, so the fixture itself is
// deterministic) so simplification has real work to do.

import { writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));

function makeGrid(n, jitter) {
  // subdivided unit cube → triangles; deterministic jitter per vertex
  const positions = [];
  const indices = [];
  const face = (axis, fixed, u, v) => {
    const base = positions.length / 3;
    for (let i = 0; i <= n; i++) {
      for (let j = 0; j <= n; j++) {
        const a = u === 0 ? i / n : i / n;
        const b = v === 0 ? j / n : j / n;
        const p = [0, 0, 0];
        p[axis] = fixed;
        p[(axis + 1) % 3] = a - 0.5;
        p[(axis + 2) % 3] = b - 0.5;
        // deterministic jitter (fixed sequence, no RNG)
        const k = (i * 7 + j * 13 + fixed * 29 + axis * 3) % 11;
        if (jitter) p[axis] += (k / 11 - 0.5) * 0.04;
        positions.push(...p);
      }
    }
    for (let i = 0; i < n; i++) {
      for (let j = 0; j < n; j++) {
        const a = base + i * (n + 1) + j;
        const b = a + 1;
        const c = a + (n + 1);
        const d = c + 1;
        indices.push(a, c, b, b, c, d);
      }
    }
  };
  face(0, 0.5, 1, 2); face(0, -0.5, 1, 2); face(1, 0.5, 2, 0); face(1, -0.5, 2, 0); face(2, 0.5, 0, 1); face(2, -0.5, 0, 1);
  // per-vertex normals: just radial-ish normalization of position (cheap, fine for tests)
  const normals = positions.map((v) => v);
  for (let i = 0; i < normals.length; i += 3) {
    const l = Math.hypot(normals[i], normals[i + 1], normals[i + 2]) || 1;
    normals[i] /= l; normals[i + 1] /= l; normals[i + 2] /= l;
  }
  return { positions: new Float32Array(positions), normals: new Float32Array(normals), indices: new Uint32Array(indices) };
}

const N = 12;
const box = makeGrid(N, true);
const rock = makeGrid(20, true); // same generator, denser — distinct geometry

// ---- assemble a little glTF document (float32, uncompressed — like a Blender export)
const buffers = [];
let offset = 0;
const bufferViews = [];
const accessors = [];
const pushView = (ta, target) => {
  const bytes = Buffer.from(ta.buffer, ta.byteOffset, ta.byteLength);
  buffers.push(bytes);
  bufferViews.push({ buffer: 0, byteOffset: offset, byteLength: bytes.length, target });
  offset += bytes.length;
  return bufferViews.length - 1;
};
const pushAcc = (view, componentType, type, count, minMax = null) => {
  const a = { bufferView: view, componentType, type, count };
  if (minMax) { a.min = minMax.min; a.max = minMax.max; }
  accessors.push(a);
  return accessors.length - 1;
};

const minMaxOf = (f32, n) => {
  const min = [Infinity, Infinity, Infinity]; const max = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < f32.length; i += 3) for (let c = 0; c < 3; c++) {
    if (f32[i + c] < min[c]) min[c] = f32[i + c];
    if (f32[i + c] > max[c]) max[c] = f32[i + c];
  }
  return { min: min.map((v) => +v.toFixed(5)), max: max.map((v) => +v.toFixed(5)) };
};

const boxPos = pushAcc(pushView(box.positions, 34962), 5126, 'VEC3', box.positions.length / 3, minMaxOf(box.positions));
const boxNrm = pushAcc(pushView(box.normals, 34962), 5126, 'VEC3', box.normals.length / 3);
const boxIdx = pushAcc(pushView(box.indices, 34963), 5125, 'SCALAR', box.indices.length);
const rockPos = pushAcc(pushView(rock.positions, 34962), 5126, 'VEC3', rock.positions.length / 3, minMaxOf(rock.positions));
const rockNrm = pushAcc(pushView(rock.normals, 34962), 5126, 'VEC3', rock.normals.length / 3);
const rockIdx = pushAcc(pushView(rock.indices, 34963), 5125, 'SCALAR', rock.indices.length);

const json = {
  asset: { version: '2.0', generator: 'world-stream fixture' },
  scene: 0,
  scenes: [{ nodes: [0, 1, 2, 3] }],
  meshes: [
    { name: 'Crate', primitives: [{ attributes: { POSITION: boxPos, NORMAL: boxNrm }, indices: boxIdx, material: 0 }] },
    { name: 'Rock', primitives: [{ attributes: { POSITION: rockPos, NORMAL: rockNrm }, indices: rockIdx, material: 1 }] },
  ],
  materials: [
    { name: 'wood', pbrMetallicRoughness: { baseColorFactor: [0.55, 0.35, 0.15, 1], metallicFactor: 0, roughnessFactor: 0.8 } },
    { name: 'stone', pbrMetallicRoughness: { baseColorFactor: [0.5, 0.5, 0.52, 1], metallicFactor: 0, roughnessFactor: 0.95 } },
  ],
  nodes: [
    { name: 'Crate_0001', mesh: 0, translation: [-2, 0, 0] },
    { name: 'Crate_0002', mesh: 0, translation: [0, 0.5, -1], scale: [1.5, 1.5, 1.5], rotation: [0, 0.3826834, 0, 0.9238795] },
    { name: 'Crate_0003', mesh: 0, translation: [2, 0, 0.5], scale: [0.7, 0.7, 0.7] },
    { name: 'Rock_0001', mesh: 1, translation: [0, -0.5, 2], scale: [2, 1.2, 2] },
  ],
  accessors,
  bufferViews,
  buffers: [{ byteLength: offset }],
};

const bin = Buffer.concat(buffers);
const jsonBytes = Buffer.from(JSON.stringify(json));
const jsonPad = (4 - (jsonBytes.length % 4)) % 4;
const binPad = (4 - (bin.length % 4)) % 4;
const jsonLen = jsonBytes.length + jsonPad;
const binLen = bin.length + binPad;
const glb = Buffer.alloc(12 + 8 + jsonLen + 8 + binLen);
glb.writeUInt32LE(0x46546c67, 0); glb.writeUInt32LE(2, 4); glb.writeUInt32LE(glb.length, 8);
glb.writeUInt32LE(jsonLen, 12); glb.writeUInt32LE(0x4e4f534a, 16);
jsonBytes.copy(glb, 20);
glb.writeUInt32LE(binLen, 20 + jsonLen); glb.writeUInt32LE(0x004e4942, 24 + jsonLen);
bin.copy(glb, 28 + jsonLen);

const out = resolve(here, 'fixture_source.glb');
writeFileSync(out, glb);
console.log(JSON.stringify({ out, bytes: glb.length, triangles: { box: box.indices.length / 3, rock: rock.indices.length / 3 } }));
