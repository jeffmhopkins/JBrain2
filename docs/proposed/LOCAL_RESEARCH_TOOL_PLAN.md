# Local Research Tool — Build Plan

> **Status:** Proposed · **Last verified:** 2026-07-29

A prompt-customizable, **local-only** fork of the `deep_research` engine: the same
`plan → gather → analyze → reflect → synthesize → critique → revise` loop, but pinned
to the owner's own corpora (the Research Library and — as first-class seed material —
EMR/health facts), never the web, and driven by a **caller-supplied objective** so one
engine serves many synthesis recipes. The motivating recipe: *"given medical history
from date X to date Y and the library topics under category W, produce an idealized
treatment plan if the symptoms were to recur."* That is one directive among many
(literature synthesis, differential summary, timeline reconciliation, …), not a
bespoke tool.

## Why this, and why now

Two capabilities already exist in isolation:

- **EMR import** (`docs/plans/EMR_IMPORT_PLAN.md`) lands medical history as cited,
  health-firewalled facts, readable in a date range via `read_labs` / `read_encounters`.
- **`deep_research`** (`docs/plans/DEEP_RESEARCH_TOOL_PLAN.md`) runs a multi-stage,
  cited, sub-agent research pipeline and already has a `library` source mode that keeps
  every fan on a local corpus with **zero web egress**.

What is missing is a way to point that pipeline at the owner's *own* knowledge with a
*custom objective*. The owner's insight — the reason this is defensible rather than a
reopening of the medical-safety line — is that **relying only on material already in the
database means no PHI ever becomes an outbound web query.** This plan turns that insight
into an enforced property, not an intention.

### The one boundary we are and aren't crossing

This is not zero-egress: the synthesis is still an LLM call through the adapter
(non-negotiable #1), so history reaches the model provider — but that boundary is
*already* accepted by the entire EMR feature (curator answers health questions via the
LLM today). This fork crosses **no new egress boundary**. It specifically closes the
*web* boundary: no web sub-agent ever runs, so health-derived text never becomes a
search term. See "Security spine" below for how that is enforced structurally rather
than by a runtime block.

## What exists — the engine and its seams

The pipeline is one service class, `DeepResearchService.research()`
(`backend/src/jbrain/agent/deep_research.py:457`), run in-request. Key seams a fork
diverges at (all verified against the code, 2026-07-29):

| Seam | Location | Fork's use |
|---|---|---|
| Driver state machine | `deep_research.py:457` `research()` | cloned; most stages reused verbatim |
| Source→persona switch | `deep_research.py:116` `_personas_for` | pinned to library (no-web) personas only |
| Orchestrator prompts (sidecars) | `prompts/deep_research_{plan,reflect,synthesize}.prompt` | plan + synthesize gain an objective slot |
| Directive threading model | `deep_research.py:1166` `_DEPTH_DIRECTIVE` / `:1186` `_depth_directive` | the exact pattern for injecting a caller directive |
| Synthesize user-text builder | `deep_research.py:988` | health seed + objective injected as inert feed |
| Analyze / critique briefs (inline) | `deep_research.py:913`, `:1103` | objective threaded so the critic checks the right goal |
| Child sandbox (empty scope) | `spawn.py:531,988,1059` (`domain_scopes=[]`, `scopes=()`) | **left untouched** — the load-bearing invariant |
| Persistence (best-effort) | `deep_research.py:764` `_persist` → `research_corpus.py:97` | **skipped** in v1 (ephemeral return) |
| Wildcard exclusion | `toolregistry.py:35` `NEVER_DEFAULT` | fork stays excluded; granted to curator explicitly |

## Design

### 1. Local-only sources — the security spine

The fork never selects a web-capable persona. `deep_research`'s `_personas_for`
(`deep_research.py:116`) already returns `(research_library, research_library,
review_library)` for the `library` mode, and those personas' allowlists
(`agents.py:228`, `RESEARCH_LIBRARY_TOOLS`) contain **no web tool**. The parent⊆child
clamp (`spawn.py:922`) can only narrow, so a child can never acquire one. The fork
hard-pins to library-class personas and forbids `web`/`library_first`.

**The invariant that makes this safe with health data:** *no web-capable agent ever
receives health-derived text.* Health facts flow only into the **orchestrator** LLM
calls (`plan`, `synthesize`), which run directly in the parent turn
(`self._router.complete` / `converse_stream`, not spawns), and — if needed — into the
`review_library` critic, which has no web tools. The gather/refill fans (the only
agents that in other modes could reach the web) are never handed the health seed. This
is structural: the exfiltration property holds by *which agents see what*, not by a
flag that could be flipped.

**Corpus note / real extension.** Today the library sub-agents read the *analysed-video*
corpus (`search_external_video` / `read_external_video`), while the **Research Library**
of persisted reports is read by `search_research_report` / `read_research_report`
(jerv's own tools, `research_corpus.py`). The owner's "library topics under category W"
means the **reports**. Because reports are already condensed synthesis, v1 does **not**
fan sub-agents over them — the parent *selects and reads* the relevant reports (by query
and/or report group) and seeds them directly, which both is cheaper and keeps the
"rely on what's already in the DB" story literal. A later wave may add report-corpus
tools to the library persona if fanned retrieval over reports proves worthwhile.

### 2. Customizable objective — recipes, not hardcoded logic

The engine gains an `objective` parameter (owner-authored, trusted at depth 0). It
threads through the seams above exactly as `_depth_directive` already does:

- **Plan** (`_plan`, `deep_research.py:855`) — the objective reshapes the sub-questions
  and section outline, so a "treatment plan" run gathers differently than a "literature
  summary" run.
- **Synthesize** (`_synthesize`, `:976`) — the objective becomes an `OBJECTIVE:` block
  in the user message. Because the synthesize *system* prompt currently hardcodes "write
  ONE report that answers the question," the fork makes that prompt renderable with an
  `output_kind` variable (or ships a sibling `local_research_synthesize.prompt`) so the
  writer produces the requested artifact shape rather than always a report.
- **Reflect / analyze / critique** — the objective is threaded into each inline brief so
  the coverage judge and the critic evaluate against the *stated goal* (a critic told the
  goal is "a hypothetical treatment plan grounded in the record" checks different things
  than one checking a literature summary).

A **recipe** is a named, saved `objective` (+ default `sources`, breadth, and which seed
readers to run). The treatment-plan recipe is the first; recipes are data, so adding the
next one is no code change.

### 3. Health facts as parent-assembled seed (the architecture-blessed path)

The spawned-sub-agent sandbox mints `domain_scopes=[]` and runs `scopes=()`
(`spawn.py:531,988,1059`) — described in-code as "the ONE trusted place." Giving
sub-agents health RLS scope would break that invariant, so we do **not**. Instead, the
fork is invoked from a **curator** (Full Brain) turn, which *does* hold `read_labs` /
`read_encounters` under a health-scoped session. The tool, given `since` / `until`,
reads the EMR range itself in the parent, wraps the facts in the standard inert-data
envelope (`compose_feed_block` / the `_findings_block` machinery), and threads them into
`plan` + `synthesize`. The sub-agents never touch health data; the parent did the read
under proper health RLS. This is the same shape as library findings feeding the writer —
no sandbox change.

### 4. Owner-invoked from curator; registration

- **New `.tool` sidecar + handler ref**, cloning `deep_research.tool` / `DeepResearchRef`.
  Params: `objective` (or `recipe`), `since`, `until`, `library_query` and/or `group`,
  `breadth`. `sources` is fixed local — not caller-selectable.
- **Permission class `read`** (not `web`), so a health-scoped curator session sees it,
  gated by health-domain RLS rather than the web gate (`toolregistry.py:118`). The child
  fans keep their own web-incapable library semantics regardless.
- **Stays in `NEVER_DEFAULT`** (it spawns fans; it should not fall into curator's
  wildcard silently). It is granted to curator via an explicit allow — see Open decision
  D1 for the exact mechanism (an `extra_tools` union on the profile vs. a dedicated owner
  persona).

### 5. Output — ephemeral in v1

`research()` builds and returns its `ToolOutput` independently of `_persist`, which is
best-effort (`deep_research.py:701-808`, `require_persist=False` for in-request runs).
The fork simply **does not persist**: the plan is returned inline to the owner, nothing
written. This sets no new firewall precedent and needs no migration. The `external`
report corpus is the *wrong* home for EMR-derived output (it is owner-wide, not
health-firewalled); if persistence is ever wanted, it must be a **health-scoped** store
(new table, health `SessionContext`, health-scoped read tool, RLS isolation test) — a
deferred wave, not v1.

## Safety frame — a narrow, documented carve-out

`EMR_IMPORT_PLAN.md` §1 binds the health tools to "never synthesize a diagnosis, never
present an inference as fact, never recommend action," with "clinical decision support of
any kind" out of scope. This fork deliberately relaxes that line **for one narrow,
owner-only case**, and records the relaxation rather than silently overriding it:

- **Owner-invoked only.** Never auto-run, never a workflow action, never surfaced in the
  wiki. A human owner asks for it each time.
- **Hypothetical and prospective.** The recipe framing ("*if* symptoms were to recur") is
  load-bearing — the output is an educational, hypothetical landscape, not a present-tense
  diagnosis or an instruction to act.
- **Cited and grounded.** Every claim cites a library report or a specific EMR fact; the
  critique stage checks citation-faithfulness against the seeded sources (reusing
  `deep_research`'s v10–v14 attribution/quantitative-provenance discipline).
- **Never a committed fact.** The output is ephemeral prose; it never writes a
  `MedicalCondition`, a `measurement`, or any health fact, and never a wiki article.
- **Not medical advice.** The output carries an explicit standing disclaimer; treatment
  sequencing is a clinician's decision.

`EMR_IMPORT_PLAN.md` §1 must gain a pointer to this carve-out in the same PR that ships
Wave W1 (per `DOC_LIFECYCLE.md`: behaviour that changes what a doc asserts is reconciled
in the shipping PR).

## Waves

| Wave | Scope | Gate |
|---|---|---|
| **W1** | Engine fork: local-only source pinning, `objective` threading through plan/synthesize (+ renderable synth system prompt), ephemeral return, `.tool` + handler + curator grant. Library reports seeded by query/group; **no** health seed yet. First non-medical recipe end-to-end. | On-box run of a general recipe; unit tests on the directive seams; `test_agents.py` allowlist pins |
| **W2** | Health seed: curator reads `since`/`until` EMR range, wraps as inert feed, threads into plan/synthesize. The treatment-plan recipe. Exfiltration property test: assert no web persona is ever spawned and no health text reaches a fan child. | RLS isolation test (health read stays health-scoped); a test proving the child sandbox is untouched; on-box treatment-plan run |
| **W3** | Recipe registry (named `objective` presets + defaults) and the owner UI to pick a recipe / date range / category. | Recipe round-trip test; mock-gate sign-off per `DESIGN.md` |
| **W4** *(deferred)* | Health-scoped persistence: revisitable saved plans in a health-firewalled store. Only if v1 usage shows it's wanted. | New table + RLS isolation test |

## Open decisions

- **D1 — how curator holds one `NEVER_DEFAULT` tool.** Curator is `tools=None`
  (wildcard); the fork must be reachable without dropping it into the wildcard for every
  session. Options: (a) add an `extra_tools` frozenset to `AgentProfile` that unions with
  the wildcard (small, general, reusable); (b) a dedicated owner persona (changes
  session-selection UX). **Recommend (a).**
- **D2 — "category W" selector.** No category/topic column exists on `research_reports`;
  the only grouping is **report groups** (owner-named folders, `app.report_groups`,
  migration 0149), currently opaque to the agent tools. Options: (a) v1 uses a **search
  query** as the topic selector (zero schema work); (b) teach the reader to filter by a
  named group (lifts group opacity to the tool). **Recommend (a) for W1, (b) as a W3
  add** since "category" maps naturally to a folder.
- **D3 — objective as free text vs. named recipe only.** Free-text `objective` at depth 0
  is owner-trusted and maximally flexible; named-recipe-only is safer against a
  half-formed prompt. **Recommend both:** free-text allowed, named recipes as presets.
- **D4 — tool/name.** Working name `local_research`; alternatives `study`, `synthesize`
  (collides with the internal stage), `recipe_research`. Bikeshed at W1.

## Out of scope

- Web egress of any kind in this tool (the whole point).
- Giving research sub-agents health RLS scope (breaks the child sandbox invariant).
- Any auto-run / workflow-triggered invocation, or wiki surfacing, of a treatment recipe.
- Committing any synthesized health fact, or writing to the `external` report corpus.
- The background `deepest`-style resumable lane — v1 is one interactive owner turn.

## Testing

Per `CLAUDE.md` non-negotiables and `DEVELOPMENT.md`: 80% backend coverage, security
paths at 100%, real Postgres via testcontainers, LLM calls faked. Specific to this fork:

- **Exfiltration property (100%):** a test asserting that for every local-mode run, only
  `*_library` personas are spawned and the health seed is present in the parent
  synthesize call but absent from every spawned child's inputs.
- **Sandbox untouched:** a test proving spawned children still mint `domain_scopes=[]` /
  `scopes=()` under this tool.
- **RLS isolation:** the health read runs health-scoped; a non-health session yields
  nothing; any future persistence table ships its own isolation test.
- **Directive fidelity:** faked-LLM tests that the `objective` reaches plan, synthesize,
  and critique text.
