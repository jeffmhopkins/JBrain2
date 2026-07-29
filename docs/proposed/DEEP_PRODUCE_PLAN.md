# Deep Produce — one produce engine, two verbs

> **Status:** Proposed · **Last verified:** 2026-07-29

Generalize the `deep_research` pipeline into a single **produce engine** behind an
abstraction layer, surfaced as two verbs:

- **`deep_research`** — the existing *research → cited report* behavior, now literally a
  preset of the engine (`output_kind = report`). Unchanged for jerv.
- **`deep_produce`** — the general verb: the caller supplies a **Directive** (an
  objective + an output kind other than a report — a plan, a comparison table, a brief, a
  differential, a timeline), and the engine produces that artifact from whatever sources
  the calling persona is allowed to use.

The same engine serves both **jerv** (research or produce over web/library, output to the
external corpus) and **curator** (produce with *enhanced sources* — the owner's own
Research Library plus EMR/health facts seeded under RLS, never the web, output ephemeral).
The motivating case — *"given medical history from date X to date Y and the library
topics under category W, produce an idealized treatment plan if the symptoms were to
recur"* — is simply `deep_produce(output_kind = plan)` from a curator call with a health
seed.

## Why one engine, not a fork

An earlier draft of this doc proposed a separate local-only fork of `deep_research`. That
was the wrong altitude: *what to produce* (a report vs. a plan vs. a table) and *what to
produce it from* (web vs. library vs. an owner-KB seed) are **orthogonal**. Baking them
into two decoupled inputs on one engine means:

- jerv gains "produce," not just "research," with **zero new sources** — a strict
  generalization of a shipped capability.
- curator reuses the *same* producer with the health/KB seed added — a ~20-line
  entrypoint, not a cloned pipeline.
- the medical-safety property is enforced in **one** place (the abstraction), instead of
  living implicitly in "which tool you called."

## The abstraction

The engine is one service, `DeepResearchService.research()`
(`backend/src/jbrain/agent/deep_research.py:457`), refactored to a `produce()` core that
takes two decoupled inputs plus the existing breadth/mode knobs.

### 1. `Directive` — *what to produce*

`objective` (owner/caller text) + `output_kind` (`report | plan | table | brief |
differential | timeline | …`). It threads through the pipeline exactly as the existing
`_depth_directive` string already does (`deep_research.py:1166` / `:1186`):

- **Plan** (`_plan`, `:855`) — reshapes the sub-questions and the section outline, so a
  "treatment plan" run gathers differently than a "literature summary" run.
- **Synthesize** (`_synthesize`, `:976`) — the objective becomes an `OBJECTIVE:` block in
  the user message, and the synthesize *system* prompt (today hardcoded to "write ONE
  report that answers the question") becomes **renderable with `output_kind`** so the
  writer emits the requested artifact shape.
- **Reflect / analyze / critique** (`:943`, `:913`, `:1103`, all inline briefs) — the
  objective is threaded so the coverage judge and the critic evaluate against the *stated
  goal* (a critic told the goal is "a hypothetical, record-grounded plan" checks different
  things than one checking a literature report).

`deep_research` is just `Directive(output_kind = report)` with the objective defaulted
from `question` — so the existing behavior is a preset, byte-for-byte unless a caller opts
into a different `output_kind`.

### 2. `SourcePlan` — *access is resolved from the caller (jerv vs curator)*

The heart of the abstraction: an invocation's **access** — which sources and seed it may
read, whether web fans may spawn, and where output lands — is resolved from the **calling
persona's context** (`ToolContext`), *never* from a caller-supplied argument. Who is
calling decides what is reachable; the caller cannot pass its own access as a flag (which
could be spoofed or misused). This makes the persona boundary the **single access-control
point** for the engine.

| Call is from | Sources | Seed | Web fans | Sink |
|---|---|---|---|---|
| **jerv** | `{web, library}` | none | allowed | `external` corpus |
| **curator** | `{library}` | KB/health facts read under RLS | **forbidden** | ephemeral (v1) |

This is why `deep_produce` can be **one tool held by both personas**: what differs is the
resolved `SourcePlan`, computed at dispatch from the caller, not the tool. jerv's web
egress (when its `SourcePlan` allows it) happens in the *fan children* — so the parent
tool is `read`-class even though jerv's children reach the web (the same relationship
`deep_research` already has: a parent driving web children).

### 3. The invariant the abstraction owns

Because access is keyed on the caller, the exfiltration property falls out as a central,
asserted precondition rather than a per-tool convention:

> **`seed present (KB/health) ⇒ no web fan may spawn ∧ sink ≠ external`.**

With the caller-resolved `SourcePlan` above it holds **by construction** (a curator call
is web-false / seed-true; a jerv call is web-true / seed-none), and `produce()` also
**asserts** it at runtime as defense-in-depth. Concretely: health-derived text flows only
into the **orchestrator** LLM calls (`plan`, `synthesize`) — which run directly in the
parent turn, not spawns — and at most into the `review_library` critic, which has no web
tool. The gather/refill fans (the only agents that in other modes reach the web) never
receive the seed. The property is structural: *no web-capable agent ever receives seed
text.* This is the 100%-coverage security path.

## How a curator call gets the "enhanced sources"

The spawned-sub-agent sandbox mints `domain_scopes=[]` / `scopes=()`
(`spawn.py:531,988,1059`, the in-code "ONE trusted place") — we **do not** touch it.
Instead, `deep_produce` invoked from a **curator** turn (which holds `read_labs` /
`read_encounters` under a health-scoped session) reads the EMR `since`/`until` range *in
the parent*, wraps the facts in the standard inert-data envelope (`compose_feed_block` /
the `_findings_block` machinery), and threads them into `plan` + `synthesize` as the seed.
The sub-agents never touch health data; the parent did the read under proper health RLS.
Same shape as library findings feeding the writer — no sandbox change.

**Corpus note.** The library sub-agents today read the *analysed-video* corpus
(`search_external_video`/`read_external_video`); the **Research Library** of persisted
reports is read by `search_research_report`/`read_research_report`. "Library topics under
category W" means the **reports**. Because reports are already condensed synthesis, v1 does
not fan sub-agents over them — the parent *selects and reads* the relevant reports (by
query and/or report group) and seeds them, keeping "rely on what's already in the DB"
literal. Fanned report retrieval is a later option if it proves worthwhile.

## Tool surface & registration

- **`deep_research`** — unchanged. jerv's existing `.tool`, `web` class, `external`
  persist, now dispatches into the shared `produce()` with `output_kind = report`.
- **`deep_produce`** — new `.tool`, **`read` class** (so a health-scoped curator call sees
  it, gated by health RLS, not the web gate; jerv's fan children still do web when the
  caller-resolved `SourcePlan` allows). Held by **both** jerv and curator; the engine
  resolves access from the caller. Stays in `NEVER_DEFAULT` (it spawns fans), so curator
  needs an explicit grant — see D1.

## Safety frame — a narrow, documented carve-out

`EMR_IMPORT_PLAN.md` §1 binds the health tools to "never synthesize a diagnosis, never
present an inference as fact, never recommend action," with "clinical decision support of
any kind" out of scope. A curator `deep_produce` with a health seed deliberately relaxes
that line for **one narrow, owner-only case**, recorded rather than silently overridden:

- **Owner-invoked only** — never auto-run, never a workflow action, never wiki-surfaced.
- **Hypothetical and prospective** — the "*if* symptoms were to recur" framing is
  load-bearing; the output is an educational landscape, not a present-tense diagnosis or an
  instruction to act.
- **Cited and grounded** — every claim cites a library report or a specific EMR fact; the
  critique stage checks citation-faithfulness (reusing `deep_research`'s v10–v14
  attribution / quantitative-provenance discipline).
- **Never a committed fact** — ephemeral prose only; never writes a `MedicalCondition`, a
  `measurement`, or a wiki article.
- **Not medical advice** — carries an explicit standing disclaimer.

`EMR_IMPORT_PLAN.md` §1 gains a pointer to this carve-out in the PR that ships Wave W2
(per `DOC_LIFECYCLE.md`).

## Waves

| Wave | Scope | Gate |
|---|---|---|
| **W1** | Refactor the engine to `produce(Directive, SourcePlan)`: thread `objective` + `output_kind` through plan/synthesize (renderable synth system prompt) and the reflect/analyze/critique briefs; **resolve `SourcePlan` (access) from the calling persona**; assert the seed⇒local invariant. `deep_research` re-expressed as `output_kind = report` (behavior identical). Ship **jerv `deep_produce`** with a non-report recipe end-to-end (no seed yet). | On-box jerv produce run; unit tests on the directive + access-resolution seams; `test_agents.py` pins; **`deep_research` output byte-stable** regression |
| **W2** | **curator `deep_produce`**: the curator branch of the access resolver (library + KB/health seed, web-forbidden, ephemeral sink), parent-side EMR range read + seed envelope, the treatment-plan recipe. | RLS isolation test (health read stays health-scoped); the exfiltration property test (no web persona spawns, no seed text reaches a fan child); sandbox-untouched test; on-box treatment-plan run |
| **W3** | Recipe registry (named `objective` + `output_kind` presets + default breadth) and the owner UI to pick a recipe / date range / category. | Recipe round-trip test; mock-gate sign-off per `DESIGN.md` |
| **W4** *(deferred)* | Health-scoped persistence: revisitable saved artifacts in a health-firewalled store, only if v1 usage shows it's wanted. | New table + RLS isolation test |

## Open decisions

- **D1 — how curator holds a `NEVER_DEFAULT` tool.** Curator is `tools=None` (wildcard);
  `deep_produce` must be reachable without falling into every session's wildcard. Options:
  (a) an `extra_tools` frozenset on `AgentProfile` that unions with the wildcard (small,
  general, reusable); (b) a dedicated owner persona. **Recommend (a).**
- **D2 — "category W" selector.** No category column exists on `research_reports`; the only
  grouping is **report groups** (owner-named folders, `app.report_groups`, migration 0149),
  currently opaque to the agent tools. **Recommend** a search query as the topic selector
  in W2 (zero schema work), with named-group filtering as a W3 add (a folder *is* the
  "category").
- **D3 — one `deep_produce` tool vs. two verbs as two tool files.** Recommend **one
  `deep_produce` tool** (access resolved from the caller, honoring "same tool + abstraction
  layer") plus the untouched `deep_research` tool. Whether `deep_research` is eventually
  re-expressed as `deep_produce(report)` at the tool surface, or kept as a distinct name
  for UX, is cosmetic and deferred.
- **D4 — `output_kind` taxonomy.** Start small (`report`, `plan`) and grow the enum as
  recipes demand; each kind is a synth-prompt variant, not new pipeline logic.

## Out of scope

- Any web egress on a seeded/curator call (the invariant).
- Giving research sub-agents health RLS scope (breaks the child sandbox).
- Auto-run / workflow-triggered invocation, or wiki surfacing, of a seeded produce run.
- Committing any synthesized health fact, or writing seeded output to the `external`
  corpus.
- The background `deepest`-style resumable lane — v1 is one interactive turn.

## Testing

Per `CLAUDE.md` non-negotiables and `DEVELOPMENT.md`: 80% backend coverage, security paths
at 100%, real Postgres via testcontainers, LLM calls faked. Specific to this engine:

- **Exfiltration property (100%):** for every seeded run, assert only `*_library` personas
  spawn and the seed appears in the parent synthesize call but in **no** spawned child's
  inputs; assert the `seed ⇒ ¬web ∧ ¬external-sink` precondition rejects a bad combination.
- **Access resolution:** a jerv call resolves to web/library/external; a curator call
  resolves to library/seed/ephemeral/web-forbidden — from context, ignoring any
  caller-supplied source hint.
- **Sandbox untouched:** spawned children still mint `domain_scopes=[]` / `scopes=()`.
- **`deep_research` regression:** `output_kind = report` produces byte-stable output vs.
  the pre-refactor pipeline (the generalization is behavior-preserving).
- **Directive fidelity:** faked-LLM tests that `objective` + `output_kind` reach plan,
  synthesize, and critique.
- **RLS isolation:** the curator health read runs health-scoped; a non-health session
  yields nothing; any future persistence table ships its own isolation test.
