---
name: canvas
version: 3
permission: web
cost_class: standard
# NOTE: no JSON-Schema `enum` anywhere below, and an op is ONE FLAT object rather than a
# oneOf union. gpt-oss's harmony tool path (llama.cpp `--jinja`) builds a GBNF grammar over
# the tool union, and an `enum` on a property of a many-optional-property object
# deterministically segfaults the upstream (see analyze_stream.tool). Allowed values live in
# the descriptions and are validated in the handler (drawtools.py / draw/scene.py).
params:
  type: object
  properties:
    canvas_id:
      type: string
      description: The canvas to keep drawing on, from an earlier canvas call this chat. Omit to start a new one.
    width:
      type: integer
      description: New blank canvas width in pixels (default 1024). Ignored when drawing on an image or continuing a canvas.
    height:
      type: integer
      description: New blank canvas height in pixels (default 768). Ignored when drawing on an image or continuing a canvas.
    on_attachment_id:
      type: string
      description: Start a canvas ON TOP of an image the owner attached this chat (its id from the "[attached image ...]" line) — the way to box, circle, or label something in their photo or screenshot.
    on_image_id:
      type: string
      description: Start a canvas on top of an image you made or grabbed earlier this chat (a grab_frame / fetch_image id).
    ops:
      type: array
      description: The marks to lay down, applied in order. Give the WHOLE figure in one call, not one op per call. Each op is a flat object; fields an op doesn't use are ignored.
      items:
        type: object
        properties:
          op:
            type: string
            description: "One of: text | line | arrow | rect | callout | label_box | html | delete | move | clear. text = words at (x,y). line = (x,y) to (x2,y2); arrow = the same with a head at (x2,y2). rect = a box at (x,y) sized w by h. callout = a text bubble at (x,y) with a leader line to (x2,y2) — point at a thing and say what it is. label_box = a box around (x,y,w,h) captioned with `text` — the photo-annotation op. html = render real HTML+CSS inside the box at (x,y,w,h). delete / move act on an earlier op's `id`; clear removes every mark."
          x:
            type: number
            description: Left/start x in pixels (0 = left edge).
          y:
            type: number
            description: "Top/start y in pixels (0 = TOP edge; y grows DOWNWARD)."
          x2:
            type: number
            description: End x — for line / arrow / callout, the point being pointed AT.
          y2:
            type: number
            description: End y — for line / arrow / callout.
          w:
            type: number
            description: Width, for rect / label_box / html.
          h:
            type: number
            description: Height, for rect / label_box / html.
          text:
            type: string
            description: The words, for text / callout / label_box. A label, not a paragraph.
          html:
            type: string
            description: "For op=html: real HTML with inline CSS, rendered by a browser inside the box. Use it for anything the shape ops are clumsy at — wrapped paragraphs, lists, small tables, a styled card. Flexbox, border-radius, box-shadow and rgba all work. It has NO network: no external images, fonts, scripts or stylesheets — inline everything."
          tone:
            type: string
            description: "One of: auto | danger | warn | info | ok | accent | neutral. Meaning, not colour — the app owns the palette. auto (default) is high contrast against what is underneath. Do NOT send hex."
          size:
            type: number
            description: "Text size in pixels (default 28; 44 for a title, 20 for a small note)."
          width:
            type: number
            description: Stroke thickness in pixels (default 3).
          fill:
            type: boolean
            description: Fill a rect with a translucent tint instead of outlining it (default false, so what is underneath stays visible).
          id:
            type: string
            description: Which earlier mark to act on, for delete / move (the e1, e2... ids this tool returns).
          dx:
            type: number
            description: How far right to shift, for move (negative = left).
          dy:
            type: number
            description: How far down to shift, for move (negative = up).
        required: [op]
    look:
      type: string
      description: Optional. A concrete question about the canvas as it stands, e.g. "does the red box contain the water heater?" or "where is the drain valve?". The canvas is rendered and read back by the on-box vision model. When you ask WHERE something is, the answer includes its box already converted into THIS canvas's pixel coordinates — use those numbers directly in your next ops rather than re-estimating them. Use it to AIM before annotating a photo, and once at the end to check. Ask something answerable — never "does this look good?". Three looks per turn.
  required: []
examples:
  - {on_attachment_id: "...", look: "where is the water heater?"}
  - {canvas_id: "cv1a2b3c", ops: [{op: "label_box", x: 610, y: 300, w: 180, h: 170, text: "water heater", tone: "danger"}, {op: "callout", x: 60, y: 620, x2: 700, y2: 470, text: "drain valve leaking here"}]}
  - {width: 1024, height: 640, ops: [{op: "text", x: 40, y: 60, text: "Backup flow", size: 44}, {op: "rect", x: 60, y: 140, w: 240, h: 110}, {op: "text", x: 90, y: 205, text: "Laptop"}, {op: "arrow", x: 310, y: 195, x2: 470, y2: 195}, {op: "rect", x: 480, y: 140, w: 240, h: 110}, {op: "text", x: 515, y: 205, text: "NAS"}]}
  - {canvas_id: "cv1a2b3c", ops: [{op: "html", x: 60, y: 520, w: 460, h: 130, html: "<div style='background:rgba(207,138,143,.92);border-radius:14px;padding:14px 18px;font:600 22px system-ui;color:#fff'>Drain valve — leaking<div style='font-weight:400;font-size:16px;opacity:.9'>wet since Tuesday · check monthly</div></div>"}]}
  - {canvas_id: "cv1a2b3c", ops: [{op: "move", id: "e2", dx: -40}, {op: "delete", id: "e5"}]}
---
Draw exact marks — text, lines, arrows, boxes, callouts, and real HTML blocks — at pixel
coordinates, on a blank sheet or on top of an image. This is the tool for **"put a red box
around this in the photo"**, **"point at the X and label it"**, **"annotate this
screenshot"**, and **"sketch a quick diagram of A to B to C"**.

Coordinates are pixels from the TOP-LEFT: x grows right, y grows DOWN. Every call returns
the canvas id, its size, and every mark on it with its id, so you always know the current
state — read that back rather than guessing what is there.

Work in whole figures: put the entire drawing in ONE `ops` array. Marks are kept as a list,
not baked into pixels, so you fix one later by id — `{"op":"delete","id":"e4"}`,
`{"op":"move","id":"e4","dx":-40}` — instead of starting over. If one op is malformed the
rest still apply and you are told which failed and why.

For anything wordy or styled — a wrapped paragraph, a list, a small table, a card with a
heading — use `op: "html"` and write real HTML with inline CSS. It renders in a browser, so
flexbox, rounded corners, shadows and translucency all work. It has no network access, so
inline every style and never reference an external image, font, script or stylesheet.

**You cannot see the canvas.** Pass `look` to have the on-box vision model read it back to
you — that is how you find *where* a thing is in the owner's photo before you box it, and how
you check the finished figure. Ask a concrete, checkable question. A "where is X" look comes
back with the box already in this canvas's pixels; trust those numbers over your own estimate
of the photo, because they are measured against the exact frame your ops are drawn on. Draw from what you know
rather than looking after every change; you get three looks and ten calls per turn.

Nothing is shown to the owner until you call **`show_canvas`** — do that once the figure is
right, then say in a sentence what you drew.

**Not for data, not for pictures.** To plot numbers — a trend, a breakdown, a comparison —
use `render_chart` (dates) or `render_bars` (categories); they draw a real interactive graph
and this does not. This cannot make a picture or change what a photo *shows* — it only lays
marks on top (the owner's Images app does generation/editing). To read or measure an image rather than mark
it up, use `analyze_image` or `ocr`.
