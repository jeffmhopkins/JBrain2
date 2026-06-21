---
name: generate_image
version: 3
permission: web
side_effecting: true
cost_class: expensive
params:
  type: object
  properties:
    prompt:
      type: string
      description: A vivid description of the image to create, e.g. "a watercolor fox asleep in autumn leaves".
    negative_prompt:
      type: string
      description: What to keep OUT of the image, e.g. "blurry, extra fingers, text, watermark" (optional).
    aspect:
      type: string
      enum: [square, portrait, landscape]
      description: The image shape. Defaults to square.
    resolution:
      type: string
      enum: [small, medium, large]
      description: The image size. Defaults to medium (the model's native ~1MP). Use small for a quicker, lighter render and large for more detail.
    effort:
      type: integer
      description: Quality/time tradeoff, 0–10. 1 is a quick draft, 5 is normal high quality (the default), 10 is maximum detail. Higher means more diffusion steps and a slower render — use a low effort to preview, then re-run at the same seed with higher effort to finalize.
    seed:
      type: integer
      description: A fixed seed for a repeatable result (optional). When omitted a random seed is chosen and recorded so the owner can reproduce it.
  required: [prompt]
---
Generate a brand-new image from a text description, using the owner's local
image model. This takes a moment — generation runs on-box and the turn waits for
it. The app renders the finished image inline in the chat for the owner; you do
NOT receive the image bytes or any link, so never paste a URL or claim to show
the image yourself — just describe in a sentence what you made. To change an
existing image instead of making a new one, use edit_image.
