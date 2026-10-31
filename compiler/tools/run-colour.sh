#!/bin/bash
# run-colour.sh — the round-2 colour pipeline on the Thor, one lock session.
# Everything heavy runs under flock ~/lm-wt/heavy.lock (the box's OOM rule);
# every python entry re-checks ≥25 GB free before it starts.
set -e
S=~/lm-wt/world-stream-scratch
PY=$S/uvvenv/bin/python
BLEND=$S/export1/scene.blend
cd $S

echo "=== flatten A (GPU, 32²)"; $PY bake_colour.py --blend $BLEND --mode flatten --device GPU --outdir flatA
echo "=== flatten B (GPU, 32²) — determinism twin"; $PY bake_colour.py --blend $BLEND --mode flatten --device GPU --outdir flatB
echo "=== flatten determinism:"; sha256sum flatA/source_flat.glb flatB/source_flat.glb

echo "=== bake A (GPU, 512², base+rough+normal)"; $PY bake_colour.py --blend $BLEND --mode bake --res 512 --device GPU --outdir bakeA
echo "=== bake B (GPU, 512²) — drift twin"; $PY bake_colour.py --blend $BLEND --mode bake --res 512 --device GPU --outdir bakeB
echo "=== bake C (CPU, 512², base+rough) — the deterministic-device check"; $PY bake_colour.py --blend $BLEND --mode bake --res 512 --device CPU --skip-normal --outdir bakeCPU

echo "=== drift: bake A vs B (pixel stats per PNG pair)"
$PY - bakeA bakeB <<'EOF'
import sys, json, hashlib
from pathlib import Path
import numpy as np
from PIL import Image
a_dir, b_dir = Path(sys.argv[1]) / "textures", Path(sys.argv[2]) / "textures"
pairs = diff = 0
maxd = 0.0; sumd = 0.0; sumn = 0
sha_diff = 0
for pa in sorted(a_dir.glob("*_base.png")):
    pb = b_dir / pa.name
    if not pb.exists():
        continue
    pairs += 1
    ha, hb = hashlib.sha256(pa.read_bytes()).hexdigest(), hashlib.sha256(pb.read_bytes()).hexdigest()
    sha_diff += ha != hb
    da = np.asarray(Image.open(pa), dtype=np.int16)
    db = np.asarray(Image.open(pb), dtype=np.int16)
    d = np.abs(da - db)
    diff += int((d > 0).any())
    maxd = max(maxd, float(d.max()))
    sumd += float(d.sum()); sumn += d.size
print(json.dumps({
    "png_pairs": pairs, "sha_differ": sha_diff, "pixels_differ_any": diff,
    "max_abs_delta_8bit": maxd, "mean_abs_delta": round(sumd / max(sumn, 1), 6),
}, indent=1))
EOF

echo "=== texture bytes: PNG vs WebP (lossy q85) + webp encode determinism"
$PY - bakeA <<'EOF'
import sys, io, json, hashlib
from pathlib import Path
from PIL import Image
tex = Path(sys.argv[1]) / "textures"
png = webp = 0
first = None
for p in sorted(tex.glob("*_base.png")):
    png += p.stat().st_size
    img = Image.open(p).convert("RGB")
    buf = io.BytesIO(); img.save(buf, "WEBP", quality=85, method=4)
    webp += buf.tell()
    h = hashlib.sha256(buf.getvalue()).hexdigest()
    buf2 = io.BytesIO(); img.save(buf2, "WEBP", quality=85, method=4)
    if first is None:
        first = (h, hashlib.sha256(buf2.getvalue()).hexdigest())
print(json.dumps({"png_bytes": png, "webp_q85_bytes": webp,
                  "webp_ratio": round(webp / png, 3),
                  "webp_encode_deterministic": first[0] == first[1]}, indent=1))
EOF

echo "=== BC7/BCn wheel probe (no source build)"
$S/uvvenv/bin/pip download bc7enc -d /tmp/bc7probe 2>&1 | tail -1 || true
$S/uvvenv/bin/pip index versions texcomp 2>&1 | tail -1 || true
echo "=== colour pipeline done"
