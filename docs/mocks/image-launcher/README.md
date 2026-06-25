# Image launcher — screen-path generate/edit (mock gate)

Four interactive directions for a **standalone image generation/editing screen** — a
new **card-launcher destination** that drives the on-box ComfyUI image model
**directly**, so the **language models stay unloaded**. Today image gen only happens
as a jerv tool call (`generate_image`/`edit_image`) inside a Full-Brain chat, which
requires the LLM resident; this screen is the "I just want a picture — don't wake the
brain" path. Each mock keeps the screen-vs-agent contrast legible (an unobtrusive
"ask jerv in chat" note) without becoming a chat.

Per `docs/PROCESS.md` / `docs/DESIGN.md` "UI development process": these are presented
to the owner to choose one; the chosen mock becomes the binding spec and the rest are
retained here as the record. **No code until the owner picks.**

All four are tokens-only (`frontend/src/styles/tokens.css`), dark-first with a working
light/dark toggle, phone-framed ≤430px, fully offline (generated images are inline SVG
`data:` stand-ins for the by-id production source `/api/images/generated/{id}`), and use
the **violet** image accent (image models ride violet on the residency ladder). Each
covers the real tool knobs — **speed** (dreamshaper/fast/quality), **aspect**
(square/portrait/landscape/tall/wide), **resolution** (small/medium/large), **steps**
(20–40, locked when speed≠quality), **seed** (blank=random, recorded) — plus a
**negative prompt**, **edit-with-upload** + up to 2 **reference** images, and an honest
synchronous render-state sequence (queued → rendering… → done; no fake progress bar).

| File | Direction | Shape | Best when |
|---|---|---|---|
| `launcher-a-composer-dock.html` | **Composer-dock studio.** A bottom-docked image composer (prompt + Generate\|Edit segment + send) under a scrollable board of this session's renders; config behind a "tune" bottom sheet, key settings echoed as glanceable chips. | The omnibox paradigm, for images. | Iterative bursts (generate → tweak → edit the result); lowest learning curve since it mirrors the home omnibox. |
| `launcher-b-segmented-form.html` | **Segmented Generate \| Edit form.** The Data-screen segmented-tasks paradigm: one focused task panel at a time, every knob laid out explicitly in a config card. | Config-forward, deliberate. | "Set up all the knobs, render once" — maximum legibility/control of settings; weakest for fast iteration. |
| `launcher-c-pinboard.html` | **Pinboard gallery.** A 2-column board of renders is the hero (the literal "pins page"); creation is a summoned bottom-sheet composer; tap a pin for a detail view with edit/regenerate/seed/save. | The pictures are the screen. | Browsing/collecting renders matters as much as making them; creation is a deliberate, summoned act. |
| `launcher-d-render-console.html` | **Render console / darkroom.** A large image stage dominates the top with an honest render-state overlay + a violet "image model resident · language models unloaded" residency pill; controls (toggle, prompt, collapsible tune drawer) below; a filmstrip of recent renders along the bottom. | Focus on one image; leans hardest into the unloaded-residency story. | Single-image focus and making the direct/synchronous, LLM-unloaded nature the star. |

## Trade-offs

- **A** keeps the primary action in the thumb zone and makes the generate→edit loop
  frictionless via "use as source", and reuses a paradigm the owner already knows — but
  config is one tap down behind the sheet (chips are glance-only), and the transcript
  metaphor risks reading chat-like, which the screen-vs-agent framing must actively counter.
- **B** is the most legible and controllable for settings and aligns cleanly with the
  settled Data-screen segmented pattern — but it's form-heavy and the slowest for rapid
  iteration; the result is a secondary surface rather than the star.
- **C** makes the render *collection* first-class and matches "pins page" most literally,
  with edit before/after in the pin detail — but creation is always a modal away (one extra
  surface), so a make-heavy session does more sheet-opening.
- **D** foregrounds the direct/synchronous render and the unloaded-LLM status better than
  any other, with a darkroom single-image focus — but it's a tall scrolling column (the
  filmstrip often sits below the fold) and its config drawer departs slightly from the shared
  `<Sheet>` workhorse in favor of an inline collapsible tune drawer.

## Decision

_Pending owner review._ Once chosen, the selection + rationale land here and in
`docs/DESIGN.md` (a new "Image launcher" component entry), and the rejected three are
retained in this directory as the record.
