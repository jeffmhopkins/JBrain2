# Loop 2 — Skill learning (build plan)

Phase-6 follow-on (ROADMAP §"Phase 6 follow-ons"); binding design = `docs/ASSISTANT.md`
§"Self-improvement loops / 2. Skill / playbook learning" + the memory/retrieval section. Executed
under `docs/PROCESS.md` (parallel tasks → per-task adversarial review → per-wave review → one PR per
wave → green→merge→next wave).

## What a "skill" is (binding interpretation)

A skill is a **distilled, parameterized multi-step playbook** (text), surfaced to the model as
context at turn time — **not executable code**. "Running" a skill = the model has it in context and
follows it. So:

- **Read-only vs mutating** is derived from the **tool permissions** the playbook directs
  (`read | mutate | external | sensitive` on the `.tool` sidecars): a playbook that names only
  `read` tools is **read-only** (auto-promote eligible); any `mutate/external/sensitive` step, or a
  cross-domain playbook, is **owner-gated** (non-neg #5: a skill runs at a single domain scope).
- The body/description are **sanitized data, never copied trace prose** — generic procedure with
  parameter placeholders, no owner world-facts or PII (the wiki test: if it'd belong in the wiki, it
  is not a skill). Enforced by the distillation prompt + a parameterization step, and bounded by the
  single-domain + read-only-tools framing.

## Shipped spine to REUSE (Phase 5 groundwork — do not rebuild)

- `app.skills` (migration 0036): `id, name, version, status('shadow'|'active'|'quarantined'),
  domain_code (FK, **RLS `has_domain_scope`**), body, description, embedding vector(384) + HNSW,
  embedding_model, success_stats jsonb, created_at, UNIQUE(name,version)`. `runs.skill_version`
  (0043) audit column (unwritten today).
- Eval harness: `EvalRunStore` (`app.eval_runs`, append-only, owner-only), `build_live_scorer`,
  `EvalRunAction` (budget-gated), the nightly `eval_run` schedule.
- **`promotion.promotion_decision(baseline, candidate, *, new_case)`** — the pure safety-inclusive
  gate (no task regression, no safety regression, new case passes). Reuse verbatim.
- `SelfImprovementGate.check/record_spend` (daily budget + kill-switch, settings-backed).
- ActionSpec / `Handler`|`ScopedHandler` / worker dispatch / seed-migration pattern
  (`wiki/actions.py` + 0047 as the template).
- Proposals: `skill-promotion` kind already in the CHECK; `proposal_nodes` + `enactment_plan` +
  `LeafExecutor` (`agent_note_executor` as the template).
- Episodes (`agent_episodes`), `runs`/`run_steps`, `AgentTurn.tools` (JSONB — the tool args), the
  RRF recall pattern (`MemoryService.recall`), the embed write/query pattern (`vector_literal` +
  `<=> cast(:v AS vector)`).

## Waves

### Wave 1 — Skills spine: repo + retrieval + turn-time injection (no autonomy)

Make active skills *usable*; ship the consumption path before distillation populates it (like the
wiki reader before the builder). No behavior change in practice — no `active` skills exist yet.

- **`SkillsRepo`** (`agent/skills.py`): RLS-scoped CRUD — create (shadow), get, list-by-status,
  set-status, bump success_stats, embed write. Mirrors the notes/analysis repo shape; embedding via
  raw SQL.
- **Skill retrieval** — `recall_skills(ctx, query, scopes, limit)`: RRF (dense `<=>` + FTS over a
  `to_tsvector` on description/body) over **`status='active'`** skills in the session's domain
  scope, top-K; reuse `rrf_scores`. (Skills are domain-scoped; RLS + the scope filter keep an
  out-of-scope skill unreachable.)
- **Turn-time injection** — surface the top matching active skills into the turn as a **"Relevant
  playbooks"** context block via the `AgentLoop.run`/`run_stream` `system=`-style seam (the T2
  `system=` precedent on `run`; add the equivalent to `run_stream`), behind a `skills_enabled`
  setting (default **on**, but inert until skills exist). Record which skills were surfaced
  (for usefulness in Wave 3) on the run.
- **`runs.skill_version` stamping**: when a skill is surfaced, stamp the run (audit).
- **Tests**: RLS isolation (the new query path; skills table already has a policy but the repo +
  retrieval need an isolation test — a narrowed/non-owner session sees only in-scope skills);
  retrieval ranking; injection renders only `active` in-scope skills; off-switch. Unit + integration.

### Wave 2 — Distillation: shadow skills from verified runs (no promotion)

Nightly `skill_distill` engine action. Writes **shadow** skills only — zero behavior change (shadow
skills are never injected), so this wave is safe to land before the gate.

- **Candidate selection**: successful runs (`status='done'`, `stop_reason='end_turn'`) with **≥2
  tool calls** reconstructed from `AgentTurn.tools` (args) + `run_steps` (sequence/ok), within the
  self-improvement budget; **untrusted-origin runs never trigger distillation** (ASSISTANT.md). Skip
  runs already distilled (track a high-water mark / dedup).
- **Distillation call** (router adapter, budget-gated via `SelfImprovementGate`): an LLM prompt
  (`agent/prompts/skill_distill.prompt`) shapes the run into a **parameterized, sanitized** playbook
  (name, description, body with `{placeholders}`, the tool sequence). Refuses/► drops a candidate
  that isn't a reusable ≥2-step procedure.
- **Domain classification (fail-closed)**: the skill's `domain_code` = the most-restrictive scope
  the source run's tools read (reuse the episodic classifier `episodic_scopes`); a skill is
  single-domain (non-neg #5) — a cross-domain run yields an **owner-gated** candidate tagged at the
  most-sensitive domain, never split.
- **Dedup**: cosine-similarity against existing skills (same domain) above a threshold → bump/skip
  rather than duplicate.
- **Write** as `status='shadow'` with embedding; `success_stats` seeded `{}`.
- **Seed** the nightly `skill_distill` schedule (disabled by default; Ops/manual enable), budget-
  gated, `cost_class='expensive'`.
- **Tests**: distillation produces shadow skills from a scripted FakeLlm run; ≥2-tool gate;
  domain classification fail-closed; sanitization (no world-facts copied — assert placeholders, not
  values); budget refusal; dedup; RLS. Unit + integration.

### Wave 3 — Promotion + quarantine (the autonomy wave — RLS/firewall/red-team)

The security-sensitive wave: shadow→active gated by a **safety-inclusive replay eval**; mutating /
cross-domain skills owner-gated via a Proposal; degradation guards.

- **Replay-eval-gated promotion** (`skill_promote` action): for each shadow skill, run the eval
  suite for its **task class** twice — baseline (no skill) and candidate (skill injected) — store
  both via `EvalRunStore`, and decide with `promotion_decision(baseline, candidate, new_case)`.
  - **Read-only skill** (all-`read` tools, single-domain) → **auto** `shadow→active` on a passing
    decision (auto-with-rollback).
  - **Mutating / external / sensitive / cross-domain skill** → **stage a `skill-promotion`
    proposal** (owner-gated); a `skill_promotion_executor` `LeafExecutor` flips it to `active` on
    owner enact. Never auto.
  - **Open decision (escalate):** the replay-eval-with-skill mechanism — how the scorer injects a
    candidate skill and which fixtures constitute a skill's "task class" (ASSISTANT.md: "same
    originating task class for skill replay"). Proposed: tag eval fixtures with a task class; the
    skill carries its originating class; the scorer injects the skill for that class's fixtures.
- **Quarantine + eviction**: `skill_sweep` action — per-skill rolling success from `success_stats`
  (surfaced vs. helped, the latter from Loop-1 reflexion verdicts / eval signal) below a threshold →
  `active→quarantined`; an **active-skill cap per domain** with **usefulness-decay** eviction
  (`active→shadow`) of the least-used.
- **Seed** the nightly `skill_promote` + `skill_sweep` schedules (disabled by default), budget-gated.
- **Tests (security-100% on the gate/executor)**: read-only auto-promotion only on a passing
  safety-inclusive decision; a safety regression **blocks** promotion; mutating/cross-domain →
  proposal, never auto; the proposal executor flips to active only on enact; quarantine on rolling
  failure; eviction respects the cap; RLS isolation on every new query path; the budget gate; the
  kill-switch halts the loop. Per-wave adversarial red-team (autonomy + firewall).

## Cross-cutting non-negotiables (CLAUDE.md / ASSISTANT.md)

- LLM via the **router adapter only**; all DB on RLS-scoped sessions; the domain firewall in
  Postgres + **an RLS isolation test per new query path**; skills are **single-domain** (#5); the
  agent is a **source, not an editor** of citable knowledge (skills are behavioral, never world-facts
  — the bright line); **untrusted-origin content never triggers a self-improvement job**; the
  data/instruction boundary holds (a distilled skill is sanitized **data**, and injecting it must not
  let trace prose act as instructions — the distillation prompt + parameterization enforce this, and
  the injected block is framed as reference, not commands); tests-with-code (80% / security-100%);
  Conventional Commits + one PR per wave + CI green; no new deps; `dev-setup.sh` current.

## Open decisions to escalate (PROCESS §critical decisions)

1. **Replay-eval-with-skill mechanism** (Wave 3) — the load-bearing autonomy measurement (fixture
   task-class tagging + scorer skill-injection). Architectural; surfaced before Wave 3 builds.
2. **Tunables (§7-style)** — daily budget split for distillation/promotion, the active-skill cap per
   domain, the quarantine success threshold, the dedup similarity threshold, the retrieval top-K.
3. **Wave 1 injection default** — ship `skills_enabled` on (inert until skills exist) vs off.

No GUI surface in Waves 1–3 (engine + agent-context only); a future Ops/Proposals surface for skill
review would trigger the three-mockup GUI gate then, not here.
