# Video/Image Inspection Tools — Design Spec

> **Status:** In progress · **Last verified:** 2026-07-19 · **Waves:** V0✅ V1✅ V2✅ V3✅ V4✅ V5✅ V6◻️ (all code + steering docs shipped on-branch; on-box end-to-end sign-off against the live VL/ffmpeg/ComfyUI models pending — CI covers the logic via fakes + real-Postgres testcontainers)

> Reconciled with the root `CLAUDE.md` non-negotiables — every VLM call through
> the LLM adapter (rule 1), every blob through the storage abstraction (rule 2),
> the new image rows RLS-scoped with an isolation test (rule 3), and the two new
> outbound legs (`fetch_image`, the `grab_frame` URL path) held to the
> jerv-sandbox egress discipline (invariant #9), exactly as `analyze_stream` is.

> **v2 — reconciled with a four-lens review** (codebase-accuracy, security,
> architecture, process) on 2026-07-19. The review overturned the original B1
> diagnosis (§1), replaced the `kind`-overload storage cut with a `provenance`
> column (§3), reshaped `compare_images` from an a/b object into a list contract
> (§4), corrected the image-validation / decompression-bomb / redirect-guard
> primitives (§5), and re-waved `fetch_bytes` + the two-image spike (§6). The
> compare contract shape (open decision 1) was **confirmed list-based** by the
> owner on 2026-07-19.

Give jerv the ability to **look at a specific still** — from a video or the web —
and to **compare images**, so a visual question is answered from pixels it
actually saw rather than from a plausible guess.

---

## 1. Why — the failure this fixes

On 2026-07-19 the owner asked jerv (agent session `fa4a462a`):

> "Can you find the video from my library when Luke talks about his Metropolis,
> and a screenshot of the video from that time, and compare it to online pictures
> of the metropolix and metropolis modules from intellijel to determine if it was
> a tts issue?"

jerv made **20 tool calls** and produced a confident, largely **fabricated**
answer — a comparison table of "the video frame" against "online product photos"
with invented Reverb/Intellijel image URLs and invented visual traits, concluding
"a mis-heard name (TTS issue)." It never saw the frame or any photo. The trace:

| Sub-task | What ran | Outcome |
|---|---|---|
| Find the video | `search_external_video`, `read_external_video` | ✅ found *"Modular Jam on a Rainy Day"* (`iTjNgeuqoA8`), got the transcript |
| Screenshot at the moment | `analyze_stream mode=single seek=164`, then `seek=165` | ❌ both returned **"completely black screen"** |
| Fallback | `analyze_stream mode=window seek=160 window_s=10` | ⚠️ described a Eurorack rack, but never resolved *Metropolis vs Metropolix* |
| Compare to online photos | `web_search` + `web_fetch` ×many | ❌ `web_fetch` returns **text only** — no photo was ever seen |

Three capability gaps and one bug, each independently sufficient to sink the task:

- **G1 — no usable still from a video.** `analyze_stream` returns a text caption
  and an owner-facing card; jerv gets nothing it can re-examine, hand to another
  tool, or compare. There is no "give me the actual image at time T."
- **G2 — jerv is blind to web images.** `web_fetch` (`agent/webtools.py`) refuses a
  non-textual body (`_is_textual`, `web/fetch.py`) and returns readable text. Asked
  to compare against *online* photos, jerv had no way to see them — so it invented
  them. This is the most dangerous gap: it converts "I can't" into a fabricated
  "here's the comparison."
- **G3 — no image comparison.** `analyze_image` takes exactly one source
  (`agent/imagegentools.py`, `_source_bytes` rejects two). Even with both images in
  hand there is no compare primitive.
- **B1 — `single`-mode grab ignores `seek` and always samples t=0.**
  **(Root cause corrected in v2.)** `analyze_stream mode=single` dispatches through
  `ingest/stream_analysis.py` `sample_for_mode`, which calls the sampler with
  `frames=1, window_s=0.0` and **passes no `seek_s`** — so a single grab always
  reads the frame at t=0 (a YouTube intro/fade → black), regardless of the `seek`
  the model sent. The `window` branch one line below *does* thread `seek_s`, which
  is the entire reason `window seek=160` showed content while `single seek=164/165`
  were black. The sidecar (`analyze_stream.tool`) documents `seek` as applying to
  "single/window mode," so jerv used the tool exactly as documented and got t=0.
  The original v1 diagnosis (a pre-keyframe decode-runway problem in
  `_extract_frames`/`_grab_one`) was **wrong for the exercised path**: with
  `seek=0` no `-ss` is even emitted, and `_grab_one` is only reached by full mode.
  The fix is to **thread `seek_s` through the single-mode dispatch**; the
  accurate-seek/luma-retry ideas are worthwhile *hardening once seek is honored*,
  not the root cause.

The owner also asked for one ergonomic control: **an analyze-video/analyze-stream
call that does its work but does _not_ render its big scrubbing card in chat** —
because when a video read is an intermediate step toward the real answer, the card
is noise.

## 2. What we build

Two new jerv tools + one widened tool + a display flag + a bug fix. All are
`web`-class, jerv-only, on-box-orchestrated (the `analyze_stream`/image-gen
posture); each drops from the registry when its backing capability is absent
(graceful degrade).

| # | Change | Closes | Backing |
|---|---|---|---|
| T1 | **`grab_frame`** — extract a still at time T from a video URL *or* chat attachment; persist it as a first-class image; return its `image_id`. Optional inline `question` (grab **and** vision-read in one hop) and optional `n` (2–3 stills around T) | G1, B1, chain length | ffmpeg (+ yt-dlp for the URL path) |
| T2 | **`fetch_image`** — fetch an image *URL* through the per-hop SSRF guard, validate it is really an image, downscale, persist it | G2 | the shared web fetcher |
| T3 | **`analyze_image` widened to 2..N sources** (+ a thin `compare_images` sidecar over the same handler) — a compare is analyze-image with two sources and a compare-framed prompt; **always emits a side-by-side artifact** the owner can see | G3, transparency | the `agent.vision` route |
| D1 | **`show: false`** on `analyze_video` / `analyze_stream` / `grab_frame` — run the analysis, suppress the inline card | ergonomics | none (drop the view) |
| B1 | **Honor `seek` in single-mode grab** (+ robust settled-frame hardening) | B1 | ffmpeg |

The corrected flow for the original request:
`read_external_video` → `grab_frame(url, seek=164, question="what sequencer module
is this? read any panel text")` (one hop: still **and** caption) → `fetch_image(<a
metropolis photo>)` + `fetch_image(<a metropolix photo>)` → `analyze_image([frame,
photo_a, photo_b], "which does the frame match? note panel-layout differences")` —
which renders a side-by-side the owner sees. Every claim grounded in images jerv
actually saw, and the owner can verify each one.

## 3. Storage — grabbed and fetched images are first-class chat images

`analyze_image` resolves a `source_image_id` by looking it up in
`app.generated_images` (`models/images.py`, owner-only RLS, mirrors `wiki_*`). For a
grabbed/fetched still to be re-examinable **by id**, it must live in that same
lookup space. Resolution is kind-agnostic (`repo.get` by id), so a grabbed/fetched
row resolves unchanged once it is a `generated_images` row.

**Decision: reuse `app.generated_images`, but add a nullable `provenance` column —
do NOT overload `kind`.** The review flagged that `kind` is **behaviour-bearing**,
not descriptive: `kind == "edit"` drives the before/after card view and the
`/images/{id}/source` route (`api/images.py`). Frame/fetched stills are *provenance*
(where the pixels came from), not a new behaviour, so:

- Keep `kind ∈ {generate, edit}` unchanged (no CHECK-constraint migration on the
  behaviour column, no re-audit of every `kind ==` site).
- Add a **nullable `provenance` column** (`'ffmpeg' | 'web_fetch' | NULL`) via a
  reversible Alembic migration (`provenance` starts NULL for existing rows). A
  grabbed frame is `kind='generate', provenance='ffmpeg'`; a fetched image is
  `kind='generate', provenance='web_fetch'`. `model` records the concrete source
  (`ffmpeg`/`web_fetch`), `prompt` the human origin (source URL + `t=…s`, or the
  fetched URL), `source_sha256` NULL, `steps`/`seed` `0` (valid non-null Integers).

This inherits the existing `generated_image` card + `_resolve_source` + blob/thumb
serving, but the review found **two surfaces that misrender on day one** and must be
handled **in V1, not "later":**

- **Gallery.** `GET /images/generated` (`api/images_render.py`) lists *all* rows
  with no filter, so a fetched Intellijel product photo would appear as a gallery
  tile. → V1 adds a `provenance IS NULL` (owner-generated only) filter to the
  gallery query.
- **In-chat card.** `registry.tsx` `GeneratedImage` renders `${width} × ${height} ·
  seed ${seed} · ${model}` — a fetched row would read "· seed 0 · web_fetch." → V1
  makes the card provenance-aware: suppress `seed`/`steps` for non-generate
  provenance and label the origin honestly ("grabbed from video" / "fetched from
  web"), never "generated."

The blob goes through `BlobStore` (rule 2); the row is written on the caller's
RLS-scoped session (rule 3). **Note (corrected in v2):** `generated_images` is
owner-**global**, not per-chat — any jerv turn with a valid uuid resolves any owner
image row. The tool contracts should say "an image id from **this owner's** images,"
and the real boundary is uuid-unguessability, not chat scoping. (A per-session scope
column is a possible later hardening — open decision 4.)

**Alternative considered and rejected:** a dedicated `app.chat_images` table. The
killer isn't semantics, it's the **second id-space** every resolver
(`analyze_image`, the card, the serve routes) would have to learn — content-addressed
blobs already give dedup/shared storage. The `provenance` column is the smaller,
safer cut.

## 4. The tools

### T1 · `grab_frame`

Extract one (or a few) stills and persist them. The still-image sibling of
`analyze_stream` (URL) and `analyze_video` (attachment), but it returns a **reusable
image id**, not a text caption.

```
name: grab_frame           permission: web    cost_class: standard
params:
  url                 string   a video URL (yt-dlp-resolvable) — give this OR source_attachment_id
  source_attachment_id string  a video the owner attached this chat — give this OR url
  seek                number   seconds into the video for the still (VOD only; ignored for a live edge)
  n                   integer  optional: grab this many stills (1–3) around `seek`, return all ids (default 1)
  question            string   optional: also run a vision read with this prompt and return the caption + id in ONE hop
  show                boolean  render the still(s) as a card to the owner (default true)
required: exactly one of {url, source_attachment_id}
```

- **URL path:** `resolve_stream` (SSRF-guarded, `stream.py`) → a robust single grab
  at `seek` (§B1). Second sanctioned outbound leg, same discipline as
  `analyze_stream`; refuse a live stream (`resolved.is_live`) with "no fixed
  timestamp on a live edge — use analyze_stream."
- **Attachment path (net-new primitive, not a one-liner).** `jbrain.media.sample_frames`
  samples *across the whole clip* with dedup — there is **no** seek-to-exact-T grab
  on the attachment side. Reuse the **fixed** `stream.py` grab pointed at the
  attachment's bytes written to a temp file (the grab already accepts a local file
  path via `_input_guard_args`), not `sample_frames`. Called out as a real V2
  sub-task.
- **`question` (chain-collapse).** The overwhelmingly common next step is
  `analyze_image` on the grabbed still. When `question` is set, `grab_frame` grabs,
  persists, **and** runs the `agent.vision` read inline, returning caption + id in
  one hop — this *is* the "answer a visual question about a video moment"
  affordance, without a new tool, and it shortens the five-call happy path the
  review flagged as fragile on gpt-oss.
- **`n` (robustness).** Part of the original failure was guessing the exact usable
  second. `n=2..3` grabs a few stills around `seek` (deduped) so a single bad frame
  doesn't force a re-call.
- Persist each JPEG via `BlobStore`; insert a `generated_images` row
  (`provenance='ffmpeg'`); return `ToolOutput(summary, view=…)` — or no view when
  `show=false`. Apply the §5 pixel cap on the decoded still.
- Caps: reuse the stream height cap and the `analyze_video` byte ceiling.

### T2 · `fetch_image`

Fetch an image URL and persist it, so jerv can *see* a web image.

```
name: fetch_image          permission: web    cost_class: standard
params:
  url    string   the image URL to fetch (e.g. a product photo found via web_search)
  show   boolean  render the fetched image as a card to the owner (default true)
required: [url]
```

- **Fetch — new `fetch_bytes` path, redirect-safe (corrected in v2).** `WebFetcher`
  today returns text only and needs a bytes path. It **must reuse the existing
  per-hop guard loop** (`_get_following_safe_redirects` in `web/fetch.py`:
  auto-redirect OFF, `guard_public_host` re-run at *every* hop, `_MAX_REDIRECTS`
  ceiling) — a one-shot guard + `follow_redirects=True` would let a public host
  30x to `169.254.169.254`/`db:5432` and then we'd persist those bytes. Only a final
  hop whose host passed the guard is read. Raise the fetch byte cap for this path
  (the text path's `_MAX_BYTES = 2 MB` is tight for a product photo).
- **Validate it is really an image (corrected in v2).** Use
  `api/images.py:sniff_image_type` (returns `None` on an unrecognised header), **not**
  `imagegentools._sniff_media_type` (which falls through to `"image/png"` for *any*
  bytes and so can never reject). `None` → clean tool error; an HTML error page or a
  polyglot never reaches the VLM as "an image."
- **Bound decoded pixels (corrected in v2 — decompression-bomb).** The byte cap
  bounds only *encoded* size; a 2 MB flat PNG decodes to gigapixels. Read dimensions
  from the header before full decode, reject over an explicit pixel ceiling, set
  `Image.MAX_IMAGE_PIXELS`, and route the bytes through
  `ingest/imageprep.py:downscale_for_vision` before any decode/dedup/VLM step. (This
  cap applies equally to `grab_frame`'s attachment decode and, ideally, to
  `analyze_image` generally — today the vision path base64s raw bytes with no
  downscale.)
- **Dep plumbing (corrected in v2).** `fetch_image` persists a row + blob, so it
  needs `WebFetcher` **and** `BlobStore` + `GeneratedImageRepo` + the session
  `maker` — none of which `build_web_handlers` receives today. Thread them
  (expanded `build_web_handlers` or a dedicated builder). Real plumbing, not a
  drop-in.
- Persist (`provenance='web_fetch'`, `model='web_fetch'`, `prompt=url`), return the
  image id + card (unless `show=false`). Surface the origin URL as a `WebSource` so
  it is a real citation, not model-authored prose (explicit V3 plumbing).

### T3 · `analyze_image` widened to 2..N sources (+ `compare_images` sidecar)

**Reshaped in v2.** The original a/b-sides object (`image_id_a`,
`attachment_id_a`, `image_id_b`, `attachment_id_b` with a cross-field pairing rule)
is exactly the "many-optional-fields object" shape the `analyze_stream.tool` sidecar
documents as **deterministically segfaulting** the gpt-oss harmony tool-grammar
builder. The repo already has the right idiom: `edit_image` handles "one primary + N
more" with `reference_image_ids: []` / `reference_attachment_ids: []` lists, a
`MAX_EDIT_IMAGES` cap, and the shared source resolver.

- **Widen `analyze_image`** to accept a **list** of 2..N sources (reusing
  `edit_image`'s `reference_*` list shape and the resolver loop verbatim), plus the
  existing single-source form. A compare is analyze-image over two sources with a
  compare-framed prompt — which also answers "N-way compare" for free.
- **Keep a `compare_images` sidecar** for discoverability (the owner asked for the
  verb), implemented as a thin wrapper over the **same** list-based handler — never
  the a/b object. *(Owner-confirmed list-based, 2026-07-19: the "dedicated tool"
  choice is preserved as a name, but the contract is a list, not paired fields.)*
- **Always emit a side-by-side artifact (corrected in v2).** The original "compare
  has no view, the owner already saw each image" reasoning breaks against §2's own
  flow, where the grabs run `show:false` — the owner would see *nothing* and get a
  confident verdict, re-creating the exact failure this plan exists to kill. So a
  multi-source analyze/compare **stitches its inputs into one side-by-side image**
  (built with `jbrain.media`) and renders it as the card — the same stitch the
  two-image-VLM fallback needs anyway (§6 V1 spike). The owner can always verify
  what jerv compared.
- **Resolver sharing (corrected in v2).** `_resolve_source` is a **private closure**
  inside `build_image_handlers` (capturing `maker`/`repo`/`blob_store`/`attachments`);
  it must be hoisted to a module-level helper to be shared.
- **Wiring / gating (corrected in v2).** `analyze_image` today is gated on ComfyUI
  (`OPTIONAL_IMAGE_TOOLS`), yet a vision read needs only the `agent.vision` router.
  The multi-source read + `compare_images` must be wired against **router**
  availability, not the image-gen gate, or they wrongly vanish on a box without
  ComfyUI. (Consider un-gating the vision-read path from ComfyUI as part of this.)
- **Vision route reality (corrected in v2).** `agent.vision` defaults to
  **`xai:grok-4.3`** (`llm/router.py`), i.e. **remote** unless the operator repoints
  it local — so "on-box VL model / only pixels stay local" is inaccurate; pixels go
  to the configured vision provider. Two images are structurally supported on the
  wire (the adapter forwards an `images` sequence with no cap); whether the on-box VL
  model accepts two is the V1 spike (§6). The returned VLM text is **untrusted
  web-derived content** re-entering jerv's turn (an adversarial image can steer it) —
  classify it like `web_fetch` output, not as trusted, even though `_VISION_SYSTEM`
  frames the *image* as data-not-instructions.

### D1 · `show: false` — suppress the inline card

Add an optional `show` boolean (default `true`) to `analyze_video`, `analyze_stream`,
and `grab_frame`. When `false`, the handler returns its summary `ToolOutput` **with
no `view`** — `ViewPayload` is optional on `ToolOutput` (`agent/loop.py`), a `None`
view emits no `ToolViewEvent`, and the frontend builds cards only from those events
(verified end-to-end: `loop.py` emit sites, `transcript.ts`, `registry.tsx`). The
model still reads the full summary; the owner's chat isn't cluttered by an
intermediate read, and the tool-result chip still shows the call happened (so
suppression is not *invisible* — a transparency point behind §T3's always-visible
compare artifact). The deferred `analyze_stream` path still emits its `task_status`
card (a long job needs its progress affordance); `show` governs only the **final**
`video_analysis`/`generated_image` card.

### B1 · Honor `seek` in single-mode grab (bug fix, ships first, stands alone)

**Corrected in v2 — the fix is in the dispatch, not the sampler internals.**

- **Root fix:** thread `seek_s` (from `arguments`) through the single-mode branch of
  `ingest/stream_analysis.py` `sample_for_mode` and give the grab a small non-zero
  decode window, so `mode=single seek=T` actually samples at T instead of t=0. This
  alone resolves the reported black-frame failure.
- **Hardening (once seek is honored):** in `stream.py`, make the settled-frame grab
  robust — a **hybrid seek** (fast `-ss` before `-i` to just before T, then a small
  accurate `-ss` after `-i` for a decode runway) so a precise grab is clean *without*
  decoding from t=0. Reject a near-black frame (mean-luma threshold) and retry
  **exactly once** a beat later, so a genuinely black scene is distinguished from a
  decode artifact without doubling cost.
- **Blast-radius guards (from the design review):** `_grab_one` is also used by
  `sample_stream_full` up to `MAX_FULL_FRAMES = 60` per call — the hybrid seek must
  **preserve fast seeking** there (never decode-from-zero ×60, which would
  timeout-storm), and the multi-frame `window` fps path must be proven
  byte-for-byte unchanged. The luma retry is bounded to once so full mode's budget
  isn't 2×'d.
- Also update the `analyze_stream.tool` sidecar if any wording implies single-mode
  seek worked before. This wave is worth shipping even if T1–T3 slip.

## 5. Security & invariants

- **Egress (invariant #9).** `fetch_image` and `grab_frame`'s URL path are new
  outbound legs; both go through the SSRF guard before any byte is read. `fetch_bytes`
  **re-guards every redirect hop** (auto-redirect off) — guarding once is *not*
  sufficient (§T2). The `grab_frame` URL path inherits `analyze_stream`'s guard on
  both the input URL and the resolved media host plus the ffmpeg protocol whitelist.
  DNS-rebind TOCTOU remains an accepted residual shared with `web_fetch`/`analyze_stream`
  (resolve-then-reconnect); persisting bytes doesn't worsen it, but §5 should not
  read as "SSRF fully closed."
- **Content validation (corrected in v2).** Validate with `sniff_image_type`
  (reject-on-`None`), not `_sniff_media_type`; enforce an explicit **pixel ceiling**
  (header-parse dims before full decode; set `Image.MAX_IMAGE_PIXELS`) so a
  decompression bomb can't OOM the worker; downscale via `downscale_for_vision`
  before any decode/dedup/VLM. Applies to both the fetched and the attachment decode
  paths.
- **RLS (rule 3).** Every new row is written/read on the caller's RLS-scoped session;
  `generated_images` is owner-only, so a non-owner scoped agent sees none. The new
  `provenance` column doesn't change the owner-only POLICY, so the isolation-test
  extension is a light confirmation (the load-bearing correctness item is the
  provenance-aware gallery/card, §3). The table is owner-**global**, not chat-scoped
  (§3 note).
- **Prompt injection.** A grabbed/fetched image is untrusted; `_VISION_SYSTEM` frames
  the image as data-not-instructions for the vision sub-call, **and** the returned
  vision text is treated as untrusted web-derived content when it re-enters jerv's
  turn (§T3).
- **Adapter/storage (rules 1–2).** All VLM calls via `router.complete`; all blobs via
  `BlobStore`. No provider SDK, no raw paths.
- **Resource/DoS.** Refuse a live stream to `grab_frame`; reuse the stream height cap,
  read/wall-clock timeouts, and byte ceilings; the pixel cap bounds decode memory.

## 6. Waves

Sequential; each wave is its own PR with the standard gates (`PROCESS.md`): backend
≥80% (security paths 100%), real-Postgres testcontainers, LLM/ffmpeg/**yt-dlp**/fetch
faked, no network in tests, CI green, docs reconciled, `.tool`/`.prompt` digest pins
bumped for any sidecar/prompt added or changed.

| Wave | Scope | Gate notes |
|---|---|---|
| **V0 ✅** | **B1** — threaded `seek_s` through single-mode dispatch (`stream_analysis.py`) + hybrid fast+accurate seek and a bounded near-black retry in `stream.py` (`_grab_one` full-mode path untouched, its fast seek preserved). | **Shipped on-branch.** Regression tests: a synthetic black-intro clip asserts `seek` moves the single grab off the black intro and the near-black retry reaches content (`test_stream.py`); the streamtools test asserts single mode threads `seek` (was dropped). `window`/`full` paths unchanged. The sidecar already documented single-mode `seek` (now true), so no `.tool` bump. |
| **V1 ✅** | Shared substrate: the reversible `provenance` Alembic migration (nullable `text`, no CHECK), the model `insert(provenance=…)` param + a gallery `list()` filter (`provenance IS NULL`, `get(id)` unchanged), the **provenance-aware in-chat card copy** ("grabbed from video"/"fetched from web", never "seed 0 · web_fetch"), and the RLS-isolation confirmation. | **Shipped on-branch.** RLS test extended (provenanced row owner-only, hidden from the gallery, still resolvable by id) — verified against real Postgres; frontend card test added. **Deferred:** the `_resolve_source` hoist moves to **V4** (its first consumer — no point refactoring with no caller in V1); the **two-image `agent.vision` probe** is an on-box runtime check (no local VL model in CI) — the adapter already forwards a multi-image sequence uncapped (accuracy review), so V4 carries the native-vs-stitch decision with the stitch as the guaranteed path. |
| **V2 ✅** | **`grab_frame`** (URL + the net-new attachment seek-to-T primitive via a temp file + the V0 grab) + `question` (grab-and-read in one hop) + `show` + the `grab_frame.tool` sidecar + `agent/chat_images.py` (shared persist/view/pixel-cap) + registry/jerv-allowlist/main.py wiring (dropped without ffmpeg). | **Shipped on-branch.** Unit tests: sidecar is `web`/read-shaped/jerv-only, dropped without a handler. Integration (real Postgres): URL + attachment grabs persist a `provenance='ffmpeg'` row (hidden from the gallery, resolvable by id), `show=false` suppresses the card, `question` runs the inline vision read, and the one-source/live/no-frame/non-video error paths. `test_agents`/version-pin updated. **Deferred:** `n` (multiple stills around T) — the V0 near-black retry already covers the "landed on black" case; a multi-image return needs a multi-card view, so it rides a later iteration (open decision 7). |
| **V3 ✅** | **`fetch_image`** + the redirect-safe **`fetch_bytes`** on `WebFetcher` (reuses `_get_following_safe_redirects`, `_read_capped` parametrized, a 10 MB image cap) + strict `sniff_image_media_type` (reject-on-`None`) + the pixel cap (shared `persist_chat_image`) + `WebSource` citation + `agent/fetchtools.py` + main.py dep plumbing (one shared `WebFetcher`) + registry/jerv-allowlist wiring. | **Shipped on-branch. Security paths covered:** unit tests prove `fetch_bytes` follows a redirect through the per-hop guard and **refuses a `file:` redirect** (auto-redirect off) and caps size; the sniff rejects HTML/junk (reject-on-`None`); the pixel cap rejects a bomb from header dims. Integration (real Postgres): a fetched image persists a `provenance='web_fetch'` row (hidden from gallery, resolvable by id) with a `WebSource` citation; a lying-content-type HTML body is refused with **nothing stored**; `show=false` suppresses the card. `test_agents`/version-pin updated. |
| **V4 ✅** | **`compare_images`** — a dedicated **list-based** tool (`image_ids`/`attachment_ids` arrays, owner-confirmed shape; never an a/b object) that resolves N chat images, runs one compare-framed `agent.vision` call, and **always stitches + persists + shows a `provenance='compare'` side-by-side** so the verdict is verifiable (F2). `_resolve_source` hoisted to `chat_images.resolve_source` (edit_image/analyze_image now delegate — one RLS resolution path). Router-gated (not ComfyUI). `agent/visiontools.py` + sidecar + wiring + jerv allowlist + the `compare` card label. | **Shipped on-branch.** (Scope note: `analyze_image` was **not** widened — the owner chose a dedicated `compare_images` verb, so the multi-image path lives there, not as an analyze_image overload.) The two-image spike is subsumed: the stitch is the guaranteed owner-facing artifact regardless of native multi-image support. Unit: sidecar wiring, `_collect_source_ids` list contract, `stitch_side_by_side`. Integration (real Postgres): compare persists + shows the stamped side-by-side (hidden from gallery), `show=false` suppresses it, and the <2 / too-many / foreign-id / empty-prompt error paths; the hoisted resolver re-verified via `test_imagegentools_pg`. Frontend card test for the compare label. `test_agents`/version-pin updated. |
| **V5 ✅** | **`show: false`** on `analyze_video` + `analyze_stream` (the in-turn card) + sidecar copy + version bumps (analyze_video 1→2, analyze_stream 5→6). The deferred stream path keeps its `task_status` progress card; `show` governs only the final in-turn card. | **Shipped on-branch.** The owner's original "analyze-video component NOT in chat" ask. `view=None` when `show=false` (the analysis still runs, the summary still reaches the model). Tests assert both tools suppress the card while keeping the summary (and analyze_video's per-call suppression over the cached path). Version-pin updated; the `analyze_stream` no-enum guard still holds (`show` is a boolean). |
| **V6 ◻️** | jerv steering (`ASSISTANT.md` + sidecars + the `EXTERNAL_VIDEO_WATCH` runbook): teach the grab(`question`)→fetch→compare flow so the model reaches for it. **Steering docs shipped on-branch** (ASSISTANT.md "Seeing a specific still", the runbook cross-ref + B1 note; each sidecar already self-documents the chain). **Pending:** the on-box end-to-end sign-off — driving the real chain against the live VL model + ffmpeg-over-network + ComfyUI, which CI can't exercise (it fakes the models and uses testcontainers). Flip to ✅ / archive the plan once that sign-off lands. | Behaviour + docs, no schema. |

Frontend: T1/T2 reuse the `generated_image` card (V1 makes it provenance-aware);
the multi-source analyze/compare renders the side-by-side stitch as a
`generated_image`. No net-new component, so the DESIGN mock-gate binds only as a
registered-view **copy** change (recorded in §7).

## 7. Docs to reconcile when this lands

- Promote out of `proposed/` → `plans/` on schedule (flip to `Scheduled`, add the
  header `Waves:` strip already present, a `ROADMAP.md` slot + `plans/README.md` row
  + `proposed/README.md` removal), then tick waves and archive on the last, per
  `DOC_LIFECYCLE.md`.
- `docs/reference/ASSISTANT.md` — the new tools in jerv's toolset + the visual-QA
  flow (V6).
- `docs/reference/DESIGN.md` — the `generated_image` tool-view now renders
  frame/fetched provenance copy (a registered-view change; no new mock required).
- `docs/runbooks/EXTERNAL_VIDEO_WATCH.md` / the `analyze_stream` sidecar — cross-ref
  `grab_frame` as the "get the actual still" companion, and note the B1 fix.
- `scripts/dev-setup.sh` — no new dependency expected (ffmpeg/yt-dlp/fetcher all
  present); confirm at build and update in the same PR if that changes (rule 8).
- Per-wave `.tool`/`.prompt` digest pins for the new/edited sidecars and
  `compare_images`' compare-framed system prompt (a versioned `.prompt` artifact).

## 8. Open decisions

1. **`compare_images` contract shape — RESOLVED (2026-07-19, owner-confirmed
   list-based).** The a/b-sides object is a gpt-oss grammar-segfault shape; the
   `compare_images` **name** is kept (a thin sidecar) over a **list-based**
   `analyze_image` widening. Not an open question anymore — recorded here for
   provenance.
2. **Two-image VLM call vs. side-by-side stitch** (§T3) — settled by the V1 spike; if
   native multi-image fails on-box the stitch becomes the design (it is emitted
   either way for owner transparency).
3. **`generated_images` + `provenance` column vs. a new `chat_images` table** (§3).
   Recommend the `provenance` column; revisit only if per-chat scoping or a separate
   lifecycle is needed.
4. **Per-session/chat scoping for chat images** (§3 note) — today owner-global +
   uuid-unguessable; a scope column is a possible later hardening if "from this chat"
   must be enforced, not just documented.
5. **Promote a `video_analysis` timeline `thumb_id` into a first-class image** — the
   likely follow-up ask ("grab *that* frame from the analysis and compare it") avoids
   a network round-trip for a frame already on disk. Named here; out of scope for the
   initial waves.
6. **Lifecycle/cache for chat images** — grabbed/fetched rows are permanent; if the
   gallery filter (§3) hides them the accumulation is invisible, but a `(url, seek)`
   grab cache and a cleanup story are deferred out-of-scope, named not silent.
7. **`grab_frame` `n` (multiple stills around T)** — deferred from V2 (§T1). The V0
   near-black retry already handles the "landed on a bad frame" case; returning N stills
   needs a multi-image card (the single `generated_image` view shows one). A later
   iteration if the single grab proves insufficient in practice.
