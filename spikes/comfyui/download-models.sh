#!/usr/bin/env bash
# Try-it-once: fetch the Qwen-Image model files ComfyUI needs into ./models/.
# ~28 GB total (fp8 model + 7B text encoder + VAE). Ungated (Apache); an HF login
# avoids rate limits. Run from this spike dir.
#
# Files come from the Comfy-Org/Qwen-Image_ComfyUI repo. If a filename has changed,
# check the repo's file tree and edit the FILES list below.
set -euo pipefail

REPO="Comfy-Org/Qwen-Image_ComfyUI"

# repo-relative path  ->  local ComfyUI subfolder
FILES=(
  "split_files/diffusion_models/qwen_image_fp8_e4m3fn.safetensors|models/diffusion_models"
  "split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors|models/text_encoders"
  "split_files/vae/qwen_image_vae.safetensors|models/vae"
)

# Pick whichever HF CLI is installed.
if command -v hf >/dev/null 2>&1;      then DL=(hf download);
elif command -v huggingface-cli >/dev/null 2>&1; then DL=(huggingface-cli download);
else echo "Need the huggingface_hub CLI: pipx install huggingface_hub" >&2; exit 1; fi

for entry in "${FILES[@]}"; do
  src="${entry%%|*}"; destdir="${entry##*|}"
  mkdir -p "$destdir"
  echo "[download] $src -> $destdir/"
  # Download into a staging tree, then place the file by its basename.
  "${DL[@]}" "$REPO" "$src" --local-dir ./_hf_stage
  cp "./_hf_stage/$src" "$destdir/$(basename "$src")"
done
rm -rf ./_hf_stage
echo
echo "[done] models laid out under ./models:"
find models -type f -printf '  %p  (%k KB)\n' 2>/dev/null || true
echo "Next: docker compose up   (then open http://localhost:8188)"
