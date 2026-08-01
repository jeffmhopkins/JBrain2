---
name: analyze_image
version: 3
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
decide how to edit it, when you cannot see it yourself. When the image contains
text (a screenshot, a document, a receipt, a slide), this ALSO returns a verbatim
transcription of that text as Markdown — headings, tables, lists, and emphasis
preserved — appended after the description under a "Full text (verbatim)" heading,
so a single call gives you both a reading of the image and its exact words, and you
do NOT need a separate call to get the text. Use the `ocr`
tool instead only when you need a deterministic, hallucination-proof transcription
and no description (or for a PDF, which this tool does not read). Returns the vision
model's text answer; it inserts nothing and shows the owner nothing, so report what
you learned in your own words.
