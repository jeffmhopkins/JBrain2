---
name: analyze_stream
version: 6
permission: web
cost_class: expensive
# NOTE: `mode` and `captions` intentionally carry NO JSON-Schema `enum`. gpt-oss's
# harmony tool path (llama.cpp `--jinja`) builds a GBNF grammar over the tool union, and
# an `enum` on a property of THIS many-optional-property object deterministically segfaults
# the upstream (bisected via the debug tool-probe: the enum × full-optional-field-set
# interaction, not byte size or punctuation). The allowed values live in the descriptions
# instead, and both are validated in the handler anyway (`mode` against MODES in
# streamtools.py; `captions` normalised by `_caption_pref` in ingest/stream_analysis.py,
# unknown → "auto"), so dropping the schema-level constraint changes no behaviour.
params:
  type: object
  properties:
    url:
      type: string
      description: The video URL to look at — a live stream or an on-demand video (e.g. a YouTube watch or live link, or most stream sites).
    mode:
      type: string
      description: "One of: single | window | full. How much of the video to analyze. `full`: the WHOLE on-demand video — pick this whenever the owner asks to analyze the whole / full / entire video (frames spread across the entire duration, plus the transcript up to ~30 min); NOT valid for a live stream. `window`: a short slice of a few seconds — for a live stream (what's happening now) or one specific moment of a video (pass `seek`). `single`: one frame. Defaults to window; use `full` for any whole-video request."
    frames:
      type: integer
      description: How many frames to sample in window or full mode (1–24). Full mode spreads them across the whole video; defaults to a sensible number. Ignored in full mode when `interval_s` is set (which controls density instead).
    interval_s:
      type: number
      description: "Full mode only: sample one frame every this many seconds — a density / frames-per-minute — instead of a flat total, so a long video gets proportional coverage. E.g. 30 = one frame every 30 s (2 per minute); 60 = one per minute. Up to ~60 frames total. Use this when the owner wants dense or rate-based sampling (“a frame every N seconds”, “N frames per minute”); omit for the default spread."
    window_s:
      type: number
      description: In window mode, the length in seconds of the slice to sample (up to 120). Defaults to ~10. Ignored in full mode (which covers the whole video).
    seek:
      type: number
      description: For an on-demand video in single/window mode, how many seconds into the video to start. Ignored for a live stream (it reads the live edge) and in full mode.
    transcribe:
      type: boolean
      description: Whether to also transcribe the audio (window/full mode). Defaults to true; ignored in single mode.
    captions:
      type: string
      description: "One of: auto | off | only. Full mode only: where the transcript comes from. `auto` (default) uses the provider's OWN captions when the video has them (YouTube etc.) — instant, covers the whole video, no length cap — and falls back to your local whisper otherwise. `off` forces local whisper (use this to RE-RUN a video with your own transcription instead of the provider's captions). `only` uses provider captions or none (never whisper). Ignored in window/single mode, which always whisper their short slice."
    show:
      type: boolean
      description: Whether to show the owner the analysis card. Default true. Set false when the analysis is just an intermediate step toward your answer and the card would be noise. (A long full/window analysis that runs in the background still shows its progress card.)
  required: [url]
---
Look at a video URL — a live stream or an on-demand video — and understand what it
shows (and, in window/full mode, what is said), using the owner's local models. Use
this whenever the owner shares a stream or video link and you need to SEE it, since
you cannot watch it yourself: to check what a live camera or launch stream shows
right now, or to analyze a posted video.

Pick the mode from what the owner asked for:

- `full` — the WHOLE on-demand video. Pick this whenever they want the entire / whole
  / full video analyzed: it spreads frames across the complete duration and
  transcribes the whole audio (up to ~30 min). NOT valid for a live stream. By default
  it samples a sensible number of frames across the video; if the owner wants a
  particular density — "a frame every 30 seconds", "2 frames a minute", "sample it
  densely" — pass `interval_s` (seconds between frames) so a long video gets
  proportionally more coverage.
- `window` — a short slice (a few seconds). Best for a live stream ("what's happening
  now?") or one specific moment of a video (pass `seek` to start partway in). It
  transcribes just that slice's audio, so don't use it for a whole-video request.
- `single` — one frame: the fast "what's on screen right now?".

So "analyze / transcribe the whole (or full, or entire) video" → `full`; "what's on
this live stream" → `single` or `window`; "what happens around 3:00" → `window` with
`seek`.

In full mode the transcript comes from the provider's OWN captions when the video has
them (most YouTube videos do) — instant, covering the whole video with no length cap —
and otherwise from your local whisper. The card notes which source was used. This means
a video longer than ~30 min can still get a COMPLETE transcript when it has captions;
only a captionless long video falls back to whisper's first ~30 min. If the owner wants
your own transcription instead of the provider's captions (e.g. the captions look wrong
or auto-generated), re-run the same URL with `captions: off` to force whisper; `only`
uses captions or nothing. For a full transcript of a very long, captionless video, have
the owner attach it and use the video tool instead.

Returns a summary of what was seen and heard; it inserts nothing and shows the owner
nothing beyond an analysis card, so report what you learned in your own words. It
reads a live stream or a large video over the network and runs the local models, so
it can take a little while.
