// viewer.js — the world-stream thin client.
//
// Loads the compiled world file (deterministic glTF: templates +
// EXT_mesh_gpu_instancing + EXT_meshopt_compression + KHR_mesh_quantization
// + LOD chains in node.extras.lods), renders it with three.js — WebGPU when
// Chrome on this box offers it, WebGL2 otherwise — and keeps it live:
//
//   SSE world.dirty {branch, head}  →  GET /api/scene?since_version=<v>
//                                  →  apply the delta op list in place
//
// The glTF is parsed by hand (the wire is ours): meshopt-compressed
// bufferViews decode through three's MeshoptDecoder, quantized attributes
// dequantize into the geometry (the compiler pads VEC3 strides to 4-byte
// multiples, so attributes are interleaved x,y,z,pad), one InstancedMesh per
// template carries every LOD geometry, and the active LOD swaps by
// screen-space error with hysteresis. ?autodemo=<ms> applies the first
// delta after <ms> without waiting for the SSE event (screenshot mode).

import {
  WebGPURenderer, PerspectiveCamera, Scene, HemisphereLight, DirectionalLight,
  MeshStandardMaterial, BufferGeometry, BufferAttribute, InstancedMesh,
  InstancedBufferAttribute, Matrix4, Color, Vector3, Quaternion,
  MathUtils, ACESFilmicToneMapping, DynamicDrawUsage,
  Texture, SRGBColorSpace, RepeatWrapping, ClampToEdgeWrapping,
} from './vendor/three.webgpu.js';
import { MeshoptDecoder } from './vendor/meshopt_decoder.module.js';

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const AUTODEMO_MS = params.has('autodemo') ? parseInt(params.get('autodemo'), 10) : null;
const LOD_PX_COARSE = 3.0; // switch to a coarser LOD above this screen error
const LOD_PX_FINE = 2.0;   // …and back to a finer one below it

const state = {
  branch: null, head: null,
  templates: new Map(),   // hash → record (see addTemplate)
  entries: new Map(),     // entry id → {template, instance}
  lodsShown: [0, 0, 0, 0],
};

// ------------------------------------------------------------------ glTF in

function parseGlb(buffer) {
  const view = new DataView(buffer);
  if (view.getUint32(0, true) !== 0x46546c67) throw new Error('not a GLB');
  const jsonLen = view.getUint32(12, true);
  const json = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 20, jsonLen)));
  let off = 20 + jsonLen;
  let bin = null;
  while (off + 8 <= buffer.byteLength) {
    const len = view.getUint32(off, true);
    if (view.getUint32(off + 4, true) === 0x004e4942) { bin = new Uint8Array(buffer, off + 8, len); break; }
    off += 8 + len;
  }
  return { json, bin };
}

// Decode one accessor's bufferView (meshopt-compressed or a plain slice).
async function readAccessorBytes(json, bin, accessor) {
  const bv = json.bufferViews[accessor.bufferView];
  const ext = bv.extensions?.EXT_meshopt_compression;
  if (ext) {
    const source = bin.subarray(ext.byteOffset, ext.byteOffset + ext.byteLength);
    return MeshoptDecoder.decodeGltfBufferAsync(accessor.count, ext.byteStride, source, ext.mode, ext.filter || 'NONE');
  }
  return bin.subarray(bv.byteOffset || 0, (bv.byteOffset || 0) + bv.byteLength);
}

const TYPED = { 5120: Int8Array, 5121: Uint8Array, 5122: Int16Array, 5123: Uint16Array, 5125: Uint32Array, 5126: Float32Array };

// glTF image → three Texture (embedded in the BIN chunk as a plain
// bufferView; PNG/JPEG bytes as the compiler stored them)
async function loadTexture(json, bin, texIndex) {
  const tex = json.textures[texIndex];
  const img = json.images[tex.source];
  const bv = json.bufferViews[img.bufferView];
  const bytes = bin.subarray(bv.byteOffset || 0, (bv.byteOffset || 0) + bv.byteLength);
  const bmp = await createImageBitmap(new Blob([bytes], { type: img.mimeType || 'image/png' }));
  const t = new Texture(bmp);
  t.flipY = false; // glTF UV convention
  t.colorSpace = SRGBColorSpace;
  const s = json.samplers?.[tex.sampler ?? 0];
  t.wrapS = s?.wrapS === 10497 ? RepeatWrapping : ClampToEdgeWrapping;
  t.wrapT = s?.wrapT === 10497 ? RepeatWrapping : ClampToEdgeWrapping;
  t.needsUpdate = true;
  return t;
}

// ---------------------------------------------------------------- scene load

async function loadWorld(manifest) {
  const glb = parseGlb(await (await fetch(`/build/${manifest.file}`)).arrayBuffer());
  const { json, bin } = glb;
  await MeshoptDecoder.ready;

  const materials = [];
  for (const m of json.materials ?? []) {
    const pbr = m.pbrMetallicRoughness ?? {};
    const def = {
      color: new Color().fromArray(pbr.baseColorFactor ?? [0.8, 0.8, 0.8, 1]),
      roughness: pbr.roughnessFactor ?? 1,
      metalness: pbr.metallicFactor ?? 0,
      side: m.doubleSided ? 2 : 0,
    };
    if (pbr.baseColorTexture) def.map = await loadTexture(json, bin, pbr.baseColorTexture.index);
    materials.push(new MeshStandardMaterial(def));
  }

  for (const tpl of manifest.templates) {
    const node = json.nodes[tpl.node];
    const half = node.scale;
    const center = node.translation;
    const instAcc = node.extensions.EXT_mesh_gpu_instancing.attributes;
    const count = json.accessors[instAcc.TRANSLATION].count;

    // decoded views arrive as bytes — re-view them as the accessor's type
    const asTyped = async (acc, Type, n) => {
      const buf = await readAccessorBytes(json, bin, acc);
      return new Type(buf.buffer, buf.byteOffset, acc.count * n);
    };

    const t = await asTyped(json.accessors[instAcc.TRANSLATION], Float32Array, 3);
    let q = instAcc.ROTATION !== undefined
      ? await asTyped(json.accessors[instAcc.ROTATION], Float32Array, 4)
      : new Float32Array(count * 4);
    if (instAcc.ROTATION === undefined) for (let i = 3; i < q.length; i += 4) q[i] = 1;
    let s = instAcc.SCALE !== undefined
      ? await asTyped(json.accessors[instAcc.SCALE], Float32Array, 3)
      : new Float32Array(count * 3);
    if (instAcc.SCALE === undefined) s.fill(1);

    // one geometry per LOD — real simplified meshes, dequantized to metres
    const lodGeoms = [];
    for (const lod of node.extras.lods) {
      const prim = json.meshes[lod.mesh].primitives[0];
      const posAcc = json.accessors[prim.attributes.POSITION];
      const nrmAcc = prim.attributes.NORMAL !== undefined ? json.accessors[prim.attributes.NORMAL] : null;
      const idxAcc = json.accessors[prim.indices];

      // positions: interleaved i16 x,y,z,pad → dequantize (norm × half + center)
      const posBuf = await readAccessorBytes(json, bin, posAcc);
      const posQ = new Int16Array(posBuf.buffer, posBuf.byteOffset, posAcc.count * 4);
      const positions = new Float32Array(posAcc.count * 3);
      for (let v = 0; v < posAcc.count; v++)
        for (let c = 0; c < 3; c++)
          positions[v * 3 + c] = (posQ[v * 4 + c] / 32767) * half[c] + center[c];

      const geometry = new BufferGeometry();
      geometry.setAttribute('position', new BufferAttribute(positions, 3));
      if (nrmAcc) {
        const nrmBuf = await readAccessorBytes(json, bin, nrmAcc);
        const nrmQ = new Int8Array(nrmBuf.buffer, nrmBuf.byteOffset, nrmAcc.count * 4);
        const normals = new Float32Array(nrmAcc.count * 3);
        for (let v = 0; v < nrmAcc.count; v++)
          for (let c = 0; c < 3; c++) normals[v * 3 + c] = nrmQ[v * 4 + c] / 127;
        geometry.setAttribute('normal', new BufferAttribute(normals, 3));
      } else geometry.computeVertexNormals();
      if (prim.attributes.TEXCOORD_0 !== undefined) {
        const uvAcc = json.accessors[prim.attributes.TEXCOORD_0];
        const uvBuf = await readAccessorBytes(json, bin, uvAcc);
        let uvs;
        if (uvAcc.normalized && uvAcc.componentType === 5123) {
          const q = new Uint16Array(uvBuf.buffer, uvBuf.byteOffset, uvAcc.count * 2);
          uvs = new Float32Array(uvAcc.count * 2);
          for (let i = 0; i < q.length; i++) uvs[i] = q[i] / 65535;
        } else {
          uvs = new Float32Array(uvBuf.buffer, uvBuf.byteOffset, uvAcc.count * 2);
        }
        geometry.setAttribute('uv', new BufferAttribute(uvs, 2));
      }

      const idxBuf = await readAccessorBytes(json, bin, idxAcc);
      const IdxT = TYPED[idxAcc.componentType];
      const indices = new IdxT(idxBuf.buffer, idxBuf.byteOffset, idxAcc.count);
      geometry.setIndex(new BufferAttribute(indices, 1));
      geometry.computeBoundingSphere();
      lodGeoms.push({ geometry, material: materials[prim.material], lod });
    }

    addTemplate(tpl.hash, lodGeoms, { t, q, s }, count, tpl.entryIds, half);
  }
  const ex = json.asset.extras;
  $('world').textContent = `${ex.world} · ${ex.branch}`;
  document.title = `world-stream — ${ex.world}@${ex.version}`;
}

// ------------------------------------------------------- three.js plumbing

let renderer, scene, camera;
const orbit = { theta: -0.7, phi: 1.05, dist: 14, target: new Vector3(0, 1, 0) };

function addTemplate(hash, lodGeoms, inst, count, entryIds, half) {
  const matrices = new Float32Array(count * 16);
  const { t, q, s } = inst;
  const m = new Matrix4(); const tq = new Quaternion(); const v = new Vector3(); const sv = new Vector3();
  for (let i = 0; i < count; i++) {
    tq.set(q[i * 4], q[i * 4 + 1], q[i * 4 + 2], q[i * 4 + 3]);
    m.compose(v.set(t[i * 3], t[i * 3 + 1], t[i * 3 + 2]), tq, sv.set(s[i * 3], s[i * 3 + 1], s[i * 3 + 2]));
    m.toArray(matrices, i * 16);
  }

  const mesh = new InstancedMesh(lodGeoms[0].geometry, lodGeoms[0].material, count);
  mesh.instanceMatrix = new InstancedBufferAttribute(matrices, 16);
  mesh.instanceMatrix.setUsage(DynamicDrawUsage);
  mesh.frustumCulled = false;
  scene.add(mesh);

  // world-space centroid + conservative radius (drives LOD choice per template)
  const centroid = new Vector3();
  for (let i = 0; i < count; i++) centroid.add(v.set(t[i * 3], t[i * 3 + 1], t[i * 3 + 2]));
  if (count) centroid.multiplyScalar(1 / count);
  let maxScale = 1e-6;
  for (let i = 0; i < s.length; i++) maxScale = Math.max(maxScale, Math.abs(s[i]));
  const radius = Math.hypot(half[0], half[1], half[2]) * maxScale * 2 + 0.4;

  const rec = { hash, lodGeoms, active: 0, matrices, t, q, s, count, entryIds, mesh, centroid, radius };
  state.templates.set(hash, rec);
  entryIds.forEach((id, i) => state.entries.set(id, { template: hash, instance: i }));
}

function pickLods() {
  const h = Math.max(window.innerHeight, 1);
  const pxPerUnitAtOneMetre = h / (2 * Math.tan(MathUtils.degToRad(camera.fov) / 2));
  for (const rec of state.templates.values()) {
    const pxPerUnit = pxPerUnitAtOneMetre / Math.max(camera.position.distanceTo(rec.centroid), 1e-3);
    let best = 0;
    for (let i = rec.lodGeoms.length - 1; i > 0; i--) {
      if ((rec.lodGeoms[i].lod.worldError ?? 0) * pxPerUnit <= LOD_PX_COARSE) { best = i; break; }
    }
    // hysteresis: once coarse, only come back inside the finer threshold
    if (best > 0 && rec.active === 0
      && (rec.lodGeoms[best].lod.worldError ?? 0) * pxPerUnit > LOD_PX_COARSE) best = 0;
    if (best !== rec.active) {
      rec.active = best;
      const lm = rec.lodGeoms[best];
      rec.mesh.geometry = lm.geometry;
      rec.mesh.material = lm.material;
    }
  }
}

// ------------------------------------------------------------- delta apply

async function applyDelta(delta) {
  let applied = 0;
  const m = new Matrix4(); const tq = new Quaternion(); const v = new Vector3(); const sv = new Vector3();
  for (const op of delta.ops) {
    const where = state.entries.get(op.id);
    if (!where) continue;
    const rec = state.templates.get(where.template);
    if (op.op === 'instance.update') {
      const i = where.instance; const p = op.params;
      rec.t.set(p.t, i * 3); rec.q.set(p.q, i * 4); rec.s.set(p.s, i * 3);
      tq.set(p.q[0], p.q[1], p.q[2], p.q[3]);
      m.compose(v.set(p.t[0], p.t[1], p.t[2]), tq, sv.set(p.s[0], p.s[1], p.s[2]));
      m.toArray(rec.matrices, i * 16);
      rec.mesh.instanceMatrix.needsUpdate = true;
      applied++;
    } else if (op.op === 'instance.remove') {
      rec.mesh.count = Math.max(0, rec.mesh.count - 1); // demo-level; full remove compacts the instance buffer
      applied++;
    }
    // instance.add to a new template fetches the delta's fragment GLB (design.md §deltas)
  }
  logCard(delta.provenance?.author ?? 'unknown', delta.provenance?.note ?? `${applied} ops`,
    `v${delta.from_version} → v${delta.to_version} · ${delta.counters?.ops ?? applied} op(s), ${applied} applied live`);
  $('version').innerHTML = `v<b>${delta.to_version}</b>`;
  $('version').classList.add('dirty');
  state.head = delta.to_version;
}

function logCard(who, what, meta) {
  const el = document.createElement('div');
  el.className = 'entry';
  el.innerHTML = `<div><span class="who">${who}</span> <span class="what">— ${what}</span></div>`
    + (meta ? `<div class="meta">${meta}</div>` : '');
  $('log').prepend(el);
  while ($('log').children.length > 5) $('log').lastChild.remove();
}

// ------------------------------------------------------------------- boot

async function boot() {
  camera = new PerspectiveCamera(55, innerWidth / innerHeight, 0.05, 500);
  scene = new Scene();
  scene.background = new Color(0x0b0d12);
  scene.add(new HemisphereLight(0xbfd4ff, 0x443a2e, 1.1));
  const sun = new DirectionalLight(0xfff1da, 2.4);
  sun.position.set(6, 10, 4);
  scene.add(sun);

  renderer = new WebGPURenderer({ canvas: $('gl'), antialias: true });
  await renderer.init();
  renderer.toneMapping = ACESFilmicToneMapping;
  const sizeRenderer = () => {
    renderer.setSize(innerWidth, innerHeight);
    camera.aspect = innerWidth / innerHeight;
    camera.updateProjectionMatrix();
  };
  sizeRenderer();
  addEventListener('resize', sizeRenderer);
  const backend = renderer.backend?.isWebGPUBackend ? 'WebGPU' : 'WebGL2';
  const chip = $('backend');
  chip.textContent = `backend: ${backend}`;
  chip.classList.add(backend === 'WebGPU' ? 'backend-webgpu' : 'backend-webgl2');

  const info = await (await fetch('/api/scene')).json();
  const manifest = await (await fetch(`/build/${info.manifest_file}`)).json();
  state.branch = manifest.branch; state.head = manifest.version;
  await loadWorld(manifest);
  $('version').innerHTML = `v<b>${manifest.version}</b>`;
  $('stats').innerHTML = `<b>${state.templates.size}</b> templates · <b>${state.entries.size}</b> instances`;

  // frame the scene on its centroid
  const all = new Vector3();
  for (const rec of state.templates.values()) all.add(rec.centroid);
  if (state.templates.size) all.divideScalar(state.templates.size);
  orbit.target.copy(all);
  orbit.dist = Math.min(60, Math.max(4, ...[...state.templates.values()].map((r) => r.radius * 2.5)));

  subscribe();
  // dev introspection handle (HUD screenshots, console poking)
  window.worldStream = { renderer, scene, camera, state, orbit };
  if (AUTODEMO_MS !== null && Number.isFinite(AUTODEMO_MS)) {
    setTimeout(async () => {
      const payload = await (await fetch(`/api/scene?since_version=${state.head}`)).json();
      if (payload.ops) await applyDelta(payload);
    }, AUTODEMO_MS);
  }
  animate();
}

function subscribe() {
  const es = new EventSource('/api/events');
  es.addEventListener('world.dirty', async (ev) => {
    const { head } = JSON.parse(ev.data);
    if (head === state.head) return;
    const payload = await (await fetch(`/api/scene?since_version=${state.head}`)).json();
    if (payload.ops) await applyDelta(payload);
    else location.reload(); // delta chain broken → full re-stream
  });
}

// -------------------------------------------------------------- interaction

(function hookControls() {
  let drag = null;
  const canvas = $('gl');
  canvas.addEventListener('pointerdown', (e) => { drag = { x: e.clientX, y: e.clientY }; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener('pointermove', (e) => {
    if (!drag) return;
    orbit.theta -= (e.clientX - drag.x) * 0.005;
    orbit.phi = MathUtils.clamp(orbit.phi - (e.clientY - drag.y) * 0.005, 0.05, Math.PI - 0.05);
    drag = { x: e.clientX, y: e.clientY };
  });
  canvas.addEventListener('pointerup', () => { drag = null; });
  canvas.addEventListener('wheel', (e) => {
    orbit.dist = MathUtils.clamp(orbit.dist * (1 + Math.sign(e.deltaY) * 0.1), 0.5, 250);
  }, { passive: true });
})();

let frames = 0; let fpsT = performance.now(); let lodT = 0;
function animate() {
  requestAnimationFrame(animate);
  const now = performance.now();
  camera.position.set(
    orbit.target.x + orbit.dist * Math.sin(orbit.phi) * Math.sin(orbit.theta),
    orbit.target.y + orbit.dist * Math.cos(orbit.phi),
    orbit.target.z + orbit.dist * Math.sin(orbit.phi) * Math.cos(orbit.theta),
  );
  camera.lookAt(orbit.target);
  if (now - lodT > 150) { pickLods(); lodT = now; }
  renderer.render(scene, camera);
  frames++;
  if (now - fpsT > 1000) {
    state.lodsShown = [0, 0, 0, 0];
    for (const rec of state.templates.values()) state.lodsShown[rec.active] = (state.lodsShown[rec.active] || 0) + 1;
    $('fps').textContent = `${frames} fps`;
    $('lods').innerHTML = `lod mix <b>${state.lodsShown.join('/')}</b>`;
    frames = 0; fpsT = now;
  }
}

boot().catch((err) => {
  const el = $('err');
  el.style.display = 'block';
  el.textContent = `viewer failed: ${err?.message || err}`;
  console.error(err);
});
