---
name: crop_regions
version: 1
permission: web
side_effecting: true
cost_class: standard
params:
  type: object
  properties:
    source_attachment_id:
      type: string
      description: The image the owner attached this chat to cut regions out of (its id from the "[attached image ...]" line). Give this OR source_image_id.
    source_image_id:
      type: string
      description: An image you made or grabbed earlier this chat (generate_image / edit_image / grab_frame / fetch_image id). Give this OR source_attachment_id.
    target:
      type: string
      description: What to cut out, in plain words — "each face", "every product label", "the two dogs". One crop per distinct instance found. Give this OR boxes.
    boxes:
      type: array
      description: Explicit regions in PIXELS, when you already know where they are (e.g. read off a canvas look). Give this OR target.
      items:
        type: object
        properties:
          x:
            type: number
            description: Left edge in pixels.
          y:
            type: number
            description: Top edge in pixels (0 = TOP; y grows downward).
          w:
            type: number
            description: Width in pixels.
          h:
            type: number
            description: Height in pixels.
          label:
            type: string
            description: A short name for this crop, shown under it.
        required: [x, y, w, h]
  required: []
examples:
  - {source_attachment_id: "...", target: "each face"}
  - {source_attachment_id: "...", target: "every product label"}
  - {source_image_id: "...", boxes: [{x: 610, y: 300, w: 180, h: 170, label: "water heater"}]}
---
Cut regions out of ONE image and show the owner each one as its own saveable picture — the
tool for **"export each face as a separate image"**, **"crop out every label"**, **"pull
that one part out of the photo"**.

Give a source (an attachment the owner sent, or an image you made/grabbed) plus EITHER
`target` — what to find, in words — or `boxes` when you already know the pixel regions. The
card shows the original with each region marked, so the owner can see where every crop came
from.

**Say how many you found, and do not imply it is all of them.** Finding regions in an image
is unreliable in one specific direction: it MISSES things silently. Six faces returned from a
photo of fourteen people looks exactly like a complete answer. The result tells you the count
that was actually cut — repeat that number to the owner, and if they expected more, say
plainly that some may have been missed rather than asserting the set is complete.

At most 12 crops per call; anything smaller than a thumbnail is skipped as a mis-detection.

To MARK UP an image instead of cutting it apart — a box, an arrow, a label drawn on top —
use `canvas`. To read or describe an image, use `analyze_image` or `ocr`. This tool does not
alter the original: it only copies regions out of it.
