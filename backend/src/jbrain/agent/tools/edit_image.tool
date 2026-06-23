---
name: edit_image
version: 6
permission: web
side_effecting: true
cost_class: expensive
params:
  type: object
  properties:
    prompt:
      type: string
      description: The edit instruction — what to change, e.g. "make it night-time" or "add a red hat".
    speed:
      type: string
      enum: [fast, quality]
      description: Speed vs. fidelity. quality (the default) runs the full edit model at 20–40 diffusion steps — best detail, but a slow render. fast runs the same model through a 4-step Lightning distillation — much quicker at slightly lower detail. Prefer fast for quick, exploratory tweaks and when the owner wants a result now; use quality for a finished result.
    negative_prompt:
      type: string
      description: What to keep OUT of the result, e.g. "blurry, extra fingers, text, watermark" (optional).
    source_image_id:
      type: string
      description: The id of an image you generated earlier this chat to edit.
    source_attachment_id:
      type: string
      description: The id of an image the owner attached to this chat to edit.
    reference_image_ids:
      type: array
      items:
        type: string
      description: Optional extra images you generated this chat, by id, to use as references alongside the main image (e.g. a style or a subject to bring in). Up to 2.
    reference_attachment_ids:
      type: array
      items:
        type: string
      description: Optional extra images the owner attached this chat, by id, to use as references alongside the main image. Up to 2.
    aspect:
      type: string
      enum: [square, portrait, landscape, tall, wide]
      description: The output shape. square (1:1); portrait/landscape (gentle 3:4); tall/wide (dramatic 16:9). Defaults to square.
    resolution:
      type: string
      enum: [small, medium, large]
      description: The output size. Defaults to medium. Use small for a quicker, lighter render and large for more detail.
    steps:
      type: integer
      description: Number of diffusion steps on the quality path, 20–40 (default 20). 20 is a quick but finished render; raise toward 40 for more detail at more time. Values outside the range are clamped. Ignored when speed is fast (that path is a fixed 4 steps).
    seed:
      type: integer
      description: A fixed seed for a repeatable result (optional). When omitted a random seed is chosen and recorded.
  required: [prompt]
---
Edit an existing image with the owner's local image model, following the prompt
as an edit instruction. Give EXACTLY ONE main source — the image being edited:
either source_image_id (an image you generated earlier this chat) or
source_attachment_id (an image the owner attached this chat) — not both, not
neither. To COMBINE images (compositing — e.g. "put the person from this photo
into that scene", "apply this style"), add up to 2 more by id via
reference_image_ids and/or reference_attachment_ids; the main source is what's
edited and the references are extra inputs. Up to 3 images total. This takes a
moment; generation runs on-box and the turn waits. The app renders the result
inline for the owner; you do NOT receive the image bytes or any link, so never
paste a URL or claim to show the image yourself — just describe what you changed.
