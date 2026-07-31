---
name: ocr
version: 1
permission: web
cost_class: standard
params:
  type: object
  properties:
    source_attachment_id:
      type: string
      description: The id of an image or PDF the owner attached this chat (the id from the "[attached …]" line).
  required: [source_attachment_id]
---
Read the exact text out of an image or PDF the owner attached this chat, using the
owner's on-box OCR — a deterministic engine, not a vision model. Prefer this over
analyze_image whenever you need the LITERAL text: a screenshot of an error or a
terminal, a receipt, a scanned document, a form, a code snippet. It transcribes
verbatim and never guesses or paraphrases, so it will not invent text the way a
vision model can. Pass source_attachment_id (the id from the "[attached …]" line); a
PDF is read page by page. Returns the transcribed text; it inserts nothing and shows
the owner nothing, so use the text directly in your answer. For a description of what
an image shows (its content, not its text), use analyze_image instead.
