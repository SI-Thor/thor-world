#!/usr/bin/env python
"""bake_colour.py — give the Infinigen living room its colour back, two ways.

The bpy glTF exporter stalls on Infinigen 2.0's procedural node graphs
(measurements.md §1.1), so the wire's colours cannot ride the stock export.
This tool runs on the Thor in the v2 venv and measures both paths the brief
names:

  flatten  32² Cycles GPU bakes per mesh object → per-slot average base
           colour, quantized to 8-bit sRGB steps → each (object, slot) gets a
           NEW flat Principled material (object-linked; shared meshes stay
           safe) → export `source_flat.glb`. The graph walk that stalls the
           exporter never sees the old materials, so the export is fast AND
           coloured. Quantization is what makes it deterministic: bake pixels
           are ±1 LSB on this box (builder-ide warm-scene.md §3), and the
           per-slot mean sits far from an 8-bit boundary — two runs must give
           byte-identical glb (measured).

  bake     512² Cycles bakes per mesh object — base colour (DIFFUSE/COLOR),
           roughness, normal — saved as PNGs with per-object timings, plus a
           Cycles beauty render as ground truth, plus a `source_textured.glb`
           whose materials are flat Principled + Image Texture (base colour
           only; roughness/metal stay scalar constants read off the graphs).
           The bake pixels are NOT bit-deterministic (measured, ±1 LSB) —
           this path's determinism story is recorded, not claimed.

  uvcheck  heuristic overlap test on each mesh's UV map (rasterize sampled
           triangles into a grid; flag texels claimed by triangles whose 3D
           centroids are far apart) — tells us whether the generator's own
           UVMaps can be trusted for baking, and generates cube-bucket UVs
           for the meshes that have none.

Metallic/roughness constants come from a visited-set walk of each material's
node groups (the exporter's stall was an *unbounded* re-walk; one bounded
traversal is trivial).

Every write goes under --outdir. Run under `flock ~/lm-wt/heavy.lock`; the
script refuses to start if less than 25 GB of memory is free.

    ~/lm-wt/v2-thor/.venv/bin/python bake_colour.py --blend scene.blend \
        --mode flatten|bake|uvcheck --res 512 --outdir OUT [--device GPU|CPU]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np


def log(msg: str) -> None:
    print(f"[bake {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_mem(min_gb: int = 25) -> None:
    free_kb = None
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            free_kb = int(line.split()[1])
            break
    free_gb = (free_kb or 0) / (1 << 20)
    if free_gb < min_gb:
        raise SystemExit(f"only {free_gb:.1f} GB memory available — need ≥ {min_gb} (thor memory rule)")
    log(f"memory ok: {free_gb:.0f} GB available")


def setup_cycles(device: str, samples: int = 1) -> dict:
    import bpy

    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    prefs = bpy.context.preferences.addons["cycles"].preferences
    used = {"compute": "CPU", "devices": []}
    try:
        prefs.compute_device_type = "CUDA"
        devs = prefs.get_devices_for_type("CUDA")
        for d in devs:
            d.use = d.type == "CUDA"
            used["devices"].append({"name": d.name, "type": d.type, "use": bool(d.use)})
        if device == "GPU" and any(d["use"] for d in used["devices"]):
            scene.cycles.device = "GPU"
            used["compute"] = "GPU"
        else:
            scene.cycles.device = "CPU"
    except Exception as e:  # noqa: BLE001
        scene.cycles.device = "CPU"
        used["error"] = str(e)
    scene.cycles.samples = samples
    scene.cycles.use_adaptive_sampling = False
    scene.cycles.seed = 1
    return used


# --------------------------------------------------------------------- UVs

def xatlas_uv(mesh, name: str, cache_dir: Path | None) -> dict:
    """Deterministic UV atlas via xatlas (MIT): unwrapped per-corner UVs into
    a new 'WB_atlas' layer, made active. The generator's own UVMaps overlap
    badly on many props (uvcheck), so every mesh gets a fresh atlas. Results
    are cached by geometry hash (xatlas is byte-deterministic, so a hit is
    exact) — several runs share one unwrap of the 1.77 M-triangle shell."""
    import xatlas

    t0 = time.perf_counter()
    verts = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", verts)
    mesh.calc_loop_triangles()
    lts = mesh.loop_triangles
    if len(lts) == 0:
        return {"object": name, "skipped": "no triangles", "seconds": 0.0}
    tris = np.empty(len(lts) * 3, dtype=np.uint32)
    lts.foreach_get("vertices", tris)
    key = hashlib.sha256(verts.tobytes() + tris.tobytes()).hexdigest()[:16]
    cache = cache_dir / f"{key}.npz" if cache_dir else None
    hit = bool(cache and cache.exists())
    if hit:
        with np.load(cache) as z:
            loop_uvs = z["loop_uvs"]
    else:
        atlas = xatlas.Atlas()
        atlas.add_mesh(verts.reshape(-1, 3), tris.reshape(-1, 3))
        atlas.generate()
        _, indices, uvs = atlas.get_mesh(0)
        # output triangle k == input triangle k; corner j of triangle i takes
        # the uv of output vertex indices[k, j]
        loop_ids = np.empty(len(lts) * 3, dtype=np.int64)
        lts.foreach_get("loops", loop_ids)
        loop_uvs = np.zeros((len(mesh.loops), 2), dtype=np.float32)
        loop_uvs[loop_ids] = uvs[np.asarray(indices, dtype=np.int64).ravel()]
        if cache:
            np.savez(cache, loop_uvs=loop_uvs)
    layer = mesh.uv_layers.get("WB_atlas") or mesh.uv_layers.new(name="WB_atlas")
    layer.data.foreach_set("uv", loop_uvs.ravel())
    mesh.uv_layers.active = layer
    mesh.update()
    return {"object": name, "triangles": int(len(lts)), "cache": "hit" if hit else "miss",
            "seconds": round(time.perf_counter() - t0, 3)}


def uv_overlap_score(mesh, samples: int = 4000, grid: int = 512) -> dict:
    """Heuristic: rasterize sampled triangles in UV space; a texel claimed by
    two triangles whose 3D centroids are far apart means overlapping islands."""
    tris = len(mesh.loop_triangles)
    if tris == 0 or not mesh.uv_layers:
        return {"triangles": tris, "overlap_fraction": None}
    mesh.calc_loop_triangles()
    lt = mesh.loop_triangles
    pick = np.linspace(0, tris - 1, num=min(samples, tris)).astype(np.int64)
    uv_l = mesh.uv_layers.active
    if uv_l is None:
        return {"triangles": tris, "overlap_fraction": None}
    uvs = np.empty(len(mesh.loops) * 2, dtype=np.float64)
    uv_l.data.foreach_get("uv", uvs)
    uvs = uvs.reshape(-1, 2)
    verts = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", verts)
    verts = verts.reshape(-1, 3)
    owner = np.full(grid * grid, -1, dtype=np.int64)
    owner_xyz = np.zeros((grid * grid, 3), dtype=np.float64)
    conflicts = 0
    claimed = 0
    diag = float(np.linalg.norm(verts.max(0) - verts.min(0))) or 1.0
    for t in pick:
        tri = lt[t]
        uv = uvs[[tri.loops[0], tri.loops[1], tri.loops[2]]]
        c3 = verts[[tri.vertices[0], tri.vertices[1], tri.vertices[2]]].mean(0)
        x0 = max(0, int(uv[:, 0].min() * grid)); x1 = min(grid - 1, int(uv[:, 0].max() * grid))
        y0 = max(0, int(uv[:, 1].min() * grid)); y1 = min(grid - 1, int(uv[:, 1].max() * grid))
        if x1 < x0 or y1 < y0:
            continue
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                cell = gy * grid + gx
                o = owner[cell]
                if o == -1:
                    owner[cell] = t
                    owner_xyz[cell] = c3
                    claimed += 1
                elif o != t and np.linalg.norm(c3 - owner_xyz[cell]) > 0.02 * diag:
                    conflicts += 1
    return {"triangles": tris, "sampled": int(len(pick)), "claimed_texels": claimed,
            "conflicts": conflicts, "overlap_fraction": round(conflicts / max(claimed, 1), 4)}


def cube_bucket_uv(mesh) -> float:
    """Deterministic UV for meshes with none: 6 normal-axis buckets stacked in
    v, planar projection inside each. No packing — good enough for flat-ish
    props; overlaps are possible where two surfaces share a dominant axis."""
    polys = mesh.polygons
    n = len(polys)
    loops = mesh.loops
    verts = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", verts)
    verts = verts.reshape(-1, 3)
    normals = np.empty(n * 3, dtype=np.float64)
    polys.foreach_get("normal", normals)
    normals = normals.reshape(-1, 3)
    axis = np.abs(normals).argmax(1)            # 0=x 1=y 2=z
    lo = verts.min(0); hi = verts.max(0); span = np.maximum(hi - lo, 1e-9)
    loop_starts = np.empty(n, dtype=np.int64)
    loop_totals = np.empty(n, dtype=np.int64)
    polys.foreach_get("loop_start", loop_starts)
    polys.foreach_get("loop_total", loop_totals)
    loop_verts = np.empty(len(loops), dtype=np.int64)
    loops.foreach_get("vertex_index", loop_verts)
    uvs = np.zeros((len(loops), 2), dtype=np.float32)
    for p in range(n):
        a = int(axis[p])
        bu, bv = (0, 1) if a == 0 else ((1, 2) if a == 1 else (0, 2))
        lv = loop_verts[loop_starts[p]:loop_starts[p] + loop_totals[p]]
        co = verts[lv]
        u = (co[:, bu] - lo[bu]) / span[bu]
        v = (co[:, bv] - lo[bv]) / span[bv]
        uvs[loop_starts[p]:loop_starts[p] + loop_totals[p], 0] = u * 0.999 + 0.0005
        uvs[loop_starts[p]:loop_starts[p] + loop_totals[p], 1] = (v * 0.999 + 0.0005) / 6 + axis[p] / 6
    layer = mesh.uv_layers.get("WB_cube") or mesh.uv_layers.new(name="WB_cube")
    layer.data.foreach_set("uv", uvs.ravel())
    mesh.update()
    return len(loops)


def prep_uvs(objects, mode: str, report: dict, cache_dir: Path | None) -> None:
    t0 = time.perf_counter()
    if mode == "xatlas":
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        per_mesh = []
        kept = []
        XATLAS_MAX_TRIS = 300_000  # charting is superlinear; above this we keep
        for o in objects:          # the generator's own UVMap (uvcheck showed
            mesh = o.data          # the big room shell is not an offender)
            mesh.calc_loop_triangles()
            ntri = len(mesh.loop_triangles)
            if ntri > XATLAS_MAX_TRIS:
                if mesh.uv_layers:
                    mesh.uv_layers.active_index = 0
                    kept.append({"object": o.name, "triangles": ntri})
                else:
                    cube_bucket_uv(mesh)
                    kept.append({"object": o.name, "triangles": ntri, "uv": "cube"})
                continue
            per_mesh.append(xatlas_uv(o.data, o.name, cache_dir))
        slowest = max(per_mesh, key=lambda r: r.get("seconds", 0))
        report["uv"] = {
            "mode": mode, "count": len(per_mesh),
            "hits": sum(1 for r in per_mesh if r.get("cache") == "hit"),
            "kept_own_uv": kept,
            "seconds": round(time.perf_counter() - t0, 3),
            "slowest": {k: slowest[k] for k in ("object", "triangles", "seconds") if k in slowest},
        }
    elif mode == "cube":
        generated = []
        for o in objects:
            if not o.data.uv_layers:
                loops = cube_bucket_uv(o.data)
                generated.append({"object": o.name, "loops": loops})
        report["uv"] = {"mode": mode, "count": len(generated),
                        "seconds": round(time.perf_counter() - t0, 3),
                        "objects": [g["object"] for g in generated]}
    else:
        report["uv"] = {"mode": "none", "count": 0, "seconds": round(time.perf_counter() - t0, 3)}


# ------------------------------------------------------- material constants

def walk_principled(mat) -> dict:
    """Bounded visited-set walk: find Principled BSDFs (including inside node
    groups); report whether Base Color is linked and the scalar Metallic /
    Roughness if those inputs are constants."""
    import bpy

    out = {"base_linked": True, "metallic": None, "roughness": None, "principled_found": False}
    seen = set()
    stack = [mat.node_tree] if mat.node_tree else []
    metal_vals, rough_vals = [], []
    while stack:
        tree = stack.pop()
        if tree is None or tree.name in seen:
            continue
        seen.add(tree.name)
        for node in tree.nodes:
            if node.type == "GROUP" and node.node_tree is not None:
                stack.append(node.node_tree)
            elif node.type == "BSDF_PRINCIPLED":
                out["principled_found"] = True
                for key, acc in (("Metallic", metal_vals), ("Roughness", rough_vals)):
                    sock = node.inputs.get(key)
                    if sock is not None and not sock.is_linked:
                        try:
                            acc.append(float(sock.default_value))
                        except (TypeError, ValueError):
                            pass
                bc = node.inputs.get("Base Color")
                if bc is not None and not bc.is_linked:
                    out["base_linked"] = False
    if metal_vals:
        out["metallic"] = round(float(np.median(metal_vals)), 6)
    if rough_vals:
        out["roughness"] = round(float(np.median(rough_vals)), 6)
    if not out["principled_found"]:
        out["base_linked"] = True  # unknown → treat as needing evaluation
    return out


# ------------------------------------------------------------------- baking

def make_image(name: str, res: int, srgb: bool):
    import bpy

    img = bpy.data.images.new(name, res, res, alpha=False, float_buffer=False)
    img.colorspace_settings.name = "sRGB" if srgb else "Non-Color"
    return img


def bake_object(obj, img, bake_type: str, pass_filter=None, margin: int = 4) -> None:
    import bpy

    # point every material of this object at the bake image via a temporary
    # unlinked (therefore shade-invisible) Image Texture node, marked active
    added = []
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None or mat.node_tree is None:
            continue
        node = mat.node_tree.nodes.new("ShaderNodeTexImage")
        node.image = img
        node.name = "WB_bake_target"
        mat.node_tree.nodes.active = node
        added.append((mat, node))
    try:
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        kwargs = dict(type=bake_type, margin=margin, use_clear=True,
                      use_selected_to_active=False)
        if pass_filter:
            kwargs["pass_filter"] = pass_filter
        bpy.ops.object.bake(**kwargs)
    finally:
        for mat, node in added:
            mat.node_tree.nodes.remove(node)


def image_pixels(img, res: int) -> np.ndarray:
    px = np.empty(res * res * img.channels, dtype=np.float32)
    img.pixels.foreach_get(px)
    return px.reshape(res, res, img.channels)


def save_png(img, path: Path) -> None:
    img.filepath_raw = str(path)
    img.file_format = "PNG"
    img.save()


def slot_average(obj, px: np.ndarray, res: int) -> list[dict]:
    """Per-material-slot mean base colour: sample the bake at each face's UV
    centroid (nearest texel), weight faces by area, quantize to 8-bit sRGB."""
    mesh = obj.data
    uv_l = mesh.uv_layers.active
    uvs = np.empty(len(mesh.loops) * 2, dtype=np.float64)
    uv_l.data.foreach_get("uv", uvs)
    uvs = uvs.reshape(-1, 2)
    mat_idx = np.empty(len(mesh.polygons), dtype=np.int64)
    mesh.polygons.foreach_get("material_index", mat_idx)
    areas = np.empty(len(mesh.polygons), dtype=np.float64)
    mesh.polygons.foreach_get("area", areas)
    loop_starts = np.empty(len(mesh.polygons), dtype=np.int64)
    loop_totals = np.empty(len(mesh.polygons), dtype=np.int64)
    mesh.polygons.foreach_get("loop_start", loop_starts)
    mesh.polygons.foreach_get("loop_total", loop_totals)
    n_slots = max(len(obj.material_slots), 1)
    sums = np.zeros((n_slots, 3), dtype=np.float64)
    wsum = np.zeros(n_slots, dtype=np.float64)
    for p in range(len(mesh.polygons)):
        s = loop_starts[p]; t = loop_totals[p]
        cu = float(np.clip(uvs[s:s + t, 0].mean(), 0, 1))
        cv = float(np.clip(uvs[s:s + t, 1].mean(), 0, 1))
        texel = px[min(res - 1, int(cv * res)), min(res - 1, int(cu * res))][:3]
        m = mat_idx[p] if mat_idx[p] < n_slots else 0
        sums[m] += texel * areas[p]
        wsum[m] += areas[p]
    slots = []
    for i in range(n_slots):
        mean = sums[i] / max(wsum[i], 1e-12)
        q = [int(round(min(255, max(0, c * 255)))) for c in mean]  # 8-bit sRGB
        slots.append({"material": obj.material_slots[i].material.name if i < len(obj.material_slots) and obj.material_slots[i].material else None,
                      "rgb8": q})
    return slots


# ------------------------------------------------------------------ flatten

def assign_flat_materials(objects, flat: dict, graphs: dict) -> None:
    import bpy

    for o in objects:
        for i, slot in enumerate(o.material_slots):
            key = f"{o.name}__s{i}"
            rec = flat.get(key)
            mat = bpy.data.materials.new(key)
            mat.use_nodes = True
            nt = mat.node_tree
            nt.nodes.clear()
            prim = nt.nodes.new("ShaderNodeBsdfPrincipled")
            out = nt.nodes.new("ShaderNodeOutputMaterial")
            nt.links.new(prim.outputs["BSDF"], out.inputs["Surface"])
            if rec:
                prim.inputs["Base Color"].default_value = (
                    rec["rgb8"][0] / 255, rec["rgb8"][1] / 255, rec["rgb8"][2] / 255, 1.0)
            g = graphs.get(key, {})
            prim.inputs["Roughness"].default_value = g.get("roughness") if g.get("roughness") is not None else 0.9
            prim.inputs["Metallic"].default_value = g.get("metallic") if g.get("metallic") is not None else 0.0
            slot.link = "OBJECT"
            slot.material = mat


def export_glb(bpy, path: Path) -> float:
    t0 = time.perf_counter()
    kwargs = {
        "filepath": str(path), "export_format": "GLB", "export_apply": True,
        "export_yup": True, "export_texcoords": True, "export_normals": True,
        "export_colors": False, "export_cameras": False, "export_lights": False,
        "export_animations": False, "export_skins": False, "export_extras": False,
        "export_draco_mesh_compression_enabled": False, "export_materials": "EXPORT",
    }
    valid = {p.identifier for p in bpy.ops.export_scene.gltf.get_rna_type().properties}
    bpy.ops.export_scene.gltf(**{k: v for k, v in kwargs.items() if k in valid})
    return time.perf_counter() - t0


# --------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blend", required=True)
    ap.add_argument("--mode", choices=["flatten", "bake", "uvcheck"], required=True)
    ap.add_argument("--res", type=int, default=512)
    ap.add_argument("--device", choices=["GPU", "CPU"], default="GPU")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--uv", choices=["xatlas", "cube", "none"], default="xatlas",
                    help="xatlas: fresh atlas per mesh (default — generator UVs overlap); cube: cheap fallback; none: keep as-is")
    ap.add_argument("--skip-normal", action="store_true", help="bake mode: skip the normal pass")
    args = ap.parse_args()
    check_mem()

    out = Path(args.outdir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {"mode": args.mode, "res": args.res, "device": args.device, "blend": args.blend}

    import bpy

    t0 = time.perf_counter()
    bpy.ops.wm.open_mainfile(filepath=args.blend)
    report["open_seconds"] = round(time.perf_counter() - t0, 3)
    log(f"blend open in {report['open_seconds']} s")

    objects = [o for o in bpy.context.view_layer.objects if o.type == "MESH" and o.data]
    report["mesh_objects"] = len(objects)

    if args.mode == "uvcheck":
        uv_report = []
        for o in sorted(objects, key=lambda x: -len(x.data.polygons)):
            r = uv_overlap_score(o.data)
            r["object"] = o.name
            r["had_uv"] = bool(o.data.uv_layers)
            if r["overlap_fraction"] is not None and r["claimed_texels"] > 50:
                uv_report.append(r)
        uv_report.sort(key=lambda x: -x["overlap_fraction"])
        report["uv"] = uv_report[:25]
        worst = max((r["overlap_fraction"] for r in uv_report if r["overlap_fraction"] is not None), default=0)
        log(f"worst overlap_fraction = {worst}")
        (out / "uvcheck.json").write_text(json.dumps(report, indent=2) + "\n")
        return 0

    used = setup_cycles(args.device)
    report["cycles"] = used
    log(f"cycles device: {used}")

    # the xatlas cache is shared by every mode/run of this pipeline
    prep_uvs(objects, args.uv, report, out.parent / "uv_cache")
    log(f"uv ready ({report['uv']['mode']}: {report['uv']['count']} in {report['uv']['seconds']} s)"
        + (f" [{report['uv'].get('hits', 0)} cached]" if report["uv"]["mode"] == "xatlas" else ""))

    # scalar constants per (object, slot) from bounded graph walks
    t0 = time.perf_counter()
    graphs = {}
    for o in objects:
        for i, slot in enumerate(o.material_slots):
            if slot.material is not None:
                graphs[f"{o.name}__s{i}"] = walk_principled(slot.material)
    report["graph_walk_seconds"] = round(time.perf_counter() - t0, 3)

    t0 = time.perf_counter()
    if args.mode == "flatten":
        res = 32
        flat = {}
        img = make_image("WB_flat_bake", res, srgb=True)
        per_obj = {}
        for n, o in enumerate(objects):
            t1 = time.perf_counter()
            bake_object(o, img, "DIFFUSE", pass_filter={"COLOR"})
            px = image_pixels(img, res)
            flat.update({f"{o.name}__s{i}": rec for i, rec in enumerate(slot_average(o, px, res))})
            per_obj[o.name] = round(time.perf_counter() - t1, 4)
            if (n + 1) % 25 == 0:
                log(f"flatten {n + 1}/{len(objects)}")
        report["flatten"] = {"bake_seconds": round(time.perf_counter() - t0, 3),
                             "mean_per_object_ms": round(1000 * float(np.mean(list(per_obj.values()))), 2),
                             "slots": len(flat)}
        (out / "flat_colors.json").write_text(json.dumps(flat, indent=1, sort_keys=True) + "\n")
        assign_flat_materials(objects, flat, graphs)
        export_s = export_glb(bpy, out / "source_flat.glb")
        report["export_seconds"] = round(export_s, 3)
        report["source_flat_glba_sha256"] = sha256_file(out / "source_flat.glb")
        report["source_flat_bytes"] = (out / "source_flat.glb").stat().st_size
    else:  # bake
        texdir = out / "textures"
        texdir.mkdir(exist_ok=True)
        per_obj = {}
        total_bytes = {"base": 0, "rough": 0, "normal": 0}
        img_base = make_image("WB_bake_base", args.res, srgb=True)
        img_rough = make_image("WB_bake_rough", args.res, srgb=False)
        img_norm = None if args.skip_normal else make_image("WB_bake_norm", args.res, srgb=False)
        t_b = time.perf_counter()
        for n, o in enumerate(objects):
            t1 = time.perf_counter()
            safe = o.name.replace(".", "_")
            bake_object(o, img_base, "DIFFUSE", pass_filter={"COLOR"})
            save_png(img_base, texdir / f"{safe}_base.png")
            bake_object(o, img_rough, "ROUGHNESS")
            save_png(img_rough, texdir / f"{safe}_rough.png")
            if img_norm is not None:
                bake_object(o, img_norm, "NORMAL")
                save_png(img_norm, texdir / f"{safe}_norm.png")
            per_obj[o.name] = round(time.perf_counter() - t1, 4)
            if (n + 1) % 20 == 0:
                log(f"bake {n + 1}/{len(objects)}  ({per_obj[o.name]:.2f} s for {o.name[:32]})")
        report["bake"] = {
            "bake_seconds": round(time.perf_counter() - t_b, 3),
            "mean_per_object_s": round(float(np.mean(list(per_obj.values()))), 3),
            "max_per_object_s": round(max(per_obj.values()), 3),
            "max_object": max(per_obj, key=per_obj.get),
        }
        for p in texdir.glob("*.png"):
            kind = "normal" if p.name.endswith("_norm.png") else ("rough" if p.name.endswith("_rough.png") else "base")
            total_bytes[kind] += p.stat().st_size
        report["texture_bytes"] = total_bytes

        # textured materials: flat Principled + base-colour Image Texture per slot
        t0 = time.perf_counter()
        for o in objects:
            safe = o.name.replace(".", "_")
            for i, slot in enumerate(o.material_slots):
                mat = bpy.data.materials.new(f"{o.name}__s{i}_tex")
                mat.use_nodes = True
                nt = mat.node_tree
                nt.nodes.clear()
                prim = nt.nodes.new("ShaderNodeBsdfPrincipled")
                out_n = nt.nodes.new("ShaderNodeOutputMaterial")
                nt.links.new(prim.outputs["BSDF"], out_n.inputs["Surface"])
                img = bpy.data.images.load(str(texdir / f"{safe}_base.png"), check_existing=True)
                img.colorspace_settings.name = "sRGB"
                tex = nt.nodes.new("ShaderNodeTexImage")
                tex.image = img
                nt.links.new(tex.outputs["Color"], prim.inputs["Base Color"])
                g = graphs.get(f"{o.name}__s{i}", {})
                prim.inputs["Roughness"].default_value = g.get("roughness") if g.get("roughness") is not None else 0.9
                prim.inputs["Metallic"].default_value = g.get("metallic") if g.get("metallic") is not None else 0.0
                slot.link = "OBJECT"
                slot.material = mat
        report["textured_assign_seconds"] = round(time.perf_counter() - t0, 3)
        export_s = export_glb(bpy, out / "source_textured.glb")
        report["export_seconds"] = round(export_s, 3)
        report["source_textured_bytes"] = (out / "source_textured.glb").stat().st_size
        report["source_textured_sha256"] = sha256_file(out / "source_textured.glb")

        # ground-truth beauty render at reduced samples
        t0 = time.perf_counter()
        scene = bpy.context.scene
        scene.cycles.samples = 64
        scene.render.resolution_x = 768
        scene.render.resolution_y = 768
        scene.render.image_settings.file_format = "PNG"
        cam = scene.camera or next((o for o in bpy.data.objects if o.type == "CAMERA"), None)
        if cam is not None:
            scene.camera = cam
            scene.render.filepath = str(out / "cycles_beauty.png")
            bpy.ops.render.render(write_still=True)
            report["beauty_seconds"] = round(time.perf_counter() - t0, 3)
        else:
            report["beauty"] = "no camera in scene"

    (out / "bake_report.json").write_text(json.dumps(report, indent=2) + "\n")
    log("done")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
