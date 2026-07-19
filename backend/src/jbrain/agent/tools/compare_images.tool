---
name: compare_images
version: 1
permission: web
cost_class: standard
params:
  type: object
  properties:
    prompt:
      type: string
      description: What to compare or decide, e.g. "are these the same module? note any front-panel differences" or "which of these matches the first image?".
    image_ids:
      type: array
      items:
        type: string
      description: Ids of images to compare — images you generated, grabbed from a video (grab_frame), or fetched from the web (fetch_image). Combine with attachment_ids; two or more images total.
    attachment_ids:
      type: array
      items:
        type: string
      description: Ids of images the owner attached this chat to include in the comparison. Combine with image_ids; two or more images total.
    show:
      type: boolean
      description: Whether to show the owner the side-by-side of the compared images (default true). Set false only for an intermediate step.
  required: [prompt]
---
Compare two or more images and answer a question about them, using the owner's vision
model. Pass the images as a LIST — image_ids (images you generated, grabbed from a video
with grab_frame, or fetched from the web with fetch_image) and/or attachment_ids (images
the owner attached) — two or more in total, plus a prompt describing what to compare.

Use this whenever a question needs two images looked at together — "is the module in this
video frame the same as this product photo?", "which of these matches?", "what changed
between them?" — since you cannot see the images yourself. The typical flow: grab_frame a
still from a video, fetch_image the online picture(s) to compare against, then
compare_images their ids. It answers in words AND shows the owner a side-by-side of
exactly what was compared, so ground your answer only in what the images actually show —
never describe an image you didn't pass in.
