---
name: transcribe
version: 4
permission: web
cost_class: expensive
params:
  type: object
  properties:
    source_attachment_id:
      type: string
      description: The id of an audio or video file the owner attached this chat to transcribe.
  required: [source_attachment_id]
---
Transcribe an audio or video file the owner attached this chat, using the owner's
local speech-to-text model (a video's audio track is read automatically). Pass
source_attachment_id (the id named in the "[attached audio …]" or "[attached video
…]" line). Use this whenever the owner shares a voice memo, recording, or video clip
and you need its words — to answer about it, summarize it, or act on it — since you
cannot hear it yourself. It renders a transcript card the owner sees (the player + the
full text), so do NOT paste the transcript back — answer or summarize only what the
owner asked, in a line or two. The model loads on demand and is freed afterward, so a
long clip can take a little while.

When you only need a video's WORDS, use this and not `analyze_video`: this runs speech-to-text
alone, while `analyze_video` also samples and captions frames, so it costs several times as long
for the same transcript. Reach for `analyze_video` only when what the video SHOWS matters too.
