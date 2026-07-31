---
name: analyze_image
version: 2
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
Look at an image and answer a question about what it SHOWS, using the owner's local
vision model. Give EXACTLY ONE source: source_image_id (an image you generated
earlier this chat) or source_attachment_id (an image the owner attached this chat)
— not both, not neither. Use this to DESCRIBE an image, judge a visual detail, or
decide how to edit it, when you cannot see it yourself. For the LITERAL text in an
image or PDF — a screenshot of an error, a receipt, a scanned document, a code
snippet, anything the owner asks you to "read" or "transcribe" — use the `ocr` tool
instead: it is exact and will not misread or invent text the way a vision model can.
Returns the vision model's text answer; it inserts nothing and shows the owner
nothing, so report what you learned in your own words.
