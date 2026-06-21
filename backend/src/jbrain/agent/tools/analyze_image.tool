---
name: analyze_image
version: 1
permission: web
cost_class: standard
params:
  type: object
  properties:
    prompt:
      type: string
      description: What you want to know about the image, e.g. "describe it in detail" or "what does the sign say?".
    source_image_id:
      type: string
      description: The id of an image you generated earlier this chat to look at.
    source_attachment_id:
      type: string
      description: The id of an image the owner attached this chat to look at.
  required: [prompt]
---
Look at an image and answer a question about it, using the owner's local vision
model. Give EXACTLY ONE source: source_image_id (an image you generated earlier
this chat) or source_attachment_id (an image the owner attached this chat) — not
both, not neither. Use this whenever you need to know what an image contains —
to describe it, read its text, or decide how to edit it — and you cannot see it
yourself. Returns the vision model's text answer; it inserts nothing and shows
the owner nothing, so report what you learned in your own words.
