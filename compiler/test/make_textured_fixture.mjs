// make_textured_fixture.mjs — a tiny TEXTURED source scene for the texture
// path of the compiler (round 2). One subdivided box with deterministic
// per-face UVs, plus a small procedurally-generated PNG (checker gradient —
// hand-rolled encoder, no dependencies), bound through
// pbrMetallicRoughness.baseColorTexture. Deterministic: same bytes every run.

import { deflateSync } from 'node:zlib';
import { writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));

// ---- a 64×64 RGB PNG: 8×8 checker, two hues, gradient within each cell
function makeCheckerPng() {
  const W = 64, H = 64;
  const raw = Buffer.alloc(H * (1 + W * 3));
  for (let y = 0; y < H; y++) {
    const row = y * (1 + W * 3);
    raw[row] = 0; // filter: none
    for (let x = 0; x < W; x++) {
      const cell = ((x >> 3) ^ (y >> 3)) & 1;
      const g = ((x % 8) + (y % 8)) * 14;
      raw[row + 1 + x * 3] = cell ? 220 - g / 3 : 40 + g / 4;
      raw[row + 2 + x * 3] = cell ? 90 + g / 4 : 130 - g / 5;
      raw[row + 3 + x * 3] = cell ? 40 + g / 5 : 200 - g / 3;
    }
  }
  const crcTable = [];
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    crcTable[n] = c >>> 0;
  }
  const crc32 = (buf) => {
    let c = 0xffffffff;
    for (const b of buf) c = crcTable[(c ^ b) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  };
  const chunk = (type, data) => {
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
    const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(body));
    return Buffer.concat([len, body, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(W, 0); ihdr.writeUInt32BE(H, 4);
  ihdr[8] = 8; ihdr[9] = 2; // 8-bit, truecolor
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(raw, { level: 6 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

// ---- subdivided unit cube with per-vertex UVs (deterministic jitter)
function makeGrid(n) {
  const positions = []; const normals = []; const uvs = []; const indices = [];
  const face = (axis, fixed) => {
    for (let i = 0; i <= n; i++) {
      for (let j = 0; j <= n; j++) {
        const p = [0, 0, 0];
        p[axis] = fixed;
        p[(axis + 1) % 3] = i / n - 0.5;
        p[(axis + 2) % 3] = j / n - 0.5;
        const k = (i * 7 + j * 13 + fixed * 29 + axis * 3) % 11;
        p[axis] += (k / 11 - 0.5) * 0.04;
        positions.push(...p);
        const l = Math.hypot(...p) || 1;
        normals.push(p[0] / l, p[1] / l, p[2] / l);
        uvs.push(i / n, j / n);
      }
    }
    const base = (positions.length / 3) - (n + 1) * (n + 1);
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
  face(0, 0.5); face(0, -0.5); face(1, 0.5); face(1, -0.5); face(2, 0.5); face(2, -0.5);
  return { positions: new Float32Array(positions), normals: new Float32Array(normals), uvs: new Float32Array(uvs), indices: new Uint32Array(indices) };
}

const box = makeGrid(10);
const png = makeCheckerPng();

const buffers = []; let offset = 0;
const bufferViews = []; const accessors = [];
const pushView = (ta) => {
  const bytes = Buffer.from(ta.buffer, ta.byteOffset, ta.byteLength);
  buffers.push(bytes);
  bufferViews.push({ buffer: 0, byteOffset: offset, byteLength: bytes.length });
  offset += bytes.length;
  return bufferViews.length - 1;
};
const pushAcc = (view, componentType, type, count) => {
  accessors.push({ bufferView: view, componentType, type, count });
  return accessors.length - 1;
};

const posA = pushAcc(pushView(box.positions), 5126, 'VEC3', box.positions.length / 3);
const nrmA = pushAcc(pushView(box.normals), 5126, 'VEC3', box.normals.length / 3);
const uvA = pushAcc(pushView(box.uvs), 5126, 'VEC2', box.uvs.length / 2);
const idxA = pushAcc(pushView(box.indices), 5125, 'SCALAR', box.indices.length);
const imgView = pushView(png);

const json = {
  asset: { version: '2.0', generator: 'world-stream textured fixture' },
  scene: 0,
  scenes: [{ nodes: [0, 1] }],
  meshes: [
    { name: 'Crate', primitives: [{ attributes: { POSITION: posA, NORMAL: nrmA, TEXCOORD_0: uvA }, indices: idxA, material: 0 }] },
  ],
  materials: [
    { name: 'checker', pbrMetallicRoughness: { baseColorFactor: [1, 1, 1, 1], baseColorTexture: { index: 0 }, metallicFactor: 0, roughnessFactor: 0.7 } },
  ],
  textures: [{ sampler: 0, source: 0 }],
  samplers: [{ wrapS: 10497, wrapT: 10497 }],
  images: [{ bufferView: imgView, mimeType: 'image/png' }],
  nodes: [
    { name: 'Crate_0001', mesh: 0, translation: [-0.8, 0, 0] },
    { name: 'Crate_0002', mesh: 0, translation: [0.8, 0.2, 0], rotation: [0, 0.3826834, 0, 0.9238795] },
  ],
  accessors,
  bufferViews,
  buffers: [{ byteLength: offset }],
};

const bin = Buffer.concat(buffers);
const jsonBytes = Buffer.from(JSON.stringify(json));
const jsonPad = (4 - (jsonBytes.length % 4)) % 4;
const paddedJson = Buffer.concat([jsonBytes, Buffer.alloc(jsonPad, 0x20)]);
const binPad = (4 - (bin.length % 4)) % 4;
const jsonLen = paddedJson.length;
const binLen = bin.length + binPad;
const glb = Buffer.alloc(12 + 8 + jsonLen + 8 + binLen);
glb.writeUInt32LE(0x46546c67, 0); glb.writeUInt32LE(2, 4); glb.writeUInt32LE(glb.length, 8);
glb.writeUInt32LE(jsonLen, 12); glb.writeUInt32LE(0x4e4f534a, 16);
paddedJson.copy(glb, 20);
glb.writeUInt32LE(binLen, 20 + jsonLen); glb.writeUInt32LE(0x004e4942, 24 + jsonLen);
bin.copy(glb, 28 + jsonLen);

const out = resolve(here, 'textured_source.glb');
writeFileSync(out, glb);
console.log(JSON.stringify({ out, bytes: glb.length, png_bytes: png.length, triangles: box.indices.length / 3 }));
