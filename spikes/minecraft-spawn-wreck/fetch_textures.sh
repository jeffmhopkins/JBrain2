#!/usr/bin/env bash
# Fetch Mojang's vanilla Bedrock block textures (github.com/Mojang/bedrock-samples) into a
# cache OUTSIDE the repo and crop/tint them for render.py. The textures are Mojang's: they
# are read locally for preview renders and never committed or redistributed.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cache="${WRECK_CACHE:-$HOME/.cache/jbrain-wreck}"
mkdir -p "$cache"
if [ ! -d "$cache/bedrock-samples" ]; then
  git clone --depth 1 --filter=blob:none --sparse https://github.com/Mojang/bedrock-samples.git \
    "$cache/bedrock-samples"
  git -C "$cache/bedrock-samples" sparse-checkout set resource_pack/textures/blocks
fi
python3 -I "$here/prep_tex.py" "$cache/bedrock-samples/resource_pack/textures/blocks" "$cache/tex"
echo "textures ready in $cache/tex"
