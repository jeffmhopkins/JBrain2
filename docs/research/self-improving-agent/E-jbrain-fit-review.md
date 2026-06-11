# JBrain2-Fit Review: Cross-Check of the Self-Improving-Agent Dossiers

**Investigation role:** Reviewer E — critical editor across Wave-1 dossiers A–D.
Cross-checks (a) compliance with CLAUDE.md's eight non-negotiables and (b)
internal consistency / composition between the four dossiers, then hands the
synthesizer a clean reconciliation.
**Inputs:** `A-landscape-survey.md`, `B-memory-architecture.md`,
`C-self-improvement-loops.md`, `D-tooling-and-architecture.md`; ground truth
`CLAUDE.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `docs/ROADMAP.md`.
**Date:** 2026-06-11
**Stance:** critical editor, not cheerleader. Where dossiers already agree and
comply, said briefly; the value is in the conflicts and gaps.

---

## 1. Verdict

**The combined paradigm is sound and, with two structural fixes, JBrain2-compliant.**
The four dossiers converge on one coherent design — Hermes' memory-and-skill
core re-expressed on JBrain2's existing Postgres-RAG + job-queue + review-inbox
substrate (A §4), with a two-tier memory whose bright line is "agent remembers
how it behaves, never what is true" (B §4), a four-loop autonomy ladder gated by
blast-radius (C §1), and a thin in-house ReAct loop over the LLM adapter with
`.tool` sidecars (D §1). They compose remarkably well because all four
independently reach the same firewall: durable truth re-enters only through a
note. **However, two things block a clean implementation plan.** First, a
**phase-ordering conflict**: C and D lean heavily on the workflow engine
(`runs`/pipelines), the review inbox, the wiki, and the correction-note loop —
but `ROADMAP.md` ships the agent in **Phase 4**, *before* the workflow engine
(Phase 5), the wiki (Phase 6), and the correction-note/intake machinery (Phase
7). The proposal as written is not buildable at Phase 4 without either resequencing
or explicit fallbacks. Second, an **ownership seam nobody closes**: *who classifies
a memory's / skill's `domain_id` at write time*, and on what authority, is asserted
as "inherited from session scope" by B and C but never given a mechanism for the
ambiguous cross-domain case. Both are fixable and neither invalidates the
paradigm; details and resolutions below.

---

## 2. Non-negotiable compliance matrix

| # | Rule | Status | Issues / notes |
|---|---|---|---|
| 1 | All LLM calls via the LLM adapter | **PASS** | D §5 routes every call through `adapter.complete(...)`; C routes critique/distill/meta-pass via task profiles; A §4 explicitly refuses a provider zoo. No dossier proposes a direct SDK path. |
| 2 | All file I/O via storage abstraction | **PASS, with one watch-item** | B §3.1 is explicit: "no `/home/.../MEMORY.md` on disk," MD is a presentation format over `agent_memory` rows/blobs behind storage. **Watch:** A repeatedly uses literal `MEMORY.md`/`USER.md`/`SOUL.md` and "Skills in `~/.hermes/skills/`" (A §2, §3.10) as the *aspirational* model; the synthesizer must carry B's "rows-not-paths" correction, not A's filesystem framing, or rule 2 is silently broken. C §1/§4 says "the `.prompt` files in the repo" for Loop 4 — that is fine (repo source, not app runtime I/O), but skill *bodies* (C §2 Loop 2) must live in `skills` rows, not files. |
| 3 | RLS-scoped sessions + isolation test per new table | **PASS (well-covered)** | New tables `agent_memory`, `agent_episodes` (B §5.1), `skills` (C §2). All three dossiers explicitly call for `domain_id` + `has_domain_scope` RLS + the mandated isolation test (B §5.1, C §4 table row, D §3.4). This is the best-covered rule. **Gap is not coverage but the classification seam — see §3.4.** Episodic mixed-domain split (B §5.3) and the segregated memory namespace as an *RLS-eligible* column (B §3.2) are the two non-obvious tests to make sure land. |
| 4 | Comments explain why; lean; no commented-out code | **N/A at dossier stage** | No code proposed. Flag forward: ACE delta-edit logic (B §2.6) and the loop guardrails (D §2.4) are the spots where "why" comments (e.g. "RLS GUC must be set before query") will be mandatory. |
| 5 | Tests with code; 80% / security 100%; real PG; faked LLM | **PASS** | D §3.4 specifies fake-adapter-driven loop tests, testcontainers handler tests, per-tool RLS isolation tests, sidecar-validity unit tests. C §4 commits to "tests-with-code as usual" and the fake adapter for pure loop logic. **Gap (not a violation):** no dossier specifies the **eval/benchmark harness** that gates skill promotion (C Loop 2) and prompt edits (C Loop 4) — that is a deliberately-outside-CI eval suite (DEVELOPMENT.md), and it is currently hand-waved as "a held-out fixture set." See §4. |
| 6 | Conventional Commits; branch + PR; CI green | **PASS** | C Loop 4 (§2) makes prompt/tool self-edits land as branch + PR with CI green + owner approval — the strongest application of this rule. D §5 echoes "self-proposed change lands as a normal reviewed PR." |
| 7 | Wiki machine-written; humans correct via notes only | **PASS (architecturally enforced, not just promised)** | This is the dossiers' best work. B §4.2 makes it a foreign-key impossibility (agent memory rows aren't in `facts`/`chunks`, so a citation FK to one cannot exist) plus a segregated retrieval namespace; C Loop 3 Tier B forbids the agent minting facts and routes durable belief through an agent-authored note. D §2.3/§3.3 routes all mutating tools through correction-note/review-inbox. Three independent enforcements agree. **One residual:** "agent-authored note" provenance (C §4 `provenance` flag) must not become a back-channel that *elevates* agent notes' extraction weight to human level — keep agent notes at or below normal weight (the correction-note loop already uses "elevated extraction weight" for *owner* correction notes per ARCHITECTURE.md "Correction loop"; agent notes must not inherit that). Flag for the synthesizer. |
| 8 | `scripts/dev-setup.sh` updated in same PR as any new dep/tool/step | **GAP — unaddressed by all four** | None of A–D mention `dev-setup.sh`. The agent introduces real new surface: a `.tool` registry, possibly a sandboxed `spawn_subagent`/code-action path (A §3.6, D §1), pgvector usage over new tables, and an eval-suite runner. Any new Python dep (e.g. a sandbox lib) or setup step (eval fixtures, new migration entrypoints) triggers rule 8. **Not a violation yet (no code), but the synthesizer's implementation plan must name dev-setup.sh updates as an explicit task per PR**, or it will be missed. |

**Compliance summary:** 1, 3, 5, 6, 7 solidly pass; 2 passes only if A's
filesystem framing is overridden by B's rows-not-paths model; 4 deferred to
code; 8 is an un-owned process gap to flag, not a design flaw.

---

## 3. Inter-dossier conflicts & composition gaps

### 3.1 Phase placement: the agent (Phase 4) predates the machinery the loops assume (Phases 5–7) — **HIGH impact**

- **What.** C and D treat the **workflow engine** (`events`/`triggers`/`pipelines`/`runs`),
  the **review inbox**, the **wiki**, and the **correction-note loop** as
  already-present substrate. C §4 ("Loops as scheduled processes … Workflow engine";
  "Review inbox — already 'one unified queue'"; "Notes→facts→wiki spine +
  correction-note loop"). D §2.4/§5 ("Every run writes a full step log … into the
  workflow engine's `runs` table"; "Agent runs are pipeline `runs`").
- **Where.** `ROADMAP.md`: Phase 4 = agent; **Phase 5 = workflow engine** (so
  `runs`/pipelines do **not** exist yet at Phase 4); **Phase 6 = wiki**; **Phase
  7 = guided-intake + lab extraction**. The **correction-note loop** is described
  in ARCHITECTURE.md under "Wiki" and is a *wiki-era* (Phase 6) mechanism — at
  Phase 4 there are facts/entities/review-inbox (Phase 3) but **no wiki and no
  "discuss this article" correction note**.
- **Impact.** Several load-bearing claims are not satisfiable at Phase 4 as
  sequenced: (a) "agent runs are `runs` rows" (D §2.4, §5) — the `runs` table is
  Phase 5; (b) Loop 4's nightly batched self-edit pipelines (C §3) need the
  scheduler/pipeline engine — Phase 5; (c) Tier-B durable knowledge "re-enters
  via a correction note through the wiki contract" (B §4.2, C Loop 3) —
  the wiki and its correction loop are Phase 6. Only the **review inbox** and
  **facts/supersession** (Phase 3) genuinely pre-exist Phase 4.
- **Recommended resolution.** Do **not** silently assume the later machinery.
  Pick one, explicitly, and tell the synthesizer to state it:
  1. **(Preferred) Phase-stage the agent's self-improvement.** Ship at Phase 4
     only what Phase 1–3 substrate supports: the thin loop (D), Reflexion/Loop 1
     (ephemeral, needs nothing durable), and Tier-A `agent_memory` writes gated by
     the **review inbox** (which exists). Defer Loop 2 (skills → needs an eval
     harness + nightly scheduler), Loop 4 (prompt self-edit → needs the eval
     suite + ideally the pipeline engine), and Tier-B-via-correction-note (needs
     the wiki) to **align with Phases 5–6**. State this mapping in the synthesis as
     a per-loop "earliest phase" column.
  2. **(Alternative) Resequence** — pull a minimal `runs`-log and a minimal
     scheduler forward into Phase 4. This contradicts the roadmap's "each phase
     ends with something used daily" framing and enlarges Phase 4; recommend
     against unless the owner wants the full loop set immediately.
  Either way, **the synthesizer must add an explicit phase-fit section**; right now
  the dossiers describe a Phase-6-complete world and call it Phase 4.

### 3.2 Memory store ownership: D defers to "a sibling," B and C *both* claim it — **MEDIUM impact**

- **What / where.** D §5 says "the memory *store, retrieval policy, and compaction
  strategy belong to the memory researcher*" and exposes only `remember`/`recall`
  tools + a `memory_context` block. B §3 owns the `agent_memory`/`agent_episodes`
  schema, retrieval scoring, and compaction. C §2 Loop 3 *also* specifies an
  `agent_memory` table, a `provenance` flag, write-rate caps, and decay/pruning —
  and explicitly says "The sibling researcher owns *where* memory is stored; this
  loop owns *what is allowed to be remembered and how it becomes durable*."
- **Composition check.** This is mostly a **clean seam, not a conflict**: B owns
  the store + retrieval; C owns the write-admission policy + promotion; D owns the
  tool surface that invokes both. They agree on the table name (`agent_memory`) and
  the bright line. **Residual conflict:** B's schema (B §3 table) defines
  `block_kind` = `core | task | self_semantic` and is silent on a `provenance`
  flag; C introduces `provenance` on **notes** (Tier B), not on `agent_memory`.
  The synthesizer must confirm `provenance` lives on the **`notes`** row (agent vs
  human authorship), not on `agent_memory` — C's text is slightly ambiguous
  ("agent notes distinguished by `provenance`"). Low risk but worth one sentence.
- **Recommended resolution.** Adopt the three-way split verbatim (B=store/retrieval,
  C=admission/promotion policy, D=tool surface) and pin `provenance` to `notes`.

### 3.3 "Pointers-not-copies" (B) vs Tier-A preference writes (C) — **do they compose? Mostly yes — LOW/MEDIUM**

- **What.** B §4.2(3) mandates agent memory store **fact/entity IDs, never fact
  content** ("a superseded address in a stale memory copy is impossible because
  there is no copy"). C Loop 3 Tier A auto-writes preferences like "Jeff prefers
  metric units," "Jeff usually means the Austin office."
- **Composition check.** These compose: C's Tier-A residents are *behavioral
  self-knowledge* (B's "semantic-self"), which B explicitly permits as MD-block
  content (not pointers). The pointer rule applies to **world-facts**, which Tier A
  is forbidden to hold. **But one of C's own examples sits on the line:** "Jeff
  usually means the Austin office" encodes a disambiguation that *references a
  world entity*. Per B §4.1's test ("if it would belong in the wiki, it may not
  live in agent memory") this is borderline — "the Austin office exists / is Jeff's
  office" is a world-fact (pointer/live-retrieve), while "when ambiguous, prefer it"
  is a behavioral default (legitimate memory). **Impact:** without a stated rule,
  the agent could store the entity *content* ("Austin office = 123 Main St") as a
  preference and create exactly the shadow-truth B forbids.
- **Recommended resolution.** State the decomposition explicitly: a disambiguation
  preference stores **(behavioral rule) + (entity ID pointer)**, never entity
  content. This is just B §4.2(3) applied to C's example; make it a worked example
  in the synthesis so the boundary is unambiguous to implementers.

### 3.4 The unowned seam: **who classifies a memory's / skill's `domain_id` at write time?** — **HIGH impact**

- **What / where.** Every dossier asserts `domain_id` is carried and RLS-enforced
  (B §5.1, C §4, D §3.3) and that it is "inherited from session scope" (B §5.3
  "an episode inherits the domain-scope of the session"; D §3.3). **But the owner's
  session carries *all* scopes** (ARCHITECTURE.md "the owner's sessions carry all
  scopes"). So "inherit the session scope" is **undefined for the owner** — the
  exact, and most common, case. B §5.2 sees this for *behavioral* memory and invokes
  the ANALYSIS.md asymmetric rule ("a preference learned during a health chat
  defaults to `health`"), but that pushes the question back: **what decides a chat
  turn's effective domain when the owner is omni-scoped?** Nobody owns this
  classifier. C's `skills` get a `domain_id` "domain-tagged" with no mechanism. B's
  per-domain episode *splitting* (§5.3) presumes a per-turn domain label that
  doesn't yet exist.
- **Impact.** This is the single biggest **implementation-blocking gap**. Without a
  write-time domain classifier for agent-originated rows, either (a) everything
  defaults to `general` → behavioral leakage of health/finance preferences into
  general answers (the leak B §5.2 is trying to prevent), or (b) everything is
  over-classified → the agent can't recall its own general preferences in a general
  chat. It also gates the RLS tests (rule 3): you can't test isolation of a row
  whose domain assignment is undefined.
- **Recommended resolution.** Make this an **explicit owned component** in the
  synthesis: a small **memory-domain classifier** at write time that (i) for
  episodic rows, derives domain from the *retrieval scopes actually touched in that
  turn* (the tools ran RLS-scoped; the union of domains whose data was read is the
  episode's domain set → split per B §5.3); (ii) for behavioral/skill rows, applies
  ANALYSIS.md's asymmetric rule (default *into* the most-sensitive domain touched,
  `general` only if provably generic), with the **review inbox** as the tie-breaker
  for ambiguous consequential writes. Assign it explicitly to the memory lane (B),
  since it is a write-admission decision about storage. This must be named, or
  Phase-4 agent memory is not RLS-testable.

### 3.5 D's autonomy assumptions vs C's per-loop gates — **do they honor each other? Yes, with one mutation-path nuance — LOW**

- **What.** C §1 sets the gates (Reflexion auto; skills shadow→active; Tier-B
  human-gated; prompt-edit PR-gated). D builds the loop that must honor them.
- **Composition check — mostly aligned.** D §2.3/§3.3 routes mutating tools through
  correction-note/review-inbox and never edits facts directly (honors C Loop 3
  Tier B). D §5 says self-proposed prompt/tool changes "land as a normal reviewed
  PR — the self-improvement loop never hot-patches a live tool" (honors C Loop 4).
  D §2.4 hard guardrails (max_steps, max_cost, consecutive-error cap) honor A's
  AutoGPT-failure guardrails (A §3.9) and C's monotonic-gate runaway defense (C §3).
  **This is a strong agreement and should be stated as such.**
- **Residual nuance.** C Loop 2 says new skills enter `status=shadow` and are
  **"replayed in dry-run and evaluated before allowed to drive live mutations."**
  D's loop has no concept of a "dry-run / shadow execution" mode — its tool
  dispatch (D §3.2) either runs the handler (real effect) or doesn't. **Impact:**
  shadow-skill evaluation requires either a dry-run execution mode in the loop/tool
  layer (D's lane) or restricting shadow skills to **read-only tool compositions**
  until promoted. The cleaner answer (and it matches C's own "playbooks are
  compositions of already-audited tools, can't do anything a single call couldn't"):
  **shadow skills may only compose non-`mutating` tools; a skill that includes a
  mutating tool cannot run in shadow and must be human-gated like Loop 4.** Hand
  this to the synthesizer as a one-line rule; it removes the need to build a
  dry-run engine.

### 3.6 A's "steal the lean core" vs what B/C/D propose to build — **aligned, no conflict — note briefly**

A §4's keep/refuse list (reuse adapter/storage/RLS/job-queue/review-inbox; one
memory model two tiers; verified skill library gated by review; nightly reflection;
small tool set; refuse transports/providers/backends/plugins/second-stores/live-
self-rewrite) is **consistent with B, C, and D** and contradicts none of them. The
only tension is **scope of "skill library"**: A §3.1 says skill acceptance is
"hard-gated behind verification + the review inbox," whereas C Loop 2 makes skill
promotion **auto-with-rollback (shadow→active), not review-inbox-gated**. **This is
a real disagreement** — A wants human review on every skill; C wants automated
eval-gated promotion. Resolution in §5 (decision 3).

### 3.7 Code-actions / `spawn_subagent` sandbox — **a security seam left half-open — MEDIUM**

A §3.6 and D §1/§4 both keep an optional CodeAct-style `spawn_subagent` / sandboxed
code path "for the rare fan-out." A is explicit it must **not** run in the
internet-facing `api` (which never holds the Docker socket — ARCHITECTURE.md
"Supervisor"). D describes `spawn_subagent` as "the same loop with a fresh context"
but does **not** state where sandboxed *code* (if ever enabled) executes.
**Impact:** if the synthesizer reads D's `spawn_subagent` as a license for code
execution, it risks landing in `api`. **Recommended resolution:** carry A's
constraint forward verbatim — `spawn_subagent` is loop-isolation only (fresh
context, same RLS-scoped tool set), **no arbitrary code execution**; any future
code-action path is worker-sandbox-only and out of scope for Phase 4. State the
"no code execution in the agent" decision plainly.

---

## 4. Missing pieces for an implementation plan

1. **Concrete unified data model.** B gives `agent_memory`/`agent_episodes`
   columns; C gives `skills` columns; D gives the `.tool` sidecar. **Nobody draws
   the combined schema** with FKs, the segregated-namespace discriminator column
   (B §3.2), the episode→fact/entity pointer table (B §3 A-MEM links), and the
   `notes.provenance` enum. The synthesizer needs one ER sketch.
2. **The eval / benchmark harness — named but never specified.** Loop 2 promotion
   ("replay eval against held-out fixtures," C §2) and Loop 4 merge ("no regression
   on the existing eval set + a win on the new case," C §2/§4) both depend on an
   eval suite that **does not exist yet** (DEVELOPMENT.md only says prompt-quality
   eval is "a separate, deliberately-run eval suite outside CI" — as a *standard*,
   not a built artifact). What are the fixtures? What's the baseline? Who curates
   "the originating task class"? This is the critical unbuilt dependency for the
   two automated-improvement loops and should be its own work item (and likely its
   own phase alignment — see §3.1).
3. **Phase mapping per component** (the §3.1 fix made concrete): a table of
   {component → earliest supportable phase}. Minimum viable Phase-4 agent =
   thin loop + tools + Reflexion + Tier-A memory (review-inbox-gated) + RLS tests +
   dev-setup.sh updates. Skills, prompt-self-edit, Tier-B-via-note align to 5–6.
4. **Cost accounting.** C §3 says "per-event and per-day token/cost budget on the
   self-improvement pipelines" and D §2.4 sums `max_cost` per run — but there's **no
   aggregate accounting** across nightly distillation + reflection + eval runs.
   Generative-Agents reflection cost "scales with memory volume if not bounded" (A
   §3.4). Needs a stated daily budget ceiling and a kill-switch, reusing task
   profiles' max-cost.
5. **Migration / rollback for skills.** C gives `version` + `status`
   (shadow/active/quarantined) + `git revert` for prompts, but **skills live in a
   table, not git** — so "rollback" of a bad *active* skill is a DB state
   transition (demote/quarantine), and the **migration story for the `skills`
   schema itself** (Alembic, reversible — DEVELOPMENT.md) is unaddressed. Also: what
   happens to `runs`/data produced by a skill later quarantined? (Prompts have
   "version stamped on every record → reprocess after revert," C §2; skills need the
   analogous "skill_version stamped on runs" to be auditable.)
6. **Compaction ownership at the loop boundary.** D §5 "we *invoke* compaction; the
   sibling *owns* what survives it"; B §5.5 owns forgetting/decay. But **session
   compaction** (mid-conversation, when nearing context limit — D §5) vs **nightly
   episodic decay** (B §5.5) are two different operations that both touch
   `agent_episodes`; the synthesizer should confirm they're one mechanism or two and
   who triggers each.
7. **Importance scoring for RRF** (B §3.3) needs a concrete source — LLM poignancy
   (a cheap task profile, costs tokens nightly) vs heuristic (owner-corrected? tool
   error? "remember this"?). B lists both; pick the heuristic-first default to avoid
   a per-episode LLM call, and say so.
8. **dev-setup.sh + new-dependency list** (rule 8): enumerate likely new deps (none
   strictly required if the loop is pure stdlib + existing stack — worth confirming
   *no* new runtime dep is needed, which would be the leanest outcome and worth
   stating as a goal).

---

## 5. Prioritized reconciliation list for the synthesizer

Ordered by impact. Each: the decision, the conflict, my recommended resolution.

1. **Phase-fit the proposal (HIGHEST).** *Conflict:* C/D assume Phase-5–7
   machinery; agent is Phase 4 (§3.1). *Resolution:* Add an explicit per-component
   "earliest phase" mapping. Ship at Phase 4 only: thin loop + `.tool` registry +
   Reflexion (Loop 1) + Tier-A `agent_memory` (review-inbox-gated) + the three RLS
   tables/tests. Defer Loop 2 (skills) and Loop 4 (prompt-self-edit) to align with
   the eval harness + Phase-5 scheduler; defer Tier-B-via-correction-note to Phase 6
   (wiki). Do not describe a Phase-6 world as Phase 4.

2. **Own the write-time domain classifier (HIGH).** *Conflict:* "inherit session
   scope" is undefined for the omni-scoped owner; no component owns domain
   assignment for agent-written rows (§3.4). *Resolution:* Assign a memory-domain
   classifier to the memory lane (B). Episodic rows: domain = union of scopes whose
   data the turn's tools actually read, split per-domain (B §5.3). Behavioral/skill
   rows: ANALYSIS.md asymmetric rule (default into most-sensitive domain touched;
   `general` only if provably generic); ambiguous consequential writes → review
   inbox. Without this, Phase-4 memory is not RLS-testable.

3. **Skill promotion: auto-with-rollback vs review-inbox-gated (HIGH).** *Conflict:*
   A §3.1 wants every skill human-reviewed; C Loop 2 wants automated eval-gated
   shadow→active (§3.6). *Resolution (favor C, bounded by A's caution):* automated
   shadow→active promotion **is acceptable only because** skills are compositions of
   pre-audited RLS-scoped tools (C §2) — *and only for read-only compositions*. **Add
   the rule from §3.5:** a skill that composes any `mutating` tool cannot auto-promote;
   it routes to the review inbox (A's gate) or the Loop-4 PR regime. Read-only skills:
   auto, eval-gated. Mutating skills: human-gated. This reconciles A and C cleanly and
   removes the need for a dry-run execution engine.

4. **Override A's filesystem framing with B's rows-not-paths (HIGH for rule 2).**
   *Conflict:* A's `MEMORY.md`/`~/.hermes/skills/` language vs non-negotiable #2
   (§2 rule 2). *Resolution:* The synthesis must state that MD memory and skills are
   storage-abstraction-backed rows/blobs rendered as Markdown — never filesystem
   paths. A's framing is the *paradigm source*, B's is the *binding implementation*.

5. **Pin the bright-line worked example (MEDIUM).** *Conflict:* C's "Jeff usually
   means the Austin office" straddles B's behavioral/world-fact line (§3.3).
   *Resolution:* Disambiguation memory = (behavioral rule) + (entity-ID pointer),
   never entity content. Make it a worked example so implementers can't drift into
   shadow-truth.

6. **`provenance` lives on `notes`, not `agent_memory`; agent notes ≤ human weight
   (MEDIUM).** *Conflict:* C's slightly ambiguous `provenance` placement + rule-7
   back-channel risk (§2 rule 7, §3.2). *Resolution:* `provenance` enum on the `notes`
   row distinguishes agent- vs human-authored; agent-authored notes do **not** inherit
   the owner-correction-note "elevated extraction weight."

7. **Confirm "no code execution in the agent" (MEDIUM).** *Conflict:* D's
   `spawn_subagent` could be misread as a code-action license (§3.7). *Resolution:*
   `spawn_subagent` = loop/context isolation with the same RLS-scoped tool set, no
   arbitrary code. Any future code-action path is worker-sandbox-only, out of Phase-4
   scope. Carry A §3.6's constraint verbatim.

8. **Name the eval harness as a first-class work item (MEDIUM).** *Gap, not conflict
   (§4.2).* *Resolution:* Specify fixtures, baseline, and curation for the
   shared eval store before Loops 2 and 4 can be built; treat it as the gating
   dependency for the automated-improvement loops and phase it accordingly.

9. **Draw the combined data model + cost ceiling + skill-version-on-runs (LOWER but
   needed for a plan).** *Gaps (§4.1, §4.4, §4.5).* *Resolution:* One ER sketch
   (`agent_memory`, `agent_episodes` + namespace discriminator + episode→fact
   pointer table, `skills`, `notes.provenance`); a daily self-improvement cost
   ceiling reusing task-profile max-cost with a kill-switch; `skill_version` stamped
   on `runs` for auditability mirroring the `.prompt` version stamp.

10. **Add dev-setup.sh + "aim for zero new runtime deps" to the plan (LOWER).**
    *Gap (§2 rule 8).* *Resolution:* Make dev-setup.sh updates an explicit per-PR
    task; state the leanness goal that the thin loop adds no new runtime dependency
    (validate the sandbox/eval tooling against existing stack first).

**Where the dossiers already agree and comply (stated briefly, no padding):** the
notes-as-sole-truth firewall (rule 7) is enforced three independent ways and is the
proposal's strongest feature; the LLM-adapter and RLS rules are honored throughout;
the thin-loop / no-framework / native-tool-calling stance (D) is internally
consistent with A's anti-bloat thesis; the autonomy ladder's runaway/cost guardrails
(C §3, D §2.4, A §3.9) compose without conflict; and the three new tables all
correctly carry the mandated RLS isolation tests. The paradigm is good — these ten
decisions are what turn four agreeing dossiers into one buildable plan.
