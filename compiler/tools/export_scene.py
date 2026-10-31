#!/usr/bin/env python
"""export_scene.py — generate an Infinigen 2.0 scene and export it as glTF.

Runs on the Thor with the phase-1 v2 venv (read-only: we only import from it
and never write inside it), in ONE resident bpy process:

    generate (procfunc, CPU only — no render)  ->  save .blend  ->  export .glb

The scene-generation call is the exact code path the `infinigen` CLI uses
(`infinigen2.generate._main`), the same trick `~/lm-wt/v2-thor/warm_scene.py`
uses, so the export is "a real Infinigen 2.0 run" rather than a hand-built
scene. Everything this script writes goes under --outdir (the scratch dir).

Usage (on the Thor):
    ~/lm-wt/v2-thor/.venv/bin/python export_scene.py \
        --generator room_livingroom_rand --seed 42 --outdir ~/lm-wt/world-stream-scratch/export1

Writes: scene.blend, source.glb, export_stats.json (timings + counts).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--generator", default="room_livingroom_rand")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--outdir", type=Path, required=True)
    args = ap.parse_args()

    out = args.outdir.resolve()
    out.mkdir(parents=True, exist_ok=True)

    timings: dict[str, float] = {}

    t0 = time.perf_counter()
    import bpy  # noqa: F401
    import infinigen2.generate as ig
    timings["import_bpy_infinigen"] = time.perf_counter() - t0

    # ---- generate the scene exactly as the CLI would (minus any render pass)
    argv = [
        "infinigen",
        args.generator,
        "--seed", str(args.seed),
        "--output", str(out / "gen"),
        "--save_blend", str(out / "scene.blend"),
        "--cpu_threads", "4",          # stay polite: the box runs other jobs
    ]
    import sys
    old_argv = sys.argv
    sys.argv = argv
    t1 = time.perf_counter()
    try:
        ig._main()
    finally:
        sys.argv = old_argv
    timings["generate_scene"] = time.perf_counter() - t1

    # ---- export glTF from the same resident process (bpy still holds the scene)
    t2 = time.perf_counter()
    kwargs = {
        "filepath": str(out / "source.glb"),
        "export_format": "GLB",
        "export_apply": True,            # evaluated meshes (modifiers applied)
        "export_yup": True,
        "export_texcoords": True,
        "export_normals": True,
        "export_colors": False,
        "export_cameras": False,
        "export_lights": False,
        "export_animations": False,
        "export_skins": False,
        "export_extras": False,
        "export_draco_mesh_compression_enabled": False,  # raw source; our compiler compresses
        "export_materials": "EXPORT",
    }
    # option names move between Blender releases; keep only what this build knows
    valid = {p.identifier for p in bpy.ops.export_scene.gltf.get_rna_type().properties}
    dropped = sorted(set(kwargs) - valid)
    if dropped:
        print(f"[export] dropping kwargs unknown to this bpy: {dropped}")
    bpy.ops.export_scene.gltf(**{k: v for k, v in kwargs.items() if k in valid})
    timings["export_gltf"] = time.perf_counter() - t2

    # ---- stats for the measurements doc
    n_objects = len(bpy.data.objects)
    n_meshes = len(bpy.data.meshes)
    n_materials = len(bpy.data.materials)
    tris = 0
    for m in bpy.data.meshes:
        for p in m.polygons:
            tris += len(p.vertices) - 2

    glb = (out / "source.glb").stat().st_size
    blend = (out / "scene.blend").stat().st_size
    stats = {
        "generator": args.generator,
        "seed": args.seed,
        "objects": n_objects,
        "meshes": n_meshes,
        "materials": n_materials,
        "triangles_approx": tris,
        "source_glb_bytes": glb,
        "scene_blend_bytes": blend,
        "timings_sec": {k: round(v, 3) for k, v in timings.items()},
        "python": sys.version.split()[0],
    }
    (out / "export_stats.json").write_text(json.dumps(stats, indent=2) + "\n")
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
