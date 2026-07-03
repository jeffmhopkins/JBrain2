# Image generation as a managed service — build plan (Waves G4–G6)

> **Status:** Shipped 2026-07 · \`image_gen/gateway.py\`,\`render.py\` + Lightning workflow graphs

Extends `docs/IMAGE_GEN_PLAN.md` (the chat tools + view, Waves G1–G3, shipped on PR #414).
This plan makes the ComfyUI/Qwen image model a **first‑class managed service** controlled from
JBrain — installed, started/stopped, and **loaded/unloaded for RAM** the same way local LLMs are
today — rather than a side‑loaded toolbox. It mirrors the existing local‑LLM stack rather than
inventing a parallel mechanism. Binds on top of `docs/DEVELOPMENT.md`, `docs/PROCESS.md`, and the
`CLAUDE.md` non‑negotiables.

## Why this exists
On a 128GB unified‑memory Strix Halo the local LLM (`llama-swap`) and the diffusion model draw from
the **same** pool, so the model must be load/unload‑able and its footprint visible alongside the LLM.
The owner does not want to hand‑assemble the kyuz0 toolbox; JBrain owns everything above the host line.

## Owner decisions (locked)
| Decision | Choice |
|---|---|
| **Service shape** | A `comfyui` docker‑compose profile (ROCm ComfyUI), mirroring the `local-llm` profile |
| **Install** | One command — `sudo bash scripts/comfyui-setup.sh` (parallel to `local-llm-setup.sh`): downloads Qwen weights, flips `JBRAIN_COMFYUI_*`, starts the profile. UI does everything after |
| **Runtime control** | Start/stop/restart via the **supervisor**; model **load/unload/status** from the settings UI |
| **RAM coordination** | **Coexist within a budget** — image models share the existing LLM **RAM meter**; the drawer warns/blocks before the budget is exceeded |
| **LLM gateway** | `llama-swap` (existing `LocalGatewayClient`) — image control is a parallel client, not a change to it |
| **UI placement** | **Extend the existing `LocalModelsDrawer`** (one shared meter) so LLM + image footprints are seen together |
| **Models** | Qwen‑Image (fp8 default; bf16 optional) + Qwen‑Image‑Edit, as catalog entries |
| **Host line** | Kernel 6.18‑rc6+, BIOS UMA/GTT, `/dev/kfd`+`/dev/dri`, Docker — stay in `scripts/strix-halo-host-setup.sh`; JBrain manages only above it |

## The pattern being mirrored (grounded)
- Provisioning: `scripts/local-llm-setup.sh` → downloads GGUFs, writes `llama-swap` config, flips
  `JBRAIN_LOCAL_LLM_ENABLED`+`LOCAL_MODELS`, starts the `local-llm` compose profile.
- Gateway: `backend/src/jbrain/llm/local_gateway.py` (`/running`, unload, health‑probe‑to‑load).
- Catalog: `backend/src/jbrain/llm/local_catalog.py` (`LocalModel`: size_gb, vram, tiers…).
- API: `backend/src/jbrain/api/llm_settings.py` — `_snapshot()` + `…/local-models/{id}/load|unload|stage`,
  host RAM via `host_metrics.read_memory_gb()`.
- UI: `frontend/src/screens/LLMSettingsScreen.tsx` → `LocalModelsDrawer` (stage→load→unload, memory bar).
- Supervisor: `supervisor/src/supervisor/*` holds the Docker socket; the `api` never does (ARCHITECTURE
  "Supervisor"). Start/stop/restart + one‑shot update orchestration live here.
- Service wiring: `backend/src/jbrain/main.py` — `app.state.image_gen` already conditional on
  `comfyui_url`; `app.state.local_gateway` unconditional best‑effort.

---

## Wave G4 — the ComfyUI service (foundation + the risk)
**Gated on a validation spike on the owner's box — I cannot run gfx1151/ROCm here.**

- **Spike (first, blocking):** prove a ROCm ComfyUI runs under **docker‑compose** (not just the
  podman/toolbox image) on gfx1151 with `--device /dev/kfd --device /dev/dri`, generating one Qwen
  image. Likely reuses kyuz0's Dockerfile/ROCm base as the compose image. Output: a working image ref
  + the **API‑format workflow JSON** exported from the real graph (replaces the placeholder
  `image_gen/workflows/*.json`, node ids reconciled with `comfyui.py`). If the bridge fails, fall back
  to running the kyuz0 container as an externally‑managed service that the supervisor still start/stops.
- **`comfyui` compose profile** in `docker-compose.yml` (device mounts, models volume, loopback port),
  parallel to `local-llm`.
- **`scripts/comfyui-setup.sh`** (mirror `local-llm-setup.sh`): flock‑guarded, one‑shot download
  container for the Qwen weights into a `comfyui-models/` mount, writes the env, starts the profile.
- **Config:** `comfyui_enabled: bool`, `comfyui_models: list[str]`, `comfyui_active_model: str` in
  `config.py` (parallel to the `local_llm_*` set); keep `comfyui_url` for the in‑cluster service URL.
- **Catalog:** `backend/src/jbrain/image_gen/catalog.py` — `ImageModel` (id, label, served files,
  size_gb, vram estimate, supports_edit, quant) + `CATALOG` (qwen‑image fp8/bf16, qwen‑image‑edit).
- **Docs:** rewrite the `STRIX_HALO_SETUP.md` ComfyUI section into the real runbook (host line +
  `comfyui-setup.sh`); `dev-setup.sh` unchanged (host‑managed).
- **Tests:** catalog unit tests; `comfyui-setup.sh` `bash -n` + a dry‑run path; compose profile lint.

## Wave G5 — the managed control plane (supervisor + gateway + load/unload)
- **`ComfyUiGatewayClient`** (`image_gen/gateway.py`, parallel to `local_gateway.py`): `status()`
  (queue/object‑info → loaded? reachable?), `unload()` → `POST /free {unload_models, free_memory}`,
  `warm()` → a minimal request (or `object_info` touch) to load. Best‑effort, error‑tolerant.
- **Supervisor:** extend its service control to start/stop/restart the `comfyui` profile (it already
  orchestrates the stack); the `api` calls the supervisor, never the Docker socket.
- **API (extend `llm_settings.py`, one snapshot so the UI shares the meter):** add `image_models:
  list[ImageModelInfo]`, `image_hosting_enabled`, `image_active_model` to the snapshot; add
  `…/image-models/{id}/load|unload|stage` (owner‑only, mirroring the LLM routes). RAM **budget**:
  the snapshot's memory gauge sums LLM + image footprints; load is **rejected with a clear message**
  when it would exceed the configured budget (coexist‑within‑budget).
- **Tool‑gating:** `generate_image`/`edit_image` are offered only when an image model is **loaded**
  (not merely when `comfyui_url` is set) — the registry/`app.state` reflects live load state.
- **Tests:** gateway client against a mocked transport (status/unload/warm, error paths); the
  load/unload/stage endpoints (owner‑gating security‑100%, budget‑exceeded rejection, unreachable
  gateway → clean 502); tool‑gating flips with load state.

## Wave G6 — the UI (GUI gate)
- **GUI gate:** three interactive mocks of the image‑model controls **inside** the shared
  `LocalModelsDrawer` (the meter showing both LLM + image segments; a service status/start‑stop control;
  per‑model stage/load/unload) → owner picks before code, lands in `docs/mocks/`.
- **Implementation (after the pick):** extend `LocalModelsDrawer` + `client.ts`
  (`loadImageModel`/`unloadImageModel`/`stageImageModel`, service start/stop); the shared memory bar
  renders image segments; capability chips (generate/edit). Mock‑mode fixtures + tests.

## Wave G7 — the fast generate path (DreamShaper XL Lightning)
> **Superseded:** the `speed: fast` path now routes to the **Qwen 4‑step Lightning LoRA**
> graphs (`qwen-image-lightning` / `qwen-image-edit-lightning`, fixed 4 steps at CFG 1) for
> both `generate_image` and `edit_image`, with the quality path on a 20–40 step band. The
> `dreamshaper` checkpoint below is retained as a lightweight standalone model but is no longer
> wired to the `speed` knob. The Wave G7 design (the `_GEN_GRAPHS` registry, the catalog‑driven
> setup) still stands; only the model behind `fast` changed.

A second generate model behind a `speed` knob on `generate_image`, so jerv can choose a
near‑instant draft over the minutes‑long Qwen render. One script + one `jbrain update` enables it.

- **Catalog:** a `dreamshaper-xl-lightning` `ImageModel` — a single all‑in‑one SDXL checkpoint
  (model+CLIP+baked VAE) in a new `checkpoints` subdir (added to `MODEL_SUBDIRS`), `recommended:
  false`. The setup script is already catalog‑driven, so `sudo bash scripts/comfyui-setup.sh
  dreamshaper-xl-lightning` downloads only its ~7 GB checkpoint and leaves Qwen in place (the
  disk‑space warning is now manifest‑driven, not a fixed ~58 GB guess).
- **Driver:** `comfyui.py` resolves the generate (template, binding) from `spec.model` via a small
  registry (`_GEN_GRAPHS`) instead of a hard‑coded Qwen graph; an unknown model raises a clean
  `ImageGenError` rather than running the wrong graph. New `dreamshaper_xl.json` is the stock SDXL
  graph (CheckpointLoaderSimple → 2× CLIPTextEncode → KSampler → VAEDecode), CFG/sampler authored in.
- **Tool:** `generate_image` gains `speed: fast|quality` (default quality — unchanged behavior).
  `fast` routes to DreamShaper and a short effort→steps curve (4/6/8 at effort 0/5/10) since the
  distilled model tops out at a handful of steps; the recorded `model` is the routed id. Version
  bumped 4→5, digest re‑pinned. `edit_image` is unchanged (no fast path).
- **Residual risk:** the SDXL graph is authored from the well‑known standard form, not yet exported
  from the box, so it ships non‑recommended; a first on‑box render is the final confirmation.

---

## Non‑negotiables check
1. **LLM adapter** — n/a (image service, not an LLM call); control is a parallel gateway client, no
   provider SDK.
2. **Storage** — weights/host files are infra on a read‑only mount, **outside** the storage
   abstraction by the same explicit exception as `local_weights.py` / `host_metrics.py` (documented).
3. **RLS / privilege** — image‑model load/unload/status endpoints are **owner‑only** (mirror
   `llm_settings.py`); the **supervisor** holds the Docker socket, the `api` never does; any new
   settings rows are owner‑scoped with the isolation test if a table is added.
4–6. Comments why‑not‑what; tests with code (80% / security‑100%, ComfyUI faked); Conventional Commits,
   one PR per wave, CI green.
7. Generated images remain **chat artifacts** (unchanged from G1–G3) — never notes/RAG.
8. **`dev-setup.sh`** stays host‑managed; `scripts/comfyui-setup.sh` + `strix-halo-host-setup.sh`
   carry the setup step (rule #8).

## Open items / risks
- **Compose↔ROCm bridge (G4 spike)** is the gating risk and needs the owner's box; the fallback is a
  supervisor‑managed external kyuz0 container.
- **Budget values** (default RAM budget, per‑model vram estimates) — tune on the real box.
- **VRAM vs unified RAM** reporting — `host_metrics` reads `/proc/meminfo`; iGPU GTT accounting may
  need refinement so the meter reflects reality.
- **In‑settings install button** (supervisor‑orchestrated, no shell) — deferred follow‑on per the
  owner's "one‑command script first" choice.
