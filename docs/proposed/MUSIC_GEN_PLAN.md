# Music generation on the ComfyUI service — build plan (proposed)

**Status: proposed / icebox.** Not on the roadmap, nothing built. Extends the shipped image
stack (`docs/IMAGE_GEN_PLAN.md`, `docs/IMAGE_GEN_SERVICE_PLAN.md`, `docs/IMAGE_LAUNCHER_PLAN.md`)
by teaching the **same opt-in `comfyui` compose service** to generate **music** — text/tags +
lyrics → a full song — with **ACE-Step 1.5 XL Turbo** as the model. Binds on top of
`docs/DEVELOPMENT.md`, `docs/PROCESS.md`, `docs/DESIGN.md`, and the `CLAUDE.md` non-negotiables.
When picked up, reconcile with the non-negotiables, get a `docs/ROADMAP.md` slot, and promote out
of `proposed/` (per `docs/proposed/README.md`).

## Why this exists / why it fits
The image feature is built as a **graph-agnostic driver over a model-managed ComfyUI**: a model is
a `(workflow JSON + node binding)` pair registered in a catalog, not a code path
(`backend/src/jbrain/image_gen/comfyui.py`, `…/catalog.py`). ComfyUI gained **native ACE-Step audio
nodes** (no custom-node install), and AMD has **validated ACE-Step 1.5 on Ryzen AI Max+ / Radeon
(gfx1151) over ROCm + ComfyUI** — our exact box. So music is an *extension of the existing seam*,
not a new subsystem: same container, same setup script, same gateway, same supervisor control. The
net-new surface is one audio workflow, an audio-aware output path in the driver, an artifact
table + view, and a screen/tool.

### The Strix-Halo angle (why the *largest* ACE model)
Open music models top out at **~4B params** — there is no 50 GB-class music model to fill the
128 GB pool the way Qwen-Image (~58 GB bf16) does. The box's payoff for music is **fidelity, not
size**: run the configuration that is a stretch on a 12–20 GB consumer GPU — **ACE-Step 1.5 XL
Turbo (4B DiT) + the largest compatible Qwen LM planner, bf16, no offload, no quantization** —
because we have the memory for it. That mirrors the image-side decision to ship **native bf16, not
fp8** (gfx1151 upcasts fp8 anyway: same RAM, less quality loss). Resident footprint is only
~20–25 GB, which also opens a **co-residency** option the image path can't take (Open Items).

## Proposed decisions (to confirm with the owner before promotion)
| Decision | Proposed choice |
|---|---|
| **Model** | **ACE-Step 1.5 XL Turbo** (4B DiT) + the largest compatible Qwen LM planner, **bf16**, split-file form (`diffusion_models` / `text_encoders` / `vae`) — the max-fidelity config the box affords |
| **Service shape** | **Reuse the existing `comfyui` compose profile** — no new service. Music models are new **catalog entries** the operator provisions |
| **Setup** | **No new script** — `scripts/comfyui-setup.sh` is already catalog-driven (downloads whatever files the catalog names into the ComfyUI subdirs); add `music` ids to it |
| **Driver** | **Extend `comfyui.py`** — add a `MusicSpec` + `generate_music()` and an **audio-aware output fetch** (scan the `audio` output key, not just `images`). The submit/poll/WebSocket plumbing is reused unchanged |
| **Artifact** | A **new owner-only `generated_audio` table** (parallel to `generated_images`, not a generalization — audio has duration/format/tags/lyrics, not width/height) + migration + RLS isolation test |
| **Output format** | **MP3** for the inline player (small, streams in the PWA over `<audio>`); FLAC optional later. ComfyUI's `SaveAudioMP3` node |
| **Surfaces** | A `generate_music` agent tool **and** a direct owner **MusicScreen** (the non-agent launcher path), mirroring `generate_image` + `ImageScreen` |
| **Memory** | **Reuse the proven time-share** (free LLM → render → free ComfyUI) initially; co-residency is a later optimization (Open Items) |
| **Host line** | Unchanged: kernel ≥ 6.18.4, `/dev/kfd`+`/dev/dri`, `HSA_OVERRIDE_GFX_VERSION=11.5.1`, ROCm ComfyUI image. The one risk is whether the pinned image carries the audio nodes + torchaudio (Wave M0) |

## The pattern being mirrored (grounded in shipped code)
- **Driver:** `image_gen/comfyui.py` — `_load_template` → `_fill_common` → `/prompt` → poll
  `/history/{id}` (or drive `/ws`) → fetch `/view`. Model→graph via `_GEN_GRAPHS` registry.
- **Catalog:** `image_gen/catalog.py` — `ImageModel` (id, label, kind, workflow, files[], size_gb,
  vram_gb, *_steps, recommended, note); `MODEL_SUBDIRS` already includes
  `diffusion_models/text_encoders/vae/loras/checkpoints`. Setup reads its JSON manifest.
- **Render core:** `image_gen/render.py` — `ImageRenderService` (resolve spec → `_free_local_llms`
  → drive model → `_free_comfyui_model` → blob put → RLS-scoped insert). Shared by tool + API.
- **Gateway:** `image_gen/gateway.py` — `status()`/`free()`/`interrupt()` over ComfyUI admin API
  (model-agnostic; **reused unchanged**).
- **Agent tool:** `agent/imagegentools.py` — thin handler, `_progress_callback` bridges driver
  ticks to the turn's progress sink, emits a data-only view (`generated_image_view`).
- **Direct API:** `api/images_render.py` (`/images/generate|edit`, gallery list/delete, `OwnerDep`
  + RLS) and `api/image_settings.py` (`/settings/image` snapshot, `free`, `interrupt`,
  `service/start|stop` via the supervisor).
- **Service wiring:** `main.py` builds the clients/services only when `comfyui_url` is set.
- **Frontend:** `screens/ImageScreen.tsx`; `api/client.ts` (`generateImage`, `listGeneratedImages`,
  `generatedImageUrl`, `getImageSettings`); `agent/views/registry.tsx` (`generated_image` →
  `ImageFrame`); `components/Launcher.tsx` (config-gated tile); `LLMSettingsScreen.tsx` drawer.

---

# Backend plan

## Wave M0 — host-validation spike (FIRST, blocking — needs the owner's box)
I cannot run gfx1151/ROCm here; this is the gating risk, exactly like the image G4 spike.
- **Confirm the pinned ComfyUI image has the audio path:** the native ACE-Step nodes
  (`TextEncodeAceStepAudio`, `EmptyAceStepLatentAudio`, `VAEDecodeAudio`, `SaveAudioMP3`) **and**
  `torchaudio` present in `docker.io/kyuz0/amd-strix-halo-comfyui`. If absent → bump the image tag
  to a build that includes them (or add a thin layer), then re-pin by digest in `.env`.
- **Provision + run ACE-Step 1.5 XL Turbo once** on the box; confirm GPU (not CPU) execution and
  that a ~30–60 s track renders. Capture **the exact HF repo paths + file sizes** for the XL split
  files (the standard-1.5 turbo file is `acestep_v1.5_turbo.safetensors`; the **XL** diffusion file
  and the largest Qwen LM encoder filename in `Comfy-Org/ace_step_1.5_ComfyUI_files` are
  **UNCONFIRMED** and must be read off the repo — same "repo/path UNCONFIRMED" caveat the catalog
  already uses for `qwen-image-edit`).
- **Export the API-format workflow JSON** (Dev mode → Save API Format) for the XL Turbo graph →
  becomes `workflows/ace_step_music.json`, node ids reconciled with the driver binding.
- **Output:** working image ref (digest), the confirmed file manifest, and the workflow JSON.
  Until these land, every music catalog entry ships `recommended: false`.

## Wave M1 — catalog + workflow + audio-aware driver
- **Catalog** (`image_gen/catalog.py`): add `kind="music"` and an `ace-step-xl` `ImageModel` (rename
  the dataclass's doc to "media model" or keep `ImageModel` — it's already generic enough; the
  `kind` field carries the distinction). Files = the XL DiT (`diffusion_models`), the Qwen LM
  planner (`text_encoders`), the ACE VAE (`vae`). `size_gb`/`vram_gb` ≈ 12 GB disk / ~22 GB resident
  (confirm M0). `workflow="ace_step_music.json"`. `recommended:false` until M0 validates.
  - `MODEL_SUBDIRS` is **unchanged** (all three subdirs already allowed). `scripts/comfyui-setup.sh`
    is **unchanged** (it downloads whatever the manifest names) — only its docs/recommended-set note
    gain the music ids.
- **Workflow template:** `image_gen/workflows/ace_step_music.json` (from M0). Graph shape: split
  loaders → `TextEncodeAceStepAudio` (tags + lyrics) → `EmptyAceStepLatentAudio` (seconds) →
  `KSampler` (seed/steps; cfg/shift authored) → `VAEDecodeAudio` → `SaveAudioMP3`.
- **Driver** (`image_gen/comfyui.py`):
  - New `MusicSpec(prompt_tags, lyrics, seconds, steps, seed, model, negative_tags="")` and
    `MusicBinding` (the tag node, lyrics key, the latent-audio node's `seconds`, the sampler).
  - `generate_music(spec, on_progress)` — mirrors `generate()`: load template, fill the music slots,
    `_run()`. The `_run`/`_run_ws`/`_submit`/poll plumbing is **reused as-is** (audio sampling emits
    the same `progress` frames; there are **no `b_preview` image frames**, so `preview` stays `None`
    throughout — the existing `_progress_callback` already handles that, driving just the step bar).
  - **Audio output fetch (the core change):** today `_poll_once` scans node outputs for an `images`
    array and `_fetch_view` GETs the PNG. Generalize the scan to also accept an **`audio`** array
    (`SaveAudio*` emits `{filename, subfolder, type}` under `audio`); `_fetch_view` already serves
    any `/view` file, so it's reused. Keep it a small union (`images` or `audio`), returning the ref
    — no behavioral change to the image path.
  - A `MusicGen` Protocol + the fake (`image_gen/fake.py`) gain `generate_music` so tests drive it
    with no network (DEVELOPMENT.md "no network in tests").
- **Config** (`config.py`): **no new var** — `comfyui_url`/`comfyui_enabled`/`comfyui_models` already
  gate and list provisioned ids; a music id in `comfyui_models` is the enable signal.
- **Tests:** catalog unit (manifest shape, subdirs); driver music path against `MockTransport`
  (fill slots, submit, poll the `audio` output, fetch bytes; interrupt/error/timeout paths);
  `bash -n` + dry-run of the setup script's music ids. **No live ComfyUI in CI** (host-validated seam).

## Wave M2 — artifact table + render core + agent tool
- **Migration (new, e.g. 0xxx):** `app.generated_audio`, owner-only, **immutable** chat-artifact
  table (mirrors `generated_images`): `id`, `blob_sha256`, `model`, `prompt_tags`, `lyrics`,
  `negative_tags`, `seconds`, `steps`, `seed bigint`, `format`, `created_at`. **RLS owner-only
  policy + the mandatory isolation test** (CLAUDE.md rule 3) — a non-owner session sees zero rows
  and cannot insert.
- **Model/repo** (`models/audio.py`): `GeneratedAudio` + `GeneratedAudioRepo` (`insert`/`get`/
  `list`/`delete`), taking a caller-supplied RLS-scoped session — a verbatim parallel of
  `models/images.py`. (`delete` leaves the blob — content-addressed/keep-all, same rationale.)
- **Render core** (`image_gen/render.py`): a `MusicRenderService` (or a `render_music()` method)
  reusing `_free_local_llms` → `generate_music` → `_free_comfyui_model` → blob put → RLS insert.
  Validation as typed exceptions (`RenderValidationError` for bad seconds/steps; reuse
  `ModelNotInstalledError` gated on `provisioned_models`).
- **Agent tool** (`agent/musicgentools.py`, sibling of `imagegentools.py`): `generate_music`
  (`web`-class, jerv-only, direct-exec; on-box, no egress). Args: `tags`/`prompt`, `lyrics`,
  `seconds`, optional `seed`, `negative_tags`. Resolves → drives the (faked-in-tests) service →
  stores the MP3 via the blob store → records one `generated_audio` row under the caller's RLS
  scope → returns a `generated_audio` **data-only view** (id, tags, seconds, seed, model — **no URL**;
  the app builds the `<audio>` src from the id, invariant #9). `_progress_callback` is reused.
  Failure → clean tool-error string. Wired only when a music model is provisioned (graceful degrade).
- **Tests:** repo + RLS isolation (security-100%); render service (time-share order, install-gating,
  validation); tool handler (happy path emits the view; bad args / interrupt / gateway-down → clean
  strings). ComfyUI faked.

## Wave M3 — direct owner API + settings surface
- **Direct render API** (`api/music_render.py`, sibling of `images_render.py`): `OwnerDep` + RLS.
  - `GET /music/generated` (gallery, always mounted) ; `POST /music/generate` (JSON tags/lyrics/
    seconds/seed) ; `DELETE /music/generated/{id}` ; `GET /music/generated/{id}` → **streams the
    audio bytes** with the right `Content-Type`/`Accept-Ranges` for `<audio>` (the bytes-by-id
    serve route, parallel to the image serve). Typed render errors → 400/409/502 exactly as images.
  - `generate` mounts only when `comfyui_url` is set (main.py gate); the list/serve routes are
    always available (a box keeps its past tracks).
- **Settings** (`api/image_settings.py` → keep one drawer): the `_snapshot` already lists the **whole
  catalog**; music models surface automatically once added to the catalog (their `kind` drives a
  `music` capability chip). `free`/`interrupt`/`service start|stop` are **model-agnostic and reused**.
  Decide cosmetic: one combined "On-box media" drawer vs. a music sub-section (DESIGN.md / Wave M4
  GUI gate). No new supervisor work — it already start/stops the `comfyui` service.
- **Wiring** (`main.py`): build `MusicGen`/`MusicRenderService`/`GeneratedAudioRepo` on the same
  `comfyui_url` gate; register the routers; pass the provisioned-model gate.
- **Tests:** route auth (owner-only security-100%), validation 400s, gateway-down 502, byte-serve
  range/Content-Type, list/delete RLS scoping.

---

# Frontend plan

## Wave M4 — the UI (GUI gate, then build)
- **GUI gate (DESIGN.md / PROCESS.md):** static mocks of (a) the **MusicScreen** launcher and (b) the
  **inline `generated_audio` chat card** land in `docs/mocks/` for the owner to pick **before** code,
  exactly as the image launcher/live work did.
- **MusicScreen** (`screens/MusicScreen.tsx`, sibling of `ImageScreen.tsx`): a composer form — **tags/
  style** field, **lyrics** textarea (with `[verse]`/`[chorus]`/`[bridge]` hint), **duration**
  slider, optional **seed**, **negative tags** — a render state machine (`idle → queued → rendering
  (step bar) → done/error`) reusing the live step ticks, and a **gallery** of past tracks
  (newest-first, deletable, playable). Result + gallery items render an inline **audio player**.
- **API client** (`api/client.ts`): `generateMusic(req)`, `listGeneratedAudio()`,
  `deleteGeneratedAudio(id)`, and `generatedAudioUrl(id)` (the `/api/music/generated/{id}` builder —
  **never hand-authored**, invariant #9). Types: `GenerateMusicRequest`, `GeneratedAudioOut`
  (id, tags, lyrics, seconds, model, seed, created_at). Mirror the camelCase wire (`negativeTags`).
  Mock-mode fixtures (`api/mock.ts`) so the screen renders offline with sample tracks.
- **Inline view** (`agent/views/registry.tsx`): a `generated_audio` view → a small **`AudioCard`**
  component (waveform/placeholder + `<audio controls>` built from `generatedAudioUrl(image_id-analog)`,
  tags/seconds/seed meta, a copy-seed action). Registered in the view `REGISTRY`; unknown views still
  render nothing. The live in-flight card shows the step bar (no preview frame for audio).
- **Launcher** (`components/Launcher.tsx`): a config-gated **"Music"** tile (icon + `target:"music"`),
  shown only when `getImageSettings()` reports a provisioned music model — same one-fetch gate the
  Image tile uses, so it never flashes on a box without it.
- **Settings drawer** (`LLMSettingsScreen.tsx`): the music models appear in the existing on-box
  drawer from the snapshot; add a **`music` capability chip** (alongside generate/edit) and let the
  shared memory meter show the music model's resident segment. No new fetch — the snapshot already
  carries it.
- **Tests:** component/render tests (form → request shape, state machine, gallery actions, audio
  card), mock-mode fixtures, the launcher gate.

## Wave M5 — optional follow-ons (deferred)
- **Stable Audio Open** as a lightweight **SFX / short-loop** catalog entry (native ComfyUI audio
  support, ~5 GB, no vocals) — the "DreamShaper of audio," a second `(JSON + binding)` pair.
- **Audio-to-audio** (continue / remix / style-transfer from an uploaded clip) — the music analogue
  of `edit_image`, reusing the upload + reference plumbing.
- **Co-residency optimization** (see Open Items): skip `_free_local_llms` for music when the resident
  LLM set + ~22 GB music model fits the pool, so a track doesn't pay a cold LLM reload.

---

## Non-negotiables check (CLAUDE.md)
1. **LLM adapter** — n/a (audio generation isn't an LLM call); the ACE-Step Qwen *planner* runs
   inside ComfyUI, reached only by POSTing a graph — no provider SDK, same as the image side.
2. **Storage** — result MP3 + any uploaded source go through the `BlobStore` (rule 2); weight files
   are infra on the existing read-only `comfyui-models` mount (the documented `local_weights`
   exception), not the storage abstraction.
3. **RLS** — `generated_audio` is owner-only with a Postgres policy **and a new isolation test**;
   all render/list/delete routes are `OwnerDep` + RLS-scoped; the supervisor still holds the Docker
   socket, the api never does.
4–6. Comments why-not-what; tests land with code (80% backend / **security paths 100%**, real
   Postgres via testcontainers, ComfyUI faked); Conventional Commits, branch + PR per wave, CI green.
7. **Generated tracks are chat artifacts only** — never notes, never RAG-indexed (mirrors
   `generated_images`).
8. `scripts/dev-setup.sh` stays host-managed; the music setup step rides the existing
   `scripts/comfyui-setup.sh` (catalog-driven) + the `STRIX_HALO_SETUP.md` runbook — updated in the
   same PR as the new dependency/step (rule 8).

## Open items / risks
- **M0 host-validation is the gate.** The pinned ComfyUI image carrying the ACE-Step audio nodes +
  `torchaudio`, and the exact **XL** split-file repo paths/sizes, are unconfirmed from here — both
  need the owner's box. Until then, music ships `recommended:false`.
- **Output node naming** — confirm the ComfyUI audio output key (`audio`) and the `SaveAudioMP3`
  (vs `SaveAudio`/`SaveAudioOpus`) node names against the running build during M0; the driver's
  output-scan generalization keys on it.
- **Co-residency vs. time-share** — reusing the image time-share is the safe default but makes every
  track pay a cold LLM reload. Because the XL music model is only ~22 GB, the box *can* keep it
  resident alongside the ~91 GB LLM set (under the ~124 GB pool, tight with context) — worth
  measuring on-box and switching the music path to skip the unload (M5).
- **Duration/step bands** — ACE-Step XL Turbo's step/cfg sweet spot and the max practical track
  length on the iGPU need tuning on the real box (authored into the workflow + the seconds clamp).
- **`ImageModel`/`ImageRenderService` naming** — adding `kind="music"` stretches "image"-named types.
  Decide during M1 whether to rename to a neutral "media" vocabulary or keep the names and lean on
  `kind` (lower-risk; the dataclass is already generic).
