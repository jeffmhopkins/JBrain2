# Running JBrain's local models on an AMD Strix Halo box

> **Status:** Living · **Last verified:** 2026-08-16

End-to-end runbook for self-hosting the optional local models (docs/reference/ANALYSIS.md,
"Self-hosted local models") on a **Ryzen AI Max+ 395 / 128 GB** (gfx1151,
Radeon 8060S) system. Path: **Ubuntu → kernel ≥ 6.18.4 → Vulkan → JBrain base
install → host tuning + reboot → enable local models → route in the UI.** On
**26.04 LTS** the stock kernel already clears the Phase 2 floor, so it's **one
reboot** (host tuning); an older release that needs a mainline kernel is two.

Local hosting is opt-in; the stock deploy is cloud-only. Nothing here runs
automatically — every step is a deliberate command.

---

## Phase 0 — BIOS
- **Secure Boot.** Leave it **on** if you'll run the stock signed kernel (the
  default on 26.04 LTS, whose 7.0 kernel already clears the Phase 2 floor).
  **Disable it** only if you install an unsigned mainline kernel (Phase 2) — on
  an older release, or to get a newer kernel than your distro ships.
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
- **Ubuntu 26.04 LTS** is the pick: it ships **Linux 7.0** (well above the
  Phase 2 gfx1151 floor) and a recent Mesa, so gfx1151 works on the **stock
  signed kernel** — no mainline `.deb`, no Secure-Boot-off dance — and it's the
  long-support LTS.
  - 25.10 also works but is a 9-month interim release (EOL ~mid-2026). On it,
    and on 24.04 LTS, the stock kernel predates the 6.18.4 floor, so you must
    add a mainline kernel in Phase 2 (24.04 also needs a newer-Mesa PPA).
  - **Upgrading an existing 25.10 box in place** (`do-release-upgrade`) is
    supported and lands you on the stock 7.0 kernel: at each dpkg config prompt,
    **keep your version (`N`)** for any file you customized per this runbook —
    `/etc/default/grub`, `/etc/systemd/journald.conf`, `/etc/default/earlyoom` —
    then re-run the verification below.
- Install normally, then: `sudo apt update && sudo apt full-upgrade -y`

## Phase 2 — Kernel ≥ 6.18.4  (hard requirement)
gfx1151 has a stability bug below 6.18.4. Check:
```bash
uname -r
```
**On 26.04 LTS the stock kernel is 7.0 — this requirement is already met, so
skip the rest of this phase (no mainline install, no reboot #1).** On 25.10 /
24.04 the stock kernel is older. If **< 6.18.4**, install a mainline kernel (6.18.7+ is the community-tested one)
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
✅ **Checkpoint:** you must see **Radeon 8060S**. The RADV device string is
`RADV STRIX_HALO` on recent Mesa (26.04) and `RADV GFX1151` on older Mesa — both
are correct. (An `llvmpipe` entry alongside it is the software fallback; ignore
it.) If the Radeon device is absent, Mesa is too old (on 24.04 add
`ppa:kisak/kisak-mesa`) or the kernel is too old — fix before continuing.

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

> **26.04 note — `crashkernel`.** Ubuntu 26.04 adds a `crashkernel=…:4096M`
> reservation on a 128 GB box (it shows up in `/proc/cmdline`, and it's why
> `free` reports ~121 GB rather than ~125). kdump can't survive the
> reclaim-livelock freeze this hardware is prone to — the box locks before it
> runs — so on a memory-tight local set you can reclaim that ~4 GB of headroom:
> add `crashkernel=0` to `GRUB_CMDLINE_LINUX_DEFAULT`, `sudo update-grub`,
> reboot. Optional — a headroom-vs-crash-diagnostics trade.

## Phase 6 — Enable the local models
```bash
sudo jbrain enable-local-models
```
Builds the gateway (the community-maintained gfx1151 llama.cpp image +
llama-swap), downloads the recommended set — **Qwen3-VL-30B-A3B Q8 (~32 GB)** +
**gpt-oss-120b MXFP4 (~59 GB)**, ~91 GB total — generates the llama-swap config,
and starts the gateway.

**The app is the box's sole model evictor, and it restores.** Every model is a llama-swap
non-swapping group member, so the gateway never auto-evicts anyone — instead, before a
model loads, the app (`jbrain.llm.residency`) frees the **fewest** resident models needed to
keep **≥15% of RAM free** after it's resident (weights + KV, measured against live
`/proc/meminfo` so image-gen and OS pressure count too), evicting biggest-first. So you can
**load any model**: a small model (Qwen3.5-0.8B/4B) stays hot beside gpt-oss-120b, requesting
the coder evicts the *big* model — not the tiny one — and a model too large to co-reside
evicts everything and takes the box. Whatever a *transient* eviction removed (an image render
freeing the LLMs, or a code session giving the coder the box) is **remembered and restored at
end of turn**, so the box drifts back to its prior steady state instead of cold-loading on
demand. This replaced the old all-or-nothing pin that co-resided ~91 GB with no headroom and
drove kernel-reclaim hard-freezes (see "Stability — hard-freeze / OOM hardening" below); the
budget is what makes it safe. The 15% floor is tunable **live from Settings → LLM → On-box
memory** (the "Free-RAM headroom" control, 5–50%, read by the evictor in both the api and
worker on the next load with no restart) or, at deploy time, via `LOCAL_LLM_FREE_RAM_FRACTION`;
the stored control value overrides the env default. Lower it to co-reside more (e.g. keep the
Q8 vision model hot beside gpt-oss-120b) at a thinner margin; raise it for more freeze safety.
The Settings → On-box models screen exposes the same rule: **Staging** an available model is a
transient *preview* — it dry-runs the eviction (`plan-load`) so you can see what loading it
would evict before you commit, and **Load** applies exactly that. (There is no persisted
keep-hot pin: models you use stay warm on their own via the restore above.) The **Catalogue**
tab also carries an **Available** switch per installed model: making one *unavailable* takes it
out of the router's swap roster (and unloads it) without deleting the weights, so it flips back
instantly — distinct from **Uninstall**, which prunes the weights. A model that
**can't fit the box at all** — its footprint exceeds total RAM even after evicting everything —
is **refused, not attempted**, on every path (the manual Load 409s, an auto-load fails the
completion), and nothing is evicted for it: loading it would only OOM-crash the box, so the
app declines rather than trying. That guard is the app's; earlyoom below is the OS-level
backstop for the rest.

**The interactive model is kept resident AND primed across restarts (`jbrain.llm.warm_keeper`).**
The slow bit of a first chat turn isn't the weight-load — it's the **prompt prefill**: the
model reading the whole persona + tool schemas before it emits token one (tens of seconds on
a 120B). The gateway runs `--cache-reuse`, so a turn can reuse a matching leading prefix
instead of re-prefilling it — but only if that prefix was primed first, and nothing did that
after a restart (residency's restore only undoes *same-process* evictions; its keep-hot set is
empty on a fresh boot, and an on-demand load is bare). The **WarmKeeper** fills the gap: a
detached boot + interval reconciler that keeps the model `agent.turn` routes to (when it's
local) resident **and primed**. It primes by issuing a throwaway turn down the **same path a
real turn takes** — `router.converse("agent.turn", …)` with jerv's persona + tools + the
resolved effort — so the primed KV prefix is byte-identical to what a real turn sends and the
reuse actually lands. That call also loads the model on demand through residency, so one prime
both resides and warms it. It only ever *adds* that one model, under the same free-RAM floor
and code-mode hold as everything else, and reconciles on an interval so it self-heals after an
app restart, an update (fresh container), or a standalone gateway (llama-swap) restart.

**Installing a model from the PWA makes it the active model.** When an update installs a
model the operator queued, the model sync re-points `agent.turn` at it (`jbrain.cli
local-activate`, called from `deploy/local-models-sync.sh`), so the just-installed model
becomes the box's active chat model and the WarmKeeper above keeps *it* hot — instead of
leaving whatever the post-update smoke-test probe loaded (e.g. gpt-oss-120b) resident. It
activates the most-recently-queued installed model, with the same reasoning-effort gating the
Settings screen applies; a routine or uninstall-only update leaves routing untouched. Change
your active model any time in **Settings → LLM routing** (or the debug console's `llm-set`).

Two subtleties the prime must respect, both learned the hard way:
- **Match the tools exactly.** Under the gateway's `--jinja` the chat template renders the tool
  definitions into the prompt's *leading* tokens, so a persona-only warm diverges from a real
  tool-carrying turn before the reusable prefix ends and `--cache-reuse` salvages almost
  nothing. The prime sends the same tool schemas a real turn does.
- **Re-prime on a liveness flip.** The primed tool set depends on ComfyUI liveness (the
  image-gen tools are hidden when it's down). A prime taken at boot while ComfyUI was still
  unreachable hides those tools, but a real turn once ComfyUI is up shows them — a mismatch that
  silently defeats the reuse. So the keeper keys its "already primed" state on
  `(model, hidden-tool-set)` and re-primes when the hidden set changes, self-correcting once
  liveness settles. The manual **Load** button primes the same persona+tools shape too.

**Per-model context windows survive a deploy (the deploy re-stamp applies overrides; boot reconcile is the backstop).**
Each on-box model has a **context-window** picker (Settings → LLM); the chosen value is persisted
(`app.settings → llm_local_context_windows`) and stamped into the gateway's `-c`. The **deploy**
re-stamp (`deploy/local-models-sync.sh` → `python -m jbrain.llm.llama_swap_config`) used to
regenerate `llama-swap.yaml` from the **base catalog** with no overrides — so every update silently
reset a raised window to its catalog default. That bit twice: Nemotron 3.5 Lightning (32k base,
raised to 500k) reset to 32k, and Qwen3.8-27B-Q4 (32k base, raised to 128k) reset to 32k while the
meter still showed 128k — the model then overflowed ("this model ran out of context") at a
displayed ~25%, because the agent's persona + ~39 tool schemas is already ~33k tokens. Now
`llama_swap_config._main` **loads the saved windows/slots from the settings store (`_saved_overrides`)
and applies them itself**, so the deploy re-stamp is correct at write time — it no longer depends on
the api restarting (a model-only sync's `up -d` often doesn't restart it). `reconcile_gateway_windows_on_boot`
remains as a **backstop**: idempotent (a no-op when the config already matches, so a plain restart
keeps its warm model), best-effort (a missing weight or down gateway never blocks startup), and it
evicts the affected resident models to reload at the corrected `-c` only when the served config
actually changed.

**Optional: a dedicated interactive slot (Settings → LLM → On-box models).** A single llama-server
KV slot holds the primed jerv prefix well enough for ordinary traffic (small background/title
completions don't evict a large prefix), but a genuinely large, different prompt — a 90k-token
`note.extract`, a long non-jerv turn — *can* push it out. Each on-box model has an **interactive
slot** toggle beside its context-window picker: turning it on gives that model llama-server
`-np 2` (two KV slots). llama-server routes each request to the slot with the longest matching
prefix, so jerv turns keep their primed KV in one slot while title/background traffic uses the
other — neither can evict the other's cache. The cost is KV RAM: a second slot **doubles** the
model's KV (the meter and the residency eviction budget both account for it), so `-c` is set to
`window × slots` to keep each slot at the full window. Editable only while the model isn't
resident (a running process can't add a slot live); the change unloads it so the next request
reloads with the new `-np`.

**Code mode reserves the box (exclusive while ON).** Two ~60 GB models — gpt-oss-120b and the
Qwen3-Coder-Next coder — can't safely co-reside on 128 GB, and the swap between them has a
load/unload memory transient the 15% floor doesn't cover. A background deep-research load
contending with a freshly-opened jcode session drove the box past physical RAM and earlyoom
took down a process. So while **code mode is ON**, it reserves the box for code mode's own
models: the jcode power toggle writes a `code_mode_hold_name` flag holding the served names of
**both** the coder (executor) **and** the plan subagent's model (so jcode's own plan↔execute
swap isn't refused). While it is set (1) `residency.ensure_room` **refuses to load any model
outside that set** unless already resident — nothing evicts code mode's models or co-loads a
second large model — and (2) the **worker pauses** its job loop and scheduler tick, so no
autonomous research/ingestion starts and contends. Chat, vision, and background jobs are
declined with "code mode is holding the box — turn it off" until you toggle code mode OFF, which
clears the flag and re-warms the general hot set (gpt-oss + vision). This makes the
contention/OOM impossible by construction rather than relying on the swap transient staying
under the floor. The flag is **cleared at API startup** so a crash/reboot mid-session (earlyoom,
a redeploy) can't strand it — a stale hold would otherwise wedge the box (all loads refused,
worker paused) while the launcher, which reads live service state, shows code mode OFF. Both the
residency and worker reads **fail open** (a settings-read hiccup lifts the reservation rather
than wedging on it), so a full DB outage — which also stops the job queue — is the only case
that drops protection.

The reservation stops *new* contention, but a report can already be **mid-generation** on
gpt-oss when you open code mode. Warming the coder unloads gpt-oss out from under it, so the
launcher surfaces that turn instead of killing it silently: `/jcode/power` reports `active_turn`
— true when a worker job is **running** AND a non-coder model is **resident** (precisely the turn
the coder swap would end; a job on the separate embed/TEI service leaves nothing reasoning-resident
and isn't flagged). When it's set, toggling code mode **on** holds at a confirm gate ("a
`<kind>` turn is running … activating code mode ends it") before starting anything. On confirm,
`set_power` **cancels** that turn — `queue.cancel_running` marks it terminally failed with
`attempts` forced to the ceiling, so the worker's own mid-call failure handling can't requeue it
(`queue.fail` has no `status='running'` guard). A cancelled report does **not** resurrect (matching
the deliberate "end it" choice); self-healing kinds (ingest/embed/integration) are re-enqueued by
their reconcile sweeps regardless. Cancel is scoped to the disruptive turn only, so a background
embed/ingest job on the TEI service is left to finish.

✅ **Checkpoint:** `jbrain status` shows `local-llm` running; `jbrain logs
local-llm` shows llama-swap listening and the resident models loaded.

> **Known caveat — gpt-oss-120b on Vulkan.** MXFP4 is supported on the Vulkan
> backend, but gpt-oss-120b has a reported Vulkan KV-cache OOM
> ([llama.cpp #15120](https://github.com/ggml-org/llama.cpp/issues/15120)) on
> some setups despite free memory. If it fails to load, either reduce its
> context in the generated `local-models/llama-swap.yaml` (add `-c 8192`), or
> switch the gateway to the ROCm fp4 base (below), which is the better fp4 path.
>
> **Known caveat — gpt-oss tool-calling grammar (JSON-Schema `enum`).** gpt-oss's
> harmony tool path (llama.cpp `--jinja`) builds a GBNF grammar over the tool union,
> and a JSON-Schema `enum` on a property of a many-optional-property tool object
> deterministically segfaults the upstream (turn returns HTTP 500). Bisected via the
> debug `tool-probe` (`docs/runbooks/DEBUG_ACCESS_SESSION_GUIDE.md`) as the
> enum × full-optional-field-set interaction — not tool count, byte size, or
> non-ASCII text. **When authoring a `.tool` sidecar that may be served by gpt-oss,
> keep allowed values in the description and validate them in the handler rather than
> using `enum`** (there is a regression test pinning `analyze_stream` enum-free).
> Cloud models and Qwen are unaffected.

## Phase 7 — Route tasks to local (in the UI)
Open your domain → paste the owner key (`jbrain reset-owner-key` to mint a new
one) → **Settings → LLM**:
- **Vision** → `Qwen3-VL 30B` (OCR/captions run on-box; text-only models are
  filtered out of this tier).
- **High-stakes reasoning** → `GPT-OSS 120B`.
- Leave the rest on cloud or go fully local — per task, your call.

### Adding / removing models later — from the PWA, no shell
Once hosting is on, **Settings → LLM → On-box models** lists the whole catalog,
not just what's provisioned. Each un-provisioned model (e.g. **Qwen3.8 27B** at
Q8, ~28 GB) has an **Install** button. Tapping it **starts the download
immediately** — a dedicated weight-sync one-shot, **not** a system update: it
pulls the queued weights, adds them to `LOCAL_MODELS`, re-stamps the gateway
config, and restarts the gateway — the same provisioning `enable-local-models`
does, but with no `git pull` or image rebuild. The drawer follows it live (a
per-model GB bar reading the bytes on disk); the coarse phase and the verbose
per-model download log stream into the queue banner. **Removing** is symmetric:
an installed model's **Uninstall** button (on the Installed or Catalog tab) applies
through the same sync one-shot, dropping it from `LOCAL_MODELS` and pruning its
weights. A large model that can't co-reside (e.g. the ~85 GB Q8 coder, or Nemotron
3 Super at ~78 GB) evicts down to at most a small low-tier model when loaded — it
runs effectively standalone with the box to itself. Going the other way,
**Qwen3-VL 30B at Q4_K_M (~18 GB)** sits in the catalog beside the recommended Q8 vision
model as a memory-saver twin: half the weights, so it co-resides with gpt-oss-120b under the
free-RAM floor instead of evicting it — at some OCR-fidelity cost on dense/small text (its
vision projector stays F16, so the fine-text hit is limited). Install it when co-residence
headroom matters more than the last bit of transcription accuracy. First-time host prep (GPU GIDs, the gateway image,
kernel params) still needs Phases 1–6 on the box; the PWA path only *adds/removes
models* on an already-enabled stack.

**MTP (faster text) variant — `Qwen3.8 27B · MTP`.** A catalog entry that serves the same
Qwen3.8-27B Q4_K_M weights as the interactive twin but with llama.cpp multi-token prediction
(`--spec-type draft-mtp`) for ~1.4–2× faster **decode** — self-speculation off the MTP head
that unsloth's GGUF already bakes in (no separate draft model). Two hard limits come from
llama.cpp's MTP path, both reflected in the entry: it is **text-only** (MTP can't run the
vision projector, so this variant has no vision) and **single-slot** (`-np 1`, so it can't take
the interactive keep-warm second slot). It speeds decode only — prompt processing is a touch
slower. **Caveat:** MTP on the Vulkan/RADV gateway is build-fragile (it crashed on this exact
gfx1151 arch at llama.cpp's MTP launch and still has open Vulkan MTP bugs), and the gateway
tracks llama.cpp master (see "Tracking newest llama.cpp" below) — so after an update, confirm
it loads and generates; if a build regresses, uninstall it and fall back to `qwen3.8-27b-q4`.

> **When a download fails**, the banner shows the reason (last log line). For the
> full verbose log — the resolved repo, include globs, and the hf error (404 / auth
> / disk / network) — read `GET /api/debug/provision/status` via a debug token
> (docs/runbooks/DEBUG_ACCESS.md), or the Ops update log.

## Phase 8 — Confirm it's really local
- Add a note with a photo → it should OCR locally; watch `jbrain logs local-llm`.
- Ops screen → AI usage card shows the local model serving those tasks ($0 cost,
  since local isn't in the price table).

The gateway has **no published port** (internal network only) — verify via the
app and `jbrain logs`, not `curl localhost:8080`.

---

## Image generation — ComfyUI + Qwen-Image (optional, opt-in)
Powers jerv's `generate_image` / `edit_image` tools
(`docs/archive/IMAGE_GEN_SERVICE_PLAN.md`): text→image via **Qwen-Image** (native bf16),
a near-instant **fast** path via the **Qwen-Image 4-step Lightning** LoRA (`generate_image`
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
  **recommended** (part of the default provisioned set) — its graph is validated
  structurally (exported from the box).
- **Fast path — Qwen-Image 4-step Lightning.** `generate_image` with `speed: fast`
  routes to the Qwen-Image base model driven through the shared 4-step Lightning
  LoRA at CFG 1 — the same ~58 GB Qwen family as the quality path, so it returns in
  a fraction of the quality render time without a second large checkpoint.
  **DreamShaper XL Lightning** is a separate opt-in tier (`speed: dreamshaper`): a
  single all-in-one SDXL checkpoint (~6.7 GB, baked VAE) driven by the stock SDXL
  graph at 4–8 steps that renders in **seconds** — lower fidelity than Qwen, but a
  tiny standalone for quick or exploratory requests. It ships **non-recommended**
  (opt in with `comfyui-setup.sh dreamshaper`; its standard SDXL graph is authored,
  not yet box-exported); a first on-box render is the final confirmation.
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
~31 tok/s on gpt-oss-120b, ~30–45 tok/s on Qwen3-VL. Models co-reside up to the RAM
budget: as many stay hot as fit under the ≥15%-free floor, and a load evicts the fewest
others needed to make room (so a text↔vision switch only cold-loads if both don't fit).
Tune the headroom from **Settings → LLM → On-box memory** (live) or with
`LOCAL_LLM_FREE_RAM_FRACTION` (deploy default).

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

## Stability — hard-freeze / OOM hardening
The recommended local set keeps **~91 GB resident** in the 130 GB unified pool,
leaving only ~30 GB for the OS, containers, and KV-cache. A sudden allocation on top
of that — a `jbrain update` recreating containers, a model swap, an image render, or
context growth — can push the box to its memory ceiling. On this hardware the kernel
does **not** always OOM-kill cleanly: it can enter a **reclaim livelock** where every
core spins in page reclaim and the whole machine hard-locks — USB keyboard and mouse
included — until a power cycle. The log signature is a burst of *missed kernel
messages* ending mid `Mem-Info`/slab dump, and Postgres recovering with "database
system was not properly shut down" on the next boot.

Update-time allocation is handled for you, and the handling is structural rather than
best-effort — see "An update never competes with itself for memory" below for the
sequence. The short version: an update quiesces the stack, hardens *before* the heavy
phase, drops the page cache, and refuses a model load it cannot afford.

The host OS hardening is **applied automatically** by `deploy/oom-hardening.sh` — run
from `scripts/local-llm-setup.sh` (install and every `jbrain enable-local-models`) **and**
from the host `jbrain update` path when local hosting is on, so an existing box gets
hardened on its next update with no manual step. A **PWA** update cannot run that script
(earlyoom needs `apt`, the persistent file needs `/etc/sysctl.d`), but it does apply the
reclaim-headroom knobs in step 2 for the current boot through a privileged one-shot — the
`vm.*` knobs are not namespaced, so a container writes the same values the host would. The
steps below are what the hardening does and how to verify:

1. **earlyoom** — kill the biggest hog on memory pressure *before* the kernel stalls.
   Installed and configured by the setup script; verify (or reapply by hand):
   ```bash
   systemctl is-active earlyoom && cat /etc/default/earlyoom
   # EARLYOOM_ARGS="-r 60 -m 10 -m 5 -s 5 -s 3 --prefer ^llama-server$ --avoid ^(sshd|systemd|systemd-.*|dockerd|containerd|postgres|supervisor)$"
   ```
2. **Reclaim headroom (sysctl)** — start reclaiming earlier, thrash into swap less.
   Written to `/etc/sysctl.d/99-jbrain-oom.conf` by the setup script; verify:
   ```bash
   sysctl vm.min_free_kbytes vm.watermark_scale_factor vm.swappiness
   # vm.min_free_kbytes = 2097152 · vm.watermark_scale_factor = 200 · vm.swappiness = 10
   ```
3. **The app evicts to a RAM budget, so nothing over-commits the box** — every load frees
   the fewest resident models needed to keep ≥15% of RAM free (`LOCAL_LLM_FREE_RAM_FRACTION`)
   before it loads, and nothing is ever pinned beyond that floor. There is no keep-hot pin to
   over-commit: a manual **Load** (Settings → LLM → On-box models) evicts to the same budget —
   the **Stage** preview shows what it will evict first — and models you use stay warm via the
   end-of-turn restore, not a held pin. **Cross-process serialization:** the `api` and the
   `worker` each run their own `ResidencyCoordinator`, so to stop two processes co-loading
   past the floor, the automatic evict+load path holds a **Postgres transaction-level
   advisory lock** (`pg_box_lock`, `jbrain.llm.residency`) box-wide — only one process
   evicts+loads at a time, and the loaded model's memory is committed before the lock
   releases so the next process's plan sees it. It's best-effort: a DB hiccup degrades to
   unlocked rather than failing a turn. A rare gap remains — a manual operator **Load**
   (Settings → LLM) isn't under that lock — so earlyoom (steps 1–2) stays the catch-all
   backstop for any residual overcommit.
4. **Persistent logs** so the next event's full dump survives the freeze:
   ```bash
   sudo mkdir -p /var/log/journal
   sudo sed -i 's/^#\?Storage=.*/Storage=persistent/' /etc/systemd/journald.conf
   sudo systemctl restart systemd-journald
   ```

> **Swap is deliberately small (8 GB).** Do **not** enlarge it to "fix" the freezes —
> on a reclaim livelock a large swap just prolongs the thrash instead of breaking it.
> The levers above (kill fast, keep headroom) are the real fix; swap is only a shallow
> cushion for brief spikes.

## Troubleshooting
| Symptom | Likely cause / fix |
|---|---|
| Whole box hard-freezes (kbd/mouse dead), needs a power cycle | Unified-memory OOM / reclaim livelock — see "Stability — hard-freeze / OOM hardening" above. |
| `vulkaninfo` shows no device | Mesa or kernel too old (Phases 2–3). |
| Unsigned kernel won't boot | Secure Boot still on (Phase 0). |
| Gateway crash-loops | `jbrain logs local-llm`; missing GGUF shard (setup validates this) or config path. |
| gpt-oss OOMs on load | Vulkan KV-cache bug — add `-c 8192` in `llama-swap.yaml`, or use the ROCm fp4 base. |
| Model loads but slow / OOM | GTT param didn't take — re-check `/proc/cmdline` (Phase 5). |
| `/dev/dri` permission denied in container | host render GID not written — check `.env` `RENDER_GID`, re-run `enable-local-models`. |

## Reproducibility / trust
The gateway base is a **community** image, pinned by digest in
`deploy/Dockerfile.local-llm` (`LOCAL_LLM_BASE`): the rolling `:vulkan-radv` tag tracks
llama.cpp master, and a floating build once let a gpt-oss tool-call grammar regression
ride in unnoticed, so a known-good digest is the reproducible baseline. That pinned
digest is now the **rollback target** for the auto-update below, not a frozen ceiling —
override it in `.env` (`LOCAL_LLM_BASE=…@sha256:<digest>`) to move the baseline, e.g. a
`rocm-*` variant.

### Tracking newest llama.cpp automatically (default-on)
A **just-released** model can need a llama.cpp newer than the pinned base — its
architecture won't load (llama-server exits at model-load: `upstream command exited
prematurely` in the gateway log; the Nemotron-3.5 hybrid-Mamba arch is the canonical
case). So on a box with local hosting enabled, **every `jbrain update` (host or PWA)
auto-updates the gateway** — no flag, no shell step (the owner drives this box from the
PWA, no terminal): it rebuilds on the **floating** tag with `docker compose build --pull`
(a cheap manifest check when llama.cpp hasn't moved; a real rebuild when it has), then
**smoke-tests** the build — loads the smallest installed model and, when gpt-oss is
installed, runs one tool-carrying probe (the exact surface the past regression broke).
**If the smoke test fails, the update rolls the gateway back to the pinned
`LOCAL_LLM_BASE`** and keeps serving, logging a `WARNING` to the update log. That
rollback net is what makes tracking master safe by default: a bad upstream build never
leaves the box unable to serve, and a good one lands automatically.

**There is ONE update script.** `deploy/update-inner.sh` is the implementation; the PWA
runs it as a detached one-shot and `jbrain update` runs it directly with
`JBRAIN_HOST_UPDATE=1`. It used to be two hand-maintained copies, and they drifted in
exactly the way that matters: the host copy never rebuilt the gateway on the floating
llama.cpp tag or loaded a model to smoke-test it, and the containerized copy never
re-applied the OOM hardening — so the PWA path did the memory-heavy work *without* the
protection against precisely that, and hard-locked the box. Steps that genuinely need the
real host (mDNS via systemd/avahi, on-box image models, `earlyoom` via `apt`) are gated on
that flag inside the one script rather than living in a second one — and the list is kept
as short as it can honestly be: the reclaim sysctls left it once a privileged one-shot
turned out to reach them.

**Before any of the churn, the gateway is emptied and removed.** An update asks it to
release every loaded model (`jbrain.cli local-llm-unload`), then removes the container
rather than merely stopping it, then waits for it to actually be gone. Stopping alone
leaves the kernel reclaiming tens of gigabytes exactly as the build starts allocating —
the race that drove a reclaim livelock and hard-locked the host, keyboard included — and a
merely-stopped container is one stray `up -d` away from reloading the weights mid-update.
`local-models-sync.sh` honours `JBRAIN_SKIP_GATEWAY_START` for the same reason: its own
restart used to land squarely in that window. The gateway comes back once, deliberately,
after the rebuild.

### An update never competes with itself for memory
Emptying the gateway was not enough on its own. Updates kept hard-locking the box, and the
kernel trace named the culprit exactly: `llama-server` failing an **order:0** allocation —
a single 4 KB page — inside `amdgpu_ttm_tt_populate` during a GEM buffer create, with
`__GFP_RETRY_MAYFAIL` in the flags. That flag tells the kernel to reclaim hard and then
*fail* rather than invoke the OOM killer, which is why nothing was killed, nothing showed
up in the app's metrics, and the host simply froze in reclaim. earlyoom and the sysctls
above were already in place when it happened, so hardening alone does not cover this.

The fix is that `deploy/update-inner.sh` stops doing several memory-heavy things at once.
Its phase order is deliberate:

1. Backup, source refresh, `.env` backfills — stack up, cheap.
2. **Quiesce.** Empty and remove the gateway, then stop every service except the control
   plane (`db`, `api`, `supervisor`, `proxy`, `cloudflared`). Those stay up so the PWA can
   still stream the update log — going fully dark would free about a gigabyte and cost you
   any way to tell a slow build from a wedged one. An `EXIT` trap restores the stopped
   services even if the updater is killed mid-phase, and it restores the profile-gated ones
   (`comfyui`, the mqtt pair) that a plain `up -d` would never name.
3. Reclaim hardening, image build, migrations — all against the quiesced stack.
4. Gateway rebuild on the floating tag, **page cache dropped**, then the smoke test. The
   tool probe loads gpt-oss-120b (~59 GB), so it happens at the emptiest moment of the
   whole update rather than on top of a freshly-recreated stack.
5. Empty the gateway again, recreate the stack, restore the quiesced services.
6. Model syncs, gateway restart, prune.

Two details matter more than they look. The **page-cache drop** comes immediately before
the load because `MemAvailable` counts reclaimable cache as free — and reclaiming it under
pressure is the very thing that livelocks this box, so after a build that just wrote tens
of GB of layers the number reads fine and cannot be realised in time. Dropping first turns
the reading into real free pages. And the smoke test then **refuses a load it cannot
afford**: it checks `MemAvailable` against the model's weights plus
`smoketest.LOAD_HEADROOM_GB` (10 GB) and, if short, reports the shortfall and fails —
which routes into the existing rollback, so a box without the room keeps the pinned,
known-good base instead of betting the host on a verification step. In the update log
that reads as `NOT ENOUGH MEMORY to load … Refusing the load`.

**Every one-off `compose run` in an update is bounded.** The toggle read gets 120s and the
smoke test 600s, and a timeout is treated as a FAILURE — so a hung smoke test rolls the
gateway back to the pinned base rather than stopping the update dead. This exists because
an update stalled twice at `jbrain-api-run-… Created`: a container compose created and
never started, with `set -e` and no ceiling meaning the update simply stopped there
forever, stack half recreated. The gateway client's own httpx timeouts cannot help when
the process never starts. A killed run also sweeps the orphaned one-off container, which
`--rm` never cleans up because the container belongs to the daemon rather than to
`compose run`.

**Turning it off is a PWA toggle**, not a file edit: **Ops → Update → "Track newest
llama.cpp"**. The owner runs this box remotely with no terminal (CLAUDE.md #10), and this
is the one switch that decides whether an update loads a model into the iGPU at all — the
heaviest thing an otherwise-routine update does — so it has to be reachable from the
surface they actually operate. The stored setting is read by the update one-shot; an
`.env` opt-out still wins, so an existing frozen box stays frozen.

The smoke test is also **skipped when the rebuild produces the identical image**: it
exists to vet a new upstream build, and re-loading a model to re-verify a byte-identical
gateway was pure cost on every no-op update.

Knobs (all optional, `.env` — the first is the same switch as the PWA toggle above):

```
# Freeze the gateway on the pinned base instead of tracking newest (reproducible builds):
LOCAL_LLM_AUTO_UPDATE=false
# The rolling tag to float onto (default below); rocm users point at a rocm-* tag:
LOCAL_LLM_BASE_FLOATING=docker.io/kyuz0/amd-strix-halo-toolboxes:vulkan-radv
```

Gated on `LOCAL_LLM_ENABLED`, so a stock cloud stack is never touched. Refreshes on
**update**, not on a bare reboot — a reboot restarts the existing gateway image
unchanged, so **run an update from the PWA (Ops → Update) to pick up a newer llama.cpp**.
A still-unsupported architecture (the community image hasn't caught up to a day-old
model yet) is NOT caught by the smoke test — it checks the smallest model + gpt-oss, not
every model — so the floated build is kept and that one model simply keeps failing to
load until upstream lands the arch; the next update retries automatically.
