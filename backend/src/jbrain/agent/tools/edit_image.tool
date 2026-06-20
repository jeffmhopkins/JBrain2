---
name: edit_image
version: 1
permission: web
side_effecting: true
cost_class: expensive
params:
  type: object
  properties:
    prompt:
      type: string
      description: The edit instruction — what to change, e.g. "make it night-time" or "add a red hat".
    source_image_id:
      type: string
      description: The id of an image you generated earlier this chat to edit.
    source_attachment_id:
      type: string
      description: The id of an image the owner attached to this chat to edit.
    aspect:
      type: string
      enum: [square, portrait, landscape]
      description: The output shape. Defaults to square.
    steps:
      type: integer
      description: How many diffusion steps to run (optional; a sane default is used when omitted).
    seed:
      type: integer
      description: A fixed seed for a repeatable result (optional). When omitted a random seed is chosen and recorded.
  required: [prompt]
---
Edit an existing image with the owner's local image model, following the prompt
as an edit instruction. Give EXACTLY ONE source: either source_image_id (an image
you generated earlier this chat) or source_attachment_id (an image the owner
attached this chat) — not both, not neither. This takes a moment; generation runs
on-box and the turn waits. The app renders the result inline for the owner; you do
NOT receive the image bytes or any link, so never paste a URL or claim to show the
image yourself — just describe what you changed.
