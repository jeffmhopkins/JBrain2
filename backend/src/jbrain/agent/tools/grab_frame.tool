---
name: grab_frame
version: 1
permission: web
cost_class: standard
params:
  type: object
  properties:
    url:
      type: string
      description: A video URL to grab a still from (a YouTube watch link or most video sites). Give this OR source_attachment_id, not both.
    source_attachment_id:
      type: string
      description: The id of a video the owner attached this chat (the id from the "[attached video …]" line). Give this OR url, not both.
    seek:
      type: number
      description: How many seconds into the video the still should be taken (e.g. 164 for 2:44). Omit for the very start. Ignored for a live stream (which has no fixed timestamp — use analyze_stream instead).
    question:
      type: string
      description: Optional. If set, also look at the grabbed still and answer this about it (e.g. "what module is this? read any panel text"), returning the answer alongside the image id — so you grab and see it in one step.
    show:
      type: boolean
      description: Whether to show the still to the owner as a card (default true). Set false when the grab is just an intermediate step toward your answer and the owner doesn't need to see it.
  required: []
---
Grab a single still frame from a video — a URL or a video the owner attached — at a
specific moment, and keep it as an image you can then look at or compare. Give EXACTLY
ONE source: url (a video link) or source_attachment_id (an attached video). Pass `seek`
(seconds) for the moment you want.

Use this whenever you need to SEE what a video shows at a particular time — to inspect
an object on screen, read on-screen text, or get a still to compare against another
image — since you cannot watch the video yourself. It returns an image_id: hand that to
analyze_image (with source_image_id) for a detailed look, or to compare_images to
compare it with another image. If you already know what you want to ask about the
frame, pass `question` to grab it and get the answer in one step.

By default the owner sees the still as a card; pass show=false when it's only a step
toward your real answer and the card would just be noise. Grabbing reads a bit of the
video over the network (for a URL) and runs a local model (for `question`), so it can
take a moment.
