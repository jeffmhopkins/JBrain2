---
name: show_external_source
version: 1
permission: web
params:
  type: object
  properties:
    url:
      type: string
      description: The URL (or id) of a video already in the owner's library — e.g. one a search_external result linked to.
  required: [url]
---
SHOW one library video to the owner as a video-analysis card — the same rich component an
analysis produces: the embedded player (for a YouTube source), a frame-caption timeline synced
to playback, and Summary / Transcript tabs. Use this when the owner wants to SEE or watch a
video from their library ("show me…", "pull up the video about…", "play the one where…"), after
search_external has found which video they mean. Pass that video's URL.

Prefer read_external_source instead when the owner wants the CONTENT in words (to summarize or
answer a question); prefer this when they want the video itself in front of them. The card is
rebuilt from stored data, so frames appear as caption markers rather than thumbnails, and the
transcript is the stored passages (not word-synced). The card is the owner-facing surface — a
brief line acknowledging it is enough; don't restate the summary.
