> **Status:** Research · **Last verified:** 2026-09-08

# X2 — How attachments enter the ingest conversation

Scope: the owner's proposed replacement of `extract → Integrator → arbiter → apply` with a
tool-using local agent whose turn 0 is a note *and its attachments*. This document answers only
the media half: what produces the text an agent would read, what the capture-race machinery
guarantees, whether the agent should look at pixels mid-conversation, and what a citation is when
the source is an image. It does not design the graph-writing tools.

Everything below is either **VERIFIED** (a `path:line` with the code read) or **ASSUMED** (an
extrapolation, marked as such). Two things the mission statement asserts turned out to be false
against the tree; they are the first section on purpose.

---

## 0. Recommendation, first

**Pre-digest all media to text before the conversation's first pass. Do not give the agent a
synchronous `look_at_image` that can trigger a model swap. Give it three cheaper things instead:**

1. **Keep the existing async media jobs exactly where they are** — `ocr_attachment`
   (`backend/src/jbrain/ingest/ocr.py:212`), `transcribe_attachment`
   (`backend/src/jbrain/ingest/transcribe_job.py:103`), `analyze_video_attachment`
   (`backend/src/jbrain/ingest/video.py:195`) — writing `app.attachment_extracts` rows. They are
   already idempotent, already cached, already the thing the capture-race gate keys on, and they
   are the only reason the box does not re-bill a vision model on every re-ingest. The agent
   conversation starts when that cache is complete, not before.
2. **Turn 0 carries the pre-digest as labeled, per-source blocks** — the shape
   `analysis/prompt.py:122-148` (`prompt_block`) already produces (`[ocr from …]`,
   `[image caption of …]`, `[low-confidence transcript from …]`, `[video analysis of …]`) — plus a
   manifest row per attachment: id, filename, media type, which extract kinds exist, the
   dual-engine agreement score, and the confidence cap.
3. **A `read_attachment_text(attachment_id, …)` tool that never touches a GPU** — it reads the
   cache and, for a verbatim re-read ("what does the third line say?"), calls the **RapidOCR
   sidecar** (`backend/src/jbrain/vision/rapidocr.py:41`), which is a CPU container with a ~15 MB
   ONNX model, is not on the LLM residency budget at all, and costs a ~1–2 s cold start
   (`docs/plans/RAPIDOCR_PLAN.md` §2). This answers most "look again" questions with zero swap
   risk.
4. **A rare, explicit `look_again(attachment_id, question)` escalation that is DEFERRED, not
   awaited.** It records the question, the pass ends, and a batched job answers *every* pending
   question for that note in **one** vision residency, persists each answer as an
   `attachment_extracts` row, re-ingests, and re-drives the conversation. Batching is the whole
   point: the swap cost is per-residency, not per-question, so N questions must cost one round
   trip, not N.

**Why not synchronous look-at-image:** because the round trip is minutes, not seconds, and the
agent would pay it per call (§4). **Why not "no looking at all":** because the pre-digest is two
fixed prompts written before anyone knew what the note would ask, and the whiteboard/receipt
failure modes in §6 are exactly the cases where a targeted question beats a generic caption.

**One structural alternative deserves a decision rather than a default (§4.4): run the ingest
conversation itself on a vision-capable tool-using model.** `qwen3.8-27b-q4` and
`qwen3-vl-30b-a3b` are both `supports_vision=True` *and* `supports_tools=True`
(`backend/src/jbrain/llm/local_catalog.py:747-749`, `:469-471`), so the agent could see the
pixels inline with no swap, ever. The cost is reasoning quality against `gpt-oss-120b`, and the
owner has already routed 16 of 19 tasks to gpt-oss and only the 3 vision tasks elsewhere
(`docs/reference/MODEL_ACCESS_INVENTORY.md` §E "Routing is already fully local").

---

## 1. Two premises in the brief that the tree contradicts

### 1.1 "Only one large model resident at a time" — MEASURED FALSE

`backend/src/jbrain/llm/local_catalog.py:213-217`, verbatim:

> MEASURED ON THIS BOX, no longer assumed. Loading `qwen3.8-27b-q4` beside a resident
> gpt-oss moved GTT 67.71 -> 93.73 GiB (+26.02, against 25.60 predicted for flash attention
> ON and 29.62 for OFF), and a subsequent full-resolution 2.1 MB image encode moved it only
> 93.73 -> 93.84 (+0.11 GiB).

So the text model and a vision model **have co-resided on this box**, measured, at 93.73 GiB. The
admission ceiling is `total_gb - max(reserve_gb, total_gb * fraction)`
(`backend/src/jbrain/llm/residency.py:611-614`) with `reserve_gb = gpu_guard.MIN_FREE_GTT_GB = 6.0`
(`backend/src/jbrain/llm/ledger.py:141`, `gpu_guard.py:100`) and a **live** operator fraction of
0.05 (`docs/reference/MODEL_ACCESS_INVENTORY.md` §E "The auto-restore toggle is already off"). On a
~124 GiB pool that is a ceiling near **117.8 GiB**, leaving ~24 GiB of headroom over the measured
pair. `qwen3-vl-30b-q4` exists specifically so that "it co-resides beside gpt-oss-120b with real
headroom under the free-RAM floor instead of evicting it" (`local_catalog.py:490-494`).

The caveat that keeps this from being a free lunch: the model the owner has actually routed the
three vision tasks to is `qwen3.8-27b-abliterated`, and it is **served at `-c 262144` against a
catalog default of 32768**, which took a load that pre-flighted at 20.29 GB to a **measured
36.92 GB** (`local_catalog.py:1274-1278`). 69.26 (gpt-oss peak, `MODEL_ACCESS_INVENTORY.md` §W3) +
36.92 = **106.2 GiB** — still under the ceiling, but with ~11 GiB of margin on a box that also
hosts ComfyUI, whisper, TEI and Kokoro (`MODEL_ACCESS_INVENTORY.md` §B, `ledger.py:150-161`).

**Consequence for this design:** co-residence is a *configuration* outcome, not a law. The design
must work when the pair fits (fast path: vision calls cost an image encode, no swap) and when it
does not (slow path: batch the questions). It must never assume either.

> The live routing of vision to an **abliterated** checkpoint is itself worth a flag. The catalog
> says of it: "NEVER recommended, and nothing routes here by default. This is a PROBE, not a
> worker … Putting it on a real task would put the embedded prompt below in front of every JBrain
> system prompt" (`local_catalog.py:824-828`). Yet §E of the inventory records all three vision
> tasks resolving to it. Whatever the ingest agent's vision path is, it inherits that.

### 1.2 The capture-race migration is 0156, not 0154

`docs/reference/ANALYSIS.md:3` says "`notes.attachments_expected`, migration 0154". The column
ships in `backend/migrations/versions/0156_note_attachments_expected.py:33`; `0154` is
`reports_subagent_personas`. A one-line doc correction, noted because §3 below depends on reading
that machinery correctly.

---

## 2. Today's attachment dispatcher, end to end

### 2.1 The routing table, as code

Media type → chain is `ExtractorRegistry`, longest-prefix match
(`backend/src/jbrain/ingest/extract.py:142-170`):

| media | what runs | where | local/remote | model |
|---|---|---|---|---|
| `text/*` | UTF-8 decode → one `text-layer` segment | in-process, `asyncio.to_thread` (`ingest/pipeline.py:426`) | local | none |
| `application/pdf` (text layer) | PyMuPDF `get_text` per page → one `text-layer` segment per page, `anchor="page N"` | in-process | local | none |
| `application/pdf` (no text layer = a scan) | rasterize @200 DPI (`imageprep.py:47`) → per-page `vision.ocr` → one `ocr` row per page, `anchor="page N"` (`ocr.py:346-386`) | `ocr_attachment` job | router-decided; live = local | `qwen3.8-27b-abliterated` |
| `image/*` | `vision.ocr` **and** RapidOCR concurrently (`asyncio.gather`, `ocr.py:287`), **plus** `vision.caption` when mode=`full` (`ocr.py:314-342`) | `ocr_attachment` job | VLM via router; RapidOCR = CPU sidecar | VLM + PP-OCR ONNX |
| `audio/*` | whisper.cpp over the second llama-swap gateway → one `transcript` row with per-word timings (`transcribe_job.py:146-178`) | `transcribe_attachment` job | local, opt-in (`whisper_url`) | whisper GGML |
| `video/*` | ffmpeg frame sample → per-frame `agent.vision` caption → whisper the audio → fuse a `[mm:ss]` timeline → `video.summarize` reduce → one `video_analysis` row (`video.py:195-270`) | `analyze_video_attachment` job | local | VL + whisper + text model |

`ingest_note` itself **never calls a model**: for any attachment with cache rows it is a pure read
over `attachment_extracts` via `image_segments` (`ingest/pipeline.py:417-421`,
`ingest/extract.py:109-139`). That separation is the doctrine — "capture-to-searchable never waits
on a cloud LLM" (`ocr.py:3-5`) — and it is what makes the whole thing restartable.

### 2.2 What produces text vs. structure

- **Text only:** `text-layer`, `ocr` (both engines), `caption`. Stored as `AttachmentExtract.text`
  and chunked (`ingest/pipeline.py:429-435`).
- **Text + structure:** `transcript` carries `words` jsonb — per-word `{text, start_ms, end_ms,
  confidence}` (`models/notes.py:170-173`, `transcribe_job.py:160-168`) — display-only today.
  `video_analysis` carries `analysis` jsonb — `{duration_ms, frames:[{t_ms, caption, thumb_id}],
  transcript}` (`models/notes.py:174-178`).
- **Structure computed and thrown away:** RapidOCR returns `lines: [{text, box, score}]` and
  `mean_score` (`vision/rapidocr.py:31-38`, `:71-76`) — and the pipeline persists **only
  `rapid.text`** (`ocr.py:308-313`, `_rapid_row` at `:414-436`). The per-line bounding boxes never
  reach the database. §7 argues this is the missing image citation primitive.
- **A divergence score computed and only logged:** `_agreement` (`ocr.py:72-81`) is a
  whitespace-collapsed `SequenceMatcher` ratio between the VLM's reading and RapidOCR's, emitted as
  a `ocr.crosscheck` log line (`ocr.py:438-449`) and persisted nowhere. `RAPIDOCR_PLAN.md` §R2
  explicitly deferred persisting it ("out of scope for R2 unless a review UI needs it"). §6 argues
  the agent conversation **is** that review UI.

### 2.3 Confidence caps — the invariant the agent must inherit

| kind | cap | citation |
|---|---|---|
| `ocr` (both engines) | 0.7 | `ocr.py:60`, `:63`; RapidOCR row keeps the same cap `ocr.py:65-69` |
| `caption` | 0.6 | `ocr.py:62-63` |
| `transcript` | 0.8, further reduced to the words' mean | `transcribe_job.py:67`, `:70-76` |
| `video_analysis` | 0.6 | `video.py:85` |
| note body | 1.0 (`Segment.confidence` default) | `ingest/extract.py:38` |

These flow into the extraction prompt only because `prompt_block` *labels* the block
(`analysis/prompt.py:126-131`: "the system prompt's confidence rule … only fires if the model can
TELL the text is machine-read"), and then into `supersession.decide()` via the model's
`self_confidence` (`ANALYSIS.md` "Guards"). **In an agent-writes-the-graph model this stops being a
prompt convention and has to become a server-side ceiling**: the tool that writes a fact must clamp
its confidence by the provenance of the text it cites, or the agent can assert a 0.95 blood
pressure from a blurry photo and auto-supersede a real reading.

### 2.4 Size and cost bounds already in place

- Image OCR is skipped at *enqueue* above `MAX_OCR_BYTES = 8 MiB`, deliberately with no cache row
  (`ocr.py:53-56`, `ingest/pipeline.py:289-297`).
- Images are downscaled to a 2048 px long side before the model (`imageprep.py:23`, `:85-108`) —
  the fix for "thousands of image tokens … which wedged local OCR into a timeout/retry loop"
  (`imageprep.py:1-8`).
- PDF rasterization is double-bounded: `MAX_PDF_PAGES = 200` and `MAX_PDF_RASTER_BYTES = 512 MiB`,
  truncate-and-log rather than refuse (`imageprep.py:43-44`, `:64-81`).
- Audio is skipped above `DEFAULT_TRANSCRIBE_MAX_BYTES = 100 MiB` (`transcribe_job.py:84`).
- A chat turn's inline attachments are capped at 10 attachments / 20 images / 10 PDF pages
  (`agent/attachment_content.py:37-43`) — the closest existing analogue to "attachments in a
  conversation", and a reasonable starting bound for turn 0.

---

## 3. The capture race, and what the conversation must inherit

### 3.1 How it works today

The PWA posts a note and its files as **separate requests** (`frontend/src/notes/outbox.ts:119-145`:
`api.createNote(...)` then a `api.uploadAttachment(...)` loop, persisting progress per file so a
retry never re-uploads). So the first `ingest_note` can run with **zero** attachments present while
a photo is still uploading. Three mechanisms stop a body-only pass:

1. **The client's declared intent.** `POST /notes` carries `attachments_expected`
   (`api/notes.py:141`, `:175`; sent by `outbox.ts:135` as `note.attachments.length`), persisted on
   the note (`models/notes.py:53`, migration `0156`). Ingest reads it (`ingest/pipeline.py:93`) and
   computes `attachments_settled = len(attachments) >= attachments_expected`
   (`ingest/pipeline.py:185`). Until settled it **does not emit `note.ingested`**
   (`ingest/pipeline.py:186-223`), so nothing drives integration.
2. **The outstanding-vision-work gate.** Even when settled, the emit waits on
   `outstanding` — the set of attachment ids with a queued/running `ocr_attachment`
   (`ingest/pipeline.py:252-325`) unioned with `transcribe_attachment`
   (`:327-381`). The gate keys on *work*, never on extract kinds or the image-analysis mode
   (`ingest/pipeline.py:161-174`) — a mode flip on a cached attachment enqueues nothing and must
   not block.
3. **Re-drive on arrival.** Each attachment upload re-enqueues `ingest_note`
   (`api/notes.py:300-302`); each vision/audio job re-enqueues `ingest_note` after writing its cache
   rows (`ocr.py:466`, `transcribe_job.py:189`). So the note converges to "extracted once, with its
   OCR text" rather than a blind pass plus a re-run.

The reconciler honors the same wait, bounded: `backfill_pending_integration` skips a note whose
`attachments_expected` exceeds the attachments present **unless** the settle window has lapsed
(`queue.py:631-636`), `INTEGRATION_ATTACHMENT_SETTLE_SECONDS = 300` (`queue.py:571`). It runs on a
300 s schedule (`MODEL_ACCESS_INVENTORY.md` §E schedule table). Two escape hatches keep a note from
stranding: an oversized image never becomes outstanding (`ingest/pipeline.py:289-297`), and an
`ocr_attachment` that exhausts its retries (`max_attempts` default 5,
`migrations/versions/0003_jobs_chunks_ingest_state.py:40`; backoff `2^N` minutes capped at 1 h,
`queue.py:146-151`) falls back to enqueueing body-only integration directly
(`ocr.py:101-130`).

### 3.2 The hole: the settle window is measured from CLIENT capture time

`backfill_pending_integration`'s escape clause is `n.created_at < now() - (:settle * interval '1
second')` (`queue.py:635`). But `notes.created_at` is the **client's** capture time, deliberately:
"the offline outbox flushes later, so server now() would be wrong"
(`notes/repo.py:85-87`, `captured = {"created_at": created_at} …` at `:87`, written at `:103`;
`outbox.ts:123` sends it).

So for a note captured while the phone was offline for hours and flushed on reconnect:

- `createNote` succeeds with `attachments_expected = 1` and `created_at` = three hours ago.
- The upload of the photo has not happened yet (it is the next statement in the flush loop, and a
  network drop between the two leaves it undone — `outbox.ts:148-159` breaks out of the loop).
- `ingest_note` runs, sets `ingest_state = 'indexed'` **unconditionally**
  (`ingest/pipeline.py:113-118`), then correctly declines to emit.
- The reconciler, on its next 300 s tick, evaluates the note: `attachments_expected (1) <=
  present (0)` is false, **but `created_at < now() - 300 s` is true by three hours** → it enqueues
  `integrate_note` → a body-only pass on a note that promised an image.

This is exactly the double-pass the gate exists to prevent, and the exact scenario the brief names
("the owner's phone may be offline for hours"). It is a **verified defect in today's deterministic
machinery**, not a new risk introduced by the agent model. The fix is to settle against a
server-observed clock — the note's row `updated_at`/insert time, or a dedicated
`attachments_deadline_at` stamped at insert — never the client's capture instant.

### 3.3 What the agent-conversation model must inherit, and what changes

**Inherit unchanged:**

- The conversation's first pass is driven by an *event that fires only when the media is settled*.
  Whatever replaces `integrate_note` must be enqueued through the same gate
  (`ingest/pipeline.py:185-223`), not on `note.created`.
- The reconciler is the safety net for a dropped event, and it must apply the same wait
  (`queue.py:631-636`) — with the clock fixed per §3.2.
- Idempotency: `ingest_note` deletes and rebuilds all chunks (`ingest/pipeline.py:109-110`), and
  each media job does delete+insert of only the kinds it recomputed (`ocr.py:451-466`). An agent
  conversation is *not* naturally idempotent, which is the sharpest new problem: a second pass must
  be an *incremental upsert on the structural identity key* (`ANALYSIS.md` "Reprocessing"), never a
  replay that re-asks the owner questions already answered.

**Must change:**

- **The gate must become "settled OR the owner said go".** Today a stranded note silently
  integrates after 300 s. In a conversation the honest move is to *say so in the conversation*:
  "you told me a photo was coming and it never arrived — shall I read the note without it?" That is
  strictly better than the current silent body-only pass, and it costs nothing to build because the
  conversation is already the owner-facing surface.
- **Late arrival must re-open the conversation, not restart it.** Today the late photo re-ingests
  and re-integrates, and the incremental upsert absorbs it. The conversational equivalent is a new
  turn appended to the same conversation: "the photo landed; here is its OCR and caption — does it
  change anything you told me?" The alternative (a fresh conversation per re-drive) re-asks
  questions and burns a 198 s residency each time.
- **A pass must be resumable across a worker restart.** The current pipeline's durability is the
  job row plus `integration_state`; a conversation's durability is the transcript. That is a
  storage question for X1/X3, but it is a *hard* prerequisite for anything that waits minutes on a
  batched vision pass (§4.3).

---

## 4. Vision/text interleaving

### 4.1 The swap cost, quantified

Measured figures from the tree:

| quantity | value | citation | kind |
|---|---|---|---|
| gpt-oss-120b weights on disk | 59.0 GiB (MXFP4) | `local_catalog.py:584` | verified |
| gpt-oss-120b resident, peak-across-warm @131k | 69.26 GB | `MODEL_ACCESS_INVENTORY.md` §W3 table | measured |
| **cold load of the 69 GB model** | **198 s** | `llm/admission.py:254`, `llm/ledger.py:446` | measured |
| weights read rate during load | ~1.5 GiB/s (~50 GiB of page cache in ~35 s) | `llm/local_gateway.py:698-702`, `:52-56` | measured |
| gpt-oss persona+tools prefill (~29k tokens) | ~60 s | `llm/kv_prefix.py:3-4` | measured |
| …restored from a saved KV slot instead | ~2 s (~90 ms page-cache-warm) | `llm/kv_prefix.py:9-11` | measured |
| gpt-oss is KV-slot-restore eligible | yes (plain attention, non-speculative) | `llm/kv_prefix.py:229-250`, `local_catalog.py:610-612` | verified |
| llama-swap graceful stop | 10 s, then SIGKILL | `llm/local_gateway.py:485`, `:992` | verified |
| wait for a stop to actually settle | bounded at 60 s | `llm/local_gateway.py:60-71` (`STOP_SETTLE_TIMEOUT_S = 60.0`) | verified |
| abliterated 27B weights on disk | 16.5 GiB (15.66 + 0.86 F16 projector) | `local_catalog.py:872-874` | verified |
| abliterated 27B resident at its served `-c 262144` | 36.92 GB | `local_catalog.py:1276-1277` | measured |
| a full-res image encode on a resident VL | +0.11 GiB, one-off | `local_catalog.py:215-217` | measured |
| one image's context cost on the local VL | 2048 (floor) – 4096 (ceiling) image tokens | `local_catalog.py:189-198`, `:864` (`image_min_tokens=2048`) | verified |
| re-paying a vision encode every turn | the "~35 s Reading your prompt" the owner watched | `api/agent.py:893-895` | measured |

**A no-co-residence swap round trip, per `look_at_image` call:**

| step | cost |
|---|---|
| unload gpt-oss-120b | 10 s graceful, up to 60 s to settle |
| load the VL (16.5 GiB weights) | ~11 s of pure read at 1.5 GiB/s; **~55 s** total, extrapolating 198 s / 59 GiB — **ASSUMED**, never measured for this model |
| the vision call itself | seconds to a minute, image-dependent |
| unload the VL | 10–60 s |
| reload gpt-oss-120b | **198 s** |
| restore the ingest prefix | ~2 s (KV slot) or ~60 s (cold prefill) |
| **total** | **~285–375 s ≈ 5–6 minutes, per call** |

That is the number that settles the design. An agent that calls `look_at_image` three times in one
note costs a quarter of an hour of box time and evicts the interactive persona the owner is
chatting on, three times. It also fights the residency coordinator's *restore* behaviour
(`llm/residency.py:21-30`) — every eviction is recorded as a transient displacement to be reloaded
at end of turn, and `RESTORE_RETRY_S = 240.0` exists because "whatever held the box has finished a
model load (measured at 100-200 s here)" (`llm/residency.py:79-82`).

**With co-residence (§1.1), the same call costs one image encode and ~0 s of swap.** The existing
agent tools already exploit exactly this: `vision_read_spec` reuses the conversation's own
vision-capable model "no residency swap on the memory-bound box … (the cold-load the owner saw as a
slow analyze_image)" (`agent/chat_images.py:55-68`, used at `agent/visiontools.py:125-131`).

### 4.2 The recommendation, restated with the mechanism

**Pre-digest is mandatory; look-again is deferred and batched.**

```
note settled (§3 gate)
  → pre-digest cache complete (ocr / caption / transcript / video_analysis rows)
  → PASS 1: agent conversation, turn 0 = body + per-source pre-digest blocks + attachment manifest
       tools: read_attachment_text  (cache read; RapidOCR re-read — CPU, no swap)
              ask_owner             (a question, in the conversation)
              look_again(id, q)     (records a request; DOES NOT block)
              …graph-writing tools (X1/X3)
  → if any look_again requests were recorded:
       enqueue vision_followup{note_id}         [one job, all questions]
       → ONE vision residency: answer every question for every attachment of this note
       → persist each answer as an attachment_extracts row
            kind='vision_answer', tool='<provider>:<model>', source_anchor=<the question>
       → re-ingest (chunks rebuild, answers become citeable)
       → PASS 2: append the answers as a new turn; the conversation continues
```

Three properties this buys:

1. **The swap is amortized.** N questions across M attachments of one note = **one** round trip.
   And because `vision_followup` is a queue job, several notes' followups can coalesce into one
   residency if they land in the same window — the same trick `caption_frames` already uses for
   video (`video.py:226-256` loops all frames inside one residency).
2. **The answer is durable and citeable.** It becomes an `attachment_extracts` row like every other
   media product, so it chunks, it is searchable, it re-ingests idempotently, and a fact derived
   from it cites a real chunk with a real span (§7). Nothing new in the schema — `kind` is free
   text (`models/notes.py:166`), and `_prefer_ocr` only arbitrates between `ocr` rows
   (`ingest/extract.py:96-106`), so a new kind rides through untouched.
3. **It is honest about latency.** A deferred pass tells the owner "I want a closer look at the
   receipt; I'll come back" instead of freezing a conversation for six minutes with a spinner —
   which is precisely the failure `chat_images.py:55-63` documents ("the cold-load the owner saw as
   a slow analyze_image").

**The escalation must be rationed.** A per-note ceiling (2–3 look-agains), and a prompt contract
that says look-again is for a *specific unanswerable question*, never "let me double-check". The
cheap path must be exhausted first: the cache, then RapidOCR, then the owner, then pixels.

### 4.3 What makes the deferred design hard

- **The conversation must survive the gap.** A `vision_followup` round trip is minutes; a worker
  restart in that window must not lose the pass. This forces conversation state to be persisted
  server-side (a transcript row per note-pass), not held in a worker's memory. Non-negotiable.
- **Re-entry must be additive.** Pass 2 appends; it must not re-derive pass 1's facts from scratch
  or the owner sees duplicates. The structural-identity upsert (`ANALYSIS.md` "Reprocessing") is
  the existing answer and should be kept.
- **The batched job needs its own admission behaviour.** `video.py` already imports `ResidencyError`
  (`video.py:53`) and unloads the whisper model when done (`transcribe_job.py:198-208`, with
  `box_events.because("transcription is done with it")`). The followup job should mirror that:
  narrate the swap into `box_events` so the Ops screen tells the owner why their chat model went
  away, and unload the VL when finished so the restore puts gpt-oss back.
- **The 409 semantics already exist and must be respected.** `POST /notes/{id}/analyze` refuses
  while ingest or OCR would run one anyway (`api/notes.py:366-392`), and
  `POST /attachments/{id}/analyze` refuses a duplicate in-flight run (`api/notes.py:396-417`). The
  followup job should reuse the second endpoint's job kind (`ocr_attachment` with `mode="full"`)
  where the question is really "re-describe this", rather than minting a parallel path.

### 4.4 The alternative worth an owner decision: one model for the whole pass

If the ingest conversation ran on `qwen3.8-27b-q4` (`supports_vision=True`, `supports_tools=True`,
`tiers=("vision","high")`, 16.8 GiB weights — `local_catalog.py:741-756`) or `qwen3-vl-30b-a3b`
(`local_catalog.py:463-482`), then:

- Images ride **inline in the conversation**, at their anchored positions, exactly as chat
  attachments already do (`agent/attachment_content.py:60-79`, anchoring at `api/agent.py:890-906`)
  — and the KV prefix cache means an anchored image is encoded **once** and then rides the cached
  prefix rather than being re-encoded every turn (`api/agent.py:891-895`).
- `look_at_image` becomes free: the model is already looking. "What does the third line say?" is a
  follow-up turn, not a tool call, not a job, not a swap.
- The pre-digest still earns its keep — RapidOCR's verbatim transcription is the hallucination
  check the VLM cannot be (`RAPIDOCR_PLAN.md` §1), and the cache is what keeps a re-ingest from
  re-billing the vision model.
- The cost is reasoning strength on the graph-writing half. That is the trade the owner has to
  price, and it is not measurable from this document.

The two designs are compatible: build the pre-digest + deferred-followup architecture, and treat
"the pass model is vision-capable" as a *configuration* in which the followup path is simply never
taken (the fast path in §0 item 3a). That is the same conditional `vision_read_spec` already
implements (`agent/chat_images.py:64-68`).

---

## 5. Does per-source extraction still matter at 131k context?

**The budget problem vanishes. The problem it was a proxy for does not.**

### 5.1 What the fix actually was

`group_texts_by_source` (`analysis/prompt.py:80-104`) partitions prompt blocks by
`Chunk.attachment_id` (`analysis/pipeline.py:361-364`) so the note body and each attachment extract
in their **own** `note.extract` call with their **own** `fact_cap`. The failure it fixed
(`ANALYSIS.md` "Per-source extraction", `analysis/prompt.py:86-92`) was: a note reading *"car loan
for the Kia, attached as an image"* lost its `owns → car loan` / `owns → Kia` edges the moment an
unrelated membership card's OCR was present, because the model — handed body + dense card text
under one "ceiling, not target" budget — spent the budget on the card.

The mechanism is a **shared fact budget**, threaded twice: `fact_cap(text)` scales with word count
and clamps to `[MIN_FACTS, MAX_FACTS]` (`analysis/prompt.py:44-48`), is advertised in the user
prompt as "at most N facts (a ceiling, not a target …)" (`analysis/prompt.py:151-160`) **and**
enforced in `parse_extraction` (`analysis/pipeline.py:261`). Groups are packed to
`GROUP_CHAR_BUDGET = 6000` chars ≈ 1500 tokens (`analysis/prompt.py:51-57`).

### 5.2 In a 131k-context conversation

The **context** constraint is gone outright. 6000 chars is ~0.02% of gpt-oss's 131072 window
(`local_catalog.py:590`). A note body plus a 40-page scanned PDF's OCR (~80k chars ≈ 20k tokens)
fits one turn with room for the graph context, the tool schemas, and a 29k-token persona prefix
(`kv_prefix.py:3`). Today that same note makes **~14 sequential `note.extract` calls**
(80k / 6000), each a full round trip.

The **salience** constraint is not gone, and I would argue it gets *worse* before it gets better:

- The 6000-char group was accidentally protective. Attention over 20k tokens of dense receipt text
  is measurably worse at recovering a single clause buried at position 3 than attention over 1500
  tokens is — the "lost in the middle" effect. The note body is *always* the shortest source and
  *always* the highest-value one (it is the sole source of truth; an attachment is enrichment that
  "must never delete them" — `analysis/prompt.py:86-90`). A long-context pass puts the most
  important 200 tokens next to 20,000 tokens of least-important ones.
- The budget itself is the *other* half of the failure and does not survive the redesign. An agent
  writing through tools has no `max_facts` — it emits edges until it stops. Removing the ceiling
  removes the crowding-out mechanism, which is genuinely good; but it removes the runaway bound
  too. `ANALYSIS.md` names over-extraction as "the known quality risk", and `extraction_truncated`
  review cards exist so budget loss is visible rather than silent (`ANALYSIS.md` "Model routing &
  cost"). An agent that writes 300 edges from a receipt has no such card.

**Recommendation:** keep source partitioning as a **structural** device even though the budget
reason is gone.

1. **Body first, alone, in its own explicit segment of turn 0** — with the framing that the body is
   the source of truth and the attachments are enrichment. This is the guarantee the per-source fix
   bought, restated as prompt structure rather than call structure. Cheap: it is a heading.
2. **One turn, many labeled sources** rather than one call per source. The 131k window makes
   cross-source coreference *free* — which today costs a whole reduce step
   (`extraction.merge_extractions` re-running object binding "over the full mention set (so a
   relationship whose object entity was named in another group still links)", `ANALYSIS.md`
   "Chunk-level map-reduce"). That is a real simplification: the merge exists only because the
   calls were split.
3. **Keep a per-source accounting**, not a per-source budget: after the pass, assert that the note
   body produced at least one edge when it contains an assertible clause. The "car loan for the
   Kia" regression is detectable as *zero body-sourced edges on a note with a non-trivial body*,
   and that check is worth more than any prompt sentence. It is also the eval the redesign will
   need to prove it did not regress.

---

## 6. Failure modes: when the agent asks instead of guessing

### 6.1 OCR garbage

The signal already exists and is discarded: `_agreement(vlm_text, rapid_text)` — a
whitespace-collapsed, case-folded `SequenceMatcher.ratio()` (`ocr.py:72-81`) — is logged as
`ocr.crosscheck agreement=…` and never persisted (`ocr.py:438-449`; `RAPIDOCR_PLAN.md` §R2 deferred
persisting it). **Persist it.** It is the cheapest honesty signal on the box:

- **agreement ≥ ~0.9** — the two engines converged; treat the text as verbatim and commit facts
  under the 0.7 cap as today.
- **agreement in the gray band** — the block is *contested*. The agent gets both readings, labeled,
  plus the score, and must not assert a value that differs between them. This is the case where
  `look_again` earns its swap.
- **agreement ≈ 0 with both engines producing text** — one of them is hallucinating. RapidOCR
  cannot hallucinate (it is a detector plus a recognizer); the VLM can (`RAPIDOCR_PLAN.md` §1: "it
  can hallucinate text, which is exactly why OCR-derived facts are confidence-capped at 0.7"). Ask.

There is also a **live selection bug** worth fixing on the way: `_prefer_ocr` prefers the RapidOCR
row whenever it has *any* non-empty text (`ingest/extract.py:96-106`), so the fact pipeline reads
the deterministic engine even when the deterministic engine read three characters off a whiteboard
and the VLM read the whole thing. The VLM row is stored but never chunked
(`ingest/extract.py:130-135`). In the agent model the right move is to stop choosing: hand the
agent **both** rows and the agreement score, and let it say which it trusts — the one judgment a
model is genuinely better at than a `bool(text.strip())` comparison.

### 6.2 A 40-page PDF

Path today: no text layer → `is_scanned_pdf` (`ingest/pipeline.py:274-278`) → `ocr_attachment` →
`ocr_pdf_pages` rasterizes at 200 DPI and loops pages **sequentially**, one `vision.ocr` call each
at up to `max_tokens: 8192` (`ocr.py:171-191`, `ingest/prompts/vision_ocr.prompt`). Then RapidOCR
re-rasterizes and re-OCRs every page again (`ocr.py:399-412`). Forty pages is forty sequential
vision completions in one job, plus forty CPU OCRs, inside one job whose retry backoff is `2^N`
minutes to a 1-hour cap (`queue.py:146-151`).

Implications for the conversation:

- **The pass must not start until this finishes.** The existing gate already guarantees it
  (`ingest/pipeline.py:299-300` enqueues scanned PDFs into `outstanding`), and that is correct;
  do not weaken it.
- **The agent must be told the document was truncated.** `pdf_page_images` truncates at 200 pages /
  512 MiB and logs `vision.pdf_rasterize_truncated` (`imageprep.py:75-81`) — "silently reading half
  a document is the kind of thing that should never be discovered from a wrong answer"
  (`imageprep.py:40-42`). The conversation must carry that as an explicit block, and the agent must
  refuse to assert completeness ("this is your full medical history") over a truncated read.
- **Structured medical/financial documents are already out of scope** and routed to the Phase-7
  typed parsers rather than free-extracted "into hundreds of facts" (`ANALYSIS.md` "Guards"). The
  agent needs a hard stop, not a soft preference: a 40-page EOB is a *detect-and-defer*, and the
  right conversational move is "this looks like a structured statement — I'm not going to mine it
  into facts; do you want it filed?"

### 6.3 A photo of a whiteboard

The adversarial case for every layer at once. RapidOCR's PP-OCR models are trained on printed text
and do poorly on handwriting; the VLM does better but is the engine that can hallucinate; the
caption prompt is instructed *not* to transcribe ("A separate pass transcribes its text verbatim —
do not transcribe", `vision_caption.prompt`), so the caption will summarize a whiteboard's *meaning*
without its content. Expect low agreement, an under-informative caption, and a VLM reading that is
plausible-looking prose.

Rule: **a whiteboard/handwriting read is a look-again candidate by default, and its facts are
proposals, never commits.** The cheap tell is agreement plus RapidOCR's `mean_score`
(`vision/rapidocr.py:74`, currently discarded) — low mean score with non-trivial VLM text is
"handwriting or a hostile capture".

### 6.4 An unreadable receipt

The OCR prompt already contracts for honesty: "write `[illegible]` where you cannot read a word or
region, and never guess" (`ingest/prompts/vision_ocr.prompt`), and the PWA renders the marker
distinctly (`frontend/src/components/ImageExtracts.tsx:19-36`). That marker is a first-class ask
trigger: **an `[illegible]` inside the span that carries a value means the value is unknown, full
stop.** The current pipeline has no such rule — the marker is decoration in the UI and ordinary
text to the extractor.

### 6.5 The ask/guess rule, consolidated

The agent asks the owner when **any** of these hold, and otherwise commits under the machine-read
confidence ceiling:

1. The value's only support is machine-read text **and** the predicate is deterministically
   floored-sensitive (health / finance / precise-location) — this is the existing **I5 sensitive-
   inference net** (`ANALYSIS.md` "Commit-vs-review"), which must survive the redesign verbatim.
2. The two OCR engines disagree on the span carrying the value (agreement below threshold), or
   RapidOCR's `mean_score` is low with non-trivial text present.
3. An `[illegible]` marker falls inside the value.
4. The attachment is a detected structured medical/financial document (defer, do not mine).
5. The media was truncated (PDF page/byte cap, oversized image skipped with no cache row,
   `MAX_PDF_PAGES`, `MAX_OCR_BYTES`) and the fact depends on what was not read.
6. The note body and an attachment **conflict** — the body is the source of truth
   (`analysis/prompt.py:86-90`), so an attachment that contradicts it is a question, not a
   supersession.

Cases 2, 3 and 5 are all computable **before the conversation starts**, from data the pre-digest
already produces. They belong in turn 0 as flags on the source blocks, not as things the agent must
notice.

---

## 7. Provenance: what replaces a span for an image-derived fact

### 7.1 What a citation is today

A fact carries `note_id` + nullable `chunk_id` (`models/analysis.py:179-182`); an entity mention
carries `chunk_id` + `char_start` / `char_end` + `surface_text` (`models/analysis.py:80-85`); a
temporal token the same (`models/analysis.py:115-118`). Spans are resolved by substring search over
the chunk text (`analysis/pipeline.py:200-211`). A chunk carries `attachment_id`, `source_kind`
(`note|text-layer|ocr|caption|transcript|video_analysis`) and `source_anchor` (`page N`, a filename,
an `mm:ss`) (`models/notes.py:168-178`, set at `ingest/pipeline.py:429-435`).

So **an image-derived fact already has no pixel span today** — it has a span into the *OCR text*, on
a chunk that names the attachment and its anchor. `ANALYSIS.md` "Attachments" states the intent
directly: "Chunks built from segments inherit the anchor, so citations can point at *'video X @
02:13'*."

### 7.2 The rule for an agent that looks at pixels

**A fact may only ever cite a chunk. Therefore anything the agent sees must first become text in a
chunk.** This is not a formality — it is what makes deletion, re-analysis and the retraction sweep
work (`ANALYSIS.md` "Reprocessing"; purge repairs supersession chains through `chunk_id`), and it is
what keeps the domain firewall honest ("citations always point at a chunk in the *fact's own
domain*, so no citation ever crosses the firewall").

Mechanically: the deferred `look_again` answer is persisted as an `AttachmentExtract` row
(`kind='vision_answer'`, `tool='<provider>:<model>'`, `source_anchor` = the question asked,
`confidence` ≤ the caption ceiling 0.6 — it is a model's reading, not a transcription), the
re-ingest chunks it, and the fact cites that chunk with a real character span. No schema change; the
existing `build_extract` (`ocr.py:146-168`) just needs the new kind in `EXTRACT_CONFIDENCE`.

Three properties fall out for free: the answer is searchable, it is visible in the Sources card
alongside the OCR and the description, and a re-run of the note does not re-ask the vision model
(the cache-row check at `ocr.py:236-247` is the pattern).

### 7.3 The upgrade worth building: a spatial anchor

Text spans are a weak citation for an image — "characters 40–52 of the OCR of receipt.jpg" is not
something the owner can verify at a glance. The primitive for a real one is **already computed and
thrown away**: RapidOCR returns `lines: [{text, box, score}]` (`vision/rapidocr.py:31-38`,
parsed at `:71-76`) and the pipeline persists only `.text` (`ocr.py:414-436`).

Persisting the line boxes — the same jsonb pattern `transcript.words` and `video_analysis.analysis`
already use (`models/notes.py:170-178`) — gives an image-derived fact a citation the owner can
*see*: a highlighted region on the image with the recognized line and its per-line score. The
existing `ImageExtracts` card already renders a thumbnail strip plus the verbatim OCR inset
(`frontend/src/components/ImageExtracts.tsx:1-6`); overlaying a box on the thumbnail is a small
front-end change on top of data that exists today.

Note the asymmetry this creates and accept it: RapidOCR gives boxes, the VLM does not. So a
box-anchored citation is available exactly when the deterministic engine read the line — which is
also exactly when the reading is trustworthy. The two properties travel together, which is a nice
accident.

### 7.4 What the citation must carry, minimally

For any image-derived fact: `attachment_id`, `source_anchor` (`page N` / filename / `mm:ss`), the
`tool` that produced the text (`rapidocr` vs `provider:model` — already stored,
`models/notes.py:167`), the engine-agreement score, the confidence ceiling that applied, and — where
available — the line box. The **provenance of the reading is part of the fact's identity**, because
"the VLM said 182 lb" and "PP-OCR read 182 lb and the VLM agreed" are different claims, and the
supersession machinery is entitled to know which one it is holding.

---

## 8. What the owner sees, in the conversation, for one image

Today's surface (the Analysis tab's Sources card, `frontend/src/components/ImageExtracts.tsx`):
thumbnail strip → verbatim OCR inset, clamped to ~6 lines and expandable (`OCR_CLAMP_LINES = 6`,
`:17`) with `[illegible]` rendered muted-italic (`:20-36`) → the mined description beneath → a
micro-meta line `kind · tool · confidence%` (`:38-42`) → a per-image **Analyze** action that posts
to `POST /attachments/{id}/analyze` (`api/notes.py:396-417`). The note list shows a derived
lifecycle chip that says `reading image…` while any image's vision cache is empty
(`frontend/src/notes/lifecycle.ts:31-53`).

The conversational version should be the same content, promoted from a tab to a **card inside turn
0**:

- **Thumbnail** — with, once §7.3 lands, tappable line boxes.
- **What I read** — the verbatim OCR, engine-labeled, with the agreement score rendered as a plain
  phrase ("both readers agree" / "the two readers disagree here"), never a bare float.
- **What I understood** — the caption/description.
- **What I'm unsure about** — the flags from §6.5, as sentences, before the agent's own questions.
- **"Ask about this image"** — the owner types a question; it routes to the cheap path first
  (RapidOCR verbatim re-read via the existing `ocr` jerv tool, `docs/plans/RAPIDOCR_PLAN.md` §R3,
  `backend/src/jbrain/agent/ocrtools.py`), and only escalates to a VLM look when the question is
  semantic rather than textual.
- **A visible cost when a look costs something.** If answering requires a residency swap, say so —
  "that needs the vision model, about five minutes; want me to?" — and let the owner decide. This
  is rule 10 territory: the owner operates the box through the PWA, so the PWA must be where a
  five-minute GPU decision is made, and it must be legible.

"What does the third line say?" is the archetypal question and it should be answered **without any
model at all**: RapidOCR returns per-line rows in reading order (`vision/rapidocr.py:36-38`), so the
third line is an array index, not an inference. That is the strongest argument for persisting the
line structure in §7.3 — it turns the most common owner question into a lookup.

---

## 9. Verified vs. assumed, summarized

**Verified from source (every claim above carries its `path:line`):** the dispatcher's routing table
and every chain; the async-job/cache split and its idempotency; the confidence caps and where they
are applied; the size and page bounds; the three-part capture-race gate and the reconciler's settle
clause; the per-source grouping mechanism and the failure it fixed; the discarded agreement score
and the discarded RapidOCR line boxes; `_prefer_ocr`'s unconditional preference for the
deterministic engine; the co-residence measurement; the 198 s cold load; the ~60 s / ~2 s prefill
figures; the 2048–4096 image-token range; the existing `vision_read_spec` no-swap idiom.

**Assumed / extrapolated, flagged in place:** the VL model's own cold-load time (~55 s, scaled from
198 s / 59 GiB — never measured for that model); the ~117.8 GiB admission ceiling (arithmetic from
`residency.py:611-614` and the §E live fraction against a ~124 GiB pool, not a reading); the
long-context salience argument in §5.2 (a general property of attention, not an on-box measurement);
the claim that PP-OCR degrades on handwriting (model-family knowledge, not tested here).

**Two defects found while mapping, neither introduced by the proposed change:**

1. The reconciler's settle window is evaluated against the **client's** capture time
   (`queue.py:635` vs `notes/repo.py:87`), so an offline-flushed note with a promised attachment is
   eligible for body-only integration immediately — defeating the capture-race gate in exactly the
   offline-for-hours scenario it was built for (§3.2).
2. `_prefer_ocr` (`ingest/extract.py:96-106`) chunks the RapidOCR reading whenever it is non-empty,
   discarding a better VLM reading on handwriting and other non-print captures (§6.1).

**One doc correction:** `docs/reference/ANALYSIS.md:3` cites migration 0154 for
`notes.attachments_expected`; it is `0156_note_attachments_expected.py`.

---

## Open questions for the owner

1. **Co-residence or swap?** The box has measurably held gpt-oss-120b and a vision 27B together at
   93.73 GiB (`local_catalog.py:213-217`). Do you want the ingest design to *depend* on that (fast
   look-again, tighter memory margin, more exposure to the runaway guard), or to assume swapping and
   keep the deferred-batch path as the only path?
2. **Or one model for the whole pass?** Running the ingest conversation on `qwen3.8-27b-q4` or
   `qwen3-vl-30b-a3b` removes the vision/text split entirely — images ride inline and encode once
   (`api/agent.py:891-895`). What are you willing to give up in reasoning strength to make looking
   free?
3. **Why is the live vision route the abliterated checkpoint?** The catalog says it is a red-team
   probe that should never serve a real task, and that its embedded prompt lands in front of every
   JBrain system prompt (`local_catalog.py:824-828`). Ingest reading your notes and attachments
   through it is a different exposure than a sandbox probe.
4. **How many look-agains is a note worth?** At the swap price that is minutes of box time per
   question. Is the ceiling 1, 2, 3 — or a budget the owner grants per note when asked?
5. **When an attachment never arrives, should the conversation ask or proceed?** Today it silently
   body-only integrates after 300 s (and, per §3.2, often immediately). The conversational option is
   to ask. Asking costs a notification; proceeding costs a wrong first analysis.
6. **Should the deferred vision answer be visible as its own product?** It would appear in the
   Sources card next to the OCR and the description, labeled with the question that produced it.
   Useful audit trail, or clutter?
7. **Is the line-box citation worth the migration?** One nullable jsonb column on
   `attachment_extracts` (RLS-covered, so an isolation test) buys clickable, verifiable image
   citations and turns "what does the third line say?" into a lookup. `RAPIDOCR_PLAN.md` §R2 already
   named this as the open sub-decision.
8. **What is the runaway bound once `fact_cap` is gone?** The per-note ceiling was doing two jobs;
   the redesign fixes the crowding-out one and deletes the runaway one. What stops an agent writing
   300 edges off a receipt, and does it still file an `extraction_truncated`-style card when it
   decides to stop?
