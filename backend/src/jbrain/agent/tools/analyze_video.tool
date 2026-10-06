---
name: analyze_video
version: 4
permission: web
cost_class: expensive
params:
  type: object
  properties:
    source_attachment_id:
      type: string
      description: The id of a video file the owner attached this chat (the id named in the "[attached video …]" line).
    question:
      type: string
      description: What the owner wants to know about the clip, in their words (e.g. "what numbers am I holding up?"). Pass it whenever they asked something specific — a short clip is then watched with that question in mind and you get a direct answer beside the summary. Ask again with a new question to follow up on the same clip.
    show:
      type: boolean
      description: Whether to show the owner the video-analysis card (player + frame timeline + transcript). Default true. Set false when the analysis is just an intermediate step toward your answer and the card would be noise.
  required: [source_attachment_id]
---
Understand a video the owner attached this chat — what it shows and what is said —
using the owner's local models. Pass source_attachment_id (the id from the
"[attached video …]" line) and, when the owner asked something specific about the
clip, their question. A clip of a minute or less is watched whole when the model can
take video, and your question is answered from watching it; a longer clip is read as
sampled frames plus its transcript. You cannot watch the video yourself, so use this
whenever you need its content, and call it again with a new question to follow up —
the clip is not re-read from scratch. It can take a minute or more. It renders an
analysis card the owner sees (summary + a frame timeline + transcript tabs), so answer
what was asked in a line or two — do NOT re-narrate the summary or paste the
transcript back; the card already shows them.
