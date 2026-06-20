# GUI gate — live image generation (Wave G7b)

Three interactive mocks of the **in-chat "generating" state**: a live preview that
sharpens at 25 / 50 / 75 / 100% of the sampling steps, plus a **Stop render**
control. Open each in a browser — they auto-play and have ▶ Simulate / ↺ Reset and
a working Stop. Plan: `docs/IMAGE_GEN_LIVE_PLAN.md`.

The owner picks one; the chosen mock becomes the binding spec for the G7b build.

| Variant | File | Idea | Trade-off |
|---|---|---|---|
| **A — Image-as-progress** | `live-a-image-as-progress.html` | The preview fills the final image slot and sharpens in place; slim bottom bar + a corner Stop. | Least chrome, most "magical"; less explicit about steps/percent. |
| **B — Tool-activity card** | `live-b-tool-card.html` | Generation as visible tool work: preview thumbnail + labelled progress track w/ 25/50/75/100 ticks + Stop. Matches how other tool calls render. | Consistent + informative; smaller preview, more boxy. |
| **C — Render canvas** | `live-c-render-canvas.html` | A deliberate render surface: large preview, ringed %, four stage pips, prominent Stop. | Most explicit/legible; most chrome, heaviest. |

All three share the real behaviour the build implements: preview frames + step
progress arrive over the existing chat **SSE** (`tool_progress` event, base-64
preview frames the backend authors); **Stop** calls ComfyUI `/interrupt` and the
turn continues with a "stopped — nothing saved" note; the **final** image is the
real blob-store artifact (unchanged from today).
