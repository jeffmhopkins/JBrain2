# Image launcher — build plan (standalone direct generate/edit screen)

A **card-launcher destination** for on-box image generation/editing that drives ComfyUI
**directly**, so the **language models stay unloaded**. Today image gen only exists as jerv
tool calls (`generate_image`/`edit_image`, `docs/IMAGE_GEN_PLAN.md`), which require the LLM
resident; this screen is the non-agent path — "I just want a picture, don't wake the brain."

The GUI is settled: a four-way mock review chose **B + the gallery shortcut**
(`docs/mocks/image-launcher/launcher-b-gallery.html`; rivals + rationale in that directory's
`README.md`; recorded in `docs/DESIGN.md` "The image launcher"). This binds on top of
`docs/DEVELOPMENT.md`, `docs/PROCESS.md`, and the `CLAUDE.md` non-negotiables.

## Owner decisions (locked)

| Decision | Choice | Consequence |
|---|---|---|
| **GUI** | B (segmented Generate \| Edit form) + a gallery shortcut (image-only pinboard) | Binding spec = the chosen mock; violet image accent; not a chat surface |
| **Path** | **Direct, non-agent** — the screen calls a render API, jerv is not involved | The LLM stays unloaded; the point of the surface |
| **Persistence** | Reuse the existing owner-only `generated_images` table (Wave G1) | Screen renders and jerv renders share one gallery; owner-only RLS, never a note, never RAG |
| **Edit source** | a prior render (by id) **or** an uploaded image; up to 2 references | Upload path needs size-cap + magic-byte sniff (reuse `api/images.py` helpers) |
| **Config** | the existing knobs — speed/aspect/resolution/steps/seed/negative | Same resolution/seed/steps/speed→model logic as the jerv tools (extracted, not re-implemented) |
| **Sequencing** | **Mock-first**: build the screen against the mock client, owner-approve, then backend | Per the binding UI process (`docs/DESIGN.md` "UI development process") |

### Escalation-worthy (recorded)

The direct render endpoints are a **new non-agent surface that drives ComfyUI renders**. The
original image-gen feature deliberately kept generation **jerv-only** behind the `web` tool gate
(`docs/IMAGE_GEN_PLAN.md` permission-class decision). This plan adds an owner-authenticated HTTP
path because the whole value is bypassing the agent (LLM unloaded). It is consistent with the
existing design: `generated_images` is already **owner-only** (not domain-scoped), so the
endpoints are `OwnerDep` + RLS, never reachable by a scoped token. The render logic is **extracted
and shared** with the jerv handlers so behavior never diverges. Flagged for owner awareness before
the backend wave lands.

## Wave split

- **Wave L1 — the screen, mock mode (GUI).** The `ImageScreen` implementing the chosen mock,
  the card-launcher "Image" tile, the typed client methods + **mock fixtures** (`mock.ts`), the
  shared types, and frontend tests. **No backend.** Ends at owner approval of the working mocked
  UI (the binding process gate).
- **Wave L2 — backend refactor (no behavior change).** Extract the render logic now embedded in
  `agent/imagegentools.py::build_image_handlers` (aspect/resolution/seed/steps resolution,
  speed→model + install gating, LLM-unload → render → blob put → row insert) into a reusable
  `image_gen` render service that the jerv handlers call unchanged. Pure refactor + its tests;
  the agent path is identical afterward.
- **Wave L3 — the direct render API.** Owner-only `POST /api/images/generate`,
  `POST /api/images/edit` (multipart for an uploaded source + references), and
  `GET /api/images/generated` (the gallery list), all calling the L2 service; wire the screen's
  real client over the mock. RLS owner-only, **security-100%**, the new-table isolation already
  covered by Wave G1's test (extended for the list/insert-via-API paths).

Per `PROCESS.md`: each wave is one PR, tests in the same PR, CI green before merge.

---

## Wave L1 — the screen (mock mode)

- `frontend/src/screens/ImageScreen.tsx` — implements `launcher-b-gallery.html`: the
  Generate | Edit segmented form, the collapsible config card (speed/aspect/resolution/steps-
  with-lock/negative/seed), the edit source dropzone + "pick from gallery" + 2 reference slots,
  the synchronous render-state sequence, the result with meta + actions, and the gallery overlay
  (image-only masonry, live count, tap → large view → use as edit source). Tokens/classes match
  the app (violet accent); honors reduced motion.
- **Card launcher**: add an **Image** tile (AUTHORING group) routing to the screen, following the
  existing tile/route declaration. (Exact registration per the frontend conventions map.)
- **Client + mock**: add typed client methods `generateImage(spec)`, `editImage(spec, source,
  refs)`, `listGeneratedImages()` in `api/client.ts`; back them in `api/mock.ts` with fixtures —
  a seeded gallery, a fake render that returns a placeholder after a short delay, varied
  dimensions, and empty/error states. Mock images stand in for the by-id production source.
- **Types**: a `GeneratedImageSummary` (id, kind, width, height, model, seed, prompt, createdAt)
  + the generate/edit request shapes, beside the other API types.
- **Tests** (vitest + testing-library): segment switch swaps panels; speed≠quality locks steps;
  a render adds a tile to the gallery and shows meta; "use as edit source" populates the edit
  panel; empty-gallery state; the mock client round-trips. biome + tsc green.

## Wave L2 — backend render service (refactor)

- New `image_gen` service object (e.g. `image_gen/service.py`) exposing
  `generate(spec-ish) -> GeneratedImage row` and `edit(...) -> row` that own: dims/seed/steps
  resolution, speed→model + `provisioned_models` install gating, `_free_local_llms` →
  `imagegen.generate/edit` → `_free_comfyui_model`, blob put, and the RLS-scoped row insert. It
  takes an explicit RLS context (not a `ToolContext`) so both callers supply their own.
- `build_image_handlers` becomes a thin adapter over the service (same returns/views, same
  error strings to the model). The constants (`_GEN_SPEEDS`, `_ASPECTS`, `_RESOLUTIONS`, …) move
  to the service module; the handler imports them.
- **Tests**: the existing `test_imagegentools*` keep passing unchanged (the agent path is
  behavior-identical); new direct unit tests for the service (dims/seed/steps/speed gating, the
  unload calls, the error mapping) with `FakeImageGen`.

## Wave L3 — the direct render API

- `api/images.py` (or a sibling router): `POST /images/generate` (JSON spec),
  `POST /images/edit` (multipart: source image bytes **or** a `source_image_id`, + up to 2
  reference parts/ids, + the spec fields), `GET /images/generated` (the gallery list, newest
  first, owner-only). All `OwnerDep`, RLS-scoped via `scoped_session(ctx_for(owner))`. Upload
  bytes go through the blob store with the `sniff_image_type` + size-cap guards already in
  `api/images.py`.
- The screen's client swaps from mock to the real endpoints; the gallery lists real rows; the
  serve-by-id route (`GET /images/generated/{id}`, Wave G2) renders every tile and the result.
- **Tests** (real PG + `FakeImageGen`): generate inserts a row + blob and returns the summary;
  edit by uploaded source and by `source_image_id` records `source_sha256`; the list is
  owner-scoped (a non-owner sees nothing — RLS); unset `comfyui_url` → endpoints **absent/404**
  (graceful degrade, mirrors the tool-registry omission). **security-100%** on the new routes.

## Non-negotiables check
1. **LLM adapter** — n/a (image gen isn't an LLM call; routes through `jbrain.image_gen`). The
   direct API does **not** touch the LLM router; it unloads local LLMs before a render (the point).
2. **Storage abstraction** — all result/upload/source bytes via `BlobStore`; no raw paths.
3. **RLS** — reuses owner-only `generated_images` (FORCE RLS, Wave G1 isolation test); the API is
   `OwnerDep` + RLS-scoped, with list/insert-via-API covered.
4. **Comments** — why-not-what, lean.
5. **Tests with the code** — 80% / security-100%; real PG via testcontainers; ComfyUI faked.
6. **Conventional Commits**; one PR per wave; CI green before merge.
7. **Notes are the sole source of truth** — generated images stay chat/owner artifacts: never
   notes, never RAG, never citable. Untouched by this screen.
8. **`dev-setup.sh`** — no new dep expected (reuses ComfyUI + `JBRAIN_COMFYUI_URL`); update only
   if a setup step changes.

## Open items (carried)
- **Delete / retention on the gallery** — the mock shows view + use-as-source; a delete action
  (owner-only `DELETE`) can land with L3 or after. Keep-all otherwise (content-addressed dedup).
- **Async/queue** — renders stay synchronous (one owner, ComfyUI serializes); revisit only if it
  feels slow, per the IMAGE_GEN_PLAN open item.
