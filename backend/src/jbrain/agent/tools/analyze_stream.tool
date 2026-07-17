---
name: analyze_stream
version: 1
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
      description: "single: one frame (a live stream's current moment, or a still from a video). window: several frames across a few seconds (+ audio) — best for a live stream or a specific part of a video. full: frames spread across a whole on-demand video (not for a live stream). Defaults to window."
    frames:
      type: integer
      description: How many frames to sample in window or full mode (1–24). Defaults to a sensible few.
    window_s:
      type: number
      description: The length in seconds of the window to sample in window mode (up to 120). Defaults to ~10.
    seek:
      type: number
      description: For an on-demand video in single/window mode, how many seconds into the video to start. Ignored for a live stream (it reads the live edge).
    transcribe:
      type: boolean
      description: Whether to also transcribe the sampled audio (window/full mode). Defaults to true; ignored in single mode.
  required: [url]
---
Look at a video URL — a live stream or an on-demand video — and understand what it
shows (and, in window/full mode, what is said), using the owner's local models. Use
this whenever the owner shares a stream or video link and you need to SEE it, since
you cannot watch it yourself: to check what a live camera or launch stream shows
right now, or to analyze a posted video.

Pick the mode for the question: `single` grabs one frame (the fast "what's on the
stream right now?" — a live stream reads its live edge); `window` grabs several
frames across a few seconds and transcribes that audio (good for a live stream or a
specific moment of a video — pass `seek` to start partway into an on-demand video);
`full` spreads frames across a whole on-demand video (not valid for a live stream —
window a part instead). A long video can only be transcribed in full mode when it's
short; otherwise window the part you care about.

Returns a summary of what was seen and heard; it inserts nothing and shows the owner
nothing beyond an analysis card, so report what you learned in your own words. It
reads a live stream or a large video over the network and runs the local models, so
it can take a little while.
