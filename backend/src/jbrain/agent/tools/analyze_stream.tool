---
name: analyze_stream
version: 2
permission: web
cost_class: expensive
params:
  type: object
  properties:
    url:
      type: string
      description: The video URL to look at — a live stream or an on-demand video (e.g. a YouTube watch or live link, or most stream sites).
    mode:
      type: string
      enum: [single, window, full]
      description: "How much of the video to analyze. `full`: the WHOLE on-demand video — pick this whenever the owner asks to analyze the whole / full / entire video (frames spread across the entire duration, plus the transcript up to ~30 min); NOT valid for a live stream. `window`: a short slice of a few seconds — for a live stream (what's happening now) or one specific moment of a video (pass `seek`). `single`: one frame. Defaults to window; use `full` for any whole-video request."
    frames:
      type: integer
      description: How many frames to sample in window or full mode (1–24). Full mode spreads them across the whole video; defaults to a sensible number.
    window_s:
      type: number
      description: In window mode, the length in seconds of the slice to sample (up to 120). Defaults to ~10. Ignored in full mode (which covers the whole video).
    seek:
      type: number
      description: For an on-demand video in single/window mode, how many seconds into the video to start. Ignored for a live stream (it reads the live edge) and in full mode.
    transcribe:
      type: boolean
      description: Whether to also transcribe the audio (window/full mode). Defaults to true; ignored in single mode.
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
  transcribes the whole audio (up to ~30 min). NOT valid for a live stream.
- `window` — a short slice (a few seconds). Best for a live stream ("what's happening
  now?") or one specific moment of a video (pass `seek` to start partway in). It
  transcribes just that slice's audio, so don't use it for a whole-video request.
- `single` — one frame: the fast "what's on screen right now?".

So "analyze / transcribe the whole (or full, or entire) video" → `full`; "what's on
this live stream" → `single` or `window`; "what happens around 3:00" → `window` with
`seek`. A video longer than ~30 min transcribes only its first ~30 min in full mode
(it still samples frames across all of it) — for a full transcript of a very long
video, have the owner attach it and use the video tool instead.

Returns a summary of what was seen and heard; it inserts nothing and shows the owner
nothing beyond an analysis card, so report what you learned in your own words. It
reads a live stream or a large video over the network and runs the local models, so
it can take a little while.
