# Teacher Mode — Component Catalog (for triage)

> **Status:** Living · **Last verified:** 2026-07-07

Research dossier. A consolidated, deduplicated catalog of UI components a live
LLM **teacher** could compose into a lesson. Produced by five parallel research
sweeps (assessment, visualization, lesson-structure, content/media,
dialogue/adaptivity) and merged here. **Nothing is built** — this is a menu to
triage. The eventual "teacher course plan" that would sequence these is **out of
current scope**; this only catalogs the building blocks it would draw on. The
exploratory lesson mocks that probe these components (referenced below as "Mock
01–04") live under `docs/mocks/teacher-mode/` — the canonical home for all HTML
UI mocks — not beside this doc.

## How to use this doc

Each component is a checkbox. Triage by marking:
- `[x]` keep — worth mocking
- `[~]` maybe / later
- `[ ]` → strike through to cut

Two tags per row help prioritize:
- **Effort** — `S` static/light (render + hover/reveal), `M` interactive/stateful
  (sliders, drag, steppers, a grading round-trip), `L` real engine (physics/market
  sim, code execution, a function/CAS plotter).
- **⭐ Differentiator** — only a *live LLM teacher with access to the owner's
  notes* can do this well; static courseware can't. These are the reason teacher
  mode exists, and the strongest mock candidates.

## Architectural grounding (how any of these would actually render)

From the current codebase — a mock has to fit this, so triage with it in mind:

- The frontend is **React 18 + TS + Vite**, first-party components only, styled
  with **CSS design tokens** (`frontend/src/styles/tokens.css`) — no raw hex, no
  CSS framework. Binding rules live in `docs/reference/DESIGN.md`.
- Rich agent UI renders through **one closed registry**:
  `frontend/src/agent/views/registry.tsx` maps a `view` name → a vetted React
  component. A tool emits a typed **`ViewPayload`** (`{view, surface, data, refs}`);
  unknown names render nothing. **The model emits data, never markup/HTML/URLs/colors** —
  colors/glyphs are `tone`/`flag`/`kind` enums the component maps to tokens.
  Interactive views **propose, never mutate** (a button dispatches a tool call).
- Plain answer text already renders **Markdown + KaTeX math** via
  `frontend/src/agent/markdown.tsx`. So inline prose, lists, and `$…$` / `$$…$$`
  math are effectively **free today** — they need no new component.
- A `teacher` **persona already ships** (`backend/.../prompts/teacher.prompt`,
  registered in `agents.py`) — a Socratic tutor, but with **`tools=frozenset()`
  and no knowledge-base access**. It can only emit text today. Every interactive
  component below implies (a) giving the teacher a tool that emits a registered
  `ViewPayload`, and (b) adding that component to the registry + a binding mock +
  a `DESIGN.md` section. That per-component cost is why triage matters.

**Consequence for triage:** the `S` static components are mostly *authoring
conventions over the existing text/registry path* (cheap). The `⭐` KB-linked and
adaptive components unlock the persona's missing capability (tools + retrieval) —
higher value, higher plumbing cost. A sensible first mock slice picks a few `S`
presentation blocks + one or two `⭐` differentiators to prove the whole path.

---

## A. Checks for understanding — selected & constructed response

- [x] **Single-select multiple choice** — one-correct picker; fast pulse-check, auto-graded, can gate advancement. `S` · ✓ Mock 01
- [ ] **Multi-select ("all that apply")** — probes category boundaries (over-/under-inclusion). `S`
- [ ] **True/False + justification** — binary verdict plus a required reason; defeats coin-flip guessing. `M`
- [ ] **Likert / scaled judgment** — "how strong / how likely / where on the spectrum"; trains calibrated judgment vs. binary. `S`
- [ ] **Hotspot / click-the-region** — point at the right spot on an image/diagram/code/text; spatial recognition words can't test. `M`
- [x] **Short free-response + AI grading** — open recall graded against a rubric; forces retrieval, names specific gaps. `M` ⭐ · ✓ Mock 01
- [ ] **Long-form / essay with rubric feedback** — extended argument/synthesis, per-criterion feedback not just a grade. `M` ⭐
- [ ] **Numeric / formula answer** — value or expression graded with tolerance + unit check; partial credit on method. `M`
- [ ] **Cloze (fill-in-the-blank)** — reconstruct missing terms in a passage; cued recall, middle rung between recognition and free recall. `M`
- [ ] **Cloze with word bank (drag-in)** — cloze scaffolded by a shared bank with distractors; recognition-plus. `M`
- [ ] **Code / structured cloze** — fill masked tokens/lines in code, a formula, or a proof; optionally validate. `M`
- [ ] **Matching / two-column pairing** — connect term↔definition, cause↔effect; tests many associations at once. `M`
- [ ] **Odd-one-out** — pick the member that doesn't belong (+ why); sharpens category boundaries. `S`
- [ ] **Ordering / ranking** — drag into correct sequence/rank; exact or rank-correlation partial credit. `M`
- [ ] **Drag-to-categorize (bucket sort)** — sort items into labeled bins; tests classification criteria. `M`
- [ ] **2×2 grid placement** — drop items onto a two-axis matrix (urgent×important, cost×benefit). `M`
- [ ] **Venn / overlap sort** — classify items into overlapping sets incl. the intersection. `M`
- [ ] **Summary quiz (mixed-format checkpoint)** — short varied quiz at a boundary; mastery signal + one more retrieval pass. `M`

## B. Worked examples & guided practice

- [x] **Worked example, progressive reveal** — fully solved example with steps hidden, revealed one at a time; optional "predict the next step." `M` · ✓ Mock 02 (with predict-the-step)
- [ ] **Faded worked example (completion problem)** — learner completes the later steps; the fade from watching to doing. `M` ⭐
- [x] **Hint ladder / scaffolded problem** — ordered hints (nudge → strategy → partial → full) unlocked only as needed; preserves productive struggle. `M` ⭐ · ✓ Mock 02
- [x] **Live step-check problem** — learner submits each step and is validated before proceeding; catches errors early, not at the end. `M` ⭐ · ✓ Mock 02
- [ ] **Derivation / proof stepper** — multi-line derivation revealed line-by-line, each with a "by X rule" justification. `M`
- [ ] **Predict-then-reveal** — learner commits a prediction before the outcome; the wrong-prediction surprise boosts encoding. `S`
- [ ] **Estimate-then-check (Fermi)** — estimate a quantity with reasoning, then compare order-of-magnitude to the real value. `S`
- [ ] **Error-spotting / find-the-mistake** — locate & diagnose the flaw in buggy code / a wrong proof / a bad essay. `M`
- [ ] **Fix-the-answer (correction task)** — not just spot but repair the flawed artifact to a correct state; validated. `M`
- [ ] **Interleaved mixed-practice set** — deliberately shuffles problem types so the learner must first identify the approach. `M` ⭐

## C. Data visualization & charts

- [ ] **Line chart** — trend over an ordered/continuous axis; rate & trend reading. `S`
- [ ] **Bar / column chart** — magnitude across discrete categories; grouped/stacked/100% toggles. `S`
- [x] **Scatter plot** — relationship between two variables; correlation, clusters, outliers; optional trend line. `S` · ✓ Mock 01
- [ ] **Area / stacked-area chart** — cumulative magnitude / composition over a continuous axis. `S`
- [ ] **Pie / donut** — part-to-whole at one moment; few categories only. `S`
- [ ] **Histogram** — distribution of one variable via bins; live bin-width slider; shape/skew/modality. `M`
- [ ] **Box-and-whisker** — five-number summary + outliers; compare spread across groups. `S`
- [ ] **Violin / density plot** — smoothed distribution shape; multimodality. `M`
- [ ] **Bubble chart** — scatter + size (+ color) dimension; multivariate; optional time-slider. `M`
- [ ] **Heatmap** — color-encoded matrix; pattern-spotting across two dimensions (incl. confusion/calendar data). `S`
- [ ] **Correlation matrix** — heatmap of pairwise r; click a cell → the underlying scatter. `M`
- [ ] **Choropleth / geographic heatmap** — values shaded onto map regions (repo already ships Leaflet). `M`
- [ ] **Waterfall chart** — sequential signed contributions building to a total (profit bridge, budget). `S`
- [ ] **Radar / spider chart** — multivariate profile of a few items across axes. `S`
- [ ] **Sankey / flow diagram** — weighted flows between stages; conservation & distribution. `M`
- [ ] **Small multiples (trellis)** — grid of the same chart faceted by a variable; compare many groups. `S`
- [ ] **Overlay / difference view** — two series superimposed with the delta highlighted (actual vs. predicted). `S`
- [ ] **Animated time-series replay** — playback of data evolving (bar-chart race, epidemic curve); scrub/play. `M`
- [ ] **Gauge / KPI meter** — one value against a scale/target with threshold bands. `S`
- [ ] **Treemap** — nested rectangles sized by value; hierarchical part-to-whole. `M`

> See `dataviz` skill for the shared color/mark system these should all obey.

## D. Interactive math & function plotters

- [ ] **Function grapher (single variable)** — plot `y=f(x)`; pan/zoom, trace, roots/asymptotes. `L`
- [ ] **Parametric explorer (slider-driven)** — `y=f(x;a,b,c)` with a slider per parameter; "what does *b* do?" transformations. `L` ⭐
- [ ] **Multi-function overlay** — several functions on shared axes; compare growth rates, mark intersections. `L`
- [ ] **Tangent / derivative visualizer** — movable tangent with live slope; secant→tangent limit; optional derivative curve. `L`
- [ ] **Riemann-sum / integral area** — shaded rectangles under a curve, count slider, sum converging to the integral. `L`
- [ ] **3D surface / contour plotter** — `z=f(x,y)` as surface/contour; rotate, slice; gradients, saddle points. `L`
- [ ] **Vector / slope field** — direction arrows across a plane; drag a seed to trace a solution curve. `L`
- [ ] **Unit-circle explorer** — point on the circle linked to synced sine/cosine traces; degree/radian. `M`
- [ ] **Number line** — points, intervals, inequalities on a 1D axis; drag markers, shade. `M`
- [ ] **Coordinate grid / Cartesian plane** — plot points/lines/regions; click-to-place, read coordinates. `M`
- [ ] **Fraction / tape (proportion) bar** — partitioned bars for fractions, ratios, percent; word-problem modeling. `M`
- [ ] **Matrix / linear-algebra visualizer** — edit matrix entries; watch a unit grid/vectors deform; eigenvectors. `L`
- [ ] **Interactive geometric construction** — draggable points/lines/circles with maintained constraints; live measures. `L`
- [ ] **Transformation sandbox** — translate/rotate/reflect/scale a shape; ghost-vs-after + matrix readout. `M`

## E. Diagrams — structure, process & relationships

- [ ] **Diagram block (Mermaid/Graphviz-style text spec)** — flowchart/sequence/state/ER authored from a text spec. `M`
- [ ] **Flowchart** — steps/decisions/branches; trace a path by answering decisions. `M`
- [ ] **Swimlane diagram** — flowchart partitioned by actor/role; handoffs, parallel work. `S`
- [ ] **Sequence diagram** — ordered messages between participants over time; protocols, request/response. `M`
- [ ] **State machine / automaton** — states + labeled transitions; feed input tokens to animate. `M`
- [ ] **Tree diagram** — hierarchical parent-child; collapse/expand, highlight a path (taxonomies, syntax, binary trees). `S`
- [ ] **Probability tree** — branching outcomes with edge probabilities; multiplication rule, conditional probability. `M`
- [ ] **Node-link network / graph** — entities + relationships; force layout, highlight neighbors, traversal animation. `M`
- [ ] **Algorithm-on-graph animator** — step through BFS/DFS/Dijkstra; visited set + frontier highlighted. `L`
- [ ] **Truth table / logic-gate diagram** — toggle input bits; outputs & gate states update live. `M`
- [ ] **Timeline** — events on a time axis; zoom to eras, click for detail, filter (history, project phases). `S`
- [ ] **Gantt chart** — task bars across time with dependencies; critical path. `M`
- [ ] **Mind map** — radial branching from a central topic; expand/collapse, add nodes. `M`
- [ ] **Concept map** — concepts linked by *labeled* relations ("causes", "is-a"); hide links to quiz. Fits the entity-graph backbone. `M` ⭐
- [ ] **Argument / proof map** — claims, evidence, inferences as a linked structure; flag assumptions. `M`
- [ ] **Fishbone (Ishikawa)** — causes branching toward an effect; root-cause analysis. `S`
- [ ] **Venn / Euler diagram** — overlapping sets; toggle sets, drag items into regions. `M`

## F. Simulations & manipulatives

- [ ] **Physics sandbox** — projectiles/collisions/pendulums/springs; sliders for mass/gravity/friction; live energy readouts. `L`
- [ ] **Economics / market simulator** — supply-demand, equilibrium, elasticity; shift curves, apply tax/ceiling. `L`
- [ ] **Agent-based / population model** — epidemic spread, predator-prey, segregation; watch aggregate curves emerge. `L`
- [ ] **Probability / sampling simulator** — run many trials; empirical vs. theoretical converge (LLN, CLT). `L`
- [ ] **Circuit simulator** — build a circuit; adjust R/V, live current/voltage; Ohm's/Kirchhoff's laws. `L`
- [ ] **Chemistry / molecular visualizer** — 2D/3D molecules & reactions; shift equilibrium with temp/concentration. `L`
- [ ] **Base-ten / place-value blocks** — drag/group manipulatives; regrouping, carrying/borrowing. `M`
- [ ] **3D solid viewer / net unfolder** — rotate solids, unfold to net, cross-section; surface area & volume. `L`

## G. Content & media presentation

- [ ] **Rich prose block** — narration/framing with inline formatting. *(Largely free via existing Markdown renderer.)* `S`
- [ ] **Callout / admonition box** — note / tip / warning / important, icon-marked; guides attention & tone. `S`
- [ ] **Collapsible / accordion** — titled section that expands on click; manages density, optional detours. `S`
- [ ] **Reveal-answer / spoiler** — hidden content uncovered deliberately; optional staged hints → answer. `S`
- [ ] **Tabbed content** — parallel variants (Python/JS, beginner/advanced, example/theory). `S`
- [ ] **Stepper / wizard** — content as numbered steps, one at a time, progress-tracked; optional gating. `M`
- [ ] **Image embed (caption + zoom)** — a figure with caption + required alt text; click to lightbox. `S`
- [ ] **Annotated image (callouts / hotspots)** — image with numbered/clickable hotspots revealing labels; "reveal all" toggle. `M`
- [ ] **Image carousel / gallery** — ordered images stepped through; variations or a progression. `S`
- [ ] **Before/after slider** — two aligned images with a draggable divider; change/transformation. `M`
- [ ] **Progressive-overlay figure** — a base image built up layer by layer while narrating; staged complexity. `M`
- [ ] **Video embed** — inline player with captions + optional chapters/transcript. `M`
- [ ] **Audio / pronunciation clip** — inline player; language, music, audio notes. `S`
- [ ] **Transcript-synced media** — media + scrolling transcript, click a line to seek, current line highlights. `M`
- [ ] **Code block (syntax-highlighted)** — highlighted source with language label, copy, line highlights. `S`
- [ ] **Runnable code block** — sandboxed execute + inline output; edit & re-run. `L`
- [ ] **Step-through code walkthrough** — code with a step sequence highlighting lines + explanations; optional variable-state panel. `M`
- [ ] **Diff / before-after code** — unified or split diff of a change (bug→fix, refactor). `S`
- [ ] **Terminal / command block** — shell commands + output; DevOps/tooling/setup. `S`
- [ ] **Math / LaTeX rendering** — inline & display equations. *(Free today via KaTeX.)* `S`
- [ ] **Formula reference card** — a formula with each symbol defined (hover a symbol → meaning + units). `S`
- [ ] **Definition / vocabulary card** — term + definition + POS + example + pronunciation; optional flip-to-test. `S`
- [ ] **Analogy / example card** — "X is like Y because…"; optional "where the analogy breaks down" reveal. `S`
- [ ] **Side-by-side comparison / feature matrix** — items × attributes with marks; highlight-differences toggle. `S`
- [ ] **Pros/cons or do/don't card** — two-column balanced list; teaches judgment. `S`
- [ ] **Fact / stat highlight** — a single striking number rendered large; anchors memory. `S`
- [ ] **Cheat sheet / reference panel** — dense scannable summary grouped by category; keep-open reference. `S`
- [ ] **Data table / data-grid** — rows × columns; sort/filter/search, conditional formatting, reveal-cells to quiz. `M`
- [ ] **Hover-glossary term** — inline term reveals its definition on hover/tap without leaving the flow. `S`

## H. Sourcing & notes-as-truth — the honesty layer ⭐

*The system's contract is that the owner's **notes are the sole source of truth**
and the wiki is machine-written. These components keep teacher mode inside that
contract; they're the ones a generic LMS structurally cannot have.*

- [ ] **Quote / citation block** — blockquote of the owner's *own* note, linked back to the exact source note ("you wrote this"). `S` ⭐
- [ ] **Source provenance chip / footnote** — inline marker on any claim linking to its supporting note(s)/wiki entity. `S` ⭐
- [ ] **Wiki entity callout / backlink** — boxed pointer to the maintained wiki page for the topic + "go deeper." `S` ⭐
- [ ] **"Based on your notes" banner** — header declaring which notes a lesson was assembled from, as clickable chips. `S` ⭐
- [ ] **Embedded external reference** — rich preview of an external source a note cites (URL/paper/book); distinguishes outside material. `S`
- [ ] **"Not in your notes" gap marker** — flags content the teacher supplied that *isn't* backed by a note; offers "save as a note?" `M` ⭐
- [ ] **Source reconciliation / conflict note** — surfaces when notes disagree with each other or the wiki; prompts a correction note. `M` ⭐
- [ ] **"Connect to what you already know"** — surfaces the owner's existing related notes and bridges them to the new concept; confirmed links can write back to the graph. `M` ⭐

## I. Lesson structure, navigation & progress

*(Feeds the future course-plan layer. Many read from one shared node-tree +
completion/mastery model — design that spine once; these are views over it.)*

- [x] **Learning-objectives card** — "by the end you'll be able to…"; primes attention, mirrored at close. `S` · ✓ Mock 01 (lesson-plan-driven header)
- [ ] **Prerequisites checklist** — assumed prior knowledge, each linked to remedial content; self-assess readiness. `S` ⭐
- [ ] **"Why this matters" card** — relevance/payoff up front; optionally tied to the owner's stated goals. `S` ⭐
- [x] **Course/lesson outline (expandable)** — module→lesson→section tree with completion state + "you are here." `M` · ✓ Mock 04
- [ ] **Table of contents / jump-nav** — flat list of the current lesson's sections; active section auto-highlights. `S`
- [ ] **Breadcrumb / "you are here"** — compact path trail for deep hierarchies. `S`
- [ ] **Section headers & dividers** — chunk a lesson into typed units (concept/example/practice/recap). `S`
- [x] **Course progress bar** — aggregate % across a multi-lesson course; hover for breakdown. `S` · ✓ Mock 04
- [ ] **Lesson progress bar** — progress within the current lesson; manage effort/attention. `S`
- [ ] **Step / segment progress (stepper)** — discrete numbered track for staged content; which step is active/done. `S`
- [ ] **Completion tracker / checklist** — explicit lesson requirements checking off; makes "done" unambiguous. `M`
- [ ] **Mastery / skill meter** — per-concept proficiency from demonstrated performance, not exposure. `M` ⭐
- [ ] **Estimated-time indicator** — "~12 min" per node; recomputes remaining as they progress. `S`
- [x] **"Next up" / continue card** — the single recommended next action + why; eliminates decision friction. `S` ⭐ · ✓ Mock 04 (Resume CTA)
- [ ] **Resume / "since last time" card** — auto-remembered position + a recap of last session; bridges the gap. `M` ⭐
- [ ] **Branch / path chooser** — forks ("dive deeper" vs "move on"); learner agency over the route. `M`
- [x] **Lesson-summary / recap card** — condenses what was covered; optional save-to-notes. `S` · ✓ Mock 04
- [x] **Key-takeaways callout** — 2–5 must-remember points, elevated; feeds spaced-repetition. `S` · ✓ Mock 04
- [ ] **Glossary panel** — full alphabetized/grouped term list for the lesson/course. `S`
- [ ] **Bookmark / save-for-later** — learner markers on sections/terms; browse & jump back. `M`
- [ ] **Note-taking / annotation panel** — learner writes notes / annotates passages; these can re-enter the RAG/wiki pipeline as notes. `M` ⭐
- [x] **Skill-tree / mastery map** — unlockable tree gated on prerequisites; the learner-facing face of the course plan. `M` ⭐ · ✓ Mock 04
- [ ] **Milestone / achievement markers** — celebrate thresholds; opt-in, pressure-free framing. `S`
- [ ] **Streak / engagement indicator** — consecutive-session habit tracking. *(Churn/guilt risk — flag opt-in.)* `S`

## J. Live dialogue, adaptivity & personalization — the core differentiators ⭐

*These are what a **live LLM teacher** does that fixed courseware can't. They lean
on the persona gaining tools + RLS-scoped retrieval over the owner's notes (the
domain firewalls — health/finance/location — still gate what it can pull in).*

- [ ] **Inline "ask a follow-up"** — anchored chat on any lesson span; turns monologue into dialogue. `M` ⭐
- [x] **"Explain this differently" / re-explain** — regenerate via a different representation (analogy/example/visual/formal); avoids repeating tried modes. `S` ⭐ · ✓ Mock 01
- [x] **Tone / persona selector** — warm coach ↔ terse expert ↔ Socratic gadfly; match affective needs. `S` ⭐ · ✓ Mock 03
- [ ] **Verbosity / depth dial** — how *much* is said at a given difficulty (terse ↔ expansive) — orthogonal to pace. `S` ⭐
- [x] **Difficulty / level adjuster** — shift conceptual load on the same topic; keep the learner in the ZPD. `M` ⭐ · ✓ Mock 03
- [x] **Pace adjuster** — lesson tempo & step size ("slow down / skip ahead"). `M` ⭐ · ✓ Mock 03
- [x] **Adaptive branching (choose-your-path)** — ranked next directions based on the learner model. `M` ⭐ · ✓ Mock 03
- [ ] **Just-in-time prerequisite back-fill** — detects a missing prereq mid-lesson, inserts a mini-lesson, returns. `M` ⭐
- [ ] **Zoom lens (abstraction ladder)** — big-picture ↔ concrete instance ↔ detail on the *same* idea. `S` ⭐
- [x] **"I'm confused here" flag** — one-tap panic button on a span; triggers a diagnostic re-explain. `S` ⭐ · ✓ Mock 03
- [x] **Misconception detector + targeted remediation** — infers the *specific* faulty mental model from wrong answers, counters with a contrasting case. Highest-value LLM-only component. `M` ⭐ · ✓ Mock 01
- [ ] **Error-driven counterexample generator** — on over-generalization, fabricates the minimal case that breaks the rule. `S` ⭐
- [ ] **Checkpoint / "ready to move on?"** — brief mastery gate before advancing; shared go/no-go signal. `M` ⭐
- [ ] **Live feedback on free responses** — span-level, criterion-referenced feedback, checked against the owner's own notes. `M` ⭐
- [ ] **"Explain it back to me" (teach-back)** — learner articulates the concept; teacher evaluates for gaps; protégé effect. `M` ⭐
- [ ] **Confidence-weighted questioning** — attach "how sure?" to answers; flag confidently-wrong vs. unsure-but-right. `M` ⭐
- [ ] **Spaced-recall injector** — weaves a quick retrieval question about a *prior* topic, timed to the forgetting curve. `M` ⭐
- [ ] **Socratic dialogue turn** — guiding question chain that adapts to each reply; caps + falls back to direct instruction if the learner stalls. `M` ⭐
- [ ] **Devil's-advocate / defend-your-answer** — pushes back on a *correct* answer to test real understanding. `S` ⭐
- [ ] **Case / scenario walkthrough** — realistic decision points with consequences; drives transfer to authentic contexts. `M` ⭐
- [ ] **Personalized example generator** — builds examples/analogies from the owner's hobbies, job, and notes; "use a different one." `M` ⭐
- [ ] **Knowledge-gap contextualizer** — uses the KB to skip what the notes prove is known and spotlight what's new. `M` ⭐
- [ ] **Contradiction / stale-note flagger** — lesson clashes with a note → surface it; may draft a correction note. `M` ⭐
- [ ] **Interest-based motivation hook** — opens/punctuates with why *this* matters to *this* learner, from goals/notes. `S` ⭐
- [ ] **Portfolio / progress narrator** — "three weeks ago you couldn't X; today gets you to Y"; longitudinal only a persistent system has. `S` ⭐

## K. Metacognition, goals & session meta

- [ ] **Goal-setting prompt** — elicit what the learner wants + stakes ("exam Friday"); binds the plan to it. `S` ⭐
- [ ] **Prior-knowledge probe** — front-loaded diagnostic to right-size the lesson and skip mastered material. `M` ⭐
- [ ] **Muddiest-point capture** — "what's still unclear?" free prompt; routes to adaptive branching. `S` ⭐
- [ ] **Self-explanation prompt** — "why is this step correct?"; forces inference generation, consolidates. `M` ⭐
- [ ] **Reflection / journal prompt** — "what clicked, what's fuzzy?"; metacognition + high-signal self-report saved as a note. `S` ⭐
- [ ] **Effort / affect check-in** — lightweight mood pulse that routes to tone/pace/difficulty changes. `S` ⭐
- [ ] **Study-strategy coach** — notices *how* the learner studies (skips hints then fails) and coaches the strategy. `M` ⭐
- [ ] **"What should we do next?" planner** — co-plans the next session blending learner preference with gap + spaced-repetition priorities. `M` ⭐
- [ ] **Learner-generated questions** — learner writes their own quiz Q+A; deep-processing, reveals what they think matters. `M` ⭐
- [ ] **One-sentence summary / gist** — compress the lesson to a headline; forces prioritization; AI-graded against key points. `M`
- [ ] **Curiosity tangent (bounded)** — answers an off-path question briefly, marks it a tangent, offers to bookmark. `S` ⭐
- [ ] **Session summary → note-back** — drafts a summary in the owner's note style, offers to save it as a source-of-truth note; closes the loop. `M` ⭐
- [ ] **Rapid-fire retrieval sprint / drill mode** — timed burst of quick items for fluency; toggle in on demand. `M`
- [ ] **Flashcard + spaced-repetition deck (SRS)** — two-sided recall with expanding-interval scheduling across sessions. `M`

---

## Cross-cutting notes for triage

- **Wrappers, not just widgets.** Confidence-rating, self-explanation, predict-then-reveal,
  hint ladders, and the difficulty/pace/verbosity/tone dials are *modifiers* that
  compose onto almost any base component. Consider modeling them as decorators
  rather than standalone views — triage them as a set.
- **Four orthogonal dials.** Difficulty, pace, verbosity, and tone are independent
  axes; keep them separable in the UI, not collapsed into one "level."
- **One shared data spine.** Outline, breadcrumb, TOC, progress bars, next-up,
  mastery meter, and skill-tree all read from one node-tree + completion/mastery
  model. Ditto a shared viz-primitive layer (axes, scales, legends, tooltips,
  play/step controls) under the chart/plotter families. Build each once.
- **A common `ViewPayload` envelope.** Nearly every component needs
  `{data, labels/units, scale/domain, highlights, interactivity config, provenance}`.
  A uniform envelope lets the teacher instantiate any registered view the same way,
  and makes **provenance an optional field on *every* component** — Section H is then
  just the components where provenance is the whole point.
- **Grading & latency spectrum.** Auto-gradable (selected-response, cloze, matching,
  ordering, categorize) vs. AI-graded (free-response, teach-back, essay, Socratic).
  The teacher model should know the cost/latency of each before choosing.
- **Guardrails on generative dialogue.** Socratic, devil's-advocate, and hint ladders
  need a frustration/give-up threshold wired to the affect check-in, so productive
  struggle doesn't tip into disengagement.
- **Write-back respects the wiki rule.** "Connect to what you know," contradiction
  flagging, and session note-back feed *back* into the KB — they must produce
  **owner-owned notes or correction notes, never direct wiki edits** (the wiki is
  machine-written only). All retrieval is RLS-scoped; the health/finance/location
  firewalls gate what a lesson can pull in.
- **Accessibility is required input, not optional.** Alt text, captions, transcripts,
  and non-color-only encodings are mandatory fields the teacher must supply — so it
  can also *narrate* any visual.
- **Effort ≠ value.** Most `S` presentation blocks are cheap authoring conventions
  over the existing text/registry path. The `⭐` differentiators (KB-linked +
  adaptive) cost more plumbing (persona tools + retrieval) but are the reason
  teacher mode exists. A first mock slice should prove the whole path end-to-end
  with a few `S` blocks + one or two `⭐` differentiators.
