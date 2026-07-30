# Deep Produce — one produce engine, two verbs

> **Status:** Scheduled · **Last verified:** 2026-07-30 · **Waves:** W1◻️ W2◻️ W3◻️

Generalize the `deep_research` pipeline into a single **produce engine** behind an
abstraction layer, surfaced as two verbs:

- **`deep_research`** — the existing *research → cited report* behavior, now a preset of
  the engine (`output_kind = report`). Behavior-preserving for jerv.
- **`deep_produce`** — the general verb: the caller supplies a **Directive** (an objective
  + an output kind other than a report — a plan, a comparison table, a brief, a
  differential, a timeline), and the engine produces that artifact from whatever sources
  the calling session is permitted to use.

The same engine serves both **jerv** (research or produce over web/library, output to the
external corpus) and **curator** (produce with *enhanced sources* — the owner's own
Research Library plus EMR/health facts seeded under RLS, never the web). The motivating
case — *"given medical history from date X to date Y and the library topics under category
W, produce an idealized treatment plan if the symptoms were to recur"* — is
`deep_produce(output_kind = plan)` from a curator call with a health seed.

> **This plan was adversarially reviewed against the codebase (2026-07-29).** The review
> confirmed the design is sound but surfaced blocking gaps that this revision folds in:
> `research()` has a *second* caller (`deepest`) the refactor must not break; non-report
> output has no render path; the anti-hallucination critic silently no-ops on library-only
> runs; and several framing claims ("ephemeral", "by construction", "access never
> caller-supplied") overstated the shipped code. Each is addressed below and tagged
> **[R]**.

## Why one engine, not a fork

An earlier draft proposed a separate local-only fork of `deep_research`. That was the wrong
altitude: *what to produce* (a report vs. a plan vs. a table) and *what to produce it from*
(web vs. library vs. an owner-KB seed) are **orthogonal**. Two decoupled inputs on one
engine mean jerv gains "produce" as a strict generalization of a shipped capability,
curator reuses the same producer with a seed added, and the medical-safety property is
enforced in one place instead of implicitly in "which tool you called."

## Single implementation — the no-regression spine  [R]

The engine is one service, `DeepResearchService.research()`
(`backend/src/jbrain/agent/deep_research.py:457`). It is driven by **two** callers today,
and the refactor must preserve both:

1. `DeepResearchRef.__call__` (`:1301`) — the interactive `deep_research` tool.
2. `deepest_run.run_deepest` (`deepest_run.py:165-170`) — the background `deepest` lane,
   which calls the *same method* with `on_round=` and `require_persist=True`, and is
   re-entered by `resume_deepest` (`deepest_run.py:237`). `require_persist` makes a lost
   persist **fatal** (`deep_research.py:807-808`).

**Rule:** the refactor keeps a single private implementation
`_run(ctx, *, directive, source_plan, on_round=None, require_persist=False)` that retains
**every** existing kwarg. `deep_research`, `deep_produce`, and `deepest` become thin
adapters over it. The `research()` name/signature is preserved (or delegated to `_run`)
so no caller changes shape. Python offers no compile-time signal for a dropped kwarg, so
the deepest driver is a **named line item** in the W1 regression checklist — not just
`DeepResearchRef`.

## The abstraction

`_run` takes two decoupled inputs plus the existing breadth/mode/reserve knobs.

### 1. `Directive` — *what to produce*

`objective` (caller text) + `output_kind` (`report | plan | table | brief | differential |
timeline | …`).

- **Byte-stable report rule [R].** For `output_kind = report` with the objective defaulted
  from `question`, the engine emits **no** `OBJECTIVE:` block *anywhere* — not in
  synthesize, reflect, analyze, or critique. No `OBJECTIVE` string exists in those builders
  today (`deep_research.py:988-996`, `943-959`, `913-923`, `1103-1111`), so any
  unconditional injection would mutate what the model conditions on for the report case.
  All directive injection is **strictly additive for non-report kinds only**.
- **`output_kind`-aware depth directive [R].** `_synthesize` injects
  `_depth_directive(complexity)` (`:993`), whose `deep` variant literally says "4,000–6,000
  words… develop every outline section into several substantial paragraphs" (`:1176-1182`).
  A `plan`/`table`/`timeline` must not be told to write a report. Replace with
  `_shape_directive(output_kind, complexity)` where `(report, complexity)` yields the exact
  current string, byte-for-byte.
- **System prompts, not just user text [R].** Threading the objective into *user* text does
  not retarget `_plan` and `_reflect`, whose **system** prompts are fixed renders hardcoded
  to web-research-report work (`_PLAN.render()` `:858`; `deep_research_plan.prompt:7-11`).
  Non-report kinds need `output_kind`-aware plan/reflect/critique **system** prompts, not
  only a user-message objective. Scope this into W1 (report path unchanged) / W2.

`deep_research` is `Directive(output_kind = report, objective ← question)` — the existing
behavior, reproduced exactly by the rules above.

### 2. `SourcePlan` — *keyed on seed-presence, not persona (D5 resolved)*  [R]

The review corrected the original framing. Two shipped facts:

- The tool **does** read `sources`/`mode` from caller args today
  (`deep_research.py:483,491`), and jerv must keep choosing `web|library|library_first`.
- `ToolContext` carries **no** persona/agent field; the persona is flattened upstream
  (`api/agent.py:623`) into the session's `tools_allow` + `read_scopes` + system prompt.

So the engine **cannot** ask "am I curator?" — and it does not need to. Rather than infer
persona identity, `_run` keys the SourcePlan on a fact it computes directly: *did this
invocation assemble a health/KB seed?* That boolean is exactly the antecedent of the safety
invariant, so branching on it makes the invariant self-enforcing at the one point where the
seed is known. It resolves into a clean three-way (which also subsumes grounding-refusal):

| Case | Condition | Sources | Web fans | Sink |
|---|---|---|---|---|
| **1 — seeded produce** | seed requested **and** assembled (health scope present + EMR read non-empty) | pinned local; any `web`/`library_first` arg **rejected** | **forbidden** | non-external (see Safety) |
| **2 — refuse** | seed requested but **not** assembled (no health scope / empty read) | — | — | — (returns a refusal) |
| **3 — plain produce** | no seed requested | caller picks `web/library/library_first` (unchanged) | allowed | `external` corpus |

Case 3 is the shipped jerv/`deep_research` path, byte-for-byte. jerv can never reach case 1
— it holds no health scope or EMR tools to build a seed with — so the two verbs run the same
code and the *only* brancher is seed-presence, which is directly observable and testable.

**Edge (documented, not a blocker):** if the owner pastes health text into the free-text
`objective` and requests web, that text reaches the web — but that is the owner choosing to
web-search their own words, exactly as if they typed it into jerv. The invariant guards the
engine-assembled seed against *system* leakage, not the owner's deliberate act.

### 3. The invariant the abstraction owns  [R]

The exfiltration property is enforced, **not** "by construction":

> **`seed present (KB/health) ⇒ no web fan may spawn ∧ sink ≠ external`.**

- **`_persist` is the sink-enforcement point.** `research()` calls `_persist`
  *unconditionally* on finish (`deep_research.py:721`) → `persist_report` under
  `SYSTEM_CTX` into `app.research_reports` (`domain_code='external'`, `0140:64`), which jerv
  reads (`research_corpus.py:89-94`) and public share links expose (`0150`). The engine must
  **actively** make `_persist` a no-op when `SourcePlan.sink != external`, asserting before
  the write. A test proves a curator seeded run writes **zero** `research_reports` rows and
  that jerv/report persists byte-identically.
- **Web-fan suppression.** A seeded/curator run pins to library personas; `_personas_for`
  never returns a web persona, and `library_first` (which *can* reach web on refill) is
  forbidden for seeded runs. Enforced as a precondition on `_run`, asserted at runtime as
  defense-in-depth.
- **Seed vs. draft [R].** The raw seed goes only into the parent `synthesize` call
  (`:995`); the critic receives only the *draft* (`:1069`), and on a library run the critic
  persona is `review_library` (no web tool). So "no web-capable agent receives seed text"
  holds — but see the critic gap in B3 below, which is about *discipline*, not egress.

## Output surfacing — reuse the shipped view, no new GUI surface  [R] (revised)

**Decision:** the produced artifact *is a Markdown document* — the same product
`deep_research` already delivers. `output_kind` shapes what the `.md` *contains* (a plan, a
table, a brief), never how it renders. So `deep_produce` reuses the **existing**
`deep_research_report` view and its in-progress streaming phase component verbatim; there
is **no new/changed GUI surface, and therefore no `PROCESS.md` mock gate**. This dissolves
the review's B2 finding by reuse rather than by building a render path.

- `_report_view` keeps emitting `view="deep_research_report"` (`deep_research.py:1259-1290`)
  and the streaming Write/Revise phases (steps 6/8) are unchanged — a plain-path
  `deep_produce` run renders and streams identically to a `deep_research` run, and persists
  to the library the same way (a `.md` report row, distinguished only by the `tool` column).
- `_frame`/`_report_view` are **not** re-shaped per `output_kind`; the artifact is presented
  as the report-family document it is. (A seeded/library run simply shows no web-sources
  strip — already the case for the shipped `library` source mode, so no refinement needed.)
- **Report regression pin:** golden `ViewPayload.data` dict + persisted row for a report
  run, plus a smoke assertion it still emits stream steps 6/8 (`_WRITE_STEP`/`_REVISE_STEP`,
  `:266-267`) and `view="deep_research_report"` — now also covering a non-report
  `output_kind`, which must emit the *same* view.

## How a curator call gets the "enhanced sources"

The spawned-sub-agent sandbox mints `domain_scopes=[]` / `scopes=()`
(`spawn.py:531,988,1059`, the in-code "ONE trusted place") — untouched. `deep_produce` from
a **curator** turn (holding `read_labs` / `read_encounters` under a health-scoped session)
reads the EMR `since`/`until` range *in the parent*, wraps the facts in the inert-data
envelope (`compose_feed_block` / `_findings_block`), and threads them into `plan` +
`synthesize`. The sub-agents never touch health data.

**Fail-closed grounding [R].** Nothing forces the curator session to actually hold `health`
scope; `read_context` fails *open to empty*, not to refusal (`session.py:99-104`). So a
`deep_produce(output_kind=plan)` from a non-health session would produce an **ungrounded**
plan. W2 must assert `health` scope **and** a non-empty EMR read before synthesizing, and
**refuse** otherwise — an ungrounded "treatment plan" is the worst failure mode.

**Corpus note.** The library sub-agents today read the *analysed-video* corpus
(`search_external_video`/`read_external_video`); the **Research Library** of persisted
reports is read by `search_research_report`/`read_research_report` (which are
`permission: web`, jerv-only). "Library topics under category W" means the **reports**.
Because reports are already condensed synthesis, v1 has the parent *select and read* the
relevant reports (by query and/or report group) and seed them — this also sidesteps having
to grant curator the web-class report tools. Fanned report retrieval is a later option.

## Tool surface & registration  [R]

- **`deep_research`** — unchanged tool, `web` class, `external` persist; dispatches into
  `_run` with `Directive(report)`.
- **`deep_produce`** — new `.tool`, **`read` class**, **empty `domains`** (all-scope
  visibility, so a health-only curator session still sees it — a health-only `domains` list
  would make `_visible` fail closed, `toolregistry.py:125`). Held by both jerv and curator;
  stays in `NEVER_DEFAULT` (it spawns fans).
- **D1 — the `_admits` change, concretely.** Curator is `tools=None` (wildcard);
  `_admits` (`toolregistry.py:106-125`) is the *single* gate that also governs jerv's
  `deep_research`. Add a per-profile `extra_tools` frozenset on `AgentProfile`, admitted
  **ahead of** the `web` (`:118`) and `NEVER_DEFAULT` (`:123`) short-circuits, so a wildcard
  persona can be granted one otherwise-excluded tool without widening the wildcard.
  **Regression test:** jerv's and teacher's admitted tool sets are byte-identical
  before/after; curator can call `deep_produce` but still **not** `spawn_subagent`,
  `deep_research`, or any web tool.

## Safety frame — a narrow, documented carve-out  [R]

`EMR_IMPORT_PLAN.md` §1 binds the health tools to "never synthesize a diagnosis, never
present an inference as fact, never recommend action," with "clinical decision support of
any kind" out of scope. A curator `deep_produce` with a health seed relaxes that for one
narrow, owner-only case, recorded rather than silently overridden:

- **Owner-invoked only** — never auto-run, workflow-triggered, or wiki-surfaced.
- **Hypothetical and prospective** — the "*if* symptoms were to recur" framing is
  load-bearing.
- **Grounded or refused** — asserts health scope + a non-empty EMR read before synthesizing.
- **Cited, with discipline that survives every `output_kind` (fixes B3).** The v10–v14
  citation / quantitative-provenance / attribution rules must not be forked per kind: keep
  **one** synth prompt where the provenance block is `output_kind`-invariant and only the
  artifact-shape section is templated. **And** decouple the critic's faithfulness pass from
  `_can_open_sources`: today `verify = _can_open_sources(source_mode) and bool(sources)` and
  `_can_open_sources` returns `source_mode != "library"` (`:137-143`), so a **library-only
  run gets no citation/quantitative/attribution check at all** — exactly the seeded case.
  The check must fire whenever there is anything (library reports or seed) to cite against.
- **Owner-wide, not health-firewalled, in v1 (D6 resolved) [R] — not "ephemeral."** The
  return persists via `record_exchange` into `agent_turns`, whose RLS is `USING(is_owner())`
  with **no** domain predicate (`0020:44-50`) — so a health-derived plan is durably readable
  by any later owner-scoped session. **v1 accepts this and documents it**, because it is
  **not new to `deep_produce`**: every curator health answer today ("what were my last labs")
  already lands health content in that same owner-wide `agent_turns`. The storage property is
  identical; only the content is richer. What v1 does *not* do is write to the `external`
  corpus — that write (jerv-readable + publicly shareable via `0150`) is genuinely worse and
  is suppressed (`_persist` no-op when `sink != external`). Truly domain-tagging the
  transcript is a **systemic** follow-up (it must fix *every* health chat turn, not just this
  verb), tracked as a separate effort — see W4. If cross-*scope* visibility of a health plan
  *within the owner's own sessions* is unacceptable to the owner, that systemic work becomes a
  W2 prerequisite; otherwise it does not gate v1.
- **Never a committed health fact** — never writes a `MedicalCondition`, a `measurement`,
  or a wiki article.
- **Not medical advice** — carries an explicit standing disclaimer.

`EMR_IMPORT_PLAN.md` §1 gains a pointer to this carve-out in the PR that ships W2.

## Budget & cost profile  [R]

- Curator runs at `budget_multiplier=1` (~2.5M pool) vs. jerv's 6 (`agents.py:298,310`),
  but the review reserves are absolute jerv-sized constants — `DR_ANALYST_RESERVE=1_125_000`
  + `DR_CRITIQUE_RESERVE=375_000` (`:194-196`), set with no clamp (`:527`). ~1.5M is ~60% of
  curator's pool, starving a curator fan (~200k/child). W2 must either raise the
  produce-holding curator's multiplier or make `DR_*_RESERVE` **proportional to**
  `tree.children_budget()`, with a seatability unit test.
- **Relocated cost profile:** a curator produce can peg the box for the full
  `TREE_WALL_CLOCK_S=6600s` deadline (`tree.py:114`, persona-agnostic) inside one
  interactive turn. Decide a lower ceiling for the curator verb, or route heavy runs to a
  background lane; document the inheritance either way.

## Prompt versioning  [R]

**W1 avoids this entirely.** Artifact shaping rides in the synth **user** message
(`_shape_directive` + an `OBJECTIVE` block) — the version-pinned
`deep_research_synthesize.prompt` **system** prompt is never templated or touched, so its
v10–v14 citation / quantitative-provenance / attribution discipline is invariant across
every `output_kind` by construction (the cleanest possible B3 fix) and no prompt version
pin or `dr-synth` bump is needed. The note below stands only as a **guard for a future
wave** that ever does template the system prompt:

> The `.tool` digest guard (`toolfile.py`) covers `.tool` files only — templating a
> `.prompt` is **not** caught by the "version bump forced" guard. Any wave that templates
> `deep_research_synthesize.prompt` must add a content/version pin (sha256 of the body + a
> `_SYNTH.version` assert, mirroring `test_promptfile.py`) and bump `dr-synth-v6 → v7` in
> the same PR.

## Waves

**W1 stands alone.** W1 delivers a complete, shippable jerv capability — a standalone
`deep_produce` verb that turns web/library research into a caller-chosen artifact (plan,
table, brief, differential, timeline), not only the report `deep_research` already gives.
This value is **independent of the health/curator use case**: it lands even if W2 (the
seeded curator path) is never built. W1 touches **no** health, seed, firewall, or
medical-safety surface — it is a pure generalization of a shipped jerv tool, gated only by
`deep_research`/`deepest` behavior-preservation. Treat W2 as an optional consumer of the
W1 engine, not a dependency of it.

| Wave | Scope | Gate |
|---|---|---|
| **W1 — jerv `deep_produce` (standalone value)** | Single-impl refactor: `_run(directive, source_plan, on_round, require_persist)`; `deep_research`/`deepest` as thin adapters (both kwargs threaded); byte-stable report rule (no OBJECTIVE block; `_shape_directive`); `output_kind` plumbing; `extra_tools` gate. Ship a **jerv-facing `deep_produce`** with ≥1 non-report `output_kind` (e.g. `plan`) end-to-end over web/library, external sink — reusing the shipped `deep_research_report` view (the artifact is a `.md` document; **no new GUI surface, no mock gate**). **No** curator/seed/health surface. B3 discipline applies here too: jerv's non-report output keeps the v10–v14 provenance block (invariant) with only the artifact-shape section templated. | `deep_research` **and** `deepest` regression: golden `ViewPayload` + persisted row + step 6/8 + `deepest_run.py:165`/`resume_deepest` exercised; `_admits` byte-identical for jerv/teacher; directive-fidelity (report run's plan/reflect/synth/critique inputs identical pre/post); a jerv non-report run produces the requested artifact and emits the *same* `deep_research_report` view, with the provenance block intact |
| **W2** | curator `deep_produce`: seed-keyed SourcePlan (D5) — the three-way seeded/refuse/plain split; health seed + fail-closed grounding refusal; `_persist` external-write suppression (D6); web-fan suppression; budget decision; treatment-plan recipe; document the owner-wide `agent_turns` property. (Output still renders through the shipped report view — a seeded run simply shows no web-sources strip, as `library` mode already does.) | RLS isolation (health read health-scoped; zero `research_reports` rows written); exfiltration property test (no web persona spawns, no seed reaches a fan child); non-health session **refuses**; sandbox-untouched; on-box run |
| **W3** | Recipe registry (named `objective` + `output_kind` presets) and owner UI (recipe / date range / category). | Recipe round-trip; mock-gate |
| **W4** *(systemic follow-up, not a v1 blocker — see D6)* | Domain-tag the agent transcript so health turns (all of them, not just `deep_produce`) are firewalled; optionally a revisitable health-scoped saved-artifact store under `domain_code='health'`. Promotes to a W2 prerequisite only if the owner requires cross-scope isolation of health output within their own sessions. | New/changed RLS + isolation test on `agent_turns`; artifact-store isolation test |

## Open decisions

- **D1 — `extra_tools` gate.** Resolved above: per-profile `extra_tools` admitted ahead of
  the `web`/`NEVER_DEFAULT` short-circuits; `deep_produce` empty-`domains`.
- **D2 — "category W" selector.** No category column on `research_reports`; the only
  grouping is **report groups** (owner folders, `app.report_groups`, migration 0149),
  opaque to the agent tools. Recommend a search query in W2 (zero schema work), named-group
  filtering as a W3 add.
- **D3 — one `deep_produce` tool vs. two files.** Recommend one `deep_produce` tool +
  untouched `deep_research`. Re-expressing `deep_research` as `deep_produce(report)` at the
  surface is cosmetic, deferred.
- **D4 — `output_kind` taxonomy.** Start with `report`, `plan`; grow the enum as recipes
  demand. Each kind templates only the artifact-shape section — the provenance block is
  invariant (B3).
- **D5 — how `_run` determines its SourcePlan. Resolved:** key on **seed-presence**, not
  persona identity (see §SourcePlan). The engine keys the plan on whether it assembled a
  health/KB seed — the exact antecedent of the safety invariant — giving the three-way
  seeded / refuse / plain-produce split. jerv and curator run the same code; the only
  brancher is a directly-observable boolean. Avoids reading a persona field `ctx` doesn't
  carry and makes the invariant self-enforcing.
- **D6 — sink for v1. Resolved:** **accept and document** the owner-wide `agent_turns`
  property (it is pre-existing for all curator health chat, not introduced here); **suppress**
  the `external`-corpus write (the one genuinely-worse, jerv-readable/shareable sink). Do
  **not** pull W4 forward as a v1 blocker — domain-tagging the transcript is a systemic effort
  that must cover every health turn. It becomes a W2 prerequisite *only* if the owner deems
  cross-scope visibility within their own sessions unacceptable.

## Out of scope

- Any web egress on a seeded/curator call (the invariant).
- Giving research sub-agents health RLS scope (breaks the child sandbox).
- Auto-run / workflow-triggered invocation, or wiki surfacing, of a seeded produce run.
- Committing any synthesized health fact, or writing seeded output to the `external` corpus.
- The background `deepest`-style resumable lane for the curator verb (a curator produce
  runs in one interactive turn; see the cost-profile note).

## Testing

Per `CLAUDE.md` non-negotiables and `DEVELOPMENT.md`: 80% backend coverage, security paths
at 100%, real Postgres via testcontainers, LLM calls faked. Specific to this engine:

- **`deep_research` + `deepest` regression:** report preset produces a byte-stable
  `ViewPayload.data` + persisted row; stream steps 6/8 + `view="deep_research_report"`
  present; `deepest_run.run_deepest` and `resume_deepest` still pass `on_round` /
  `require_persist` and a lost persist still raises.
- **Exfiltration property (100%):** for every seeded run, only `*_library` personas spawn;
  the seed appears in the parent synthesize call but in **no** spawned child's inputs; the
  `seed ⇒ ¬web ∧ ¬external-sink` precondition rejects a bad combination; a curator seeded
  run writes **zero** `research_reports` rows.
- **Discipline survival (B3):** the rendered synth system prompt for **every** `output_kind`
  still contains the v10–v14 provenance/citation/attribution clauses; the critic's
  faithfulness pass fires on a library-only/seeded run.
- **Grounding refusal:** curator `deep_produce` from a non-health session, or with an empty
  EMR read, **refuses** rather than emitting an ungrounded plan.
- **Admission:** jerv/teacher admitted tool sets byte-identical before/after; curator sees
  `deep_produce` but not `spawn_subagent`/`deep_research`/web tools.
- **Budget:** a curator-scale tree seats the intended breadth around the reserves.
