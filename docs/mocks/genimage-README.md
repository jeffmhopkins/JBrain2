# Generated-image chat view — mock gate (Image-gen Wave G3)

Three interactive directions for the **in-chat `generated_image` tool-view** —
the card jerv shows after a `generate_image` or `edit_image` tool call
(`docs/PROCESS.md` GUI gate; `docs/IMAGE_GEN_PLAN.md` "Wave G3"). Pick one; the
chosen mock becomes the binding spec and the other two are retained here as the
record (mirrors the `wiki-talk-README` convention).

All three honor the tool-view contract (`docs/DESIGN.md` "Agent tool views"):
the model authors **no markup and no URLs** — it only fills the data-only slots
`{image_id, kind ('generate'|'edit'), prompt, width, height, model}`. The
component builds the image source as `/api/images/generated/${image_id}` and
sizes the frame from `width`/`height` so the bubble reserves space (no layout
shift while the blob loads). Mocks are **tokens-only** (the app token set in
`frontend/src/styles/tokens.css`), phone-framed, dark-first with the global
light/dark toggle, outline icons, and the card frame matches the live
`.tool-view` (`--surface-2` / `--border` / 12px radius). They render offline:
images are inline SVG `data:` URIs standing in for the by-id production source
(noted in each file's header comment).

| File | Direction | Shape | Best when |
|---|---|---|---|
| `genimage-a-result-only.html` | **Result-only.** Just the sized image, a small `kind` badge, and a one-line caption (dimensions · model). No disclosure. | The picture *is* the message. | You want the lowest-chrome, most photo-feed-like card; prompt/seed rarely needed inline. |
| `genimage-b-disclosure.html` | **Result + disclosure.** Image plus a collapsed **Details** strip — tap to reveal prompt, model, seed, dimensions, and a **Regenerate** (+ Save) affordance. | Clean by default, reproducibility one tap away. | You regularly want the seed/prompt to re-run or tweak, but not cluttering every card. |
| `genimage-c-edit-aware.html` | **Edit-aware before/after.** A *generate* renders like A; an *edit* renders the source→result link as a draggable **swipe compare** with a Before/After/Compare toggle. Transcript shows one generate card **and** one edit card. | Provenance of an edit (what changed). | `edit_image` is used a lot and "what did the edit do?" is the key question. |

## Trade-offs

- **A** is the calmest and most native to a chat scroll, and it's the least code
  to ship — but it hides the prompt/seed entirely, so reproducing or tweaking a
  result means scrolling back to the request, and it shows an edit no
  differently from a fresh generate.
- **B** keeps A's clean default while putting reproducibility (seed/prompt) and a
  **Regenerate** action one tap away; the cost is a second interaction surface
  per card and slightly more component state. It still renders an edit like a
  plain generate (no before/after).
- **C** is the only direction that makes an **edit** legible — the swipe compare
  ties source to result, which matches that `edit_image` is in scope — but it's
  the most interaction-heavy (drag + pointer-capture) and the most to build/test,
  and the compare UI is wasted on the common generate case (which falls back to A).

## Decision

**Chosen: C — edit-aware before/after** (`genimage-c-edit-aware.html`). It is the
**binding spec** for the `GeneratedImage` component and its `.tv-genimg-*` classes
in `frontend/src/agent/views/registry.tsx`: a *generate* renders like A (sized
image, `kind` badge, dimensions·model caption); an *edit* renders the
source→result link as a draggable swipe-compare with a Before/After/Compare
toggle, pulling the "before" image from the owner-gated
`/api/images/generated/${image_id}/source` route. C won because **`edit_image` is
in scope**, so the source→result provenance must be legible — A and B render an
edit no differently from a fresh generate. **A** (result-only) and **B**
(result + a collapsed prompt/seed/Regenerate disclosure) are retained in this
directory as the record (C subsumes A's generate-only layout, so this choice also
fixes the generate rendering). The selection + rationale also land in
`docs/DESIGN.md` (the `generated_image` tool-view entry, per the GUI gate).
