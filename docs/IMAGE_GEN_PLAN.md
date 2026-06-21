# Image generation — build plan (local diffusion via ComfyUI)

A chat-driven image generator: the **jerv** chatbot gains two tools — `generate_image`
(text→image) and `edit_image` (image→image) — that drive a **localhost ComfyUI** running
**Qwen-Image-2512** (and the **Qwen-Image-Edit** sibling) on the owner's AMD Strix Halo
box (gfx1151). The result streams back **inline in the chat turn**, is stored as a
**chat-only artifact** in the blob store, and is **never** a note, never indexed into the
RAG corpus, and never a source of truth (invariant #7 untouched). This binds on top of
`docs/DEVELOPMENT.md`, `docs/PROCESS.md`, and the `CLAUDE.md` non-negotiables.

## Owner decisions (locked)

| Decision | Choice | Consequence |
|---|---|---|
| **Model** | Qwen-Image-2512, full bf16; Qwen-Image-Edit for edits | Apache-2.0, ungated, validated on the kyuz0 gfx1151 toolbox; fits the ~60GB budget at full precision |
| **Delivery** | **Synchronous** — the turn awaits the image and streams it inline (~15–40s) | No async job/delivery channel; ComfyUI serializes, fine for one owner |
| **Agent** | **jerv only**, direct execution (no per-image Proposal) | jerv reads no knowledge base, so nothing personal is in its context; prompts go only to **on-box** ComfyUI (no egress) |
| **Persistence** | **Chat-only artifact** — blob store + by-id URL; no gallery, not attachable to notes | Owner-only RLS (mirrors `wiki_*`), not domain-scoped; never in RAG |
| **Capabilities** | `generate_image` **and** `edit_image` (Qwen-Image-Edit) | `edit_image` needs an input image, resolved from a prior generation **or** a chat attachment |
| **Defaults** | no content filter; keep all blobs (content-addressed, dedup); aspect presets + seed/steps | single-owner box; cleanup deferred |
| **ComfyUI lifecycle** | host-managed (kyuz0 toolbox), **not** containerized by JBrain2 | one `comfyui_url` config; setup documented in `STRIX_HALO_SETUP.md` + `dev-setup.sh` |

### Permission-class decision (escalation-worthy — recorded)

`generate_image`/`edit_image` are placed in the **`web`** permission class (policy outcome
`direct`), **jerv-only via the `JERV_TOOLS` allowlist** — following the exact
`current_location` precedent (`agent/contracts.py`: a non-internet tool "in this class
purely for its gate"). Rationale: they must **run directly** (no Proposal) and only for
jerv, which is what the `web` gate already provides. They are **on-box** (localhost
ComfyUI) — *no* egress despite the class name; the registry's web-tool gate still requires
explicit opt-in, so the `curator` never gains them. The alternative (a new `local`/`compute`
PermissionClass + `DEFAULT_OWNER_POLICY` entry) is heavier surface for no behavioral gain
and is **not** taken; if the owner later wants `curator` image-gen, domain-scoping + a
dedicated class is the follow-up.

## Wave split

- **Wave G1 — backend foundation** (no GUI): the `jbrain.image_gen` ComfyUI client + Qwen
  workflows + `comfyui_url` config + `FakeImageGen`; the `generated_images` table
  (migration 0077) + repo + **RLS isolation test**.
- **Wave G2 — tools + serving** (no GUI): the `generate_image`/`edit_image` `.tool`
  sidecars + handlers, jerv allowlist wiring, the by-id PNG serving endpoint.
- **Wave G3 — the chat view** (GUI): the `generated_image` tool-view component. **Gated on
  the GUI mock round** (three interactive HTML mocks → owner choice → `docs/mocks/`) per
  `PROCESS.md` before any implementation.

Per `PROCESS.md`: each wave runs its tasks in parallel worktrees off a `wave-N` branch,
gets an **independent** per-task review and a per-wave (red-team, since G1 touches RLS)
review, and lands as **exactly one PR per wave**, CI green before merge, then the next wave
begins automatically.

---

## Wave G1 — backend foundation

### Migration 0077 (head is 0076) — one owner-only table

Generated images are **chat artifacts, not domain facts**, so the table is **owner-only**
(mirrors `wiki_articles`/`wiki_talk_*`: `app.is_owner()` USING+CHECK, FORCE RLS), **not**
domain-scoped. jerv runs `reads_knowledge_base=False` with empty read scopes, so there is
no domain to attribute anyway.

```
app.generated_images
  id            uuid pk default gen_random_uuid()
  blob_sha256   text NOT NULL                       -- the result PNG in the blob store
  kind          text NOT NULL CHECK (kind IN ('generate','edit'))
  model         text NOT NULL                       -- 'qwen-image-2512' | 'qwen-image-edit'
  prompt        text NOT NULL
  source_sha256 text                                -- input blob for edits; NULL for generate
  width         int  NOT NULL
  height        int  NOT NULL
  steps         int  NOT NULL
  seed          bigint NOT NULL                      -- the resolved seed (random → recorded for repeatability)
  created_at    timestamptz NOT NULL DEFAULT now()
  INDEX (created_at DESC)
```

ENABLE + FORCE RLS; `generated_images_owner` policy `USING (app.is_owner()) WITH CHECK
(app.is_owner())`; GRANT SELECT/INSERT/DELETE to `jbrain_app` (no UPDATE — rows are
immutable). Downgrade drops the table. ORM model `GeneratedImage` in `models/` mirroring
the migration (queries may be raw SQL; the repo keeps the model in sync).

### The image-gen service — `jbrain/image_gen/comfyui.py`

A **sibling adapter** to the LLM adapter (image generation is *not* an LLM call, so it does
not go through the LLM router — but it is the *only* path to the image model, mirroring
rule #1's spirit). Protocol + a fake; **all** HTTP via `httpx.AsyncClient` (already a dep —
**zero new runtime deps**).

```python
class ImageGen(Protocol):
    async def generate(self, spec: GenSpec) -> bytes: ...          # PNG bytes
    async def edit(self, spec: EditSpec, source: bytes) -> bytes: ...
```

`ComfyUiImageGen(base_url, client)`:
- `_upload_input(data: bytes) -> str` — `POST /upload/image` (edits only), returns the
  server-side filename to reference in the graph.
- `_submit(workflow: dict) -> str` — `POST /prompt`, returns `prompt_id`.
- `_await(prompt_id) -> bytes` — poll `GET /history/{prompt_id}` until the output node is
  present, then `GET /view?...` for the PNG. Bounded by an overall timeout (≈120s) and a
  poll interval; raises `ImageGenTimeout`/`ImageGenError` on failure (handlers turn these
  into a clean tool-error string — never a stack trace to the model).
- Workflow templates are **JSON graph files** (`workflows/qwen_image.json`,
  `workflows/qwen_image_edit.json`) with typed placeholders (positive prompt, seed, steps,
  width/height, and — for edit — the uploaded input-image node). The client fills slots; the
  graph shape is owned by the file, not the model.

`FakeImageGen` returns a tiny **valid PNG** (real magic bytes, so the blob store + serving
sniff path exercise the real code) for all tests — no live ComfyUI, no network (rule #5).

### Config

`comfyui_url: str = ""` in `config.py` (env `JBRAIN_COMFYUI_URL`), beside `local_llm_url`/
`embed_url`. **Empty disables the feature**: `main.py` constructs `ComfyUiImageGen` only when
set, and the tools are **absent from the registry** when it is unset (graceful degrade,
mirrors the provider-hidden-when-unkeyed pattern). The settings screen shows the image-gen
row only when configured.

### G1 tests
- **Integration (real PG):** `generated_images` **RLS isolation** — a non-owner/capability
  principal **sees no rows and cannot insert** (the per-new-table requirement); owner
  insert/select round-trips; immutability (no UPDATE grant) asserted.
- **Unit:** `ComfyUiImageGen` against a **mocked httpx transport** — submit→poll→view happy
  path; the edit input-upload path; timeout → `ImageGenTimeout`; a ComfyUI error payload →
  `ImageGenError`. `FakeImageGen` returns sniffable PNG bytes.
- ruff / ruff format / pyright green; coverage gate.

---

## Wave G2 — tools + serving

### `.tool` sidecars (`agent/tools/`)

`generate_image.tool` — `permission: web`, `cost_class: expensive`, `side_effecting: true`:
```
params:
  prompt:  {type: string}                       # required
  aspect:  {type: string, enum: [square, portrait, landscape]}   # → preset dims; default square
  steps:   {type: integer}                      # optional, sane default
  seed:    {type: integer}                      # optional; random when absent, recorded
required: [prompt]
```
`edit_image.tool` — same class/cost, plus the source selector:
```
params:
  prompt:               {type: string}          # the edit instruction; required
  source_image_id:      {type: string}          # a prior generated image
  source_attachment_id: {type: string}          # an image the owner attached this chat
  aspect/steps/seed: …
required: [prompt]                               # exactly one source_* required (validated in handler)
```
Prose bodies: what each does, that generation takes a moment, that the app renders the
image (the model must not paste URLs).

### Handlers — `agent/imagegentools.py`

`build_image_handlers(imagegen, blob_store, repo) -> dict[str, ToolHandler]`:
- `generate_image_tool`: resolve aspect→(w,h) and seed; `await imagegen.generate(...)`;
  `sha = await blob_store.put(png)`; insert a `kind='generate'` row under `ctx.session`;
  return `ToolOutput(summary, view=generated_image_view(row))`.
- `edit_image_tool`: require **exactly one** of `source_image_id` / `source_attachment_id`
  (else a clean error string); load the source bytes — by id from `generated_images`
  (RLS-scoped) **or** from the chat attachment blob (the existing `chat_attachments` path);
  `await imagegen.edit(..., source)`; store + insert `kind='edit'` with `source_sha256`;
  return the view.

`generated_image_view(row) -> ViewPayload(view="generated_image", surface="inline",
data={image_id, kind, prompt, width, height, model})` — **data-only**; the component builds
the `<img>` src from `image_id` (never a model-authored URL — invariant #9 / DESIGN.md
"Agent tool views").

Wire both into `build_registry()` and add `"generate_image"`/`"edit_image"` to
`JERV_TOOLS` in `agent/agents.py`. The registry's web-tool gate already restricts `web`
tools to agents that allowlist them, so `curator`/`teacher` never receive them.

### Serving endpoint — `api/images.py`

`GET /api/images/generated/{id}` (**OwnerDep**): look up the row (RLS owner-only → 404 when
absent), then `FileResponse(blob_store.path_for(blob_sha256), media_type=sniff_path(path))`
— reusing the existing `sniff_path` helper. No model/agent ever emits this URL; only the
view component constructs it from the id.

### G2 tests
- **Integration (real PG + `FakeImageGen`):** `generate_image` inserts a row + blob and
  returns a `generated_image` view; `edit_image` resolves a source **by generated id** and
  **by chat attachment id**, recording `source_sha256`; missing/both/unknown source → clean
  error, no row; `comfyui_url` unset → tools **absent** from the built registry.
- **Unit (TestClient, no Docker):** the serving endpoint — owner-gated (401/403), 404 on a
  missing id, correct sniffed media type; `JERV_TOOLS` contains both tools and `curator`'s
  offered set does not.
- ruff / pyright green; **security-100%** on the new endpoint + handlers.

---

## Wave G3 — the chat view (GUI gate)

**GUI gate (blocking, per `PROCESS.md`).** Before any code: **three interactive HTML mocks**
of the in-chat generated-image card (real, clickable, tokens-only per `DESIGN.md`),
presented to the owner to choose. Variation space: result-only card vs. result + collapsible
prompt/seed/regenerate affordance vs. before/after pair for edits. The chosen mock lands in
`docs/mocks/` and becomes the binding spec; the rejected two are retained in a
`…-README.md` as the record.

### Implementation (after the mock choice)
- `GeneratedImage` component added to the fixed `REGISTRY` in
  `frontend/src/agent/views/registry.tsx`, rendering from `{image_id, kind, prompt, width,
  height, model}`: it builds `src={/api/images/generated/${image_id}}`, sizes from
  width/height to avoid layout shift, and (per the chosen mock) shows prompt/seed and an
  edit's source. Tokens-only `.tv-genimg-*` classes; no model-authored markup.
- Mock-mode route in `mock.ts` + a fixture so dev exercises the card offline.

### G3 tests
- `registry.test.tsx`/a `GeneratedImage` test: renders the image with the by-id src, the
  prompt/meta per the chosen mock, the edit before/after if chosen, and that an **unknown
  view name still renders nothing** (existing invariant); the mock route round-trips.
- biome / tsc green.

---

## Docs (lands with the waves)
- `docs/STRIX_HALO_SETUP.md`: a ComfyUI + Qwen-Image-2512 / Qwen-Image-Edit section — kyuz0
  toolbox, the kernel/ROCm prereqs for stable gfx1151 image workflows, the bound localhost
  port → `JBRAIN_COMFYUI_URL`, and which workflow files JBrain2 posts.
- `docs/DESIGN.md`: record the `generated_image` tool-view (chosen mock, rationale, the two
  retained alternatives) per the GUI gate.
- `scripts/dev-setup.sh`: note the `JBRAIN_COMFYUI_URL` env + that ComfyUI is **host-managed**
  (no container, no new backend dep) — rule #8.
- `docs/README.md`: add this plan to "Active plan"; archive on completion.

## Non-negotiables check
1. **LLM adapter** — n/a: image generation is not an LLM call. It routes through the new
   `jbrain.image_gen` adapter (the sole path to the image model); **no provider SDK**, all
   HTTP via `httpx`. The LLM router is untouched.
2. **Storage abstraction** — result and input bytes go through `BlobStore.put/get/path_for`
   only; no raw paths.
3. **RLS** — `generated_images` is owner-only, FORCE RLS, enforced in Postgres, **with the
   mandatory isolation test**. (Owner-only, not domain-scoped, because chat artifacts are
   not domain facts; jerv has no domain scope.)
4. **Comments** — why-not-what, lean.
5. **Tests with the code** — 80% / security-100%; real PG via testcontainers; **ComfyUI
   faked** (`FakeImageGen`), no live calls/network.
6. **Conventional Commits**; one PR per wave; CI green before merge.
7. **Notes are the sole source of truth** — generated images are **chat artifacts**: never
   notes, never RAG-indexed, never citable. The wiki/knowledge base is untouched.
8. **`dev-setup.sh`** updated in the same wave as the `JBRAIN_COMFYUI_URL` / ComfyUI step.

## Open items (carried, not blocking)
- **Async delivery** — if generations later feel too slow to block a turn, revisit the
  `PgJobQueue` + `JobEnqueuedEvent` path (a live delivery channel). Out of scope here.
- **curator image-gen / domain-scoping** — would need a domain-scoped `generated_images`
  and a dedicated permission class; deferred with the permission-class decision above.
- **Gallery / note attachment** — explicitly out of v1 (chat-only artifact).
- **Retention/cleanup** — keep-all in v1; a sweep can come later (content-addressed, so
  dedup already bounds growth).
