# On-box fish identification — build plan (Waves F0–F5)

> **Status (branch `claude/fish-identification-tool-61qzru`):** Waves **F1–F5 shipped**
> on the branch — the adapter/gateway/catalog/config, the `identify_fish` tool
> (load→use→unload), the hero-verdict result card (GUI gate #1 → **A**), the owner-only
> control-plane API, the transient-row settings UI (GUI gate #2 → **A**), and the
> compose profile + setup script. All locally verified (ruff/pyright/biome/tsc clean;
> 1969 backend unit + 692 frontend tests green; testcontainers integration tests run in
> CI). Each wave passed an independent adversarial review. **Remaining:** the **F0 host
> spike** (build/validate the ROCm fishial service image + pin the weights release) is
> gated on the owner's Strix Halo box — until then the backend is faked in tests and the
> compose `FISH_ID_IMAGE` / `FISH_ID_RELEASE` are placeholders.

Adds an **`identify_fish`** agent tool backed by the MIT-licensed
[`fishial/fish-identification`](https://github.com/fishial/fish-identification)
models (DINOv2 + ViT classifier, segmentation/detection heads; ~866 species), run
**on-box** as a managed local service the same way the ComfyUI image model is today.
The owner asks a question with a fish photo; the agent classifies it and answers with
a rich result card. Binds on top of `docs/DEVELOPMENT.md` (standards),
`docs/PROCESS.md` (the multi-wave loop + GUI gate), and the `CLAUDE.md`
non-negotiables.

This plan is the sibling of `docs/IMAGE_GEN_PLAN.md` (chat tool + view) and
`docs/IMAGE_GEN_SERVICE_PLAN.md` (the managed service): it deliberately **mirrors
the existing image stack rather than inventing a parallel mechanism**, because the
serving, load/unload, RAM-budget, and supervisor-control problems are identical.

## Why this exists

The fishial models are self-hostable PyTorch weights, not a hosted API — so the
identification runs on the owner's hardware, no photo ever leaves the box (no egress
Proposal needed; invariant #9 holds by construction). On the 128 GB unified-memory
Strix Halo box the classifier draws from the **same** memory pool as the local LLM
and the diffusion model, so — per the owner's decision — the model is **load → use →
unload** per call: the service loads weights lazily on an identify request and is
**freed immediately after**, exactly the `_free_comfyui_model` dance the image tool
already runs (`backend/src/jbrain/agent/imagegentools.py`). It is never resident
between identifications.

## Owner decisions (locked)

| Decision | Choice |
|---|---|
| **Result UI** | A **rich in-chat card** (`fish_identification` tool-view): photo thumbnail + ranked species with confidence. **GUI gate #1.** |
| **Service scope** | **Full managed service in one go** — tool + adapter + compose profile + setup script + supervisor start/stop + settings-drawer load/unload UI + catalog. |
| **Memory model** | **Load → use → unload per call.** The service loads on identify and is freed right after; never resident between calls (mirrors the ComfyUI free-after-render path). |
| **Service shape** | A `fish-id` docker-compose profile (ROCm PyTorch) exposing a tiny loopback HTTP API, mirroring the `comfyui` / `local-llm` profiles. The backend stays dep-free (no torch in the API image). |
| **Egress** | None. On-box inference only; the photo is already an owner attachment in the RLS-scoped session. The tool is **`web`-class, jerv-only, direct-exec** (the `analyze_image` precedent: on-box, no egress despite the class name). |
| **Install** | One command — `sudo bash scripts/fish-id-setup.sh` (parallel to `comfyui-setup.sh`): downloads the fishial weights, flips `JBRAIN_FISH_ID_*`, starts the profile. |
| **Host line** | gfx1151/ROCm host prep stays in `scripts/strix-halo-host-setup.sh`; JBrain manages only above the host line. |

## The pattern being mirrored (grounded)

- **Adapter + fake:** `backend/src/jbrain/image_gen/comfyui.py` (`ImageGen` protocol,
  HTTP driver) + `image_gen/fake.py` (the only impl tests touch).
- **Memory gateway:** `backend/src/jbrain/image_gen/gateway.py`
  (`ComfyUiMemory` protocol: `status()`, `free()`), the sibling of
  `llm/local_gateway.py`.
- **Catalog:** `backend/src/jbrain/image_gen/catalog.py` (`ImageModel`, `CATALOG`,
  `python -m … <ids>` manifest the setup script reads).
- **Tool + view:** `backend/src/jbrain/agent/imagegentools.py`
  (`build_image_handlers`, `_source_bytes` one-source-by-id resolution,
  `generated_image_view`) + `frontend/src/agent/views/registry.tsx`
  (`GeneratedImage`, the closed `REGISTRY`).
- **Registry wiring:** `backend/src/jbrain/agent/readtools.py`
  (`build_registry`, `OPTIONAL_IMAGE_TOOLS` graceful-degrade).
- **Config:** `comfyui_url` / `comfyui_enabled` / `comfyui_models` /
  `comfyui_models_dir` / `comfyui_timeout` in `config.py`.
- **Service wiring:** `backend/src/jbrain/main.py` — `app.state.image_gen` /
  `app.state.comfyui_gateway` conditional on `comfyui_url`.
- **Managed service:** `docs/IMAGE_GEN_SERVICE_PLAN.md` — compose profile, setup
  script, supervisor start/stop, settings drawer, shared RAM meter.
- **Settings UI:** `frontend/src/screens/LLMSettingsScreen.tsx` → `LocalModelsDrawer`
  (stage→load→unload, shared memory bar); `backend/src/jbrain/api/llm_settings.py`.

---

## Wave F0 — grounding + the host spike (the gating risk)

**Gated on the owner's box — I cannot run gfx1151/ROCm/torch or the fishial model
here.** Like the image plan's G4 spike, this de-risks the integration seam before
code commits to it.

- **Spike (first, blocking):** stand up the fishial models under **docker-compose**
  on gfx1151 with `--device /dev/kfd --device /dev/dri`, classify one fish photo
  end-to-end, and **measure the load-time + resident footprint** (the RAM-budget
  input). Output: a working ROCm image ref + the **frozen HTTP API contract** the
  backend adapter will target (request: image bytes; response: ranked
  `{species, score}` + optional bbox/mask), plus a `free`/unload endpoint. The model
  pipeline (segment → detect → embed → nearest-species) is wrapped behind this API so
  the backend never imports torch.
- **License/attribution check:** vendor the fishial MIT license + attribution into
  the service image and `docs/`; record model provenance (weights repo, version).
- **Grounding doc** for the builder agents (paths above + the frozen API contract).

## Wave F1 — the fish-id service + backend adapter (dep-free)

- **`fish-id` compose profile** in `docker-compose.yml` (device mounts, models
  volume, loopback port), parallel to `comfyui`.
- **`scripts/fish-id-setup.sh`** (mirror `comfyui-setup.sh`): flock-guarded one-shot
  weights download into a `fish-id-models/` mount, writes env, starts the profile.
- **Catalog:** `backend/src/jbrain/fish_id/catalog.py` — `FishModel` (id, label,
  weight files + dest, size_gb, footprint estimate, species count, recommended) +
  `CATALOG`; `python -m jbrain.fish_id.catalog <ids>` manifest.
- **Adapter:** `backend/src/jbrain/fish_id/client.py` — `FishIdentifier` protocol
  (`identify(image_bytes) -> FishResult`) + `HttpFishIdentifier` over the shared
  `httpx.AsyncClient` (MockTransport in tests; zero new runtime deps) +
  `fish_id/fake.py` (the only impl tests touch, returns canned ranked species).
- **Memory gateway:** `backend/src/jbrain/fish_id/gateway.py` — `FishIdMemory`
  protocol (`status()`, `free()`), POST `/free` to unload (the load/use/unload tail).
- **Config:** `fish_id_url`, `fish_id_enabled`, `fish_id_models`,
  `fish_id_models_dir`, `fish_id_timeout` in `config.py` (parallel to `comfyui_*`).
- **Tests:** adapter against MockTransport (identify happy path, malformed/error →
  clean tool error, never a stack trace); gateway free/status + error tolerance;
  catalog units + manifest; `fish-id-setup.sh` `bash -n` + dry-run; compose lint.

## Wave F2 — the `identify_fish` tool (load → use → unload)

- **`identify_fish.tool` sidecar** — `web` permission, `cost_class: expensive`,
  jerv-only; params: `source_attachment_id` / `source_image_id` (exactly one), plus
  optional `top_k`. Prose modeled on `analyze_image.tool`.
- **`backend/src/jbrain/agent/fishtools.py`** — `build_fish_handlers`: reuse the
  `_source_bytes` one-source-by-id resolution pattern (RLS-scoped attachment/image
  read), call `FishIdentifier.identify`, then **`gateway.free()`** (unload now), and
  return a `ToolOutput` carrying the prose answer + the `fish_identification`
  `ViewPayload` (data-only; the card schema is locked by GUI gate #1).
- **Registry wiring:** add to `build_registry` and to an `OPTIONAL_FISH_TOOLS`
  set so an unconfigured box silently drops the tool (graceful degrade); wire
  `app.state.fish_id` / `app.state.fish_id_gateway` in `main.py` on `fish_id_url`.
- **Tests:** handler with the fake identifier — happy path (ranked species + view),
  one-source validation (both/neither → clean error), unknown/out-of-scope id →
  clean miss (RLS), service error → clean tool-error string, **and that `free()` is
  called after every identify** (the unload contract). Registry graceful-degrade test.

## Wave F3 — the in-chat result card (**GUI gate #1**)

- **GUI gate:** three interactive mock HTMLs of the `fish_identification` card
  (thumbnail + ranked species + confidence) → **owner picks before any frontend
  code**; the chosen mock lands in `docs/mocks/`.
- **Implementation (after the pick):** a `FishIdentification` component added to the
  closed `REGISTRY` in `frontend/src/agent/views/registry.tsx` (data-only slots:
  thumbnail image-id/attachment-id, ranked `{species, score}`); extend
  `docs/DESIGN.md` "Agent tool views" in the same PR (a new component is a deliberate
  change). Mock-mode fixtures + component tests + the `ViewPayload` schema validation.

## Wave F4 — the managed control plane (supervisor + gateway + load/unload)

- **Supervisor:** extend service control to start/stop/restart the `fish-id` profile
  (the supervisor holds the Docker socket; the `api` never does).
- **API (extend `llm_settings.py`, one shared snapshot):** add fish-id model info,
  `fish_id_hosting_enabled`, active model; `…/fish-models/{id}/load|unload|stage`
  (owner-only, mirroring the LLM/image routes). The snapshot's RAM meter sums LLM +
  image + fish footprints; a load that would exceed the budget is **rejected with a
  clear message** (coexist-within-budget). Tool offered only when reachable.
- **Tests:** gateway client (status/free/error paths); load/unload/stage endpoints
  (**owner-gating security-100%**, budget-exceeded rejection, unreachable → clean
  502); tool-gating flips with service state.

## Wave F5 — the settings UI (**GUI gate #2**)

- **GUI gate:** three interactive mocks of the fish-id controls **inside** the shared
  `LocalModelsDrawer` (the meter showing LLM + image + fish segments; a service
  status/start-stop control; per-model stage/load/unload) → owner picks before code,
  lands in `docs/mocks/`.
- **Implementation (after the pick):** extend `LocalModelsDrawer` + `client.ts`
  (`loadFishModel`/`unloadFishModel`/`stageFishModel`, service start/stop); the
  shared memory bar renders a fish segment; capability chip (identify). Mock-mode
  fixtures + tests.

---

## Non-negotiables check

1. **LLM adapter** — n/a (fish classification is not an LLM call); the on-box service
   is reached through a parallel adapter (`fish_id/client.py`), never a provider SDK.
   The tool itself routes nothing through the model except its text/view result.
2. **Storage** — the photo is read via the existing RLS-scoped attachment/blob path
   (`_source_bytes`); model weights are infra on a read-only mount, **outside** the
   storage abstraction by the same documented exception as `comfyui_models_dir` /
   `local_weights.py`.
3. **RLS / privilege** — the tool reads the image under `ctx.session` (RLS-scoped); a
   foreign-domain/unknown id is a clean miss. Load/unload/status endpoints are
   **owner-only**; the **supervisor** holds the Docker socket, the `api` never does.
   No new domain tables are introduced (no identification is persisted in v1); if one
   is added later it ships with the RLS isolation test.
4. Comments why-not-what; lean density; no commented-out code.
5. **Tests land with code** — 80% / security-100%; the fish service is **faked** in
   tests (no torch, no network), real Postgres via testcontainers where DB is touched.
6. Conventional Commits; **one PR per wave**; both review gates clean; CI green.
7. An identification is a **chat artifact**, never a note/RAG source (mirrors
   generated images). The wiki is unaffected.
8. **`dev-setup.sh`** stays host-managed for the GPU service; `scripts/fish-id-setup.sh`
   + the `strix-halo` host setup carry the setup step (rule #8); any new backend dep
   (none expected — httpx is shared) updates `dev-setup.sh` in the same PR.

## Open items / risks

- **ROCm/torch ↔ fishial bridge (F0 spike)** is the gating risk and needs the
  owner's box; the fallback is a CPU-only or externally-managed container the
  supervisor still start/stops.
- **Footprint + load-time** (RAM-budget input, the `vram_gb` estimate, and whether
  per-call load/unload is fast enough to feel responsive) — measured in the F0 spike.
- **Species coverage** — 866 classes; out-of-distribution photos (non-fish, unlisted
  species) must return a calm low-confidence result, not a confident wrong one. The
  card and prose convey confidence honestly; a threshold below which the agent says
  "not sure" is tuned on the box.
- **Detection/segmentation scope** — v1 classifies the dominant fish; multi-fish
  framing (per-bbox classification) is a deferred follow-on.

## Process notes (per `docs/PROCESS.md`)

- Each wave runs its tasks in parallel worktrees off a `wave-F<N>` branch, with an
  **independent adversarial review per task** (never the builder) and a **wave-level
  review** before the single per-wave PR; security/RLS-touching waves (F4) get a
  red-team pass. CI green → merge → next wave.
- **Two critical-decision interruptions by design:** GUI gate #1 (Wave F3, result
  card) and GUI gate #2 (Wave F5, settings controls). The F0 spike is owner-gated on
  the hardware.
</content>
</invoke>
