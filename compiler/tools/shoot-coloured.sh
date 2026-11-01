#!/bin/bash
# shoot-coloured.sh — ProArt side of the round-2 colour proof.
# Takes a coloured source glb (from the Thor's bake_colour.py), compiles the
# world, and screenshots the viewer as the table-move delta lands live (the
# delta is staged OUT of the build dir and dropped by the shot driver after
# the page subscribes — the demo feed is "versions land while you watch").
# Usage: shoot-coloured.sh <source.glb> <tag> [framing] [entry-id]
set -eo pipefail
cd /home/proart/lm-wt/world-stream
SRC="$1"; TAG="$2"; FRAMING="${3:-16,-0.7,1.2}"; ENTRY="${4:-_table_coffee_legged_rand.p0}"
B=/tmp/build-$TAG
STAGE=/tmp/stage-$TAG
rm -rf "$B" "$STAGE"; mkdir -p "$B" "$STAGE"

node compiler/worldc.mjs recipe  --source "$SRC" --world livingroom \
  --meta generator=room_livingroom_rand --meta seed=42 \
  --meta engine=infinigen@2.0.0a3+bpy4.2 --out "$B/v1.json" > /dev/null
node compiler/worldc.mjs edit --in "$B/v1.json" --id "$ENTRY" \
  --move 0.9,0,0.4 --author Architect --note "moved to open a path" --out "$B/v2.json" > /dev/null
node compiler/worldc.mjs build --recipe "$B/v1.json" --source "$SRC" --out "$B" --jobs 6
node compiler/worldc.mjs delta --old "$B/v1.json" --new "$B/v2.json" --out "$STAGE" > /dev/null

cd viewer
node server.mjs "$B" --port 8793 > /tmp/server-$TAG.log 2>&1 &
SERVER_PID=$!
sleep 1
FRAMING=$FRAMING node /tmp/ws-shot.mjs http://127.0.0.1:8793/ "/tmp/${TAG}" "$STAGE/world@2.since1.json" "$B" 2>&1 | grep -vE "ReadPixels|warning \[" || true
kill $SERVER_PID 2>/dev/null || true
echo "done: /tmp/${TAG}-v1.png /tmp/${TAG}-v2.png"
