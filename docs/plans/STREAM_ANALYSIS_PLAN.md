# analyze_stream — build plan (URL-sourced video/stream analysis)

> **Status:** In progress · **Last verified:** 2026-07-17 · **Waves:** W1✅ W2◻️ W3◻️

Give **jerv** a sense it doesn't have: pull frame(s) — and optionally the audio —
from a **video URL**, live or on-demand, so the model can actually *see* what a
stream shows instead of only reading a page's text. The motivating case: jerv is
asked "is Booster 20 still on the mount?" with nothing but a `youtube.com/live/…`
link — today it can `web_fetch` the page text but cannot look at the video.

This is the **URL-sourced sibling of `analyze_video`** (attachment-sourced,
`docs/archive/VIDEO_ANALYSIS_PLAN.md`). It reuses that pipeline's map→fuse→reduce
core wholesale; the only genuinely new capability is turning a stream URL into
frames/audio bytes. It also covers the finite case — *"analyze this YouTube
video"* — not just live streams.

Peer to `../reference/PROCESS.md` (the wave loop), `../reference/DEVELOPMENT.md`
(standards), and `../reference/ASSISTANT.md` (the jerv sandbox + the egress
invariant this plan carefully extends).

## Why it fits (the lean litmus, ASSISTANT.md)

Reuses the LLM adapter (`agent.vision` / `video.summarize` router tasks), the
storage abstraction (frame JPEGs as content-addressed blobs), the on-box
ffmpeg/whisper machinery, and the existing `video_analysis` card. It adds **one
small, well-shaped tool** and **one new dependency** (yt-dlp). One person can
reason about it: it is `analyze_video` with a different front door.

## Owner decisions (locked)

- **Tool:** `analyze_stream` — jerv-only, `permission: web`, `cost_class:
  expensive`. Sits beside `analyze_video`; the attachment tool is unchanged.
- **Resolver: yt-dlp (broad).** Handles YouTube (live + VOD) and most stream
  sites; resolves a page/watch URL to the direct media manifest without a
  headless browser. It is a **pure pip dependency** (`backend/pyproject.toml` +
  `uv.lock`), so it flows through the existing `uv sync` in `scripts/dev-setup.sh`,
  the Dockerfile, and CI — no image or workflow edit is needed (it reuses the
  ffmpeg already installed for `analyze_video`); the dep-of-note comment in
  `dev-setup.sh` and the `test_stream_deps.py` smoke test satisfy non-negotiable
  #8. A `ytdlp_available()` gate mirrors `ffmpeg_available()`;
  the sidecar is **dropped from the registry** when ffmpeg **or** yt-dlp is
  absent (graceful degrade, exactly like the image/whisper/analyze_video gates).
- **One tool, two source shapes, three sampling modes:**
  - **`single`** — one frame (live edge for a live stream; a seek point, default
    midpoint, for a VOD). The fast path for "what does it show right now?" —
    effectively `analyze_image` over a stream, no whisper, no reduce call.
  - **`window`** — **N frames across a Y-second window** (`frames` × `window_s`);
    for a live stream the window is anchored at the **live edge**, for a VOD at
    an optional `seek` offset. Optional **whisper** transcribes the same Y-second
    audio segment; frames + transcript fuse on the `[mm:ss]` timeline and reduce
    to a summary — the full `analyze_video` treatment over a bounded slice.
  - **`full`** — a finite VOD only: K evenly-spaced frames across the whole video
    (the classic `analyze_video` sampling), + whisper. Refused for a live stream
    (unbounded) with an actionable error telling the model to use `window`.
- **Whisper is opt-in per call and best-effort** (default on when the gateway is
  configured), same posture as `analyze_video`: frames-only when whisper is
  absent or `transcribe:false`.
- **Card: reuse the existing `video_analysis` view** with `source:"stream"` and
  the stream's title/URL surfaced as a chip. **No new GUI component → no 3-mock
  GUI gate** (PROCESS.md). *If* the owner later wants a visually distinct live
  card (a LIVE badge, a refresh action), that is a new surface and takes the
  3-mock gate — flagged, not assumed.
- **No cache in v1.** A live stream is non-idempotent (the frame changes every
  second), so caching by URL would be wrong; the tool runs **in the turn** like
  the other chat-media tools. A VOD-by-URL cache keyed on `(url, params)` is a
  named **v2**, not this plan.
- **Bounded, always.** Hard caps on `frames` (≤ the existing 24), `window_s`, the
  audio-segment length, the resolved media bytes ffmpeg reads (via
  `--download-sections` / `-t`, never a whole-file download), and a wall-clock
  timeout. Absurd probed durations are clamped (reuse `jbrain.media`).

## The security crux (the second sanctioned outbound leg)

Invariant #9 forbids an arbitrary fetch/HTTP tool; jerv's `web_fetch` is the one
sanctioned **direct** outbound leg, SSRF-guarded per hop. `analyze_stream` is a
**second** such leg and is documented as one in ASSISTANT.md. It is bounded the
same way:

- **jerv-only, enforced at dispatch** (the allowlist, not just visibility) — jerv
  holds no knowledge-base tools and reads no owner domain data, so there is no
  personal context to smuggle into a resolved URL. `curator` never gets it.
- **SSRF on the resolved media hosts.** yt-dlp resolves a watch URL to CDN hosts
  (`*.googlevideo.com`, …) the model doesn't control; **every resolved media URL
  is run through the shared `guard_public_host`** (reused verbatim from
  `web/fetch.py`) before ffmpeg opens it, and any private/loopback/link-local/
  reserved target is refused — so a crafted URL can't turn ffmpeg into a read
  primitive against `db:5432` or `169.254.169.254`.
- **yt-dlp is constrained, not trusted:** invoked as an **argv list, never a
  shell string** (URL is data), off the event loop; `--no-playlist`,
  **https-only** protocol allowlist, **no post-processors / no external
  downloader / no cookies / no plugins**, bounded `--max-filesize` and
  `--download-sections`, and its own timeout. The input URL is `http(s)`-validated
  before it is ever passed.
- **Results are data (#1):** the summary/transcript are wrapped in the
  data/instruction boundary like every tool result; the card carries **ids only,
  no URLs** (#9) — the component builds the thumb srcs from `thumb_id` blobs.
- **Honest confidence:** machine-watched/heard content sits at the caption
  ceiling (ANALYSIS.md); nothing here mints a citable fact — it is jerv chat
  output, and jerv writes no episodic memory.

## Reuse map

| Need | Reuses (unchanged) | Net-new |
|---|---|---|
| Frame downscale + dHash dedup | `jbrain.media.sample_frames` internals | A window/live sampler that feeds bytes from a URL, not a file |
| Caption → fuse → summarize | `ingest/video.run_video_analysis` map→fuse→reduce | Factor a `fuse_and_reduce(frames, transcript, …)` core so the URL path skips the bytes+`sampler` assumption |
| Whisper on a segment | `TranscribeClient` / `LocalGateway` | Feed it a bounded audio segment instead of a full attachment |
| Owner-facing card | `video_analysis` view (`videotools._video_view`) | A `source:"stream"` variant + source chip |
| SSRF guard | `web/fetch.guard_public_host` | Applied to the resolved media hosts |
| Degrade gate | `ffmpeg_available()` + registry drop (readtools) | `ytdlp_available()`, same pattern |

## Waves

### Wave 1 — The stream sampler (`jbrain.stream`, new module) ✅
The one genuinely new media primitive. Shipped in `backend/src/jbrain/stream.py`:
`ytdlp_available()`; `resolve_stream(url)` (yt-dlp Python API, off-loop, constrained
opts — `noplaylist`, `skip_download`, height-capped `format`, socket timeout, no
cache — → a `ResolvedStream` of media URL / title / `is_live` / duration, with the
resolved host run through the shared `guard_public_host` SSRF guard); and
`sample_stream(resolved, *, frames, window_s, seek_s, want_audio, …)` → a bounded
ffmpeg pass writing ms-stamped, downscaled, **dedup-shared** JPEGs across the window
(`frames <= 1` is the single-grab fast path) + a best-effort second pass for the
window's audio as 16 kHz mono WAV. Never buffers a whole file (`-ss`/`-t`, per-read
`-rw_timeout`, wall-clock timeout). `jbrain.media._dedup` was promoted to a public
`dedup_frames` so both samplers share the exact perceptual dedup. yt-dlp is a pip
dep (pyproject + lock); `test_stream_deps.py` is the rule-#8 smoke test. Unit tests
(`test_stream.py`) run ffmpeg against a synthetic local clip (frames path stands in
for a media URL) and fake the resolver for the pure selection/guard cases — **all
offline**, skipping the ffmpeg-backed tests when ffmpeg is absent, matching the
`analyze_video` discipline. Residual for the W3 red-team: ffmpeg fetches an HLS
manifest's **segment** URLs itself, so only the top media host passes our guard —
the segment-host case is the red-team's to close (a validating proxy or a
manifest-host allowlist).

### Wave 2 — Shared reduce core + the `analyze_stream` tool
Refactor `ingest/video.run_video_analysis` to expose a **`fuse_and_reduce(frames,
transcript, *, router, filename, on_progress)`** core; the attachment path keeps
its `data: bytes` + `sampler` front and calls it (behaviour byte-identical, guarded
by its existing tests). New `agent/streamtools.py::build_stream_handlers` →
`analyze_stream`: validate URL + params (reject `full` on a live stream, clamp
caps), call `sample_stream`, transcribe the segment (best-effort), run
`fuse_and_reduce`, return the summary + a `video_analysis` view (`source:"stream"`,
stream title/URL chip). Add `agent/tools/analyze_stream.tool` (jerv-only, `web`,
`expensive`, params: `url`, `mode`, `frames`, `window_s`, `seek`, `transcribe`).
Registry wiring (readtools) offers it only when **both** ffmpeg and yt-dlp are
present. Tests: adapter-fake multi-turn tool test (scripted `tool_use`), handler
tests with a faked sampler, `.tool` sidecar validity + version-bump guard.

### Wave 3 — Docs, egress hardening, card polish
`ASSISTANT.md`: document `analyze_stream` as the **second jerv-only sanctioned
direct outbound leg** in the web-exception section, and add it to the SERVICES.md
tool inventory. `video_analysis` card: render the stream source chip for a
`source:"stream"` payload (DESIGN.md note; still ids-only, no external load, #9).
**Wave-level red-team** on the SSRF / yt-dlp / ffmpeg surface (PROCESS.md mandates
a security gate for any egress/scope-touching wave): argv injection, resolver
protocol allowlist, redirect-to-private, oversized/slow-loris segment, a
non-video URL, a URL resolving to a private host. Reconcile
`scripts/dev-setup.sh` + compose/Dockerfile; add the ROADMAP one-liner.

## Out of scope (named, not silently dropped)

- **VOD-by-URL caching** (v2) — a non-live result keyed on `(url, params)`.
- **The note pipeline reading a stream URL** — this is a jerv chat tool only; a
  URL is not an owner note and mints no fact.
- **A distinct live card / auto-refresh** — reuse the `video_analysis` card; a new
  surface would take the 3-mock GUI gate.
- **Non-yt-dlp resolvers** (streamlink) — yt-dlp broad covers the target sites.
