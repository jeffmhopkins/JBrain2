---
name: transcribe
version: 1
permission: web
cost_class: expensive
params:
  type: object
  properties:
    source_attachment_id:
      type: string
      description: The id of an audio file the owner attached this chat to transcribe.
  required: [source_attachment_id]
---
Transcribe an audio file the owner attached this chat, using the owner's local
speech-to-text model. Pass source_attachment_id (the id named in the "[attached
audio …]" line). Use this whenever the owner shares a voice memo or recording and
you need its words — to answer about it, summarize it, or act on it — since you
cannot hear it yourself. Returns the transcript text; it inserts nothing and shows
the owner nothing, so report what you heard in your own words. The model loads on
demand and is freed afterward, so a long clip can take a little while.
