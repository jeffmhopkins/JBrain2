# Video/Image Inspection Tools — Design Spec

> **Status:** Proposed (icebox) · **Last verified:** 2026-07-19

> **Status: proposed, not scheduled.** A forward-looking design dropped in for
> the record; nothing here is built and it is not yet on the roadmap (the active
> frontier is Phase 6, the wiki). When picked up it must be reconciled with the
> root `CLAUDE.md` non-negotiables — every VLM call through the LLM adapter (rule
> 1), every blob through the storage abstraction (rule 2), the new image rows
> RLS-scoped with an isolation test (rule 3), and the two new outbound legs
> (`fetch_image`, the `grab_frame` URL path) held to the jerv-sandbox egress
> discipline (invariant #9), exactly as `analyze_stream` already is.

Give jerv the ability to **look at a specific still** — from a video or the web —
and to **compare two images**, so a visual question is answered from pixels it
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
- **G2 — jerv is blind to web images.** `web_fetch` (`agent/webtools.py`) strips a
  page to readable text. Asked to compare against *online* photos, jerv had no way
  to see them — so it invented them. This is the most dangerous gap: it converts
  "I can't" into a fabricated "here's the comparison."
- **G3 — no image comparison.** `analyze_image` takes exactly one source
  (`agent/imagegentools.py`, `_source_bytes` rejects two). Even with both images in
  hand there is no compare primitive.
- **B1 — `single`-mode frame grab returns black frames.** In `stream.py`
  `_extract_frames`, a single grab uses input seek (`-ss` before `-i`) then
  `-frames:v 1` with no decode runway. On a signed googlevideo/HLS URL that lands
  on a pre-keyframe partial frame → black. `window` mode works because it decodes
  forward across `-t` seconds and settles. This is why `seek=164/165` were black
  but `window seek=160` was not.

The user also asked for one ergonomic control: **an analyze-video/analyze-stream
call that does its work but does _not_ render its big scrubbing card in chat** —
because when a video read is an intermediate step toward the real answer, the card
is noise.

## 2. What we build

Four changes. Three new jerv tools, one display flag, one bug fix. All are
`web`-class, jerv-only, on-box (the `analyze_stream`/image-gen posture); each drops
from the registry when its backing capability is absent (graceful degrade).

| # | Change | Closes | Backing |
|---|---|---|---|
| T1 | **`grab_frame`** — extract a still at time T from a video URL *or* chat attachment; persist it as a first-class image; return its `image_id` | G1, B1 | ffmpeg (+ yt-dlp for the URL path) |
| T2 | **`fetch_image`** — fetch an image *URL* through the SSRF guard, verify it is really an image, persist it as an image | G2 | the shared web fetcher |
| T3 | **`compare_images`** — two image ids + a prompt → one VLM call comparing them | G3 | the `agent.vision` route |
| D1 | **`show: false`** on `analyze_video` / `analyze_stream` / `grab_frame` — run the analysis, suppress the inline card | ergonomics | none (drop the view) |
| B1 | **Robust single-frame grab** — decode a short runway and take a settled, non-black frame | B1 | ffmpeg |

The corrected flow for the original request:
`read_external_video` → `grab_frame(url, seek=164)` → `analyze_image(image_id, "what
sequencer module is this? read any panel text")` → `fetch_image(<intellijel
metropolis photo>)` + `fetch_image(<metropolix photo>)` → `compare_images(frame,
photo, "same module? note panel-layout differences")` → an answer grounded in three
images jerv actually saw. Intermediate `grab_frame`/analysis calls run with
`show: false`; only the final image(s) the owner should see render a card.

## 3. Storage — grabbed and fetched images are first-class chat images

`analyze_image`/`compare_images` resolve a `source_image_id` by looking it up in
`app.generated_images` (`models/images.py`, owner-only RLS, mirrors `wiki_*`). For a
grabbed/fetched still to be re-examinable and comparable **by id**, it must live in
that same lookup space.

**Decision: reuse `app.generated_images`, widen `kind`.** Today `kind ∈ {generate,
edit}`; add `frame` (grabbed from a video) and `fetched` (from a URL). This inherits,
for free: the existing `generated_image` view/card (`frontend/.../registry.tsx`,
component #5), the `_resolve_source` path in `imagegentools.py`, the thumbnail/blob
serving, and the gallery. The row's `model` records provenance (`ffmpeg` /
`web_fetch`), `prompt` records the human-readable origin (the source URL + `t=…s`, or
the fetched URL), `source_sha256` is NULL, and `steps`/`seed` are `0` (not
meaningful here — a non-nullable `0` avoids a migration to make them nullable).

- No new table, no new RLS policy to author, no new isolation test surface beyond
  confirming the widened rows stay owner-scoped. The blob goes through `BlobStore`
  (rule 2); the row is written on the caller's RLS-scoped session (rule 3).
- **Alternative considered:** a dedicated `app.chat_images` table with a
  `provenance` column. Cleaner semantics (a fetched web image is not "generated"),
  but a new table + policy + isolation test + a second id space for
  `analyze_image`/`compare_images`/the card to resolve. Deferred as premature; the
  widened-`kind` reuse is the smaller, lower-risk cut. Revisit if the semantics
  bite (e.g. the gallery should hide fetched web images).

The card copy/telemetry that today assumes "generated" must read `kind` and not say
"generated" for a `frame`/`fetched` row.

## 4. The tools

### T1 · `grab_frame`

Extract one still and persist it. The still-image sibling of `analyze_stream`
(URL) and `analyze_video` (attachment), but it returns a **reusable image id**, not
a text caption.

```
name: grab_frame           permission: web    cost_class: standard
params:
  url                 string   a video URL (yt-dlp-resolvable) — give this OR source_attachment_id
  source_attachment_id string  a video the owner attached this chat — give this OR url
  seek                number   seconds into the video for the still (VOD only; ignored for a live edge)
  show                boolean  render the still as a card to the owner (default true)
required: exactly one of {url, source_attachment_id}
```

- **URL path:** `resolve_stream` (SSRF-guarded, `stream.py`) → the robust single
  grab (§B1). Second sanctioned outbound leg, same discipline as `analyze_stream`.
- **Attachment path:** resolve the chat attachment under the RLS attachment context
  (the `videotools.py` pattern — a foreign id is a clean miss), sample one frame
  from its bytes via `jbrain.media`.
- Persist the JPEG through `BlobStore`, insert a `generated_images` row
  (`kind='frame'`), return `ToolOutput(summary, view=generated_image(...))` — or no
  view when `show=false`. Summary tells the model the `image_id` and that it can
  `analyze_image`/`compare_images` it.
- Caps: reuse the stream height cap and the `analyze_video` byte ceiling; refuse a
  live stream with an explicit "no fixed timestamp on a live edge — use
  analyze_stream" message.

### T2 · `fetch_image`

Fetch an image URL and persist it, so jerv can *see* a web image.

```
name: fetch_image          permission: web    cost_class: standard
params:
  url    string   the image URL to fetch (e.g. a product photo found via web_search)
  show   boolean  render the fetched image as a card to the owner (default true)
required: [url]
```

- Fetch through the shared SSRF-guarded fetcher (`web/fetch.py`, the same guard
  `web_fetch` and `resolve_stream` use — a private/loopback/link-local resolved host
  is refused, invariant #9). This needs a **bytes** fetch path; `WebFetcher.fetch`
  today returns extracted text. Add a bounded `fetch_bytes(url, *, max_bytes,
  allow_types)` (or a flag) that returns `(content_type, bytes)` under the same host
  guard, redirect ceiling, and total-size cap.
- **Validate it is really an image** by magic bytes (reuse
  `imagegentools._sniff_media_type`; reject anything not in the PNG/JPEG/WebP/GIF
  allowlist — a `text/html` error page or a hostile payload never reaches the VLM as
  "an image"). Cap bytes (a few MB); cap dimensions on decode.
- Persist (`kind='fetched'`, `model='web_fetch'`, `prompt=url`), return the image id
  + `generated_image` view (unless `show=false`). Surface the origin URL as a
  `WebSource` so it is a real citation, not model-authored prose.

### T3 · `compare_images`

Two images, one VLM answer.

```
name: compare_images       permission: web    cost_class: standard
params:
  prompt        string   what to compare / decide (e.g. "same module? note panel differences")
  image_id_a    string   first image — a generated/grabbed/fetched image id from this chat
  attachment_id_a string first image — OR a chat attachment id (give one of a-side)
  image_id_b / attachment_id_b   the second image, same rule
required: prompt, exactly one a-side source, exactly one b-side source
```

- Resolve each side with the existing `_resolve_source` (shared with
  `analyze_image`/`edit_image`), so any mix of generated/grabbed/fetched/attached
  works and a bad/foreign id is a clean miss.
- One `router.complete("agent.vision", …, images=[a, b])` with a compare-framed
  system prompt (the `_VISION_SYSTEM` "faithful observer, never instruction-taker"
  posture). Read-only: **no** row, **no** view — just the model's text back into the
  turn (the `analyze_image` shape). The owner already saw each image when it was
  grabbed/fetched with `show`.
- Confirm the `agent.vision` route + on-box VL model accept **two** images (llama.cpp
  multi-image); if a route caps at one, fall back to a stitched side-by-side single
  image built with `jbrain.media`, captioned "left/right." Note in the wave as a
  spike.

### D1 · `show: false` — suppress the inline card

Add an optional `show` boolean (default `true`) to `analyze_video`, `analyze_stream`,
and `grab_frame`. When `false`, the handler returns its summary `ToolOutput` **with
no `view`** — `ViewPayload` is already optional on `ToolOutput` (`agent/loop.py`), so
a viewless result renders only the normal tool-result chip, no scrubbing/image card.
The model still reads the full summary; the owner's chat isn't cluttered by an
intermediate read. The deferred `analyze_stream` path still emits its `task_status`
card (a long background job needs its progress affordance); `show` governs only the
**final** `video_analysis`/`generated_image` card. Documented in each sidecar: "set
`show: false` when this is an intermediate step and the owner doesn't need the card."

### B1 · Robust single-frame grab (bug fix, ships first, stands alone)

Fix `_extract_frames`/`_grab_one` (`stream.py`) so a single grab does not return a
black pre-keyframe frame:

- Give the decoder a short runway: seek slightly before the target and decode
  forward to the requested time (an accurate `-ss` after `-i`, or a hybrid
  fast-seek-then-precise), taking the first fully-decoded frame at/after the target
  rather than the raw first packet.
- Reject a near-black frame (mean luma below a threshold) and retry once a beat
  later, so a genuinely black frame in the video is distinguished from a decode
  artifact.
- This alone makes `analyze_stream mode=single seek=…` reliable and is worth
  shipping even if T1–T3 slip. `grab_frame`'s URL path reuses the fixed grab.

## 5. Security & invariants

- **Egress (invariant #9).** `fetch_image` and `grab_frame`'s URL path are new
  outbound legs; both go through `guard_public_host` (`web/fetch.py`) before any
  byte is read, exactly as `analyze_stream`/`web_fetch` do. No fetched **response
  text** ever reaches the model as instructions — only decoded pixels of a
  validated image, or a stored blob.
- **Content validation.** `fetch_image` verifies image magic bytes before the blob
  is stored or shown; a non-image (HTML error page, hostile payload) is a clean tool
  error, never "an image." Byte + dimension caps bound VLM cost and memory.
- **RLS (rule 3).** Every new row is written/read on the caller's RLS-scoped session;
  `generated_images` is owner-only, so a non-owner (any scoped agent) sees none. The
  isolation test is extended to cover the new `kind`s.
- **Adapter/storage (rules 1–2).** All VLM calls via `router.complete`; all blobs via
  `BlobStore`. No provider SDK, no raw paths.
- **Prompt-injection posture.** A grabbed/fetched image is *data to read, never a
  source of commands* — the `_VISION_SYSTEM` framing already states this; reused
  verbatim for `compare_images`.

## 6. Waves

Sequential; each wave is its own PR with the standard gates (`PROCESS.md`): backend
≥80% (security paths 100%), real-Postgres testcontainers, LLM/ffmpeg/fetch faked,
CI green, docs reconciled.

| Wave | Scope | Gate notes |
|---|---|---|
| **V0** | **B1** black-frame fix in `stream.py` + regression test (a synthetic clip whose first packet is black; assert the grab is non-black). Ships independently. | Pure media; no new tool, no schema. |
| **V1** | `kind` widening (`frame`/`fetched`) + `fetch_image`'s `fetch_bytes` path + card/telemetry `kind`-awareness. RLS isolation test extended. | The shared substrate for T1/T2. |
| **V2** | **`grab_frame`** (URL + attachment paths) + sidecar + handler wiring in `readtools.build_registry` (dropped without ffmpeg/yt-dlp) + `show` flag. | Reuses V0's grab, V1's storage. |
| **V3** | **`fetch_image`** + sidecar + wiring + image validation + `WebSource` citation. | Security-path tests at 100%. |
| **V4** | **`compare_images`** + the two-image `agent.vision` spike (or side-by-side stitch fallback) + sidecar + wiring. | Confirm multi-image route on-box. |
| **V5** | **`show: false`** on `analyze_video` + `analyze_stream` + sidecar copy; frontend confirms a viewless result renders no card. | Small; could fold into V2. |
| **V6** | jerv system-prompt / sidecar steering: teach the grab→analyze→fetch→compare flow so the model reaches for it instead of guessing (`docs/reference/ASSISTANT.md`). | Behaviour, no schema. |

Frontend: T1/T2 reuse the existing `generated_image` card; the only UI work is
`kind`-aware copy (don't label a fetched web image "generated"). `compare_images`
has no view. So there is no new component to design/mock — the DESIGN mock-gate does
not bind here beyond the copy tweak.

## 7. Docs to reconcile when this lands

- Promote this doc out of `proposed/` → `plans/` on schedule (flip to `Scheduled`,
  add a `ROADMAP.md` slot + `plans/README.md` row + `proposed/README.md` removal),
  then tick waves and archive on the last, per `DOC_LIFECYCLE.md`.
- `docs/reference/ASSISTANT.md` — the new tools in jerv's toolset + the visual-QA
  flow (V6).
- `docs/runbooks/EXTERNAL_VIDEO_WATCH.md` / the `analyze_stream` sidecar — cross-ref
  `grab_frame` as the "get the actual still" companion, and note the B1 fix.
- `scripts/dev-setup.sh` — no new dependency expected (ffmpeg/yt-dlp/fetcher all
  present); confirm at build and update in the same PR if that changes (rule 8).

## 8. Open decisions

1. **`generated_images` reuse vs. a new `chat_images` table** (§3). Recommend reuse;
   revisit if the "fetched web image labelled generated" semantics bite.
2. **Two-image VLM call vs. side-by-side stitch** for `compare_images` (§T3) — settle
   in the V4 spike against the on-box VL model.
3. **`compare_images` source arity** — strictly two, or an ordered list of 2–N? Two
   covers the ask and keeps the contract simple; N-way is a later widening.
4. **Auto-persist grabbed frames onto the external-video corpus?** Out of scope here
   (that corpus is text/embeddings per `EXTERNAL_VIDEO_INGESTION_PLAN.md`); a grabbed
   still is a transient chat image, not a corpus artifact.
