---
name: show_canvas
version: 1
permission: web
side_effecting: true
params:
  type: object
  properties:
    canvas_id:
      type: string
      description: The canvas to show, from an earlier canvas call this chat.
    caption:
      type: string
      description: A short caption for the card, e.g. "Water heater — drain valve" (optional).
  required: [canvas_id]
examples:
  - {canvas_id: "cv1a2b3c", caption: "Water heater — drain valve"}
---
Render a canvas you drew with the `canvas` tool and show it to the owner as an image card.
Call this once the figure is right — until you do, the owner has seen nothing.

The app renders the card itself from the saved image; you do NOT get the bytes or a link, so
never paste a URL or claim to be showing it yourself — just say in a sentence what you drew.

The canvas stays editable afterwards: keep drawing on the same `canvas_id` and call this
again to show the corrected version.
