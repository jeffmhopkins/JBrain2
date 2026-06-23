---
name: generate_image
version: 6
permission: web
side_effecting: true
cost_class: expensive
params:
  type: object
  properties:
    prompt:
      type: string
      description: A vivid description of the image to create, e.g. "a watercolor fox asleep in autumn leaves".
    speed:
      type: string
      enum: [fast, quality]
      description: Speed vs. fidelity. quality (the default) runs the full local model at 20–40 diffusion steps — best detail, but a slow render (a minute or more). fast runs the same model through a 4-step Lightning distillation — much quicker (a fraction of the time) at slightly lower detail, but still high quality. Prefer fast for casual, exploratory, or "just show me something" requests and when the owner wants a result now; use quality when they want a finished, detailed piece.
    negative_prompt:
      type: string
      description: What to keep OUT of the image, e.g. "blurry, extra fingers, text, watermark" (optional).
    aspect:
      type: string
      enum: [square, portrait, landscape, tall, wide]
      description: The image shape. square (1:1); portrait/landscape (gentle 3:4); tall/wide (dramatic 16:9 — phone-tall / cinematic). Defaults to square.
    resolution:
      type: string
      enum: [small, medium, large]
      description: The image size. Defaults to medium (the model's native ~1MP). Use small for a quicker, lighter render and large for more detail.
    effort:
      type: integer
      description: Quality/time tradeoff for the quality path, 0–10 (mapping to 20–40 diffusion steps). 0 is the lighter end, 5 is the normal default, 10 is maximum detail. Higher means more steps and a slower render — preview at a low effort, then re-run at the same seed with higher effort to finalize. Ignored when speed is fast (that path is a fixed 4 steps).
    seed:
      type: integer
      description: A fixed seed for a repeatable result (optional). When omitted a random seed is chosen and recorded so the owner can reproduce it.
  required: [prompt]
---
Generate a brand-new image from a text description, using the owner's local
image model. Generation runs on-box and the turn waits for it: a quality render
takes a moment, while a fast render (speed: fast) returns in seconds — reach for
fast when the owner wants something quick or you are just exploring an idea. The
app renders the finished image inline in the chat for the owner; you do NOT
receive the image bytes or any link, so never paste a URL or claim to show the
image yourself — just describe in a sentence what you made. To change an existing
image instead of making a new one, use edit_image.
