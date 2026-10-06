# Native video on Flash-Next — the one-minute hybrid

> **Status:** In progress · **Last verified:** 2026-10-06 · **Waves:** V0🟡 V1✅ V2✅ V3✅

Flash-Next's engine accepts video directly (`/props` on the box, llama.cpp b11332-869034b:
`modalities {vision: true, video: true, audio: false}`), but the app never sends it any.
Every video today is cut into ≤24 stills, each captioned by its own `agent.vision` call,
the audio whispered, and the text timeline reduced by `video.summarize`
(`ingest/video.py:180-290`). The model never sees motion, and a chat-attached video never
reaches it at all (`agent/attachment_content.py:168`).

**The owner's rule (2026-10-04): a clip of one minute or less goes to Flash-Next natively;
anything longer, or of unknown length, keeps today's frame pipeline.** The audio is always
whispered — the model cannot hear — and the transcript rides beside the video as text.

## 1. What the engine does with a video (read from source at 869034b, not yet measured)

- The OpenAI-compatible endpoint takes a content part
  `{"type": "input_video", "input_video": {"data": "<base64>"}}` (`tools/server/README.md`).
  The server decodes it with **ffmpeg/ffprobe found on PATH inside the engine container**,
  else it answers 400 "video input is not supported".
- It samples at `--video-fps` (server-wide, default **4**) and emits a text timestamp every
  `--video-timestamp-interval` ms (default 5000). There is no per-request fps.
- Qwen-style models merge **two consecutive frames into one image's worth of tokens**
  (`mtmd.cpp` `[QWEN_VIDEO]`). Each pair is sized by the same image token floor and ceiling
  as a photo: our `--image-min-tokens 2048`, llama.cpp's default max 4096.
- So the cost is **≈ fps/2 × 2048–4096 tokens per second of video.** At the default 4 fps
  that is ≥ 4,000 tok/s — a 256k slot holds about 50 s. At **1 fps** it is ≈ 1,000–2,000
  tok/s, so a 60 s clip is ≈ 60–120k tokens.
- The floor is server-wide and is what keeps photo reading and OCR sharp, so it stays.
  Video length is bought with fps, not with a lower floor.

## 2. Design

**The split.** `probe_duration_s` (`media.py:88`) reads the clip. `≤ 60 s` and a
video-capable engine → native. `> 60 s`, unknown duration, or a model without video →
frames. The threshold is one named constant, not a setting.

**Native call shape.** The api transcodes before sending, with the ffmpeg it already has:
first 60 s only, video at the measured fps (CFR), longest edge ≤ 1280, no audio track,
Motion-JPEG in Matroska (not H.264: see V0). That keeps the base64 body a few MB rather
than a raw phone clip's 100+ MB; resolution below the floor buys nothing, since the engine
upscales each pair to it. The request carries the transcoded clip plus the whispered transcript as `[mm:ss]` text lines,
which line up with the engine's own 5 s timestamps.

**Adapter (non-negotiable 1).** A new `LlmVideo(media_type, data)` beside `LlmImage` in
`llm/types.py`, a `videos` field on the request, and `input_video` serialisation in
`llm/openai_compat.py`. Every other provider rejects a video part with a typed error; the
gate below means none should ever receive one.

**Gate.** `supports_video` on `LocalModel` (true only for `qwen3.8-flash-next`) and an
engine-aware `providers.supports_video_for_spec`, mirroring `supports_vision_for_spec`
(`providers.py:155`). The native branch runs only when the task's *effective* spec (after
the Flash-Next remap) supports video.

**Pool fit.** `slot_roles.estimate_prompt_tokens` gains a video charge,
`ceil(seconds × fps / 2) × per-pair tokens`, using the measured per-pair figure from V0.
A clip that would not fit its role's cap takes the frame path rather than failing.

**Fallback, never a dead end.** Any native failure — 400, `SlotCapError`, timeout, the
pool busy — falls back to the frame pipeline in the same call, and the run step records
which path ran and why.

**Output stays the same shape.** `run_video_analysis` still returns the summary, the
`analysis` dict (duration, timeline, transcript) and stored frame thumbnails. On the native
path the thumbnails are sampled for display only, not captioned. Callers do not change.

## 3. Waves

### V0 — Make the engine able to take video, and measure it 🟡
- `deploy/Dockerfile.flash-next`: ensure `ffmpeg` and `ffprobe` are on PATH in the runtime
  stage (install through the base's package manager, as the build stage does) and fail the
  image build unless ffmpeg lists the `mjpeg` decoder and `ffprobe -version` runs.
- **The transcode targets Motion-JPEG in Matroska, not H.264 in MP4**, because the engine
  image's base is Fedora, whose `ffmpeg-free` carries no H.264 decoder we can count on;
  `mjpeg` is always in it. The cost is a larger body (a few MB a minute at 1 fps), which is
  what V0 measures as `payload_bytes`.
- Catalog: `--video-fps 1` in Flash-Next's `extra_server_args` (V0 confirms or moves it).
- `LlmVideo`, the `videos` request field and `input_video` serialisation, with a
  provisional pool charge (one image charge per two frames at `slot_roles.VIDEO_FPS`, plus
  the extra frame ffmpeg's fps filter can emit, and a full minute when the length is
  unknown) so the probe cannot overrun its slot; V1 sets it from the measurement.
- Debug route `POST /api/debug/video` `{attachment_id, mode: native|frames, question?,
  max_tokens?, spec?}` (and `/video-async`) and `debug-connect.sh video`: runs one clip
  either way and returns prompt tokens, latency and the answer. `spec` pins the native call
  to one model (`local:qwen3.8-flash-next`) without re-routing `video.summarize`. This is the measuring instrument, operable with a token and no terminal.
- **On-box, with notice to the owner:** two or three clips of 10–60 s. Record tokens per
  second, per-pair tokens, prefill time and a side-by-side of native vs frames answers.
  These fix the fps, the pool charge and whether 60 s fits the 128k roles.

### V1 — The hybrid in the video tools ✅
- `supports_video` flag, `supports_video_for_spec`, the pool charge.
- **The native gate keys on the engine and model (`supports_video`), never on
  `provider == "local"`.** The Standard engine is "local" too, and its models have no video
  path; V0's client check (`openai_compat` refuses video off the local provider) is only a
  backstop against cloud routes, not the gate.
- The native branch in `run_video_analysis` with the fallback and the run-step note;
  `analyze_video` and video-attachment ingest inherit it.
- Tests: the split at 60 s, unknown duration, gate off on the Standard engine and cloud
  routes, every fallback trigger, the serialised part, the charge.

### V2 — Chat and links, through a separate call ✅
- **The clip never enters jerv's own context** (owner decision 2026-10-04 — narrowed by V3 on
  2026-10-06: a clip within 64k tokens now does). A minute of video
  is 60–120k tokens; inline, it would crowd the conversation out of the interactive slot and
  be carried, or re-read, on every later turn. Instead a chat-attached video of ≤ 60 s goes
  to `analyze_video` with jerv's question: one native call holding the clip and its
  transcript, returning text only.
- That call is pinned to one role, so the clip's prefix stays cached in the slot and a
  follow-up question on the same clip re-reads it from cache rather than re-prefilling it.
- `analyze_stream`: a downloaded clip of ≤ 60 s takes the same native path.
- `jerv.prompt`: ask `analyze_video` about a video, with the question; a follow-up is
  another call with the new question.

## 3a. As built (V1 + V2, 2026-10-06)

- **V0** shipped its code in #1557 (engine ffmpeg, `LlmVideo`, the provisional charge, the
  `/video` probe). Its measurement is partly done — one clip, below — so it stays 🟡 until
  two or three clips of 10–60 s settle open question 1.
- **Gate.** `LocalModel.supports_video` (true only for Flash-Next) and
  `LlmRouter.supports_video(task)`, which reads the route after live overrides and the engine
  remap, so a Standard pick served by Flash-Next qualifies and a Standard-engine model never
  does. No `providers.supports_video_for_spec`: every native caller holds a router.
- **Route and slot.** The native call routes by `video.summarize` (one Settings pick decides
  both paths) and is pinned to `slot_roles.NATIVE_VIDEO_ROLE` = the 256k scheduled slot —
  a minute's charge (~127k) does not fit the 128k workshop slot with a transcript and an
  answer (open question 2, resolved).
- **The split** (`ingest/video.py` `run_video_analysis`): frames are always sampled (they are
  the card's thumbnails), the audio always whispered; the model gate is checked before any
  probe; then `media.native_clip` probes and transcodes only a clip of known length ≤ 60 s.
  Any `TranscodeError` or `LlmError` (an empty answer included) falls back to captioning the
  same frames in the same call. `analysis.path` (`native`/`frames`) and `analysis.fallback`
  record what ran and why; the native path also stores the transcoded clip as a blob
  (`native_clip_id`).
- **Chat (V2).** `analyze_video` takes `question`. The first call writes the cached summary,
  then asks the question on the same clip prefix (fixed system prompt, clip, transcript —
  the request goes last), so only the question is new prefill. A follow-up re-sends the stored
  clip bytes with the new question instead of re-running anything. A frames-read clip answers
  from its summary, as before. The clip never enters jerv's context.
- **Links (V2).** `analyze_stream` in `full` mode on a finite video ≤ 60 s transcodes the
  first minute straight from the resolved URL (`stream.native_stream_clip`, same protocol
  guard, headers and stall timeout as every other read) and watches it; `window`/`single` and
  live streams keep reading stills.
- **Output budget.** The native call allows 8,192 output tokens: Flash-Next's thinking
  spends from the same budget, and at the first 1,536 a careful answer about an 11.6 s clip
  was cut off mid-sentence on the box (2026-10-06).
- **Order change.** Whisper now runs before frame captioning, since the native call needs the
  transcript first; the live status reads "Extracting frames… → Transcribing audio… →
  Watching the video…" on the native path.

### Measurements

| Clip | Length | Payload | Prompt tokens | Transcode | Call | Notes |
|---|---|---|---|---|---|---|
| Hand-held numbers (phone, 2026-10-06) | 7.4 s | 547 KB | 10,460 | 0.6 s | 77 s (655 output tokens, thinking on) | ≈ 2.5k tokens per frame pair; the charge booked 20.5k. Answered the question with an ordered, timestamped sequence (1, 2, 0, 3, 1). The frame pipeline on the same clip took 238 s and named the gestures (fist, one, two, three fingers) but in no order — per-frame captions lose the sequence. |

### V3 — A short clip inside jerv's own context ✅
See §5.

## 4. Open questions
1. **fps 1 or 2.** One is cheaper and fits every role at 60 s; two sees faster motion.
   V0's numbers decide.
2. **Which role a native video ingest runs in.** If 60 s at the chosen fps exceeds the
   128k ingest cap, pin the call to a 256k role rather than shorten the threshold.
</content>
</invoke>

## 5. Inline video in jerv's context (V3, owner 2026-10-06)

V2's separate call answered the question but not naturally: jerv read a tool's text about the
clip instead of seeing it, and every follow-up was another tool call. The owner reversed the
2026-10-04 rule for short clips: **a clip whose charge fits 64k tokens rides jerv's own
conversation**, the way an attached image does; anything longer keeps the V2 tool path.

- **Budget.** `agent/inline_video.py` `INLINE_VIDEO_BUDGET_TOKENS` = 65,536, shared by every
  clip in view at once — this turn's first, then the recent turns' oldest-first. At the
  engine's 1 fps that is about 31 s for a single clip (`INLINE_MAX_SECONDS`); a longer one is
  never transcoded for inlining.
- **Gate.** The chat model sees images and `LlmRouter.supports_video("agent.turn")` (the
  per-chat model pick included), and ffmpeg is on the api (`app.state.inline_videos`).
- **Cache-stable by construction.** The clip is transcoded once, kept as a blob, and its
  record — blob id, seconds, token charge, rendered transcript — cached on the attachment row
  (`turn_attachments.native_clip`, migration 0221; separate from analyze_video's `analysis`).
  Every turn re-reads that record, so the bytes and the note are identical and the engine's
  prefix cache holds the clip. A clip too long, or one ffmpeg cannot transcode, is recorded
  too, so it is probed once.
- **Placement.** Exactly the image anchor (`api/agent.py`): the attaching turn renders its
  question as the client will echo it, then one anchor message with the clip and its note;
  later turns in the recent window re-insert that anchor after the same history entry. The
  client decorates video attachments like images (`useFullBrain.ts` `historyContent`, same
  marker — the cache contract). A clip whose turn cannot be matched is not carried on the
  volatile tail (that would re-read it every turn); its id stays in the history text.
- **What the model is told.** The anchor note says the clip is in view (about 1 fps, its
  length), gives the whispered transcript as `[mm:ss]` lines (the model cannot hear), and says
  not to call `analyze_video` for it. A video not inlined gets a note pointing at
  `analyze_video` with the owner's question.
- **Adapter.** `UserMessage.videos` serialises as `input_video` parts on the local engine;
  the xAI and Anthropic adapters refuse a clip. The router books each clip's charge against
  the chat slot and skips prefill calibration for a turn carrying one.

