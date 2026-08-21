---
name: render_html
version: 1
permission: web
side_effecting: true
cost_class: standard
# NOTE: no JSON-Schema `enum` below. gpt-oss's harmony tool path (llama.cpp `--jinja`)
# builds a GBNF grammar over the tool union and an `enum` on a property of a
# many-optional-property object deterministically segfaults the upstream (see
# analyze_stream.tool / canvas.tool). Allowed values live in the descriptions and are
# validated in the handler (htmltools.py).
params:
  type: object
  properties:
    html:
      type: string
      description: "The markup to render — real HTML with inline CSS, laid out by a browser. Flexbox, grid, tables, border-radius, box-shadow, gradients and rgba all work. It has NO network access: no external images, fonts, scripts or stylesheets, so inline everything (a data: URI is fine)."
    caption:
      type: string
      description: A short caption for the card, e.g. "Three plans compared" (optional).
    width:
      type: integer
      description: Layout width in CSS pixels (default 900, min 240, max 1600). Wider suits a table with many columns; narrower suits a phone-shaped card.
    height:
      type: integer
      description: Fixed height in CSS pixels. OMIT THIS unless you need a specific shape — left out, the page is measured and the image comes back exactly as tall as the content, with no empty space.
    theme:
      type: string
      description: "Page background: dark (the default — light text on the app's own dark surface, so the image sits flush in the card) or light (dark text on white, for a document/print look). Set colours inside your own markup as usual; this is just the ground they sit on."
    pad:
      type: integer
      description: Inner margin around the whole page in pixels (default 28, max 120). Use 0 only for artwork that should run edge to edge.
    look:
      type: string
      description: A question about the rendered image, answered by a vision model that actually looks at it (optional) — e.g. "is any text cut off or overlapping?". Use it when you are unsure the layout came out right.
  required: [html]
examples:
  - {html: "<h2 style=\"margin:0 0 12px\">Plans</h2><table style=\"width:100%;border-collapse:collapse\"><tr style=\"text-align:left\"><th>Plan</th><th>Cost</th></tr><tr><td>Basic</td><td>$9</td></tr></table>", caption: "Plans compared"}
  - {html: "<h2>Trip budget</h2><p>Flights dominate at 62% of the total.</p>", look: "is anything cut off at the bottom?"}
---
Write a page in HTML + CSS, render it to an image, and show it to the owner as a picture card.
One call does both — render and show — so the owner sees it as soon as this returns.

Reach for it when the shape of the answer matters and prose fights you: a comparison table, a
step-by-step card, a spec sheet, a receipt-style breakdown, a labelled diagram in CSS boxes, a
summary with headings and rules. You are fluent in HTML and the browser does the layout, so this
is the general "show it, don't just say it" tool.

**Do not draw a card around your card.** The app already puts the image in a framed, rounded
panel with a caption underneath. So no outer border, no background panel, no rounded corner and
no drop shadow around the whole page, and no outer margin or padding — `pad` handles the gutter.
A frame inside a frame looks like a bug. Style the CONTENT: headings, table rules, chips, dividers,
colour on the parts that carry meaning.

Leave `height` out. The image is measured to fit what you wrote, so a short card comes back short;
setting a height you guessed leaves a band of empty background that reads as a second frame.

It has **no network**. External images, fonts, scripts and stylesheets are blocked, so inline
everything; use system fonts and CSS for anything visual. Nothing you write executes anywhere —
the owner receives pixels — so the markup is yours to write freely.

Not for data plots: a bar or line chart belongs in `render_bars` / `render_chart`, which draw a
real interactive chart the owner can read values off. Not for photographs or artwork either, and
not for marking up an image the owner sent you — if `generate_image` or `canvas` are in your tool
list, those are the tools for those jobs.

The card the owner sees carries the whole render, so do NOT read it back in prose — say in one
sentence what it shows, and never paste a link or claim to be attaching a file. If you are not
sure the layout worked, pass `look` and a vision model will tell you what the image actually looks
like. The returned `image_id` stays readable for the rest of the chat — if `analyze_image` is in your
tool list, you can pass that id to it later to look at the render again.
