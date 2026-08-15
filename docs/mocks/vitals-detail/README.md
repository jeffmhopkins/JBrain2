# Vitals detail — the GUI gate

Tapping the top bar's vitals chart opens a detail surface: a larger graph with
1 / 5 / 15-minute ranges, the agent turns running right now, and per-turn raw
output plus debug-style metadata about the original call.

**Undecided — awaiting the owner's pick.** Three variants, all built against the
same brief and the same field inventory.

| Mock | Variant | Shape |
|---|---|---|
| `g-instrument-panel.html` | Instrument panel | Plot pinned top, shrinking to a strip when a row opens; turns expand in place |
| `h-dossier.html` | Dossier | Graph as a header strip; each turn opens a thorough labelled-field dossier |
| `i-drilldown.html` | Drill-down | Two levels — overview, then a pushed page that is entirely one turn |

Each file is standalone and interactive — open it directly, no build step. The
control desk drives the running / errored / no-turns states, the tap-target
treatment, theme, and reduced motion.

## Constraints they were built against

- **Full-screen card**, per the paradigm table in `docs/reference/DESIGN.md`: a
  graph plus a drillable list plus expandable raw text is past what a bottom
  sheet may carry ("at most one primary action; longer flows are full screens").
- **The tap target.** The chart is 48×20px in a 56px bar, under the ≥44px rule,
  and it is an `<output>` with an implicit `status` role — a live region must not
  also be a button. Every variant grows the *hit area* only and leaves the drawing
  untouched. All three also reverse DESIGN.md's recorded "nothing in the cluster
  is tappable"; whichever wins amends that doc in the same PR.
- **Only fields that exist.** Kind, status, elapsed, steps, tokens, progress note,
  errors, parent run, trigger, session, domain, ran-as, prompt version — plus
  model, provider, reasoning effort, context window, tool set, persona and the
  triggering message, which are resolved at turn start and need only stamping.
  The **verbatim prompt is stored nowhere**, and the dossiers say so rather than
  inventing it.
- **The honesty case.** GPU busy covers the whole box, image generation included,
  so the graph can read 95% with an empty turn list. Each variant explains that
  instead of looking broken.

## Open questions the pick does not settle

- **Bucketing.** G means the 5m/15m columns; H and I take the peak. Peak is the
  more honest default for a load gauge — a mean hides the spike you opened the
  screen to find.
- **History across a reload.** The graph runs off a ~900-sample client-side ring,
  so it resets on reload. Surviving that needs a server-side ring.
