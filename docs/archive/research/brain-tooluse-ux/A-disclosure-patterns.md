# Full Brain tool-use disclosure — Lens A: disclosure pattern & information architecture

Research report. Lens: *What is the right disclosure model for revealing an
agent's tool use beside its answer?* The current answer is a 3D **flip card**
(`FullBrainSurface.tsx` → `FlipBubble`); the owner is reconsidering it. This
report evaluates alternatives against four criteria — **cognitive load,
discoverability, reversibility, and scaling from 1 to 15 steps** — surveys how
shipping AI products disclose tool use, proposes three concrete buildable
designs, and recommends one.

Scope note: this lens is *the disclosure container*, not the styling of an
individual step/source row. The step/source row vocabulary
(`toolStep`, `SourceCard`, domain dot + derived title + snippet) is already
settled and good; every option below reuses it verbatim.

---

## 1. Problem framing for this lens

### What the flip card actually is, today

`FlipBubble` makes every assistant turn-with-tools a two-faced card. The **front**
is the answer plus a `fb-cue` button ("N tools" with a back-chevron). The **back**
("Worked · N steps · M sources") lists `toolStep`s, source cards, and a staged-
proposal chip. You reach the back by a horizontal swipe (the pointer handler claims
the gesture once it proves horizontal, `FLIP_PX = 44`) or by tapping the corner cue;
you return by swiping right or tapping "answer". The wrapper is pinned to a fixed
left-anchored width and animates `height` to whichever face shows, so a tall tool
run is never clipped.

### Why it's the wrong model — five concrete problems

1. **It hides the answer to show the work, and vice-versa.** The two faces are
   mutually exclusive. You can never see *"born March 19, 1986"* and *"because I
   read the note that says so"* at the same time — which is the one moment a
   provenance UI exists for. Verification means flipping back and forth and
   holding state in your head.

2. **Discoverability is poor and the cue fights the design system.** DESIGN.md
   §"Full Brain lateral shortcuts" bans edge chrome and makes swipe an
   *enhancement only* — "a gesture is never the only way out". The corner
   `fb-cue` is the keyboard/tap fallback, but it's a 10px `--text-3` whisper in the
   bottom-right. A first-time user does not know work is even there. Contrast the
   honesty principle (§Principles 5): "Honest status, always visible … never
   hidden behind a tap." Tool use *is* status; the flip hides it behind a tap
   **and** a discovery problem.

3. **Gesture collisions.** Three horizontal-swipe meanings now stack on the same
   axis: swipe the *composer* → Sessions/Proposals panels; swipe a *flip bubble* →
   turn it; swipe a *note bubble in Entry* → action rail. `FullBrainSurface`'s
   `onTouchStart` already has to special-case `.fb-flip` to keep the shell's
   panel-swipe from firing inside a card. Every new horizontal meaning taxes the
   recognizer and the user's mental model. DESIGN.md is explicit that horizontal
   was reserved *because modes switch by tap*; the flip spends that budget on a
   per-bubble toy.

4. **It doesn't scale.** At 1 step the flip is theatrical overkill (rotate 180°
   in 3D to reveal a single "Searched your notes · 4 results" line). At 15 steps
   the back face is a tall scroll *inside a 3D-transformed, `backface-hidden`,
   absolutely-positioned element whose parent height is JS-measured* — fragile,
   and it still competes with the page's own vertical scroll. The fixed-width
   left-anchored footprint also forces the *answer* side to reserve odd width.

5. **Reduced-motion and a11y friction.** A 420ms `rotateY` is exactly the
   "non-essential animation" §Motion says to drop under `prefers-reduced-motion`.
   The component already has to juggle `inert` on the hidden face to keep it out
   of the a11y tree and tab order — complexity that exists *only* because two
   states share one box.

### The constraints any replacement must honor (from DESIGN.md)

- Phone-first, one-thumb; touch targets ≥ 44px; primary actions bottom-half.
- Near-monochrome; **color is information** (domain dots, mode steel), never
  decoration. No gradients/glass/heavy shadows.
- Tokens only; Lucide outline icons; lowercase-calm em-dash microcopy.
- Every overlay has a visible tappable exit; gesture is never the sole path.
- Inline expansion is the sanctioned paradigm for "row-level detail that
  doesn't warrant navigation" (§Surface paradigms). A bottom **sheet** is the
  workhorse for "contextual detail/actions". Both are already-built shells.
- The data is already there: `message.tools: ToolActivity[]`, each with `name`,
  `ok`, `summary`, `sources[]`, `proposal`, `entities[]`, reduced to `ToolStep`s
  by `toolSummary.ts`. **Note:** `tool_call.arguments` is currently *dropped* by
  the reducer (`transcript.ts` `case "tool_call"`), so today we can label a step
  ("Searched your notes") but cannot show *what* it searched for. A timeline/
  "show work" model that wants query text would need the reducer to keep
  `arguments` — a small, contained change, flagged here because it gates how rich
  the trace can be.

---

## 2. How shipping products disclose tool use (survey, with sources)

The field has converged on **three** disclosure models. None of them is a flip.

**A. Citation-first / footnote (Perplexity, ChatGPT search, Claude.ai).** The
answer is primary and *always visible*; provenance attaches to it. Perplexity
streams **numbered inline footnotes** next to each claim (typical answer carries
5–10), backed by an expandable source list with title + favicon; citations are
assigned *during* context assembly, not retrofitted. ChatGPT streams hidden
private-use Unicode markers inline and swaps them for clickable citation "chips",
plus a **"Sources" affordance below the answer** that opens a sidebar; hovering a
source highlights where in the answer it was used. A small globe icon marks that
search ran. Takeaway: **the answer never moves; sources are an always-present,
low-cost layer beneath/around it**, and the mechanical step list is demoted.
ShapeOf.ai's pattern library codifies this: inline cues for sentence-level
claims, *panels/drawers for long-form exploration*, hover/tap previews to "balance
speed with thoroughness", and "make broken citations explicit rather than hiding
gaps."

**B. Collapsed "worked/thought" summary that expands inline (Claude.ai Research,
ChatGPT reasoning, Cursor, agenttrace-ui).** A single quiet line sits *above or
below* the answer — Claude.ai shows an **expandable section above the final
response** ("how it deconstructed the problem, search terms, how it evaluated
results"); while running it's a thinking indicator with a timer; when done it
**collapses to a summary that re-opens on demand**. This is the dominant agent
pattern. agenttrace-ui (a React lib built for exactly this) ships Timeline /
Graph / **Compact** views and an explicit progressive-disclosure ladder:
*summary → detailed steps → raw tool I/O*. The industry consensus quote: "after
the AI completes its work, the thinking steps collapse into a summary while
results remain persistent, and the collapsed steps can be re-opened." Cursor's
pain points (truncated collapsed terminal commands, requests to add expand/
collapse for long agent responses) are the *cautionary tale*: collapse is right,
but don't truncate the one identifying detail of a step.

**C. Side drawer / dual-pane trace (Cursor's "process left, output right",
Honeycomb agent timeline, Claude.ai's research sidebar).** For *heavy* runs the
process moves to a **separate lane** so the long internal monologue doesn't shove
the answer off-screen — a dual-scroll layout with thinking/tool-calls on the left
and outputs on the right. This is desktop-shaped (two columns), but the mobile
analogue is the bottom sheet / lateral panel — which JBrain2 **already has** for
Sessions/Proposals. Honeycomb's "agent timeline" frames the heavy case as a
flight recorder: lanes of tool calls with durations, retries, failures — i.e. a
trace you *navigate*, not read inline.

**Synthesis:** shipping products keep the **answer always visible** and treat
provenance as either (A) an inline citation layer, (B) a collapse-by-default
inline summary, or (C) an offloaded panel for the heavy case. The flip — making
answer and work mutually exclusive co-equal faces — appears **nowhere** in
production. That is the strongest signal in this report.

Sources: Perplexity citation-forward guide (unusual.ai); "How AI engines cite
sources" (Geolyze, Medium, 2025); funnelstory.ai on ChatGPT streaming citations;
OpenAI "Introducing ChatGPT search"; ShapeOf.ai citations pattern; Claude.ai
research support docs & buildfastwithai review; agenttrace-ui (Vercel community
showcase); MindStudio / agentic-design.ai progressive-disclosure patterns;
Honeycomb "Agent Timeline" blog; Cursor community forum (expand/collapse, collapsed
terminal feedback) & Cursor tool-calling docs.

---

## 3. Three concrete alternative designs

All three reuse the settled vocabulary: `toolStep` rows (Lucide glyph + label +
trailing count), `SourceCard` (44px row: domain dot · derived title · matched
snippet · chevron → opens note), the "Review proposal" chip, and entity chips.
All keep the **answer always rendered**. All drop the 3D flip, the `inert`
juggling, and the per-bubble horizontal-swipe (freeing that axis back to the
composer-only panel gesture). Sibling mock `docs/mocks/assistant-tool-usage.html`
already prototypes the core of A and the citation half of C — these are
refinements of explorations the repo has already drawn.

### Design 1 — Inline collapsed "Worked" disclosure (accordion under the answer)

The default. The answer renders as a normal `.bubble.ai`. Directly beneath it,
inside the same bubble, a hairline `border-top` separates a single **`Worked`
line**: gear glyph + `Worked` + muted summary (`searched notes · read 1`) +
trailing caret. Tap anywhere on the 44px line to expand *in place* (caret rotates
90°, §Motion 120–180ms ease-out, height auto). Expanded, it shows the existing
`toolStep` rows and indented `SourceCard`s; collapsed is the resting state.

- **Layout:** one bubble, vertical flow, no fixed width, no absolute
  positioning. Answer → hairline → Worked line → (expanded) steps/sources.
- **Affordance:** a visible, labelled, full-width tap target — not a corner
  whisper. Reads as status, satisfying §Principles 5. No gesture required;
  optional: tapping is the *only* control, so there's nothing to discover.
- **States:** *streaming* → the line shows live count ("searched notes…") echoing
  the bottom `AgentStatusLine`; *done collapsed* (default); *expanded*; *error* →
  a failed step (`ok===false`) gets a rose dot + "couldn't read note" and the
  summary line surfaces the failure so it's visible while collapsed (don't hide
  gaps).
- **1 step:** the Worked line *is* the disclosure; expanding shows one row +
  its sources. Near-zero resting cost — one quiet line.
- **15 steps:** expanded list grows the bubble; it scrolls with the page (no
  nested scroll, no clipping). Optional cap: render first ~6 steps + a "show all
  15 steps" row (same in-place-grow pattern as the Analysis-tab OCR "show all N
  lines") so a giant run doesn't dominate before the user asks.
- **Reversibility:** trivial — tap to collapse; state is local; no animation to
  reverse-engineer.
- **Reduced motion:** caret + height snap instead of animate; fully functional.

### Design 2 — Citation-first answer with a demoted trace (footnotes + sources tray)

Lead with provenance the way answer-engines do, exploiting that the codebase
**already supports `[^n]` inline citations** (`Bubble` builds `onCite` from
`flatSources`; `Markdown` renders the markers). Make that the star.

- **Layout:** answer with inline numbered `[^n]` superscript chips at the claims
  (steel-tint, ~14px square, ≥ the 44px tap area via padding/hit-slop). Beneath,
  a compact **Sources tray**: horizontally-scrollable or wrapped `cite-chip`s
  (`①` numbered disc + derived title), each opening the note — these *are* the
  `SourceCard`s collapsed to chips. Below the tray, a tiny `--text-3` **"N steps"**
  toggle that expands the *mechanical* trace (search/read/proposal rows) inline,
  demoted because sources are the thing users actually want.
- **Affordance:** citations are self-evidently tappable (universal pattern);
  tapping `[^n]` scrolls/highlights its source chip (mirrors ChatGPT hover-to-
  highlight). The "N steps" toggle is the secondary disclosure for the curious.
- **States:** *streaming* → markers stream in with the text, chips populate as
  `tool_result` sources land; *no citations* (a turn that searched but the prose
  cited nothing) → tray still lists sources under a "sources" label so work is
  never invisible; *proposal staged* → the "Review proposal" chip sits in the
  tray row, not buried in the steps.
- **1 step:** one `[^1]`, one source chip, "1 step" toggle. Clean.
- **15 steps:** the *sources* (the useful part) wrap/scroll as chips regardless
  of step count; the 15 mechanical steps stay folded behind the toggle, so step-
  count growth is absorbed by the part users ignore. Best scaling of the three
  for *source*-heavy runs; weakest when a turn has many steps but *few* sources
  (e.g. memory edits, proposals) — those live only behind the toggle.
- **Reversibility:** toggle collapses; citations are stateless links.
- **Risk:** depends on the model actually emitting `[^n]` markers reliably and on
  `flatSources` ordering matching them (already wired, but quality-sensitive).
  Mitigate by always showing the sources tray even when markers are absent.

### Design 3 — "Trace" bottom sheet (persistent thin rail → full timeline)

Keep the bubble pure; offload the work to the **already-built `<Sheet>`**. Under
the answer sits a persistent **thin trace rail**: a single 28px-tall ghost row —
gear glyph, `worked · 2 steps · 4 sources`, trailing chevron — that is *always
visible* (not a corner cue) and reads as a status line. Tapping it opens a bottom
sheet (the §Modal-system shell: drag handle, title "Worked", swipe-down/✕/scrim
dismiss) containing the full **timeline**: ordered `toolStep`s with their
`SourceCard`s, the proposal chip, and — if the reducer is taught to keep
`tool_call.arguments` — the query text per search (`searched "born" · 4 results`),
turning the rail into a real flight-recorder à la Honeycomb/Cursor.

- **Layout:** bubble = answer only. Below it, the persistent rail. Sheet hosts the
  scrollable trace, which has *room to breathe* and its own scroll container —
  decoupled from the chat scroll.
- **Affordance:** the rail is a labelled ≥28px (44px hit area) row, far more
  discoverable than `fb-cue`; opening a sheet is the most familiar mobile
  gesture-or-tap there is. Reuses the exact paradigm of Sessions/Proposals, so it
  fits the Full Brain three-pane mental model.
- **States:** *streaming* → rail animates the live step count, doubling as the
  per-bubble progress (could even retire the global `AgentStatusLine` for tool
  turns); *done*; *error* → rail goes rose ("1 step failed"); *empty tools* →
  no rail (unchanged from today's plain bubble).
- **1 step:** rail says `worked · 1 step`; sheet is arguably heavy for one row —
  acceptable, but this is the weakest case (a full modal for a one-liner).
- **15 steps:** *best in class.* The sheet scrolls a long timeline comfortably,
  never shoving the transcript; this is precisely the case the dual-pane/drawer
  pattern exists for.
- **Reversibility:** swipe-down/✕/scrim — three sanctioned exits, zero ambiguity.
- **Cost:** a modal for provenance is a bit heavyweight for the *common* small
  case; trades inline immediacy for unlimited room. "One modal at a time" means
  it can't co-exist with an open Proposal sheet — fine, since reviewing a proposal
  is a separate act.

---

## 4. Pros / cons

| Criterion | Current: Flip card | D1: Inline collapsed "Worked" | D2: Citation-first + demoted trace | D3: Trace bottom sheet |
|---|---|---|---|---|
| **Answer + work visible together** | No (mutually exclusive faces) | Partial (answer always; work one tap below, then co-visible) | **Yes** (citations live *in* the answer) | No (work in a sheet) but answer stays put |
| **Cognitive load (resting)** | High (theatrical, two states) | **Low** (one quiet line) | Low–med (inline markers add some texture) | **Lowest** (one ghost rail) |
| **Discoverability** | Poor (10px corner cue) | **Good** (labelled full-width line = status) | **Good** (citations are self-evident) | **Good** (persistent labelled rail) |
| **Reversibility** | Awkward (swipe back / find "answer") | **Trivial** (tap to collapse) | Trivial (stateless links + toggle) | **Trivial** (3 sanctioned exits) |
| **1 step** | Overkill | **Great** | Great | Heavy (modal for one row) |
| **15 steps** | Fragile (3D scroll, clip risk) | Good (inline grow + cap) | Good for sources, weak for step-heavy | **Best** (room to scroll) |
| **Fits DESIGN.md** | Violates honesty/gesture rules | **Inline-expansion paradigm, exact fit** | Reuses live `[^n]`; citation layer | Reuses `<Sheet>` + lateral mental model |
| **Build cost / risk** | (exists) | **Low** (delete flip, add accordion) | Med (citation-quality-dependent) | Med (sheet wiring; wants `arguments`) |
| **Frees the horizontal axis** | — | **Yes** | **Yes** | **Yes** |
| **a11y / reduced-motion** | Needs `inert`, 420ms rotate | **Clean** (height/caret) | Clean | Clean (sheet handles trap/focus) |

---

## 5. Recommendation

**Ship Design 1 (inline collapsed "Worked" accordion) as the default disclosure,
and fold in Design 2's already-wired inline `[^n]` citations as a complementary
layer.** Reserve Design 3's sheet only as the *escape hatch for genuinely heavy
runs* (an "open full trace" row at the bottom of an expanded Worked block when
steps exceed the inline cap), not as the primary model.

### Rationale

1. **It matches where the whole industry landed** (§2): answer always visible,
   work collapsed-by-default and expandable in place — Claude.ai's "expandable
   section above the response", Cursor's "collapse to summary, re-open on demand",
   agenttrace-ui's Compact view. The flip is an outlier no shipping product uses;
   that is decisive.

2. **It is the literal DESIGN.md paradigm.** §Surface paradigms maps "row-level
   detail that doesn't warrant navigation" → **inline expansion within the list**.
   Tool use under an answer is exactly that. The system already uses in-place
   grow for the Analysis-tab OCR "show all N lines" and inline expansion for the
   Review-history resolved rows — D1 reuses a settled, proven pattern rather than
   inventing one.

3. **It restores honesty (§Principles 5).** A labelled "Worked · searched notes ·
   read 1" line *is visible status*, not work hidden behind a discoverable swipe.
   A first-timer sees the agent did work without doing anything.

4. **It scales monotonically and degrades gracefully.** 1 step → one quiet line;
   many steps → in-place grow with a cap + "open full trace" handoff to the sheet
   for the rare 15-step case. No fragile 3D, no nested scroll at the common size,
   no clipping.

5. **It frees the horizontal-swipe axis** back to its DESIGN.md-sanctioned single
   meaning (composer → Sessions/Proposals), removing the `.fb-flip` special-case
   in `FullBrainSurface`'s touch handler and the gesture-collision tax.

6. **Citations (D2) compose for free** because `onCite`/`flatSources`/`Markdown`
   already exist — leading the answer with `[^n]` markers gives sentence-level
   provenance *without* expanding anything, and the Worked accordion carries the
   mechanical trace for those who want the "how". This is the best of A+B: the
   citation-first immediacy of Perplexity/ChatGPT with the collapsible-trace
   honesty of Claude.ai/Cursor.

### What I would *not* do

- Don't make the sheet (D3) the default — a modal per provenance glance is too
  heavy for the common 1–2 step turn, and "one modal at a time" collides with
  Proposal review. Keep it as the heavy-run overflow only.
- Don't keep any 3D flip, even as an option — it fails honesty, discoverability,
  reversibility, and scaling simultaneously.

### Two implementation notes for whoever builds the mockups

- To make any trace richer than a label (query text, "read note X"), the reducer
  must stop dropping `tool_call.arguments` (`transcript.ts`). Small, contained;
  flag it in the build.
- Don't truncate a step's one identifying detail when collapsed — Cursor's
  community pain. The Worked *summary* line can be terse, but an expanded step
  should show what it searched/read in full.
