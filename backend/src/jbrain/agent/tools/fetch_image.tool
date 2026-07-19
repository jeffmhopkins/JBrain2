---
name: fetch_image
version: 1
permission: web
cost_class: standard
params:
  type: object
  properties:
    url:
      type: string
      description: A direct URL to an image file (e.g. a product photo's .jpg/.png/.webp link found via web_search). Must point at the image itself, not a page containing it.
    show:
      type: boolean
      description: Whether to show the fetched image to the owner as a card (default true). Set false when the fetch is just an intermediate step toward your answer.
  required: [url]
---
Fetch an image from the web so you can actually SEE it. web_fetch returns a page's
text and strips its images, so you cannot look at a picture on the web without this.
Give a DIRECT image URL (the .jpg/.png/.webp/.gif link, not the page around it) — find
one with web_search first if needed.

Use this whenever you need to look at or compare against an image on the internet — a
product photo, a diagram, a reference picture — since you can't see web images
otherwise. It returns an image_id: hand that to analyze_image (with source_image_id)
to inspect it, or to compare_images to compare it with another image (for example a
still you grabbed from a video). If the URL isn't a real image, it says so — don't
invent what the picture shows; fetch it and look.

By default the owner sees the fetched image as a card; pass show=false when it's only
a step toward your real answer.
