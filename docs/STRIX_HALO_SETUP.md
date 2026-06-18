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
- **GPU/UMA memory:** leave the *dedicated* allocation modest (Auto or the
  smallest option). You do **not** carve out 96 GB here — the iGPU borrows the
  shared pool dynamically via `amdgpu.gttsize` (Phase 5). A large fixed UMA just
  wastes RAM.

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

✅ **Checkpoint:** `jbrain status` shows `api`/`db`/`proxy` healthy; site loads.

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
**gpt-oss-120b MXFP4 (~59 GB)**, ~91 GB total — generates the llama-swap config
(with both models in a non-swapping **resident** group so they stay hot), and
starts the gateway.

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

## Phase 8 — Confirm it's really local
- Add a note with a photo → it should OCR locally; watch `jbrain logs local-llm`.
- Ops screen → AI usage card shows the local model serving those tasks ($0 cost,
  since local isn't in the price table).

The gateway has **no published port** (internal network only) — verify via the
app and `jbrain logs`, not `curl localhost:8080`.

---

## Expected performance
~31 tok/s on gpt-oss-120b, ~30–45 tok/s on Qwen3-VL; both stay resident (~91 GB)
with headroom for context.

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
