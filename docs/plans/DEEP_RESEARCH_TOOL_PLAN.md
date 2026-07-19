# Deep Research Tool — Build Plan

> **Status:** In progress · **Last verified:** 2026-07-19 · **Waves:** D1✅ D2✅ D3◻️ (v1 shipped; v2 orchestration merged; v3 on-box budget tuning on a follow-up branch; mock-gate sign-off pending)

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
- **Pool headroom.** `SPAWN_MULTIPLIER` 3.5 → **5.0** (jerv tree 2.8M → **4.0M**, children
  pool 2.1M → **3.0M**) so the review reserve rides on top of a full gather round rather
  than stealing from it.
- **Planner guard** (`dr-plan-v2`). The failed run also spawned a bogus "Create a citation
  matrix for all sources gathered in the previous three sub-questions" angle — a meta task
  an isolated parallel child can't satisfy; it refused in one step. The prompt now forbids
  process/meta sub-questions and any cross-child dependence, and steers toward fewer angles.

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

- **Tree budget headroom.** ✅ (v3) `SPAWN_MULTIPLIER` raised 3.5 → **5.0** for every
  root (jerv children pool 2.1M → **3.0M**) rather than a dedicated deep-research
  multiplier — the simpler lever, and the 25% root reserve still covers the two large
  root calls (synthesis in 5, revision in 6). On top of the pool, `deep_research` carves a
  `DR_REVIEW_RESERVE` (`stage_reserve`) so the post-gather analyst + critique children
  can't be starved by a greedy gather round (see v3 revision).
- **Two-fan admission.** The admission floor (`can_admit_budget`) is checked before
  *each* fan (gather, refill) — the refill fan is skipped-loud if the pool can't seat
  its gap children, and the run synthesizes from round-1 material tagged "coverage
  limited."
- **Tree wall-clock.** A two-round + critique run is longer than a 2-wave feed; confirm
  it fits under `TREE_WALL_CLOCK_S = 3000` with synthesis headroom, or lift it (still
  under the `_MAX_TURN_WALL_CLOCK_S = 3600` turn cap). Deferred to a background job is
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
