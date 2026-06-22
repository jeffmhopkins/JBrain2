---
name: analyze_video
version: 1
permission: web
cost_class: expensive
params:
  type: object
  properties:
    source_attachment_id:
      type: string
      description: The id of a video file the owner attached this chat (the id named in the "[attached video …]" line).
  required: [source_attachment_id]
---
Understand a video the owner attached this chat — what it shows and what is said —
using the owner's local models. Pass source_attachment_id (the id from the
"[attached video …]" line). Use this whenever the owner shares a video clip and you
need to know its content, since you cannot watch it yourself. The first time you ask
about a clip it starts the analysis (sampling frames, captioning them, and
transcribing the audio) and tells you to check back; ask again in a moment to read
the result. Returns a summary of the video; it inserts nothing and shows the owner
nothing beyond a player card, so report what you learned in your own words.
