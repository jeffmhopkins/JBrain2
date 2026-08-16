# Vitals detail — the GUI gate

Tapping the top bar's vitals chart opens a detail surface: a larger graph with
1 / 5 / 15-minute ranges, the agent turns running right now, and per-turn raw
output plus debug-style metadata about the original call.

**Chosen: `i-drilldown.html`.** It is the binding spec for
`frontend/src/screens/VitalsScreen.tsx`; the reasoning lives in
`docs/reference/DESIGN.md` under "Vitals detail". The rivals are kept because that
doc records *what was chosen over what*.

| Mock | Variant | Shape | Outcome |
|---|---|---|---|
| `i-drilldown.html` | Drill-down | Two levels — overview, then a pushed page that is entirely one turn | **Chosen** |
| `g-instrument-panel.html` | Instrument panel | Plot pinned top, shrinking to a strip when a row opens | Rejected |
| `h-dossier.html` | Dossier | Graph as a header strip; each turn opens a labelled-field dossier | Rejected |

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

## Settled alongside the pick

- **Bucketing is by PEAK.** G averaged the 5m/15m columns; peak won, because a mean
  hides the spike the screen was opened to find.
- **History still resets on reload.** The graph runs off a ~900-sample client-side
  ring fed by the 1 Hz stream. Surviving a reload would need a server-side ring; not
  built, and the surface does not pretend otherwise.
