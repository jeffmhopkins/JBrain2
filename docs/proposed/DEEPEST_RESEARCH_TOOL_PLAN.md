# Deepest Research — a no-holds background research agent

> **Status:** Proposed · **Last verified:** 2026-07-22 · **Waves:** DR1◻️ DR2◻️ DR3◻️ DR4◻️ DR5◻️ DR6◻️ DR7◻️

A **no-holds** sibling to the shipped `deep_research` tool (`../plans/DEEP_RESEARCH_TOOL_PLAN.md`):
where `deep_research` is a *bounded, single-turn* pipeline — plan → gather →
analyze → reflect → **one** refill → synthesize → critique → **one** revise, all
inside one owner turn — `deepest_research` removes the *effort* bounds while
keeping every *blast-radius* bound. It is an **autonomous, resumable, background
research run** that recurses two tiers deep, loops until the topic is covered (or
a large resource ceiling is hit), checkpoints its state, and lands a cited report
in the same research library.

This is a **Proposed** design (icebox). Nothing is built. It is on the record so
the shape, the security carve-outs, and the wave breakdown are settled before any
code. It reuses the shipped spawn fan, report library, and report view; the
net-new surface is the two-tier recursion, the adaptive loop, the background
execution lane, and the checkpoint/resume state.

The guiding frame, stated once: **unbounded in effort, still bounded in blast
radius.** "No holds" relaxes the budget / depth / round holds. It does **not**
touch the `CLAUDE.md` non-negotiables — web sandbox, SSRF-guarded egress,
`no_memory`, no-location, no knowledge-base access, RLS domain firewalls. Those
hold at every depth, for every agent, always.

---

## 1. What `deepest_research` relaxes (and what it must not)

| Hold on `deep_research` | Where | `deepest_research` |
|---|---|---|
| One refill round, fixed (no loop exists) | `deep_research.py`, `DR_MAX_GAP_QUESTIONS` | **Adaptive loop** — refill until covered-and-stable or resources spent |
| `MAX_DEPTH = 1` (children are leaves) | `agent/tree.py:20` | **Two tiers** — orchestrator → task agent → sub agent (`max_depth = 2`, sub agents are leaves) |
| One critique / revise pass | `deep_research.py` | **N passes** until the critique stops finding fixable problems (capped) |
| `SPAWN_MULTIPLIER = 10.0` → ~8M tree | `tree.py:69` | **Owner-set per-run token ceiling** (big, not infinite) |
| `TREE_WALL_CLOCK_S = 3000s`, one turn | `tree.py:78` | **Background run**, minutes-to-hours, resumable across worker restarts |
| Runs in-request, blocks the turn | `deep_research.py` | **Enqueue-and-return**; owner is notified on completion |

**Untouched — the non-negotiables, at every depth (`spawn.py:526-534`, CLAUDE.md):**
`no_memory=True`, empty `domain_scopes`, empty read scope, `here`/`here_as_of`
None (no location), SSRF-guarded web egress, no KB access, RLS. A sub agent two
tiers down is exactly as sandboxed as a top-tier one.

---

## 2. Settled decisions (owner)

1. **A background run, not an in-request tool.** The owner-facing `deepest_research`
   tool *enqueues* a run and returns immediately (*"started run #N, ~30–60 min,
   I'll have it ready"*). A dedicated execution lane drives it. Rationale: a
   no-holds run is minutes-to-hours; it cannot live inside one synchronous owner
   turn, and it must not block the single-threaded job worker (§5).

2. **Two-tier recursion: `orchestrator → task agent → sub agent`, and no deeper.**
   `max_depth = 2`. A **task agent** (depth 1) is assigned one major sub-question
   and *may* spawn **sub agents** (depth 2) to decompose it; a sub agent is a hard
   leaf. This reopens — deliberately and in a **bounded** form — the `depth≥1`
   spawning that `tree.py:16-19` closed. It is defensible because privilege only
   ever *narrows* with depth and the sandbox floor is identical at every level
   (§4). Depth is a property of the **run**, not a global flip: `TreeState` carries
   `max_depth` (default `1`); only a trusted deepest run seeds it at `2`, so jerv's
   ordinary `spawn_subagent` stays depth-1.

3. **Adaptive loop, resource-terminated — not literally infinite.** The round count
   is unbounded; the *terminal condition* is coverage-and-stability **or** a hard
   resource ceiling (tokens / wall-clock / agent count) **or** diminishing returns
   (a round adds < N new sources / no new claims — the loop-until-dry pattern).
   "No unbounded autonomous loop" (the lean litmus `deep_research` obeys) is
   preserved in substance: the loop always has a terminating resource bound; it is
   just a much larger one, and the owner sets it.

4. **Owner-set per-run cost ceiling, surfaced.** A no-holds run could be 50–100M
   tokens — a real dollar figure. Every run carries an owner-set token + wall-clock
   ceiling (a generous default, an owner override), shown before kickoff and
   enforced as the hard terminal bound.

5. **Same deliverable, same library.** The finished report lands in the existing
   `app.research_reports` table (RLS `external`, migration 0140) and renders in the
   existing `deep_research_report` view — extended with the deeper provenance
   (tiers used, rounds, tokens spent, resume count). No new report surface.

6. **Reuse the spawn substrate.** Every fan — top-tier task agents, their sub-agent
   fans, the cross-check analyst, the critique — runs through `SpawnService`'s
   existing fan machinery (`run_research_fan` / `_spawn_waves`), so the parent⊆child
   tool clamp, the sandbox, and the shared tree budget apply unchanged.

---

## 3. The design

### 3.1 Execution model — a background run driven off a checkpoint

```
owner turn:  deepest_research(question, ceiling?) ──enqueue──▶ returns "run #N started"
                                                                     │
research lane (dedicated, off the job worker):                       ▼
   seed TreeState(max_depth=2, budget=ceiling, wall_clock=ceiling)
   ┌─────────────────────────────── round loop ───────────────────────────────┐
   │ plan/expand the research tree (orchestrator-side)                          │
   │ dispatch task-agent fan  ── each task agent may spawn a sub-agent fan ──   │
   │ analyze (cross-check)  →  reflect (coverage + diminishing-returns judge)   │
   │ checkpoint round state → research_run_state  (RESUMABLE POINT)             │
   │ covered-and-stable? or ceiling hit? or dry? ── no ──▶ next round           │
   └──────────────────────────────── yes ──────────────────────────────────────┘
   synthesize → critique → revise (loop, capped) → persist report → notify owner
```

The **resumable point** is the per-round checkpoint: if the worker/box restarts
mid-run, the lane rehydrates the tree state (plan, findings, sources, coverage,
round N) and continues — the engine has no resume today, so this is net-new (§5).

### 3.2 The two-tier fan (settled decision 2)

- **Orchestrator (depth 0)** — the run driver. Plans the top-tier sub-questions,
  dispatches the task-agent fan, and owns *all* judgment: cross-check, coverage,
  diminishing-returns, synthesis, critique. It grows the research tree across
  rounds from the task agents' *summaries* (never their raw fetched text).
- **Task agent (depth 1)** — assigned one major sub-question. Researches it, and
  when the sub-question is genuinely compound, emits a **structured decomposition**
  (a small set of sub-briefs, same shape as the orchestrator's plan schema) that
  spawns a bounded **sub-agent fan**. It then synthesizes its sub agents'
  summaries into one finding handed back up.
- **Sub agent (depth 2)** — a hard leaf on a narrow slice. Searches, reads, cites,
  summarizes. Cannot spawn (`depth >= max_depth`).

The spawn *decision* at depth 1 is **structured, not free-form**: a task agent
does not get a raw "spawn anything" affordance; it emits a decomposition that runs
through the same validated fan with the sandbox clamp. (Laundering is still
possible in principle — see §4 — but the sub agent it yields is just as sandboxed
as itself.)

### 3.3 The adaptive loop (settled decision 3)

`reflect` already returns a coverage verdict + gaps (`_REFLECT_SCHEMA`). The loop
generalizes it: after each round the orchestrator judges **covered** (the
question is answered across the outline) **and stable** (this round's new findings
didn't move the picture — the diminishing-returns signal). It continues while
*not (covered and stable)* **and** the resource ceiling holds **and** the round
added material. Each of the three terminal conditions is logged so the report can
say *why* it stopped (covered / ceiling / dry).

### 3.4 Cost & termination (settled decision 4)

`TreeState` gains an owner-set `budget` and `deadline` sized from the run ceiling
(not the fixed `SPAWN_MULTIPLIER`). The existing `children_exhausted` /
`out_of_time` gates already stop fans at the ceiling; the loop checks them between
rounds. A per-round floor guarantees at least the synthesis completes (the
root-reserve pattern, `tree.py:183-196`, carries over).

---

## 4. Security — why two tiers is defensible, and what it costs

`tree.py:16-19` closed `depth≥1` spawning for two reasons: the model "wouldn't use
it reliably," and it "carried the depth≥1 **brief-laundering** surface" — a
depth-1 child turning attacker-controlled fetched web text into a spawn brief.
Reopening it (bounded to one extra tier) is a real security decision. Here is the
argument and the controls.

**The argument: privilege narrows down, the sandbox floor is flat.**
- The parent⊆child tool clamp composes (`effective_child_tools`, `spawn.py:140`):
  `sub-agent tools ⊆ task-agent tools ⊆ orchestrator tools`. A sub agent can never
  hold a tool its task agent lacks.
- Every agent at every depth is minted with the same sandbox (`no_memory`, empty
  domain + read scope, no location, SSRF-guarded web). A depth-2 sub agent has
  **no privilege a depth-1 agent lacked** — there is nothing further down to
  escalate *to*.
- Therefore a laundered brief cannot cause data exfiltration or firewall crossing.
  It can only cause **more sandboxed web research** — a **resource-amplification**
  risk (wasted budget on attacker-steered searches), not a confidentiality or
  integrity one.

**The controls (what turns "acceptable in principle" into "bounded in practice"):**
1. **Depth ceiling is structural and run-scoped.** `max_depth = 2` on the run's
   `TreeState` only; depth-2 refuses to spawn. Global default stays `1`.
2. **Re-sized, per-tier caps.** `MAX_TOTAL_AGENTS_PER_TREE = 12` was sized for a
   flat fan; a two-tier tree needs a larger tree ceiling **and** a per-task-agent
   sub-fan cap (e.g. a task agent may spawn ≤ K sub agents), so one laundered brief
   cannot explode the fan. Amplification is bounded by the tree total × the token
   ceiling, both hard.
3. **Structured spawn decision at depth 1** (§3.2) — a decomposition schema, not a
   free-form spawn tool.
4. **Judgment stays orchestrator-side.** Coverage, cross-check, and synthesis run
   at depth 0 over *summaries*, so a laundered sub agent's junk findings are
   cross-checked and down-weighted, never trusted directly into the report.
5. **A hard security gate before enabling depth-2 in prod** (Wave DR2): an
   isolation test proving a depth-2 sub agent cannot reach memory, a domain scope,
   location, or a tool outside its task agent's clamp — the new-table + new-depth
   RLS/isolation coverage `CLAUDE.md` rule 3 requires.

**Residual risk, accepted and capped:** a maliciously-crafted web page read by a
task agent could steer it to spawn sub agents on attacker-chosen searches, wasting
run budget. Bounded by the per-run token ceiling, the tree agent cap, and the
per-task-agent sub-fan cap. No data leaves the sandbox; the firewall never opens.

---

## 5. Substrate grounding (what exists, what's net-new)

Verified against the codebase (file:line anchors):

**Reuse as-is:**
- **Spawn fan** — `SpawnService.run_research_fan` (`spawn.py:572`) and the staged
  `_spawn_waves` (`spawn.py:603`); the parent⊆child clamp (`spawn.py:140`); the
  sandbox mint (`spawn.py:526-534`); `_ChildResult` carrying `summary`,
  `web_sources`, `session_id`, `ok`, `truncated` (`spawn.py:110-137`).
- **Report library** — `app.research_reports` (migration 0140), RLS domain-scoped
  `external`, `persist_report` upserting on `question_hash` (`research_corpus.py:97`).
- **Report view** — `deep_research_report` (`deep_research.py:807`), citations via
  `[^n] → web_sources[n-1]`.
- **Composition root** — `readtools.py build_registry` wires `DeepResearchRef`
  late-bound (`readtools.py:660`, `755-768`); a `DeepestResearchRef` slots in the
  same way.
- **Notification** — the Routine / push substrate exists for "ping the owner when
  the run finishes."

**Net-new (the real work):**
- **`max_depth` on `TreeState`** (`tree.py`) + the depth check reading it
  (`spawn.py:427`, `deep_research.py:296`), so depth-2 is run-scoped.
- **A trusted background research run-context** that seeds a `TreeState` *outside*
  the interactive turn. Today `ctx.tree is None → refuse` (`spawn.py:421`) and the
  tree is seeded only at `api/agent.py:785`; the scheduled runner is explicitly
  refused. The lane needs a trusted path that seeds a tree for *this driver only*.
- **A dedicated execution lane.** The job worker is **single-threaded**
  (`worker.py:338`); a 20-minute research run enqueued as one fat job blocks
  ingest/embed/integrate for everyone. The lane runs off the shared worker.
- **`research_run_state` checkpoint table** (RLS `external`, mirroring 0140) — plan,
  findings-so-far, sources, coverage, round N — written each round, so a run
  resumes after restart. The engine has **no checkpoint/resume** today
  (`runlog.py`: status + a `progress_note` string only).
- **The adaptive loop + diminishing-returns judge** (§3.3).
- **The owner-facing kickoff tool** (enqueue-and-return) + per-run ceiling + a
  coarse live-progress surface (`progress_note`-grade; rich live-fan streaming from
  a background run is deferred, §8).

---

## 6. Waves

| Wave | Deliverable | Gate |
|---|---|---|
| **DR1** | **Adaptive loop spike** — the resource-bounded reflect→refill loop + diminishing-returns judge, *in-request, depth-1 only*, behind a `deep_research(mode=deepest)` path. Proves the depth value cheaply before any background/recursion infra. | Loop terminates on all three conditions in tests (faked LLM); no wall-clock regression to the existing tool. |
| **DR2** | **Two-tier recursion** — `max_depth` on `TreeState`; run-scoped depth-2; structured depth-1 decomposition; re-sized per-tier caps. **Hard security gate**: depth-2 isolation test (no memory / domain / location / tool-clamp escape). | Isolation test green; caps proven to bound a synthetic laundered-brief fan. |
| **DR3** | **Background execution lane** — the dedicated runner off the job worker; the trusted research run-context seeding a `TreeState` outside the interactive turn. | A run completes off the worker without blocking a concurrent ingest job. |
| **DR4** | **Checkpoint / resume** — `research_run_state` table (RLS `external` + isolation test); per-round write; rehydrate-and-continue after a simulated restart. | Kill-mid-run → resume produces the same report as an uninterrupted run. |
| **DR5** | **Kickoff tool + cost governance** — `deepest_research` enqueue-and-return tool; owner-set per-run token/wall-clock ceiling shown pre-kickoff; completion notification. | Ceiling enforced as the terminal bound; owner notified on finish. |
| **DR6** | **Report landing + provenance** — lands in `research_reports`; extend the report view with tiers/rounds/tokens/resume-count provenance. | Report renders with citations + deep provenance strip. |
| **DR7** | **Frontend** — a run screen entry for an in-flight deepest run (coarse progress) + the finished-report surface. | Owner can see a run in flight and open its report. |

Tests land with each wave (`CLAUDE.md` rule 5): real Postgres via testcontainers,
LLM faked, 80% backend coverage, security paths (the two isolation tests) at 100%.

---

## 7. Relationship to `deep_research`

`deepest_research` does **not** replace `deep_research`. The bounded, single-turn
tool stays the default — it is the right answer for most "research this properly"
asks (minutes, one turn, watch it live). `deepest_research` is the escalation for
a genuinely large, open question worth an hour and a real token budget. jerv's
prompt gains one line steering the rare deepest-worthy question to it, exactly as
it steers a quick lookup away from `deep_research` today. The DR1 spike may ship as
a `mode` on the existing tool before the two diverge into separate tools at DR5.

---

## 8. Deferred past this plan

- **Rich live-fan streaming from a background run.** The in-request tool streams the
  live sub-agent fan + the report being written (`ToolProgressEvent` `preview`).
  A background run surfaces only coarse `progress_note`-grade status until an
  SSE-from-background transport is built — a separate front-end/transport job.
- **Depth 3+.** The two-tier cap is deliberate (settled decision 2). A third tier
  revisits only if two proves insufficient *and* the §4 argument is re-run for the
  added tier.
- **True free-form depth-1 spawning** (a raw spawn tool for task agents, vs the
  structured decomposition of §3.2) — not needed for the two-tier model; would
  widen the laundering surface without a matching gain.
- **KB-scoped deepest research** (over the owner's notes/wiki) — inherits
  `deep_research`'s deferral; a curator-side RLS surface, out of scope here.

---

## 9. Open decisions (owner to confirm on pickup)

1. **Default per-run ceiling** — the token + wall-clock defaults (and hard maxes) a
   run carries when the owner doesn't override. Needs a number grounded in on-box
   cost.
2. **Per-tier caps** — the tree total for a two-tier run, and the per-task-agent
   sub-fan cap `K`. Sized from a real run once DR2 lands.
3. **Concurrency of runs** — one deepest run at a time (simplest; a global lock) vs
   a small pool. Affects the lane design in DR3.
4. **Ship path for DR1** — a `mode=deepest` on `deep_research` (fastest) vs a
   separate tool from the start. Recommended: `mode` for the spike, split at DR5.

---

_Grounded in a substrate map of `backend/src/jbrain/{agent,workflow,external}`
(spawn fan, tree caps, workflow engine, research-report RLS) verified 2026-07-22.
Companion to the shipped `../plans/DEEP_RESEARCH_TOOL_PLAN.md`._
