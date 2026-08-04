# Deep Research Tool — Build Plan

> **Status:** In progress · **Last verified:** 2026-08-04 · **Waves:** D1✅ D2✅ D3◻️ (v1 shipped; v2 orchestration merged; v3 on-box budget tuning merged; v4 report library merged; v5 budget-8M + meter fix merged (PR #902); v6 short sub-agent row titles + pinned header + fan auto-scroll merged (PR #903/#904); v7 streaming report + phase checklist; v8 checklist → vertical timeline with the fan nested in the active stage; v9 render gpt-oss harmony citations; v10 critique fed the cited SOURCES for citation-faithfulness checking; v11 report-depth upgrade (8–10 page `deep` reports); v12 evidence-grade signposting + scope note; v13 budget+wall-clock bump for saturating breadth-5 runs; v14 quantitative-provenance rule + citation-attribution fidelity + recommendation-grade signpost; v15 citation reliability — synth must-cite + question-aware source curator + entity-disambiguation queries; mock-gate sign-off pending)

**v15 revision (a citation-rich request came back UNCITED — the ballot-research failure).**
Two identically-prompted political reports diverged: the Senate run cited 162 `[^n]` markers
against 399 gathered sources; the CFO run gathered 129 sources but emitted ZERO markers,
closing with a Scope note that "the source list did not contain directly relevant citations …
it cannot be fully foot-noted." Root cause was the writer over-applying the "no SOURCES ⇒ cite
nothing" escape to a *noisy* list, atop a gather whose `web_search` registered every hit as
citable (a common-word name — "Frank", "Financial" — dragged in dictionary/film/bank-login
noise). Fixed across three seams, each keeping the existing machinery:

- **Synthesize prompt** (`dr-synth-v6 → v8`) — a non-empty `SOURCES` list now makes inline
  `[^n]` citation MANDATORY: cite every claim a listed source backs and mark the rest
  unconfirmed; judging the list weak/noisy is explicitly not licence to drop all citations, and
  the Scope note may no longer excuse an uncited report. The v8 escape valve (from the review)
  guards the all-noise case the keep-biased curator leaves intact: when NOT ONE listed source is
  actually on-topic, cite nothing and mark claims unconfirmed rather than fabricate a marker — so
  the must-cite mandate can never pressure a mis-citation. Only a truly EMPTY list means cite
  nothing unconditionally.
- **Source curator** (new `deep_research_curate_sources.prompt`, `dr-curate-v1`) — a keep-biased,
  fail-open, drop-capped judge culls the CLEARLY-unrelated registry entries (namesakes,
  dictionary pages, unrelated films/companies, login landings) before the writer cites, so a
  noisy list can't crowd out the real sources. Runs only once the registry is large enough to
  carry noise (`_CURATE_MIN_SOURCES`); a small list or an oversized/empty drop is left untouched.
- **Query disambiguation** (`deep_research_plan.prompt` dr-plan-v6 → v7; `research.prompt`
  agent-research-v10 → v11) — a named subject (person/org/place, esp. a common/shared name) is
  anchored with full name + role + jurisdiction + year, and namesake results (a dictionary word,
  a film, an unrelated company) are discarded rather than cited — cutting the noise at its source.

**v14 revision (stop invented precision and mislabelled citations — a frontier-model
critique of the TTP-transfusion `deep` report).** An owner-supplied review of the platelet/
red-cell transfusion report converged with the v12 finding: the writer hedges verbally yet
still manufactures precision next to the hedge. The shipped report invented sample sizes
("≈2,000 admissions", "≈100 episodes"), an effect size ("a *doubling* of AKI") one line above
"precise odds ratios are not published", and mislabelled a cited source — attributing a claim
to "ASH … (Blood 2024)" when the cited page is a 2017 *Blood* review, not a 2024 ASH guideline.
Reproduced on-box against `gpt-oss-120b` (the model `agent.turn` routes to): fed a finding that
carried only a *direction*, the v5 prompt still wrote "mortality rates roughly double" and "RR
1.5–2.0" — because inventing the point estimate itself is not the CI/pooled *dressing* the v12
rule already forbade. Fixed in the prompts, kept domain-agnostic; each change re-verified on-box
(the invented numbers disappeared, depth held, and the writer downgraded the review from
"guideline (Blood 2024)" to "the ASH review" with no fabricated year):

- **Synthesize prompt** (`deep_research_synthesize.prompt`, dr-synth-v5 → **dr-synth-v6**) —
  three additions to the existing machinery (unchanged): (1) a dedicated *quantitative
  provenance* rule — a specific quantity (sample size/study count, an effect size incl. a
  "doubling"/"tripling", a percentage, an incidence, a rate) may appear ONLY if a source
  actually stated it; never invent, estimate, round into being, or back-calculate one, and a
  guessed number is a defect even prefixed with "≈"/"roughly". Where a finding gives only a
  direction, say so qualitatively and name the study design over a number it never gave. (2)
  *Citation-attribution fidelity* — name a cited document (issuing body, year, edition, type)
  only as its `SOURCES` entry shows it; don't assign a year/society/"guideline" label the entry
  doesn't carry, and don't upgrade a review/summary into the primary guideline it discusses.
  (3) *Recommendation-grade signpost* — when a source states a formal strength/grade (GRADE,
  1A/1B, strong/conditional), report it as given; don't invent one.
- **Critique brief** (`_critique`, the v10 citation-faithfulness reviewer) — its faithfulness
  pass now also checks every SPECIFIC QUANTITY against the cited source (flag any the source
  does not state) and ATTRIBUTION (flag a mislabelled year/society or a review passed off as
  the primary guideline), catching what a plain "does the source support the claim" check let
  through.


**v13 revision (a breadth-5 two-wave run was saturating both ceilings at once).** An owner
breadth-5 run was landing right at the token cap. The run-log confirmed it was co-limited:
9 sub-agents spent **~9.14M** child tokens against the ~10M children pool (6 of them truncated
at their own `max_steps`), while the turn ran **4970s** — its last child finishing just ~30s
before the old 4500s tree deadline (≈92% of the 5400s turn cap). On the serial local box the
two ceilings are coupled (more tokens → more generation → more wall-clock), so raising the
token pool alone would only have hit the wall-clock next. Both moved together:

- **Token pool** — jerv's `budget_multiplier` **4 → 6** (`agents.py`), lifting the deep-research
  tree budget ~13.3M → ~20M and the children pool ~10M → ~15M. jerv (not the archivist's 4)
  carries it because a breadth-5 two-wave fan is its heaviest turn; an ordinary jerv chat turn
  spends far less. Pinned in `test_agents.py`.
- **Wall-clock** — `TREE_WALL_CLOCK_S` **4500 → 6600s** (`tree.py`) and the parent turn cap
  `_MAX_TURN_WALL_CLOCK_S` **5400 → 7500s** (`api/agent.py`), preserving the ~900s post-fan
  synthesis headroom between them. The `_TURN_IDLE_S` progress watchdog (900s) still catches a
  genuine stall far sooner, so the higher ceiling only helps a turn that is actually progressing.
- For a run that wants to go past even this, the **`deepest_research`** lane (50M / 3h,
  background, checkpointed) remains the right tool — the standard lane is deliberately still
  one interactive turn.

**v12 revision (signpost evidence strength, note scope, and hold claims to what their
sources say).** Two owner-supplied critiques of a `deep` medical report — a narrative-review
quality rating and a hallucination pass — converged on the same failure mode: the write-up
asserted claims without grading the evidence, and where it hallucinated it did so not by
inventing papers or biology (none found) but by *over-precision and attribution blending* —
a synthesized incidence stated with a fabricated confidence interval, and one paper's
infarct/inflammation endpoints presented as if they included the seizure/EEG endpoints a
sibling paper measured. It also carried no note of how it was researched. Fixed in the
prompts, kept domain-agnostic (the same levers help a hardware-benchmark or product-claim run):

- **Synthesize prompt** (`deep_research_synthesize.prompt`, dr-synth-v4 → **dr-synth-v5**) —
  additions to the existing corroborate-by-authority machinery (unchanged): (1) *signpost the
  evidence grade* — name whether a load-bearing claim rests on a primary source or a secondary
  summary and, for an empirical claim, whether it comes from a proposed mechanism/animal
  result, an observational human study, or a controlled/prospective one. (2) *Hold claims to
  their sources* — match a claim's strength and specificity to what the source shows (a
  directional association is not a proven "strongest predictor"; a result must not be extended
  to an endpoint/model/population the source never tested), and report a number the way its
  source gives it — a figure pooled across differing sources is an approximation, never dressed
  with a confidence interval or pooled estimate no source reported. (3) A short closing **Scope**
  note — automated synthesis of the listed web sources, gathered for this run rather than a
  systematic literature search, with roughly how many sources it drew on (provenance, not a
  methods section).
- **Research sub-agent** (`research.prompt`, agent-research-v8 → **agent-research-v9**, pin +
  hash updated in `test_agents.py`) — findings now carry a source's provenance forward (primary
  vs. secondary, and a study's design/size/setting) so the writer can grade it downstream
  instead of receiving an already-flattened percentage.
- **Critique brief** (`_critique`, the v10 citation-faithfulness reviewer) — sharpened to catch
  the blending pattern the plain "does the source support the claim" check let through: verify
  the cited page supports THAT SPECIFIC claim (same finding, endpoint, population — not an
  adjacent result), and flag a claim that over-extends its source or a number dressed with more
  precision than the source gave.

**v11 revision (reports go as deep as the question earns).** The owner asked for real
depth — a `deep` question was coming back as a one-to-two-page skim, not the eight-to-ten-page
write-up expected. The findings were never the bottleneck: a run already feeds the writer up
to ~60k chars of gathered material (`MAX_FEED_CHARS` × the fan) but the synthesizer compressed
it to ~1,400 words, because the synthesize prompt actively steered short ("Lead with the answer.
Be tight and concrete; a report is not padded length") and the `_SYNTH_MAX_TOKENS` cap sat at
6,000. Verified on-box against the routed `gpt-oss-120b` (128k window): given a depth-oriented
prompt and a 12k ceiling, the model produced a clean ~4,650-word / ~36.5k-char report that
stopped on its own (well under the cap). Fixed on three fronts, all on the existing substrate:

- **Synthesize prompt** (`deep_research_synthesize.prompt`, dr-synth-v4) — replaced the brevity
  steer with an explicit depth contract: develop every section into several substantial
  paragraphs (mechanism, background, evidence, nuance/disagreement, implications), and reach
  the length by fully developing what the findings support, never by padding or repetition.
  The anti-fabrication and citation machinery (`[^n]` ASCII markers, the numbered SOURCES
  registry, corroborate-by-authority) is unchanged — depth must come from the findings.
- **Per-report length target** (`_depth_directive`, threaded into the synth user message) —
  the shared writer prompt scales by the plan's complexity: `deep` → ~4,000–6,000 words (8–10
  pages), `comparative` → ~2,000–3,500, `simple` → a tight sub-page answer that must not be
  padded. An unknown complexity fails thorough (the deep target), matching the planner's default.
- **Room to write** — `_SYNTH_MAX_TOKENS` 6,000 → 12,000 (a generous ceiling the writer rarely
  reaches; the word target governs typical length) and `_PLAN_MAX_TOKENS` 1,500 → 2,500 for the
  fuller `deep` outline. The plan prompt (dr-plan-v5) now sketches a 6–10-section outline for a
  `deep` question (lead answer, background, mechanism, evidence, controversies, limits,
  implications) so the writer has the scaffold for a full report. Both apply to the draft AND
  the critique-revise pass, which stay under the turn wall-clock because the synthesis streams.

**v10 revision (reviewers verify against the cited sources).** The critique and cross-check
review children were fed only the draft/findings text, so to check any claim they could
only run a *fresh, unrelated* web search — they never saw the report's own `[^n]` sources
and so could never catch a **misattributed citation** (a claim whose cited page does not
actually support it). Fixed on three fronts:

- **Critique** (`_critique`) now receives the same numbered SOURCES registry the synthesizer
  cited against and checks citation faithfulness FIRST — resolve each `[^n]`, open the page
  it cites, flag any claim the cited source does not support — falling back to independent
  search only for an unreachable source or an uncited claim.
- **Cross-check analyst** (`_analyze`) is handed the pages the gather findings reached so it
  can open a shaky claim's source rather than only re-searching (its findings keep their
  sub-agents' own local `[^n]` numbering, so the list is offered as pages to open, not a
  numbering the markers map onto).
- Both gate the SOURCES list on a **web-capable** reviewer: the pure-`library` reviewer
  (`review_library`) holds `read_external_video`, not `web_fetch`, so it keeps its
  corpus-verification brief with no `web_fetch` instruction (`_can_open_sources`).

Also fixed a latent undercount found in review: `_findings_count` keyed on the bare
`research` persona, so a `library`/`library_first`/deepest run (gather on `research_library`
/ `research_deep`) reported **0 sub-agent findings** in the provenance line, the report-view
badge, and the persisted count. It now counts the research-producer FAMILY.

**v9 revision (gpt-oss harmony citations render).** A report synthesized on `gpt-oss-120b`
came back with **zero rendered citations despite ~500 collected `web_sources`**: gpt-oss
ignores the synthesize prompt's `[^n]` instruction and cites in its native OpenAI "harmony"
notation — `【410†L1-L8】` (fullwidth brackets, the number in Unicode superscripts, a `†`
dagger, a line span). The frontend `stripModelCitations` treated every `【N†…】` as the
model's private browse cursor and **deleted it** (correct for a browse turn, wrong for a
synthesizer handed a NUMBERED sources list, where `N` *is* the source index). Fix
(`markdown.tsx`): an opt-in `harmonyToFootnotes` rewrites `【N†…】` → `[^N]` (folding
superscript digits to ASCII) so the existing `[^n]→web_sources[n-1]` favicon path picks them
up; the `Markdown` `harmonyCitations` prop is set ONLY on the report render paths
(`DeepResearchReport` view + the live `.fb-drp-report`), so a normal browse turn's cursor
noise is still stripped. Verified against the real report: 74/74 harmony markers converted,
~89% land in-range and become favicon chips; out-of-range numbers (gpt-oss occasionally
emits one past the list) fail safe to a plain numbered chip, never a wrong link. The
render fix is the safety net; the synthesize prompt (`deep_research_synthesize.prompt`,
`dr-synth-v3`) is also reinforced to name the exact wrong form (fullwidth `【 】`, a `†`
dagger, an `L1-L8` span, superscript digits) and demand plain ASCII `[^n]`, so a compliant
run stores clean `[^n]` and more citations land in-range in the first place.

**v8 revision (vertical timeline, fan nested in the active stage).** The v7 checklist laid
the eight stages out as a wrapping inline row (`.fb-drp-steps` flex-wrap) with the sub-agent
fan and report stacked below it, and the whole `<SubagentFan>` rendered as a loose block
*under* the bubble — on a phone the stage row wrapped to two cramped lines and the "where am
I" read got lost between the wrap, the budget bar, and the expanding traces. `DeepResearchProgress`
is now a **vertical timeline**: the stages stack down a rail (done = steel spine + ✓, active
pulses, todo dim), and the **active stage opens a slot** that hosts its live detail line, the
`<SubagentFan>` it spawned, and (Write / Revise) the streaming report — so the roster and
budget read as part of the stage that's spending them, and nothing wraps sideways. The fan
component itself is **unchanged**; `FullBrainSurface` threads the live `fanBlocks` into
`DeepResearchProgress` for a deep_research turn (`nestFanInDr`) and suppresses the standalone
below-bubble block for that turn only — other spawn turns keep the fan as their own block. No
event or data changes; still driven by the existing `step` + `preview` `ToolProgressEvent`.

**v7 revision (no dark phases).** The four orchestrator-level stages (plan, reflect,
**synthesize**, **revise**) are jerv's own model calls — they spawn no sub-agent row, so
they showed only a spinner while the longest one (writing the ~6k-token report) ran. Two
fixes: (1) **streaming synthesis** — `_synthesize` now streams via `converse_stream`,
accumulating the draft and emitting it into the phase event's `preview` every
`_SYNTH_PREVIEW_STRIDE` chars, so the report is watched being written; usage is charged
from the closing `LlmTurn`. (2) **phase checklist** — the PWA renders the deep_research
live tool as an 8-stage checklist (`DeepResearchProgress`, driven by the existing `step`
ordinal) with the active stage pulsing and prior stages checked, plus the streaming report
pane below it. No new event types — both reuse `ToolProgressEvent` (`step` + `preview`).

**v5 revision (bigger budget + honest budget meter).** An observed run (eurorack-synth
research) still starved children: one `medium`-effort gather child burned 911k over 61
steps / 30 web calls, and with the 3.0M children pool the refill + analyst children hit
`tree_budget_exhausted`. Two fixes (the over-search itself is addressed by v4's
`effort="low"` research children):
- **Budget doubled.** `SPAWN_MULTIPLIER` 5.0 → **10.0** (jerv tree 4.0M → **8.0M**,
  children pool 3.0M → **6.0M**); the `DR_ANALYST_RESERVE`/`DR_CRITIQUE_RESERVE` scale
  with it (450k/150k → **900k/300k**), so gather gets ~4.8M and the review children a
  comfortable protected slice.
- **The budget meter now shows the children's pool, not the whole tree.** It read
  `spent / tree_budget` (e.g. 2.86M / 4.00M ≈ 72%) at the exact moment children were
  hitting `tree_budget_exhausted` — the denominator wrongly included the root's reserved
  synthesis slice that children can never touch. The subagent events now emit
  `TreeState.children_budget()` (tree budget − root reserve) as the meter's ceiling, so
  the bar fills to "budget exhausted" precisely when a child exhausts.

**v4 revision (report library + follow-up recall + view polish + low-effort research).**
Deep-research reports are now PERSISTED and recallable, closing the gap that a follow-up
turn only had jerv's summary (the report Markdown lived only in the tool-view, which the
model never sees again). Mirrors the external-video corpus:
- **Storage** — `app.research_reports` (migration 0140): `report_md`, a summary + embedding,
  a generated `tsv`, the view-rebuild flags/sources, `external`-domain RLS, and a
  `question_hash` dedup (a re-run of the same question upserts). No chunks table — a report
  has no timeline, so search is report-level (FTS + summary embedding, RRF-fused).
- **Writer** — `external/research_corpus.persist_report`, called best-effort at the end of a
  run; a `ResearchReportEmbedder` fills the summary embedding off an `embed_research_report`
  job. A DB failure never fails the rendered report.
- **jerv tools** — `list_/search_/read_/show_/remove_research_report`, the same corpus
  pattern as the video tools (`read_` returns the FULL text — the follow-up-recall path;
  `show_` rebuilds the `deep_research_report` view from stored data; `remove_` stages an
  owner-approved proposal → the `delete_research_report` executor op).
- **View polish** — the `deep_research_report` card COLLAPSES the report body by default
  (question + chips stay; body opens on tap) and gains copy-Markdown + download-`.md` header
  actions.
- **Low-effort research children** — gather + refill fans now run at `effort="low"` (a
  gather angle is focused search-and-summarize; the lower step cap curbs the over-searching
  that rate-limited the upstream engines); the review children (analyst, critique) stay medium.


**v2 revision (owner feedback — the tool "didn't orchestrate enough").** After the v1
merge, a real run of "look into 2 things" classified as `comparative` and the v1 skip
matrix stripped the coverage-check, gap-fill, and critique — so it spawned two agents and
answered, with no visible checking or iteration. **v2 reverses the skip matrix** (settled
decision 6 below is superseded): **complexity now only sizes the gather breadth; every
stage runs when the tool is invoked** (the invocation IS the signal to go deep). It also
adds two things the owner asked for:
- **A cross-agent analyst stage** — after gather, a `review` sub-agent is *fed the
  researchers' summaries* (the feeding-waves envelope) and cross-checks them (agreements /
  contradictions / single-source claims / gaps) before reflect + synthesize. A genuine
  research→analyst hand-off, not parallel-then-merge. The pipeline is now
  **plan → gather → analyze → reflect → (refill) → synthesize → critique → revise**.
- **Visible phase progress** — each stage emits a `ToolProgressEvent` phase line
  (Planning → Researching → Cross-checking → Checking coverage → Filling gaps → Writing →
  Reviewing → Revising), reusing analyze_video's multi-phase surface (no new event type,
  no frontend-event change); the analyst + critique sub-agents also surface as live rows.
- **End-to-end citation tracking + favicons** — an on-box run confirmed the child
  sub-agents' real URLs were being **lost** at the fan boundary (`AgentResult` didn't
  aggregate them, so the report cited descriptions with no followable links). Fixed:
  `AgentLoop.run` now accumulates `web_sources` into `AgentResult`; `_ChildResult` carries
  them up; deep_research builds a **global, deduped, numbered source registry** and hands
  it to the synthesizer to cite against (`[^n]` → `web_sources[n-1]`); and the report view
  carries `web_sources` so the frontend renders each `[^n]` as a tappable **favicon** — the
  same standard jerv's own web answers use.
The view gains an `analyzed` ("cross-checked") provenance chip and the `web_sources`
registry. Everything else (the tree-budget reuse, the structural one-gap-round bound, the
sandbox/clamps) is unchanged.

**v3 revision (on-box budget tuning — resolves Open decisions 2–3, partial).** A real
"how many did the 1918 flu kill?" run failed the way the budget section warned it could:
the gather round (four `medium`-effort children, one alone burning 743k tokens over 35 web
calls) drained the shared children's pool, so the cross-agent **analyst was killed
mid-search** with `tree_budget_exhausted` and wrote nothing — while the root reserve sat
idle, untouchable by a child. Root cause: the analyst and gather are separate flat fans on
one pool with **no reserve between them**, and `children_exhausted` only enforced
`stage_reserve` at admission, never while a child spent. Three fixes, all shipped here:
- **A real spend-time reserve.** `children_exhausted` is now exactly
  `children_remaining() == 0`, so it honours `stage_reserve` at spend time (not just at
  admission) — a greedy producer fan is halted **at** the reserve. `deep_research` carves
  `DR_REVIEW_RESERVE` (analyst + critique slices) off the pool before gather and steps it
  down (analyst's slice released once gather is done, critique's once the draft is
  written), restored in `finally`. The analyst can no longer be starved.
- **Pool headroom.** `SPAWN_MULTIPLIER` 3.5 → 5.0 → **10.0** (v5/PR #902; jerv tree →
  **8.0M**, children pool → **6.0M**) so the review reserve rides on top of a full gather
  round rather than stealing from it, and the meter shows the children's pool
  (`children_budget()`) so the bar fills to 100% exactly when children exhaust.
- **Planner guard** (`dr-plan-v4`). The failed run also spawned a bogus "Create a citation
  matrix for all sources gathered in the previous three sub-questions" angle — a meta task
  an isolated parallel child can't satisfy; it refused in one step. The prompt now forbids
  process/meta sub-questions and any cross-child dependence, and steers toward fewer angles.
  A follow-up (`v3`) also stopped a display leak: the local planner sometimes wrapped each
  sub-question in a JSON object (`{"id": 1, "brief": …}`) despite the string schema, which
  showed as raw JSON in the child row labels — `_coerce_brief` still unwraps that shape.
- **Short row titles** (`dr-plan-v4`, v6). Each sub-question is now a `{title, brief}`
  object: `title` is a 3–6 word angle label the child row shows, `brief` is the full
  self-contained research instruction the child works. Earlier the row label was the first
  ~96 chars of the brief, which wrapped and clipped mid-word (`…solid-stat`, `…sodium-io`).
  `_title` caps a long/fallback label at a whole-word boundary; the brief is unaffected.

Still deferred from Open decisions 2–3: the **tree wall-clock on flat fans** (the run took
~28 min; flat fans still ignore `deadline`) and the analyst's own over-search (19 web calls
to "resolve conflicts") — both tracked, not addressed here.

**Implementation status.** v1 (all three waves) is **merged to `main` (PR #887)**. The v2
orchestration above is on a follow-up branch: `agent/deep_research.py` rewritten (breadth-
only complexity, the analyst stage, always-on reflect/refill/critique, phase events), the
`deep_research_report` view + component gain `analyzed`, and the unit suites updated
(`tests/unit/test_deep_research.py`, `registry.test.tsx`) — all green. **Still open before
"settled":** the D3 **mock-gate sign-off** on the non-happy states + a reference mock
(DESIGN.md marks it pending), the **on-box budget/wall-clock tuning** (Open decisions 2–3;
v2 runs more stages, so this matters more), and the formal per-wave PROCESS.md adversarial
reviews.

A **dedicated `deep_research` tool** that turns a single research question into a
structured, cited report by orchestrating jerv's existing web-sandboxed sub-agent
fan across a **bounded plan → gather → reflect → refill → synthesize → critique**
state machine. It is **not** a new agent runtime and **not** the workflow engine in
disguise: it is the honest generalization of **feeding waves**
(`archive/SUBAGENT_FEEDING_WAVES_PLAN.md`) — same in-request, ephemeral,
one-owner-turn, structurally-capped shape — with a planner at the front, one bounded
gap-refill round in the middle, and an outline-driven report (plus a review-persona
revision pass) at the end. Web-scoped only: it rides `jerv`'s sandbox
(`web_search`/`web_fetch`, no knowledge base, no location, no memory) and the
`research`/`review`/`summarize` personas unchanged.

Synthesized against the shipped substrate — the spawn service (`agent/spawn.py`,
migration 0105), the tree caps + budget (`agent/tree.py`), the persona prompts
(`agent/prompts/{research,review,summarize}.prompt`), the `spawn_subagent` sidecar
(`agent/tools/spawn_subagent.tool`), and the tool-view registry
(`docs/reference/DESIGN.md` §"Agent tool views") — and reconciled with the
`CLAUDE.md` non-negotiables and `docs/reference/ASSISTANT.md`. The owner's reference
— `kyuz0/deep-research-agent`, a **local-model** deep-research agent (Donato
Capitella) — and an open-source landscape survey (LangChain `open_deep_research`,
`gpt-researcher`, `dzhng/deep-research`, Stanford STORM) inform the design; see
§"Prior art".

## Why this fits (the lean litmus)

Per `ASSISTANT.md`'s litmus — reuse the adapter, storage, RLS-scoped Postgres, job
queue; add at most one small tool; stay operable by one person. It fits because the
expensive, dangerous layer already exists:

| Need | Reuse vs. net-new |
|---|---|
| Parallel web gathering with per-source citations | **Reuse** `SpawnService.spawn_fan` + the `research` persona (`[^n]` + `WebSource`, SSRF-guarded `web_fetch`). |
| Dependent stages fed forward as escaped data | **Reuse** the `waves` mechanism + `compose_feed_block` envelope. |
| Shared token budget, per-child runtime caps, tree wall-clock | **Reuse** `TreeState` (`agent/tree.py`) — **retuned**, not rebuilt. |
| Web sandbox (no KB, no memory, no location) | **Reuse** `jerv` + child sandbox flags verbatim. |
| Structured report card | **Net-new** `deep_research_report` tool-view (registered, composed from existing `stat_block`/`citation_card` primitives). |
| The orchestration spine (plan / gap-eval / synthesis prompts) | **Net-new** — three `.prompt` files + one `.tool` sidecar + a service that sequences existing pieces. |

**Zero new runtime dependencies.** Net-new is one tool, one service, three prompts,
one view — no new datastore, broker, or framework runtime.

## The idea in one paragraph

`jerv` calls `deep_research` with a **question** and optional **breadth** knob. The
tool runs a fixed pipeline in one handler: **(1) Plan** — one LLM call decomposes the
question into an outline of `breadth` sub-questions; **(2) Gather** — a `research` fan
(reusing `spawn_fan`) works the sub-questions in parallel, each child returning a
cited summary; **(3) Reflect** — a gap-evaluator LLM call scores the outline's
coverage from the summaries and emits up to *k* gap sub-questions; **(4) Refill** —
**one** further `research` fan on the gaps (the second and final round — a hard cap,
mirroring `MAX_WAVES=2`); **(5) Synthesize** — an outline-driven report is written
from all summaries with attribute-at-extraction citations; **(6) Critique/Revise** —
a `review` child critiques the draft and the synthesizer does **one** revision pass.
The tool returns the report as a `deep_research_report` view; jerv presents it. The
whole run is one owner turn, ephemeral, bounded in agents, budget, and wall-clock.

## Settled decisions (owner)

1. **Dedicated tool, not prompt-only orchestration.** A `deep_research` `.tool` +
   service, wrapping `SpawnService` — not a jerv-prompt nudge to loop by hand.
2. **Web-scoped via jerv.** Rides the existing web sandbox; **no knowledge-base
   access** for the tool or any child. (A KB-scoped deep-research capability is a
   separate, curator-side design with its own RLS surface — explicitly out of scope.)
3. **Two gather rounds, fixed.** Plan → gather → reflect → **one** refill → synthesize.
   The refill round is a hard cap (`MAX_RESEARCH_ROUNDS = 2`), **not** an adaptive
   LLM-judged "loop until covered" — that would violate "no unbounded autonomous loop."
4. **Structured report tool-view.** The deliverable is a registered
   `deep_research_report` view (outline-first, sectioned, citation cards), not a bare
   chat answer. Adds a `DESIGN.md` registry entry + a frontend wave.
5. **Critique/revise pass in v1.** After synthesis a `review` child critiques the
   draft; the synthesizer runs **one** bounded revision pass (the `gpt-researcher`
   multi-agent pattern). Not an open-ended review loop — exactly one revision.
6. **Complexity-scaled entry (from `kyuz0/deep-research-agent`).** The plan step (1)
   assesses the question's complexity and **may short-circuit** the pipeline: a shallow
   question runs a single small gather fan and a plain synthesis, skipping the reflect
   round and/or the critique pass. `deep_research` is already opt-in (jerv chooses to
   call it, and jerv.prompt still steers a bare lookup to `web_search`), so this gate is
   a *within-tool* budget saver, not a second refusal — the full two-round + critique
   machine is the ceiling, not the floor. The complexity classes and exactly which
   phases each skips are a build-plan task (see Open decisions).

## Architecture — the bounded state machine

`deep_research` is a service (`agent/deep_research.py`) the tool handler drives. It is
**in-request** (awaited by jerv's turn like `spawn_fan`), **ephemeral** (writes no
durable state beyond run-log rows), and **depth-0 only** (jerv is the sole caller; the
tool is never in a child's allowlist). Every model call and child run charges the same
`TreeState` budget as a normal fan.

```
question, breadth ──▶ (0) CLASSIFY ── one cheap LLM call: rate complexity ┐
                          simple | comparative | deep  → sets the skip     │
                          matrix below (narrow-only; never widens)         │
                                                                         ▼
                      (1) PLAN ────────────────────────────────────────── ┐
                          one LLM call: outline of `breadth` sub-questions │
                          + the report's section skeleton                  │
                                                                         ▼
                      (2) GATHER  ── spawn_fan(research × sub-questions) ─┐
                          each child → cited summary (data boundary),      │
                          tiered source-quality corroboration              │
                                                                         ▼
                      (3) REFLECT ── one LLM call: score coverage of the ─┐  ⟵ skipped if
                          outline from summaries → up to k gap questions  │    simple
                          (empty ⇒ skip refill, go straight to synth)     │
                                                                         ▼
                      (4) REFILL  ── spawn_fan(research × gaps)  [ROUND 2, │  ⟵ skipped if
                          FINAL — no third round, ever]                    │    simple
                                                                         ▼
                      (5) SYNTHESIZE ── one LLM call: outline-driven ─────┐
                          report from ALL summaries, attribute-at-        │
                          extraction citations ([^n] → WebSource refs)    │
                                                                         ▼
                      (6) CRITIQUE ── spawn one review child on the draft ┐  ⟵ skipped if
                          ──▶ REVISE: one LLM call folds the critique     │    simple/comparative
                          (exactly one pass)                              │
                                                                         ▼
                                          deep_research_report view ──────┘
```

**The complexity skip matrix (step 0, borrowed from `kyuz0/deep-research-agent`).** The
classifier may only ever *narrow* the pipeline — the two-round + critique machine is the
hard ceiling, and a model that mis-rates high can never exceed it. Candidate default
(final tiers a build-plan task, Open decision 5):

| Tier | Gather | Reflect + refill | Critique/revise |
|---|---|---|---|
| **simple** (single/multi-fact) | 1–2 children | skip | skip |
| **comparative** (N angles) | `breadth` children | skip | optional |
| **deep** (synthesis) | `breadth` children | **run** | **run** |

**One call, not two (local-box efficiency).** Step 0 folds into step 1's LLM call — the
`plan` prompt returns `{complexity, outline}` in one shot — so classification costs no
extra round-trip on a slow on-box model. They are drawn separately above only to show
the control flow; a `simple` rating still yields a minimal 1–2-question outline from the
same call.

**Round accounting.** Rounds 2 (gather) + 4 (refill) are the only child fans. Together
they obey `MAX_CHILDREN_PER_PARENT` (6) across the whole run — e.g. `breadth=4` gather
+ up to 2 gap children. Round 6 spawns exactly one `review` child. So a full run mints
at most `6 + 1 = 7` children — well under `MAX_TOTAL_AGENTS_PER_TREE` (12). Steps 0, 1,
3, 5, and the revise half of 6 are direct jerv-model calls charged to the **root
reserve**, not children.

**Tiered source-quality corroboration (step 2, borrowed).** The `research` children
already corroborate across sources; the borrowed refinement is to make corroboration
*proportional to source authority* rather than flat — an authoritative source (official
docs, a spec sheet, a primary record) can stand on its own; a semi-authoritative one
(an established publication) wants a second; an informal one (a forum, a blog) must be
corroborated by at least one independent source or flagged uncertain. On a slow local
box this is a direct budget win — it stops a child burning fetches double-confirming a
primary source while still forcing corroboration where it matters. It lands as a clause
in `research.prompt` (version-bumped, CI-guarded) and a mirrored rule in the synthesis
prompt (an uncorroborated informal claim renders behind the view's **thin-sources**
flag), not as new machinery.

**Reuse, not reimplementation.** Steps 2 and 4 call the *existing* `spawn_fan` flat-fan
path; the fed-forward critique in step 6 is exactly a `waves` producer→consumer hop
(`review` child fed the draft as escaped `<untrusted_external_data>`). The state machine
adds sequencing + three prompts around machinery that already ships.

## Budget & bounds (retune `tree.py`, don't rebuild it)

A two-round run with a critique pass spends more than a single fan, so the caps need
retuning — but the **shape** (shared counter + root reserve + admission floor + tree
wall-clock) is unchanged. Proposed changes (final numbers a build-plan task, validated
on-box like the S2/F2 retunes were):

- **Tree budget headroom.** ✅ `SPAWN_MULTIPLIER` raised to **50/3 (~16.7)** for every root
  (v3 took it 3.5 → 5.0; v5 → 10.0; then → 40/3; then → 50/3), so jerv's children pool is
  **10.0M** (tree ~13.3M − the 25% root reserve; the 50/3 lands the pool exactly on 10.0M) —
  the simpler lever than a dedicated deep-research multiplier, and the
  reserve still covers the two large root calls (synthesis in 5, revision in 6). On top of
  the pool, `deep_research` carves a `DR_REVIEW_RESERVE` (`stage_reserve`, 1.5M) so the
  post-gather analyst + critique children can't be starved by a greedy gather round.
- **Review wall-clock reserve.** ✅ The token reserve has a wall-clock twin,
  `DR_REVIEW_TIME_RESERVE` (`tree.time_reserve`, 900s), stepped down at the same three
  points. A producer child's clock is bounded by `stage_seconds_left` (deadline − the
  reserve), so gather can't run the deadline down and leave the analyst a 75s scrap or the
  gap children a 0s one — each review stage keeps its guaranteed time as well as its tokens.
- **Two-fan admission.** The admission floor is checked before *each* fan (gather, refill)
  on **both** axes — `can_admit_budget` (tokens) and `can_admit_time` (the stage-clock at
  `MIN_VIABLE_CHILD_SECONDS`/child). A fan the pool or clock can't seat is skipped-loud, and
  gather breadth is clamped to what's seatable around the review reserve (`_seatable`, logged
  as `deep_research.breadth_clamped`) — honest degradation, not children that die at ~0s.
- **Tree wall-clock.** A two-round + critique run is longer than a 2-wave feed; confirm
  it fits under `TREE_WALL_CLOCK_S = 4500` with synthesis headroom, or lift it (still
  under the `_MAX_TURN_WALL_CLOCK_S = 5400` turn cap). Deferred to a background job is
  an **explicitly considered** fallback if it doesn't fit (see Open decisions).
- **Per-child caps** (`CHILD_MAX_STEPS`/`CHILD_WALL_CLOCK_S`/`CHILD_MAX_COST_TOKENS`)
  are unchanged — a research child in a deep-research fan is the same research child.

## Security & non-negotiables (red-team surface — every wave gated)

The tool inherits the sub-agent security model wholesale; nothing here relaxes it.

- **#1 data/instruction boundary.** The question is owner-authored (trusted at depth
  0). Every child summary, the fed critique, and all fetched content re-enter as
  **data**, never instruction — the outline, gap questions, and report are the *only*
  model-authored artifacts, and none of them is executable. The critique fed in step 6
  uses the existing escaped-envelope + pinned prompt clause (`compose_feed_block`).
- **#8 least privilege.** The tool is `jerv`-only and in a registry **never-default**
  set so `curator.tools=None` cannot absorb it (same guard as `spawn_subagent`).
  Children stay web-sandboxed, tools ⊆ parent, refused at `_dispatch`.
- **#9 controlled egress.** Web only, via SearXNG + SSRF-guarded `web_fetch`, per
  child. The report view is **data, not model markup** — no render-time external load
  (favicons resolved on-box, as jerv's web citations already are).
- **#10 no untrusted trigger.** A `deep_research` run happens only inside an
  owner-initiated jerv turn. No auto-fire, nothing scheduled, nothing persisted between
  turns. The reflect step's gap questions are model-authored from summaries but launch
  only the **one** bounded refill fan — not an open loop.
- **#7/#11 memory & purge.** Children are `no_memory`; the tool writes no
  `agent_episodes`, mints no notes, touches no `note_id`. The deletion cascade is
  vacuous. Durable knowledge from a report re-enters only through the notes door.

## GUI — the `deep_research_report` tool-view

A **registered** component (added to the `DESIGN.md` §"Agent tool views" registry in
the same PR — the same-PR rule), composed from existing view primitives, never bespoke
markup:

- **Outline-first layout:** the report's sections as the top-level structure, each with
  its synthesized prose and inline `[^n]` citation markers rendered as the tappable
  on-box favicons jerv already uses for web citations.
- **Provenance strip:** how many sub-questions, how many sources, whether a refill round
  ran, whether the draft was revised — derived run metadata, not new truth.
- **Non-happy states (mock-gated, like the subagent surfaces):** a **coverage-limited**
  variant (refill skipped for budget/deadline), a **truncated** variant
  (`tree_budget_exhausted` mid-run), and a **thin-sources** flag when a section rests on
  a single uncorroborated source. Live progress reuses the `subagent_*` accordion so the
  two serial rounds + critique don't read as frozen ("Planning → Researching 4 →
  Filling 2 gaps → Writing → Revising").

## Prior art (what informed the design)

- **`kyuz0/deep-research-agent`** — the **owner's reference** (Donato Capitella /
  `kyuz0`, from his "Deep Research Agent locally on Strix Halo" video), and the most
  directly-applicable one because it is **built for local models on small context
  windows** — exactly JBrain2's on-box constraint. Its load-bearing ideas, and how they
  land here:
  - **Context separation** — its Orchestrator holds *no web tools and no file-reading
    tools*; Searcher/Analyzer children pre-process so the planner's context stays lean.
    **JBrain2 already embodies this**: children return only compressed summaries and jerv
    never sees a raw page. This reference *validates* the choice; nothing to add.
  - **Complexity-scaled delegation** — it assesses query complexity first and scales
    (simple → one searcher; comparative → one per angle; only "deep research" runs the
    full machine). **Adopted** as step 0's classifier + skip matrix (Settled decision 6):
    the plan step short-circuits the pipeline for a shallow question rather than always
    paying for two rounds + a critique. Guarded narrow-only — it can never widen past the
    structural ceiling.
  - **Tiered source-quality corroboration** — its Searcher corroborates *proportional to
    source authority* (authoritative → one source suffices; informal → needs a second).
    **Adopted** as a clause in `research.prompt` + a mirrored synthesis rule (step 2
    above); a direct fetch-budget win on a slow local box, and it feeds the view's
    thin-sources flag. JBrain2's `research.prompt` corroborates flatly today; this makes
    it authority-aware.
  - **`think_tool` structured-reasoning pause** — a dedicated step that forces the agent
    to reason before acting. JBrain2's **reflect** step (3) is the orchestration-level
    analogue (an explicit coverage-scoring call between gather and refill); no per-child
    think tool is added — the children's native reasoning trace already covers it.
  - **3-tier Orchestrator→Searcher→Analyzer, downward-only** — maps onto jerv (root) +
    the `research` (searcher) and `review` (analyzer) personas; JBrain2's `MAX_DEPTH=1`
    downward-only clamp is the same no-upward-loops shape.
  - **Disk workspace as shared scratchpad** (fetch→markdown→workspace, grep/read/write,
    `final_report.md`). JBrain2 **deliberately diverges**: non-negotiable #2 forbids raw
    paths, and the `waves`/`feed` envelope already carries round-1 findings into round-2
    children as escaped data — so we keep the ephemeral-summary model, not a filesystem.
  - **Global per-tool quotas + anti-loop prompt directives** as the budget model.
    JBrain2's structural caps (per-child steps/wall-clock, tree budget, fixed round
    count) are *stronger* (harness-enforced, zero model cooperation), so we keep ours;
    the reference confirms the "must bound tool-call sprawl on a local box" instinct.
- **LangChain `open_deep_research`** — the **Scope → Research → Write** three-phase
  spine and the separate-compression-before-writer discipline. Our steps 1/2-4/5 map to
  it; per-child summaries are already the compression.
- **`dzhng/deep-research`** — fixed **breadth × depth** knobs (predictable cost, no
  LLM-judged stop). We adopt the *knobs*, cap depth at a fixed 2 rounds.
- **`gpt-researcher` multi_agents** — the reviewer/reviser critique loop (our step 6).
- **Stanford STORM** — outline-first synthesis and attribute-at-extraction citations,
  which align with JBrain2's notes-as-sole-truth ethos.

(Origin of the reference: the owner recalled "the Strix / toolboxes person has a deep
research project." **Confirmed** — "Strix" is **AMD Strix Halo** hardware, not the
`0xallam` pentesting agent; the person is **Donato Capitella / `kyuz0`**, maintainer of
the `amd-strix-halo-*-toolboxes` and author of `kyuz0/deep-research-agent`.)

## Testing (per `CLAUDE.md` #5 — 80% backend, security 100%, real Postgres, LLM faked)

- **State machine (adapter fake, deterministic):** classify → plan → gather → reflect →
  refill → synthesize → critique → revise sequences in order; an **empty gap list skips
  refill**; the **second round is the last** (a scripted third-round attempt is
  impossible by construction, asserted); a critique with no findings still runs exactly
  one (no-op) revise or skips it deterministically.
- **Complexity gate (narrow-only):** a `simple` rating skips reflect+refill+critique; a
  `comparative` rating skips reflect+refill; a classifier output that tries to *widen*
  past the ceiling (e.g. "run three rounds") is clamped to the structural max — proven
  with a scripted mis-rating that cannot exceed two rounds or `MAX_CHILDREN_PER_PARENT`.
- **Tiered corroboration:** the source-quality clause is present + version-pinned in
  `research.prompt`; a synthesized informal claim with no second source renders behind
  the view's thin-sources flag (fixture-driven).
- **Reuse boundaries:** gather/refill go through `spawn_fan` unchanged; the critique
  hop composes an escaped feed block; the flat-fan `tasks` path is byte-unchanged
  (characterization test).
- **Budget/bounds:** two-fan admission (refill skipped-loud when the pool can't seat
  gaps → coverage-limited report); root reserve survives **two** big root calls
  (synthesis + revise); tree wall-clock deadline skips the refill loud; per-child caps
  unchanged.
- **Security (red-team):** `curator` is never offered `deep_research`; a child never
  holds it (depth-0 only); an injection payload inside a child summary or the fed
  critique does not steer synthesis/revision; report view carries no model-authored
  URL/markup.
- **Frontend:** reducer + view fixtures — default / coverage-limited / truncated /
  thin-sources / long-outline; live-progress accordion for the two rounds + critique.

## Wave split (per `docs/reference/PROCESS.md`)

Each wave: parallel-task worktrees off a `wave-Dn` branch, per-task **and** wave-level
adversarial review (security/red-team for any boundary/budget/sandbox surface), one PR,
CI green before merge. GUI wave through the mock gate.

- **Wave D1 — Plan + synthesize spine (backend). ✅ LANDED (this branch).** The
  `deep_research.py` service, the `deep_research` `.tool` sidecar + never-default
  registry exclusion (`toolregistry.NEVER_DEFAULT`), the `deep_research_plan` and
  `deep_research_synthesize` `.prompt` files (the synthesize prompt carries the mirrored
  source-quality rule), the **tiered source-quality clause** added to `research.prompt`
  (v7→v8, CI-guarded + hash-pinned), the `SpawnService.run_research_fan` structured fan
  runner (extracted `_execute_fan` core, shared with the flat fan), and the plan → gather
  → synthesize path. Report returned as **text** (no view yet). Full state-machine +
  reuse-boundary + security unit tests (`test_deep_research.py`). **Deviation from the
  plan:** the `tree.py` budget was **not** retuned — the run reuses the existing tree
  pool + 25% root reserve (Open decision 3's "reuse", not the recommended dedicated
  multiplier); revisit if on-box synthesis+revise starves.
- **Wave D2 — Complexity gate + reflect + refill round + critique/revise (backend;
  red-team gated). ✅ LANDED (this branch).** The step-0 **complexity classifier +
  narrow-only skip matrix** (folded into the plan call), the `deep_research_reflect`
  `.prompt` + gap-eval call, the **one** bounded refill fan (`MAX_RESEARCH_ROUNDS = 2`
  in `tree.py`), per-round admission via `run_research_fan` (a refused refill →
  coverage-limited, not a crash), and the `review`-fed critique (escaped
  `compose_feed_block`) + one revision pass. Every cap and the classifier's narrow-only
  clamp has a zero-model-cooperation test. **Deviation:** no tree-wide wall-clock
  deadline is set (the flat fans don't consult `TREE_WALL_CLOCK_S`); a two-round + critique
  run is bounded by per-child caps × serial rounds, so the in-turn-vs-deferred question
  (Open decision 2) stays open pending on-box timing.
- **Wave D3 — `deep_research_report` tool-view (GUI; mock gate). ✅ LANDED (this
  branch; mock-gate sign-off pending).** The backend emits the `deep_research_report`
  view (`deep_research._report_view`); the frontend renders it via a registered
  component (`registry.tsx`, `.tv-dr-*` styles) — a provenance strip (complexity, source
  count, rounds, revised/coverage-limited/truncated enum-tone flags), the report body
  through the shared `<Markdown>` path, and a collapsible sub-agent roster that deep-links
  each child's session (reusing `.tv-syn-*` rows). `DESIGN.md` registry entry added;
  `jerv.prompt` v24→v25 steers when to reach for `deep_research`. `registry.test.tsx`
  covers the render, the flags, and the deep-link. **Pending:** the mock-gate sign-off on
  the non-happy states + a reference mock (the entry is marked pending in DESIGN.md).
  The registered view
  (outline layout, citation cards, provenance strip), the non-happy states, and the
  live-progress accordion reuse. `DESIGN.md` registry entry in the same PR. jerv.prompt
  steering (when to reach for `deep_research` vs. a plain fan vs. searching itself).

D2 depends on D1 (the spine). D3 depends on D1 (the returned report shape) and can
overlap D2 (different surface).

## Open decisions for the build plan

1. **Breadth knob range + default.** dzhng recommends 3–10 sub-questions; our
   `MAX_CHILDREN_PER_PARENT = 6` caps a single fan, so breadth is effectively 2–6 with a
   default around 4. Confirm, and decide whether gather-breadth and refill-`k` share the
   6-child budget or the refill gets a small reserved slice.
2. **In-turn vs. deferred.** If a two-round + critique run can't reliably finish under
   the turn wall-clock on the local box, does it **defer** to a background job (the
   `analyze_stream` full-mode / deferred-tool-call precedent) with a `task_status` card
   that auto-resumes the report into the chat? Recommend: build in-turn, measure, and
   fall back to deferred only if the on-box numbers force it (decide after D1's on-box run).
3. **Budget: shared multiplier vs. dedicated.** Reuse `SPAWN_MULTIPLIER = 3.5` for
   deep-research roots, or add a `DEEP_RESEARCH_MULTIPLIER` so an ordinary fan isn't
   inflated? Recommend a dedicated multiplier — a deep-research run is a distinct, opt-in
   cost the owner chose.
4. **Revise trigger.** Always run the one revision pass, or only when the critique
   surfaces findings above a severity bar (skip a clean bill)? Recommend: skip the revise
   call when the critique returns no actionable findings (saves a large root call).
5. **Complexity classes + skip matrix (Settled decision 6).** How many complexity tiers
   does the plan step classify into, and which phases does each skip? A candidate,
   adapted from `kyuz0/deep-research-agent`'s tiers: *simple* → 1–2 gather children, no
   reflect, no critique; *comparative* → full-breadth gather, no reflect, critique
   optional; *deep* → the full two-round + critique machine. Confirm the tiers and the
   classifier (a cheap one-shot on the question, charged to the root reserve). Guard:
   the classifier is model judgment, so it may only ever *narrow* the pipeline — it can
   never widen past the structural ceiling (two rounds, `MAX_CHILDREN_PER_PARENT`).

## Deferred past v1

- **KB-scoped deep research** (over the owner's notes/wiki/entities) — a curator-side
  capability with a full RLS sub-agent surface; a separate proposal, not this one.
- **A third+ round / adaptive depth** — the "loop until covered" the lean litmus
  refuses; revisit only if the fixed-2-round bound proves insufficient in practice.
- **Saving a report as a note** — a report the owner wants to keep re-enters through the
  normal agent-authored-note door (#7), not a privileged write; a follow-on if wanted.
