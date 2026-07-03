# Try-it-once: does ROCm ComfyUI run under Docker on this box?

**Throwaway experiment, not production.** Its only job: answer **one question** before we build
the managed-service stack (Waves G4–G6 in `docs/archive/IMAGE_GEN_SERVICE_PLAN.md`) —

> Can a ROCm ComfyUI run inside plain Docker on this Strix Halo (gfx1151) and serve on `:8188`?

If **yes** → green light, JBrain wraps this as a `comfyui` compose service and we build the rest.
If **no** → we learn what your box needs (or fall back to JBrain start/stopping the kyuz0 toolbox
container as-is) before writing code that assumes it works.

You run this **on the Strix Halo box**. I can't run it from here (no GPU).

---

## Prereqs (host line — not automated)
- Kernel **6.18-rc6+** (stable gfx1151 image workflows).
- Docker + the compose plugin; your user can run `docker`.
- The AMD GPU visible: `ls -l /dev/kfd /dev/dri/renderD*` should exist.
- `huggingface_hub` CLI for the model download (`pipx install huggingface_hub` or `pip install -U huggingface_hub`), and `huggingface-cli login` (you have an HF account now; the Qwen files are ungated but login avoids rate limits).

## Step 1 — find your GPU group IDs
```bash
echo "VIDEO_GID=$(getent group video | cut -d: -f3)"   >  .env
echo "RENDER_GID=$(getent group render | cut -d: -f3)"  >> .env
cat .env
```
(These let the container reach `/dev/dri`/`/dev/kfd`. The compose file reads them.)

## Step 2 — download the Qwen-Image model files (~28 GB)
```bash
bash download-models.sh
```
Pulls the 3 files ComfyUI needs (diffusion model fp8, text encoder, VAE) into `./models/...`.
See the script for the exact files; confirm against the HF repo file list if a name has changed.

## Step 3 — bring it up (Path A: the candidate compose)
```bash
docker compose up
```
Then open **http://localhost:8188**. If the ComfyUI graph editor loads, load the built-in
**Qwen-Image** template and run one generation.

> ⚠️ Two knobs in `docker-compose.yml` are **guesses I could not verify** and may need a tweak:
> the **ComfyUI path inside the image** (the `volumes:` target + the `command:` `main.py` path) and
> whether the kyuz0 image **auto-serves** ComfyUI vs. expects interactive use. If `docker compose up`
> exits or can't find `main.py`, run `docker run --rm -it docker.io/kyuz0/amd-strix-halo-comfyui:latest bash`
> and `find / -name main.py -path '*ComfyUI*' 2>/dev/null` to find the real path, then fix the two lines.

## Path B — if Path A fights you, use a proven docker-native setup
These repos are built specifically to run ComfyUI on gfx1151 **under Docker/compose** and are the
quickest way to a yes/no. Clone one, follow its README, get `:8188` up, then drop the Step-2 model
files into its `models/` dir:
- https://github.com/hec-ovi/comfyui-strix-docker  (verified ROCm 7.x "TheRock")
- https://github.com/bluemoehre/comfyui-strix-halo
- https://github.com/IgnatBeresnev/comfyui-gfx1151

## Success = three things to capture and send back
1. **It works:** `:8188` loads and one Qwen image generates on the GPU (check `docker logs` for
   GPU/ROCm init, not CPU fallback — CPU fallback = the HSA override/devices aren't taking).
2. **The working recipe:** whichever compose/command actually launched it (Path A as-is, Path A with
   fixed paths, or which Path-B repo) — that becomes the basis for JBrain's `comfyui` service.
3. **The workflow JSON:** in ComfyUI enable **Dev mode** (settings) → **Save (API Format)** on the
   working Qwen graph. That JSON is what JBrain posts — it replaces the placeholder
   `backend/src/jbrain/image_gen/workflows/qwen_image.json` (and the edit one).

Paste me the outcome (and any error from `docker compose up` / `docker logs`) and I'll take it from there.
