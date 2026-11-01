#!/usr/bin/env python
"""recon_colour.py — read-only survey of a saved Infinigen .blend for the colour stage.

Answers, per scene: do meshes carry UV layers; are the Principled inputs
(Base Color / Roughness / Metallic) linked (procedural → bake/flatten) or
constants (readable directly); which Cycles devices does this bpy see.

Runs on the Thor with the v2 venv (read-only import); writes nothing but
its JSON report. No GPU work.

    ~/lm-wt/v2-thor/.venv/bin/python recon_colour.py --blend scene.blend --out recon.json
"""
from __future__ import annotations

import argparse
import json
import time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blend", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    t0 = time.perf_counter()
    import bpy

    bpy.ops.wm.open_mainfile(filepath=args.blend)
    open_s = time.perf_counter() - t0

    # ---- Cycles devices this bpy can see
    devices = []
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        for ctype in ("NONE", "OPTIX", "CUDA"):
            try:
                prefs.compute_device_type = ctype
                devs = [(d.name, d.type, bool(d.use)) for d in prefs.get_devices_for_type(ctype)]
                devices.append({"type": ctype, "devices": devs})
            except Exception as e:  # noqa: BLE001
                devices.append({"type": ctype, "error": str(e)})
    except Exception as e:  # noqa: BLE001
        devices.append({"error": str(e)})

    # ---- meshes: UV layers + triangles
    meshes = []
    for m in bpy.data.meshes:
        tris = sum(len(p.vertices) - 2 for p in m.polygons)
        meshes.append({
            "name": m.name,
            "triangles": tris,
            "uv_layers": [u.name for u in m.uv_layers],
            "users": m.users,
        })
    meshes.sort(key=lambda x: -x["triangles"])

    # ---- objects: material slots per object (for the bake unit decision)
    objects = []
    for o in bpy.data.objects:
        if o.type != "MESH":
            continue
        objects.append({
            "name": o.name,
            "materials": [s.material.name if s.material else None for s in o.material_slots],
            "uv_layers": [u.name for u in o.data.uv_layers],
        })
    objects.sort(key=lambda x: x["name"])

    # ---- materials: which Principled inputs are constants vs linked graphs
    INPUTS = ("Base Color", "Roughness", "Metallic")
    materials = []
    for mat in bpy.data.materials:
        rec = {"name": mat.name, "nodes": len(mat.node_tree.nodes) if mat.node_tree else 0}
        prim = None
        if mat.node_tree:
            for n in mat.node_tree.nodes:
                if n.type == "BSDF_PRINCIPLED":
                    prim = n
                    break
        if prim is not None:
            for inp in INPUTS:
                sock = prim.inputs.get(inp)
                if sock is None:
                    rec[inp] = "missing"
                elif sock.is_linked:
                    rec[inp] = "linked"
                else:
                    try:
                        v = sock.default_value
                        rec[inp] = "const:" + (json.dumps(list(v)) if hasattr(v, "__len__") else json.dumps(round(float(v), 6)))
                    except Exception:  # noqa: BLE001
                        rec[inp] = "unreadable"
        else:
            rec["principled"] = False
        materials.append(rec)
    materials.sort(key=lambda x: x["name"])

    n_linked_base = sum(1 for m in materials if m.get("Base Color") == "linked")
    n_const_base = sum(1 for m in materials if isinstance(m.get("Base Color"), str) and m.get("Base Color", "").startswith("const:"))
    meshes_with_uv = sum(1 for m in meshes if m["uv_layers"])
    report = {
        "blend": args.blend,
        "bpy": bpy.app.version_string,
        "open_seconds": round(open_s, 3),
        "counts": {
            "objects": len(objects),
            "meshes": len(meshes),
            "materials": len(materials),
            "images": len(bpy.data.images),
            "meshes_with_uv": meshes_with_uv,
            "materials_basecolor_linked": n_linked_base,
            "materials_basecolor_const": n_const_base,
        },
        "cycles_devices": devices,
        "materials": materials,
        "top_meshes": meshes[:15],
        "objects": objects,
    }
    text = json.dumps(report, indent=2)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
    print(text)


if __name__ == "__main__":
    raise SystemExit(main())
