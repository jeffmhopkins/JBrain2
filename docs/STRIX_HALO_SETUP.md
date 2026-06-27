# Running JBrain's local models on an AMD Strix Halo box

End-to-end runbook for self-hosting the optional local models (docs/ANALYSIS.md,
"Self-hosted local models") on a **Ryzen AI Max+ 395 / 128 GB** (gfx1151,
Radeon 8060S) system. Path: **Ubuntu → kernel ≥ 6.18.4 → Vulkan → JBrain base
install → host tuning + reboot → enable local models → route in the UI.** Two
reboots.

Local hosting is opt-in; the stock deploy is cloud-only. Nothing here runs
automatically — every step is a deliberate command.

---

## Phase 0 — BIOS
- **Disable Secure Boot.** The mainline kernel you'll likely install (Phase 2)
  is unsigned and won't boot with Secure Boot on.
- **Resizable BAR / "Above 4G decoding": Enabled.**
- **GPU/UMA memory:** set the iGPU to a **small fixed** dedicated allocation, not
  `Auto`. The iGPU borrows the shared pool dynamically via `amdgpu.gttsize`
  (Phase 5), so the carve-out only needs to be tiny.
  - ⚠️ **Avoid `Auto`.** On a 128 GB box `Auto` (`UMA_AUTO`) silently carves out
    ~50% of RAM (64 GB) as fixed VRAM — the OS then sees only 64 GB and the
    ~91 GB resident set can't fit.
  - On the AMI BIOS in the GMKtec EVO-X2 the control is **Advanced → GFX
    Configuration**: set **`iGPU Configuration` = `UMA_SPECIFIED`** and
    **`UMA Frame buffer Size` = `2G`** (the smallest offered). Other boards label
    it "UMA Mode" / "UMA Frame Buffer Size" — same idea, pick the smallest.
  - **Sanity check after Phase 5's reboot** that the carve-out is actually small
    (the `Auto` trap is invisible until you look):
    ```bash
    free -h                                              # MemTotal ~125 GB (not ~64)
    cat /sys/class/drm/card*/device/mem_info_vram_total  # ~2 GB carve-out (not 64 GiB)
    cat /sys/class/drm/card*/device/mem_info_gtt_total   # ~124 GB — the pool models use
    ```
    A `vram_total` of ~64 GiB and `MemTotal` of ~64 GB means the iGPU is still on
    `Auto`/a large fixed UMA — go back into BIOS and set the small carve-out.

## Phase 1 — Install Ubuntu
- **Ubuntu 25.10** is the low-friction pick (newest Mesa/kernel for gfx1151).
  24.04 LTS is longer-supported but needs a newer-Mesa PPA *and* a mainline
  kernel, so only choose it if you specifically want LTS.
- Install normally, then: `sudo apt update && sudo apt full-upgrade -y`

## Phase 2 — Kernel ≥ 6.18.4  (hard requirement; reboot #1)
gfx1151 has a stability bug below 6.18.4. Check:
```bash
uname -r
```
If **< 6.18.4**, install a mainline kernel (6.18.7+ is the community-tested one)
from <https://kernel.ubuntu.com/mainline/> — download the `linux-headers`,
`linux-modules`, and `linux-image-unsigned` **amd64** `.deb`s for the chosen
6.18.x, then:
```bash
sudo dpkg -i linux-*.deb
sudo reboot
```
- ⚠️ Secure Boot must be **off** (Phase 0) or the unsigned image won't boot.
- ⚠️ Avoid `linux-firmware` **20251125** (breaks ROCm on Strix Halo).

After reboot, confirm `uname -r` ≥ 6.18.4.

## Phase 3 — Vulkan stack + verify the GPU
We default to **Vulkan/RADV** (needs only `/dev/dri`; no ROCm setup):
```bash
sudo apt install -y mesa-vulkan-drivers vulkan-tools
vulkaninfo --summary | grep -i deviceName
```
✅ **Checkpoint:** you must see **Radeon 8060S (RADV GFX1151)**. If absent, Mesa
is too old (on 24.04 add `ppa:kisak/kisak-mesa`) or the kernel is too old — fix
before continuing.

## Phase 4 — Install JBrain (cloud stack first)
Installs Docker, clones to `/opt/jbrain2/src`, brings up the stack:
```bash
curl -fsSL https://raw.githubusercontent.com/jeffmhopkins/JBrain2/main/deploy/install.sh | sudo bash
```
Prompts:
- **Domain** — the name you'll use to reach this box.
- **Access mode** — **1) Cloudflare Tunnel** (default, recommended for a home box
  on a dynamic IP / behind CGNAT: no static IP, no port-forwarding; full
  walkthrough in `CLOUDFLARE_TUNNEL.md`) or **2) Direct** (the box has a public
  name resolving to it with inbound 80/443 open, and Caddy fetches Let's Encrypt).
- **Anthropic / xAI keys** — paste, or leave blank to run fully local.
- **"Enable self-hosted local models?"** → **N** for now (host tuning + reboot
  comes first; you'll enable them in Phase 6).

✅ **Checkpoint:** `jbrain status` shows `api`/`db`/`proxy` healthy (plus
`cloudflared` in tunnel mode). In direct mode the site loads as soon as DNS +
Let's Encrypt resolve; in tunnel mode it loads once you've finished the
Cloudflare side (`CLOUDFLARE_TUNNEL.md`).

## Phase 5 — Host tuning + reboot (#2)
```bash
sudo jbrain strix-halo-host-setup
```
Idempotently (confirming before the GRUB edit):
- kernel params `amd_iommu=off amdgpu.gttsize=126976 ttm.pages_limit=32505856`
  (lets the iGPU address ~124 GB of the unified pool — a ceiling, not a
  reservation),
- adds you to `video`/`render`,
- installs a `tuned` accelerator-performance profile.

Then:
```bash
sudo reboot
```
✅ **Checkpoint:** `cat /proc/cmdline` contains the three params.

## Phase 6 — Enable the local models
```bash
sudo jbrain enable-local-models
```
Builds the gateway (the community-maintained gfx1151 llama.cpp image +
llama-swap), downloads the recommended set — **Qwen3-VL-30B-A3B Q8 (~32 GB)** +
**gpt-oss-120b MXFP4 (~59 GB)**, ~91 GB total — generates the llama-swap config,
and starts the gateway. The recommended set runs **co-resident by default** (~91
GB, a llama-swap `matrix` set — 120b + vl stay hot together so the agent's text
and vision models never swap each other out mid-turn). A switch to a state that
needs the whole box (an image render, the coder) is the only thing that displaces
them, and the agent re-warms the set at end of turn. On a memory-tight box, opt
out with `LOCAL_LLM_RESIDENT_GROUP=0 sudo jbrain enable-local-models` (the
recommended set then loads on demand, one at a time).

✅ **Checkpoint:** `jbrain status` shows `local-llm` running; `jbrain logs
local-llm` shows llama-swap listening and the resident models loaded.

> **Known caveat — gpt-oss-120b on Vulkan.** MXFP4 is supported on the Vulkan
> backend, but gpt-oss-120b has a reported Vulkan KV-cache OOM
> ([llama.cpp #15120](https://github.com/ggml-org/llama.cpp/issues/15120)) on
> some setups despite free memory. If it fails to load, either reduce its
> context in the generated `local-models/llama-swap.yaml` (add `-c 8192`), or
> switch the gateway to the ROCm fp4 base (below), which is the better fp4 path.

## Phase 7 — Route tasks to local (in the UI)
Open your domain → paste the owner key (`jbrain reset-owner-key` to mint a new
one) → **Settings → LLM**:
- **Vision** → `Qwen3-VL 30B` (OCR/captions run on-box; text-only models are
  filtered out of this tier).
- **High-stakes reasoning** → `GPT-OSS 120B`.
- Leave the rest on cloud or go fully local — per task, your call.

### Adding more models later — from the PWA, no shell
Once hosting is on, **Settings → LLM → On-box models** lists the whole catalog,
not just what's provisioned. Each un-provisioned model (e.g. **Qwen3-235B-A22B**
at 3-bit, ~104 GB) has an **Install** button that *queues* it. The next
**Update** (Ops → Update, or `jbrain update`) downloads the queued weights, adds
them to `LOCAL_MODELS`, re-stamps the gateway config, and restarts — the same
provisioning `enable-local-models` does, driven from the queue instead of the
shell. The drawer follows the download live (a per-model GB bar reading the bytes
on disk); the coarse phase streams into the Ops update log. A model too large to
co-reside (the 235B) installs **swappable** — it loads on its own, one at a time.
First-time host prep (GPU GIDs, the gateway image, kernel params) still needs
Phases 1–6 on the box; the PWA path only *adds models* to an already-enabled stack.

## Phase 8 — Confirm it's really local
- Add a note with a photo → it should OCR locally; watch `jbrain logs local-llm`.
- Ops screen → AI usage card shows the local model serving those tasks ($0 cost,
  since local isn't in the price table).

The gateway has **no published port** (internal network only) — verify via the
app and `jbrain logs`, not `curl localhost:8080`.

---

## Image generation — ComfyUI + Qwen-Image (optional, opt-in)
Powers jerv's `generate_image` / `edit_image` tools
(`docs/IMAGE_GEN_SERVICE_PLAN.md`): text→image via **Qwen-Image** (native bf16),
a near-instant **fast** path via **DreamShaper XL Lightning** (`generate_image`
`speed: fast`), and image→image via **Qwen-Image-Edit**, served by a **ROCm ComfyUI JBrain manages
as a compose service** — the sibling of the local-LLM gateway. Like that
gateway, it is **opt-in**: a stock deploy never starts it, and JBrain only ever
**POSTs a workflow graph** to it over HTTP (no new backend dependency). Leave it
unprovisioned to keep the feature (and both tools) off.

Prereqs are the same gfx1151 floor as the rest of this runbook: **kernel ≥
6.18.4** (Phase 2) and a working GPU stack. Unlike the Vulkan LLM path, ComfyUI's
**ROCm** stack needs **both** `/dev/kfd` and `/dev/dri` and
`HSA_OVERRIDE_GFX_VERSION=11.5.1` (the `comfyui` compose service sets this) so
ROCm treats the iGPU as gfx1151 — without it the stack silently CPU-falls-back.

**One command provisions and enables it:**
```bash
sudo bash scripts/comfyui-setup.sh             # the recommended set: Qwen-Image generate +
                                               # edit and both 4-step Lightning fast siblings
sudo bash scripts/comfyui-setup.sh qwen-image  # or explicit catalog ids
sudo bash scripts/comfyui-setup.sh dreamshaper # add the lightweight SDXL model (~7 GB)
```
The recommended set covers the `fast` and `quality` paths of both `generate_image`
and `edit_image`: the generate + edit base models plus their 4-step Lightning LoRA
siblings (the LoRA is shared, ~0.85 GB on top of the base weights). Models are
additive: provisioning `dreamshaper` downloads only its ~7 GB checkpoint and leaves
an already-installed Qwen-Image in place.
The script (the sibling of `local-llm-setup.sh`) downloads the weight files named
by the catalog (`jbrain.image_gen.catalog`) into `./comfyui-models/<subdir>`,
writes `JBRAIN_COMFYUI_*` into `.env`, and starts the `comfyui` profile. The api
reaches the service at `http://comfyui:8188` over the internal network — **no
published host port**, mirroring the LLM gateway. The model catalog is the single
source of truth for repos/filenames; add a model by adding a catalog entry, not by
editing the script.

Once image generation is enabled, **`jbrain update` re-syncs the models for you**:
after rebuilding it re-runs the provisioning step for the union of your current
selection and the recommended set, so an update that introduces a new model (or new
weight file) downloads it automatically — no manual re-run. It's idempotent, so an
unchanged catalog is a no-op; it never drops a model you provisioned, and a sync
failure is logged without aborting the update.

- **Validated on-box.** A 1328×1328, 20-step Qwen-Image renders on the iGPU from
  **native bf16** weights (~58 GB resident, the 2512 checkpoint). The renders
  **time-share** the unified memory — the local LLMs are unloaded before a render
  and ComfyUI's model is freed after — so the diffusion model has the box to itself
  and bf16 costs no more RAM than the old fp8 build (gfx1151 upcast fp8 to bf16 at
  load anyway), minus the quantization loss. The `qwen-image-edit` model ships
  **non-recommended** — its graph is wired but its bf16 weights await an on-box
  download+run.
- **Fast path — DreamShaper XL Lightning.** `generate_image` with `speed: fast`
  routes to a single all-in-one SDXL checkpoint (~6.7 GB, baked VAE) driven by the
  stock SDXL graph at 4–8 steps, so a render returns in **seconds** rather than
  minutes — lower fidelity than Qwen, but the right tool for quick or exploratory
  requests. It ships **non-recommended** (its standard SDXL graph is authored, not
  yet box-exported); a first on-box render is the final confirmation.
- **JBrain owns the graph, not the model.** The backend POSTs the workflow JSON in
  `backend/src/jbrain/image_gen/workflows/` (`qwen_image.json`,
  `qwen_image_edit.json`, `dreamshaper_xl.json`), filling typed slots (prompt,
  seed, steps, dims, and — for edit — the uploaded input image). The driver picks the
  graph from the requested model, so a model is a JSON + binding pair, not a code path.

✅ **Checkpoint:** after `comfyui-setup.sh`, ask jerv to generate an image; the
result streams back inline in the chat turn (a chat-only artifact — never a note,
never RAG-indexed). Watch the `comfyui` service logs (`docker compose logs
comfyui`) for the submitted graph.

---

## Expected performance
~31 tok/s on gpt-oss-120b, ~30–45 tok/s on Qwen3-VL. By default the recommended
set stays co-resident (~91 GB) with headroom for context; with
`LOCAL_LLM_RESIDENT_GROUP=0` each loads on demand, one at a time.

## Switching to ROCm (optional, faster)
The ROCm/rocWMMA path is often faster on gfx1151 and is the better route for
gpt-oss's fp4. To use it, set the base image and add the extra device/permission
the ROCm runtime needs, then rebuild:
```bash
# in /opt/jbrain2/.env
LOCAL_LLM_BASE=docker.io/kyuz0/amd-strix-halo-toolboxes:rocm-7.2.4
```
and in `docker-compose.yml` under the `local-llm` service add `- /dev/kfd:/dev/kfd`
to `devices:` and `security_opt: [seccomp:unconfined]`, then
`jbrain enable-local-models` (or rebuild). Benchmark before committing.

## Troubleshooting
| Symptom | Likely cause / fix |
|---|---|
| `vulkaninfo` shows no device | Mesa or kernel too old (Phases 2–3). |
| Unsigned kernel won't boot | Secure Boot still on (Phase 0). |
| Gateway crash-loops | `jbrain logs local-llm`; missing GGUF shard (setup validates this) or config path. |
| gpt-oss OOMs on load | Vulkan KV-cache bug — add `-c 8192` in `llama-swap.yaml`, or use the ROCm fp4 base. |
| Model loads but slow / OOM | GTT param didn't take — re-check `/proc/cmdline` (Phase 5). |
| `/dev/dri` permission denied in container | host render GID not written — check `.env` `RENDER_GID`, re-run `enable-local-models`. |

## Reproducibility / trust
The gateway base is a **community** image. Once a tag works, pin it by digest in
`.env`: `LOCAL_LLM_BASE=docker.io/kyuz0/amd-strix-halo-toolboxes@sha256:<digest>`.
