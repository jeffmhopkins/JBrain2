# Deepest Research — a no-holds background research agent

> **Status:** In progress · **Last verified:** 2026-07-22 · **Waves:** R0◻️ R1✅ R2✅ R3✅ R4✅ R5✅ R6✅ R7✅ R8✅

**Finish-off pass (2026-07-22).** The three remaining threads closed:
- **Tool-aware report dedup** (was an R7 sub-item) — migration `0148` widens the dedup key
  to `(question_hash, tool)` (tool NOT NULL DEFAULT 'deep_research', existing rows
  backfilled); `persist_report` conflicts on it and `deep_research` tags the row
  `deepest_research` for a deepest run — so a deep and a deepest report on one question
  coexist. (The DB coexistence test runs in CI; the code path is verified structurally.)
- **Resume path** (was an R7 sub-item) — `deepest_run.py` `resume_deepest`: atomically
  **claims** an interrupted run (exactly-once via `claim_resume`), rehydrates its
  question/session/ceiling from the checkpoint, and re-drives — a coverage-equivalent
  report (the gate's bar, not byte-equality). `_seconds_left` re-derives the remaining
  wall-clock from the checkpoint's absolute-UTC deadline. Fake-tested (claim → re-drive;
  decline-when-unclaimable). `TreeState.for_resume` is retained for a future *continue-
  from-round*.
- **R8 data path** — the `deepest_run` **tool-view** is registered (`views/registry.tsx`
  → `DeepestRunCard`, extracted to `DeepResearchProgress.tsx` to break the cycle), and R6
  now **attaches that view to its progress turn** (`step["view"]` shape), so the run
  replays as the backgrounded timeline card on reopen (§3.5). Only *live* in-place
  delivery into an already-open surface stays deferred (§8 — no per-session channel).

**R8 landed (2026-07-22).** GUI gate **settled: variant A — the backgrounded
`deep_research` card** (owner, three-way review over `docs/mocks/deepest-research/compare.html`;
folded into `DESIGN.md` §"Deepest research — the in-flight surface"). The presentational
component landed: **`DeepestRunCard`** (`FullBrainSurface.tsx`) wraps the unchanged
`DeepResearchProgress` timeline + `SubagentFan`, adding only the amber "deepest" identity
badge + a coarse per-round meta line; `.fb-deepest-*` styles reuse the `.fb-drp-*`/`.fb-sa-*`
machinery. 3 vitest tests (badge + round line + timeline reuse + fan mount + done-state);
biome + the existing `DeepResearchProgress` suite green. **The data path is now wired** (see
the finish-off pass above): the `deepest_run` tool-view renders `DeepestRunCard` from a
run-state payload, and R6 attaches that view to its progress turn, so the run replays as the
card on reopen. Only *live* in-place delivery (§8) remains deferred.

**R1 landed (2026-07-22).** The adaptive loop shipped as `deep_research(mode="deepest")`
— in-request, depth-1, no second agent tier yet. The single fixed refill became a
resource-terminated loop (`deep_research.py`: `DR_DEEPEST_MAX_ROUNDS`,
`DR_DEEPEST_MIN_NEW_SOURCES`) that stops on covered / stable / diminishing-returns /
pool-drained / round-cap, driven by a new `stable` field on the reflect judge
(`deep_research_reflect.prompt` → `dr-reflect-v2`). Standard mode is byte-identical (all
38 prior tests pass); 6 new tests cover the loop's stops. **The owner overrode the §0
park** to build the full stack in sequence; R0's value probe is folded in as a decision
the owner has made, not a gate that blocks the build.

**R2 landed (2026-07-22).** The two-tier decomposition mechanism is complete and
tested; it is inert until a `max_depth=2` tree is minted (that activation is R3/R4):
- **Run-scoped depth** — `TreeState.max_depth` (default `1`) + `can_spawn_at`; `spawn.py`
  reads the tree, not the module constant. Ordinary runs unchanged.
- **`research_deep` task-agent persona** (`research` + the one-shot `decompose_research`
  tool; sub agents run plain `research`, so a sub agent can never hold decompose — the
  transitivity property). New `research_deep.prompt`, admitted to the agent CHECKs by
  migration `0146`.
- **`decompose_research` tool + `SpawnService.decompose_fan`** — the amplification
  controls live here: depth-guarded (refuses at depth 0 and past `max_depth`), **one-shot**
  (`TreeState.decomposed`, so a task agent can't read its first sub-fan then spawn a
  second embedding it), and a **per-parent cap `K`** (`MAX_SUBFAN_PER_TASK_AGENT`).
  `web`-classed + `NEVER_DEFAULT`; jerv holds it only for the clamp.
- **Tests** — run-scoped `can_spawn_at`, depth-1→depth-2 spawn, depth-0 / leaf refusals,
  one-shot, `K` cap, and a transitivity assertion (a depth-2 sub agent's clamped tools
  exclude `decompose_research`). The depth-2 sandbox/firewall (no memory / domain / read
  scope / location) is inherited by construction — `_run_child` mints every child, any
  depth, through the identical sandbox. Also bumped `deep_research.tool` v2→v3 (the R1
  `mode` param was an un-versioned edit) and refreshed its pinned digest.

Not yet wired: a live deepest run does not spawn `research_deep` task agents or seed
`max_depth=2` — the two-tier fan activates only from the trusted background driver
(R3/R4). The mechanism sits dormant behind the default `max_depth=1`.

**R3 landed (2026-07-22).** The concurrent execution lane — `DeepestRunLane`
(`agent/deepest_lane.py`). A deepest run is minutes-to-hours, so it cannot run inline on
either sequential loop (the job worker and the tasks tick each `await` one item at a
time); the lane runs a run as a **detached, genuinely concurrent** `asyncio.Task`:
`launch` returns immediately (non-blocking — the kickoff tool gets its turn back), runs
proceed in parallel up to a pool size (default 1, open decision §9.4 — a second launch is
refused, not queued or blocked), a **watchdog** cancels a run past its wall-clock ceiling
(the backstop for work that runs outside the `/chat` and worker timeouts), and `cancel` /
`drain` settle in-flight runs cleanly (with a defensive deregister for a task cancelled
before it starts, whose `finally` never fires). Deliberately generic — it supervises an
opaque `run()` coroutine and knows nothing about the tree, the DB, or the LLM, so it is
proven with plain coroutines (7 tests, no DB). What the coroutine *does* — build the
trusted `max_depth=2` context, drive `DeepResearchService`, checkpoint, notify — is R4–R7.

**R4 landed (2026-07-22).** The trusted run-context — the two-tier mechanism is now
**live** when driven by it (no longer dormant):
- **The seed** — `TreeState.rooted_deepest(budget_tokens, wall_clock_s)` mints the owner-set
  ceiling and `max_depth = DEEPEST_MAX_DEPTH (2)`. It is the **only** constructor that
  raises the tier; `rooted()` and bare `TreeState()` stay at `MAX_DEPTH`, so a non-deepest
  seed can never produce `max_depth > 1` (negative-depth isolation, tested).
- **Two-tier activation** — `deep_research.py` gathers with `research_deep` task agents when
  `deepest and source_mode == "web" and ctx.tree.max_depth > MAX_DEPTH`. In-request deepest
  (default tree, `max_depth=1`) and library modes stay single-tier `research` (tested three
  ways).
- **The context builder** — `agent/deepest_run.py` `build_deepest_run_context`: owner-scoped
  but **KB-less** (`read_context` with empty domain scopes → the firewalled domains never
  enter the run), no location, and `agent_tools = JERV_TOOLS` (the clamp ceiling a
  `research_deep` task agent needs). Owner-set ceiling defaults, overridable per run.
- **Isolation** proven at the unit level (the context reads no domain by construction —
  `domain_scopes == ()`, stronger than "no cross-domain"). The DB session mint + a real
  testcontainer RLS isolation test land with R5's `research_run_state` table; the monotonic
  deadline is refactored to absolute-UTC (restart-safe) in R5.

**R5 landed (2026-07-22).** The durable checkpoint + resume primitives:
- **`app.research_run_state`** (migration `0147`) — RLS `external` (like `research_reports`):
  `run_id`, `session_id`, `question`, `status` (running/done/failed/cancelled), the last
  committed `round`, the owner ceiling + an **absolute-UTC** `wall_clock_deadline`
  (restart-safe, unlike a monotonic clock), the committed `spent_tokens`/`agents_spawned`,
  the `state` jsonb rehydrate payload, and the `resumed_at` atomic one-shot resume claim
  (the 0138 pattern).
- **`external/research_run_state.py`** — `create_run` / `checkpoint` (per-round commit,
  guarded on `status='running'`) / `finish` (sticky terminal status) / `claim_resume`
  (exactly-once) / `load`, all on RLS-scoped `external` sessions.
- **`TreeState.for_resume`** — rebuilds the two-tier tree with `spent`/`agents_spawned`
  rewound to the last committed round (never re-spends or double-counts the re-run round)
  and the remaining wall-clock derived from the absolute deadline.
- **Tests** — `for_resume` counter-rewind + remaining-clock (unit, passing). The
  **RLS isolation + repo round-trip** integration test (`test_research_run_state_rls.py`,
  modeled on the proven `test_media_results_rls.py`) is written and lint/type-clean but
  runs **in CI only** — the authoring sandbox has no Docker daemon for testcontainers, so
  it was not executed locally. Migration chains single-headed at `0147`.

The **end-to-end kill-mid-run → resume** gate (rehydrate findings, continue the loop,
coverage-equivalent report) needs the run driver that ties checkpoint → lane →
`DeepResearchService`; that driver is R7, so the end-to-end resume test lands there.

**R6 landed (2026-07-22).** The periodic progress channel — `DeepestProgressChannel`
(`agent/deepest_progress.py`). A background run has no live `/chat` SSE to stream into, so
it reuses the two proven off-turn paths: **`round()`** and **`done()`** each (1) append a
server-authored assistant turn to the initiating session via `AgentTranscript.record_answer`
(owner-RLS `agent_turns`, no fake user bubble — renders on reopen), (2) publish a `NotifyBus`
nudge whose `ref` is the session id (the app deep-links to the run's chat), and (3) fire an
FCM content-free `poke` to wake a closed app. Everything is **best-effort** — each leg
swallows its own error, so a progress hiccup never crashes or stalls the run. 5 tests
(fakes, no DB). Live in-place streaming into an already-open surface stays deferred (§8).

**R7 landed (2026-07-22).** The integration wave — the backend is now functionally
complete end to end:
- **The per-round hook** — `DeepResearchService.research(..., on_round=…)` fires after
  gather and each committed gap round (inert on the in-request path, which passes nothing).
- **The run driver** — `deepest_run.py` `run_deepest`: opens the checkpoint (R5), builds the
  trusted two-tier context (R4), drives `DeepResearchService` in deepest mode with a hook
  that checkpoints + posts progress (R5+R6) each round, then marks done + announces — or, on
  any error, marks failed and posts a notice. **Fail-closed** (never raises into the lane).
- **The kickoff tool** — `deepest_research` (`.tool` + `deepest_tool.py`
  `DeepestKickoffService`/`DeepestResearchRef`): jerv-only, owner-turn-only, depth-0-only,
  **enqueue-and-return** — mints a run id, `DeepestRunLane.launch`, hands the turn back with
  a "run started" acknowledgement; a run already in flight is reported, not queued.
  `web`-classed + `NEVER_DEFAULT`; wired in the composition root with the lane + progress
  channel (transcript leg; the NotifyBus/FCM legs bind where those transports are available).
- **Tests** — the driver's happy path + fail-closed (composition, fakes), the kickoff guards
  + non-blocking launch + already-in-flight path, and the snapshot/version pins for the new
  tool. 201 passed across the touched suites; ruff + pyright clean.

**Two R7 sub-items remain (both need a live DB, deferred honestly):**
- **Tool-aware report dedup** — `research_reports` still dedups on `question_hash` alone, so
  a deep and a deepest report on the same question would clobber. The `(question_hash, tool)`
  migration + the deepest tool-tag in `persist_report` is a small DB change verified in CI.
- **The end-to-end kill-mid-run → resume test** — the checkpoint/resume *primitives* (R5) and
  the driver (R7) are in, but a full kill→rehydrate→continue→coverage-equivalent-report test
  needs a live DB + a resume entry path; it lands with the dedup change.

A **no-holds** sibling to the in-progress `deep_research` tool
(`DEEP_RESEARCH_TOOL_PLAN.md`): where `deep_research` is a *bounded,
single-turn* pipeline — plan → gather → analyze → reflect → **one** refill →
synthesize → critique → **one** revise, all inside one owner turn —
`deepest_research` removes the *effort* bounds while keeping every *blast-radius*
bound. It is an **autonomous, resumable, background research run** that recurses
two tiers deep, loops until the topic is covered (or a large owner-set ceiling is
hit), checkpoints its state, sends periodic progress back to the initiating chat,
and lands a cited report in the existing research library.

**In progress** — the owner elected to build the full stack in sequence rather
than hold at the §0 park (see the R1-landed note above). The plan was **red-teamed**
first (five adversarial reviews: security, feasibility, cost/value, process, and the
progress-transport map; their findings are folded in below and dated 2026-07-22), and
those findings shape the waves. §0's value hypothesis and kill criterion are retained
as the record of *why* the machinery exists, not as a gate blocking the owner-directed
build. It reuses a large amount of shipped substrate; the net-new surface is called out
precisely in §5.

The guiding frame, stated once: **unbounded in effort, still bounded in blast
radius.** "No holds" relaxes the budget / depth / round holds. It does **not**
touch the `CLAUDE.md` non-negotiables. Per the owner's decision (§4), egress and
tool access are inherited from `deep_research` (the parent⊆child clamp + the shipped
web sandbox at every depth); the residual brief-text leak `deep_research` already
carries is accepted, and R2's security work is bounding the *amplification* the
second tier adds — not a new egress guard.

---

## §0. Value hypothesis + kill criterion (read first)

This plan proposes ~10× the token cost and Phase-scale net-new infra. It is
**parked**, and it does not proceed on faith. Two gates govern the whole thing:

**The falsifiable hypothesis.** *On genuinely large questions, a bounded
`deep_research` run at its ceiling produces reports with material, owner-visible
coverage gaps that additional adaptive depth (more rounds, a second agent tier)
closes — and that gain is worth its marginal token cost.*

**The precondition — the deferral trigger is currently UNMET.** `deep_research`
deferred adaptive depth with an explicit trigger: *"revisit only if the fixed-2-
round bound proves insufficient in practice."* Walking that tool's own revision
log (v2–v9), **every** observed on-box failure was infra/tuning — a starved
analyst (budget-reserve bug, v3), `tree_budget_exhausted` + a wrong meter
denominator (v5), dark phases / streaming / gpt-oss citation rendering (v7–v9).
**Not one** entry reads "the 2-round bound left the report under-covered." The
base tool is also not itself settled (its D3 mock-gate sign-off and on-box
budget/wall-clock tuning are still open). So: **the trigger this plan answers to
has not fired.** R0 exists to fire it — or kill the plan.

**The park condition.** No wave past R0 is scheduled until **both**: (a) Phase 6
(the wiki — the stated roadmap frontier, in progress) has shipped, freeing the
frontier; **and** (b) R0 has demonstrated the value gap on the shipped tool. R0
is a cheap standalone probe that may run opportunistically to *inform* the park;
passing it does not by itself unpark the infra — the Phase-6 precondition still
gates R1+.

**The kill gate lives at the R1→R2 boundary** (§6): everything from R2 on is the
expensive, hard-to-reverse surface (a reopened depth tier + its new exfil
control, a new execution lane, a new RLS table). R0 and R1 must *prove value* on
a pre-registered rubric before a line of that infra is written.

---

## §1. What `deepest_research` relaxes (and what it must not)

| Hold on `deep_research` | Where | `deepest_research` |
|---|---|---|
| One refill round, fixed (no loop exists) | `deep_research.py`, `DR_MAX_GAP_QUESTIONS` | **Adaptive loop** — refill until covered-and-stable or resources spent |
| `MAX_DEPTH = 1` (children are leaves) | `agent/tree.py:20` | **Two tiers** — orchestrator → task agent → sub agent (`max_depth = 2`, sub agents are leaves) |
| One critique / revise pass | `deep_research.py` | **N passes** until the critique stops finding fixable problems (capped) |
| `SPAWN_MULTIPLIER = 10.0` → ~8M tree | `tree.py:69` | **Owner-set per-run token ceiling** (big, not infinite) |
| `TREE_WALL_CLOCK_S = 3000s`, one turn | `tree.py:78` | **Background run**, minutes-to-hours, resumable across restarts |
| Runs in-request, blocks the turn | `deep_research.py` | **Enqueue-and-return**; periodic progress + completion nudge to the chat |

**Untouched — the non-negotiables, at every depth (`spawn.py:526-534`, CLAUDE.md):**
`no_memory=True`, empty `domain_scopes`, empty read scope, `here`/`here_as_of`
None (no location), no KB access, RLS. **One caveat (§4):** the web *egress*
sandbox's safety rests on "no owner data in context," which this design breaks by
threading the owner's question into every brief — that gap gets a dedicated
control and gate, not a hand-wave.

---

## §2. Settled decisions (owner)

1. **A background run, not an in-request tool.** The owner-facing tool *enqueues*
   a run and returns immediately; periodic progress flows back to the chat (§3.5).

2. **Two-tier recursion: `orchestrator → task agent → sub agent`, and no deeper.**
   `max_depth = 2`. A **task agent** (depth 1) may spawn **sub agents** (depth 2)
   to decompose one major sub-question; a sub agent is a hard leaf. This reopens —
   deliberately, in a bounded form — the `depth≥1` spawning `tree.py:16-19`
   closed. Egress and tool access are `deep_research`'s (the clamp + the shipped
   sandbox at every depth, owner decision §4); the extra tier's *amplification* is
   bounded by the §4 controls (per-parent cap, one-shot). Depth is a property of the **run**:
   `TreeState` carries `max_depth` (default `1`); only a trusted deepest run seeds
   it at `2`, so jerv's ordinary `spawn_subagent` stays depth-1.

3. **Adaptive loop, resource-terminated — not literally infinite.** The round
   count is unbounded; the terminal condition is coverage-and-stability **or** a
   hard resource ceiling **or** diminishing returns (a round adds < N new sources
   / no new claims). "No unbounded autonomous loop" is preserved in substance: the
   loop always has a terminating resource bound, just a larger, owner-set one.

4. **Owner-set per-run cost ceiling, surfaced with its worst case.** A run could
   be tens of millions of tokens. The owner sets a token + wall-clock ceiling and
   is shown, before kickoff, both the expected cost **and** the §4 worst-case
   attacker-steerable spend (residual, quantified).

5. **Same library, coexisting with `deep_research`.** The report lands in
   `app.research_reports` (migration 0140). But the table is `UNIQUE(question_hash)`
   and `persist_report` upserts newest-wins (`research_corpus.py:146`) — a deepest
   run and a prior deep run on the *same question* would clobber each other. R7
   makes the dedup key **tool-aware** (the `tool` column exists, `0140:60`) so both
   coexist. "No new report surface" is therefore *not* absolute — it is a
   constraint change, scoped and owned by R7.

6. **Reuse the spawn substrate + the headless run context.** Every fan runs
   through `SpawnService`'s existing machinery, and the background driver **reuses
   the existing headless agent-turn context** (`tasks/runner.py`), not a net-new
   one (§5).

---

## §3. The design

### 3.1 Execution model — a concurrent background run driven off a checkpoint

```
owner turn:  deepest_research(question, ceiling?) ──enqueue──▶ "run #N started"
                                                                     │
concurrent detached task (own lifecycle/cancellation, NOT the shared worker loop):
   reuse headless run-context (tasks/runner.py) + seed TreeState(max_depth=2, ceiling)
   ┌─────────────────────────────── round loop ───────────────────────────────┐
   │ plan / expand the research tree (orchestrator-side)                        │
   │ dispatch task-agent fan  ── each task agent: ONE decomposition sub-fan ──  │
   │ analyze (cross-check)  →  reflect (coverage + diminishing-returns judge)   │
   │ COMMIT round → research_run_state  +  progress turn → chat  (§3.5)         │
   │   (resumable point: in-flight/uncommitted round work is re-run, not        │
   │    reconstructed)                                                          │
   │ covered-and-stable? ceiling hit? dry? ── no ──▶ next round                 │
   └──────────────────────────────── yes ──────────────────────────────────────┘
   synthesize → critique → revise (capped) → COMMIT report → persist → notify owner
```

### 3.2 The two-tier fan (settled decision 2)

- **Orchestrator (depth 0)** — the run driver. Plans, dispatches the task-agent
  fan, and owns *all* judgment (cross-check, coverage, diminishing-returns,
  synthesis, critique). Grows the tree across rounds from task agents' *summaries*.
- **Task agent (depth 1)** — assigned one major sub-question. When it is genuinely
  compound, the task agent emits **exactly one structured decomposition** (§4
  control) that spawns a bounded sub-agent fan, then synthesizes their summaries
  into one finding handed up. It does **not** get a raw spawn tool.
- **Sub agent (depth 2)** — a hard leaf. Searches, reads, cites, summarizes.
  Cannot spawn (`depth >= max_depth`).

### 3.3 The adaptive loop (settled decision 3)

The coverage half reuses `reflect`'s `{covered, gaps}` verdict. The **stability /
diminishing-returns half is net-new** — `_REFLECT_SCHEMA` has no such field. The
source-delta signal is mechanical (round-over-round `_collect_sources` diff,
`deep_research.py:241`); the "picture didn't move" judgment is a **new prompt +
schema field** (an R1 deliverable, not a free generalization). The loop continues
while *not (covered and stable)* **and** the ceiling holds **and** the round added
material; each terminal reason (covered / ceiling / dry) is logged so the report
states *why* it stopped.

### 3.4 Cost & termination (settled decision 4)

`TreeState` gains an owner-set `budget` and an **absolute-UTC** `deadline` sized
from the run ceiling (not `SPAWN_MULTIPLIER`). Two recursion-specific fixes over
the shipped single-tier model (§5): the wall-clock must survive a restart, and
the agent-count / spend accounting must be **per-round-committed** so a resumed
round does not double-count.

### 3.5 Periodic progress back to the initiating chat (the requested component)

The in-request tool streams `ToolProgressEvent`s into the *live turn's* SSE
(`ctx.emit_event`, `loop.py:670`) — a per-run in-memory broker that dies when the
turn ends. A background run has no live turn, so that transport does not carry.
The design instead reuses two **already-proven off-turn** paths:

- **Durable delivery into the chat** — each round-commit appends a compact
  progress turn to the initiating session via `AgentTranscript.record_exchange`
  (owner-RLS, append-only `agent_turns`), exactly as `tasks/runner.py:205` already
  does off-turn. It renders on the next session load.
- **The nudge** — a `notify_owner`/`NotifyBus` notification (its `ref` already
  carries `session_id` for deep-link) plus an FCM content-free `poke`
  (`push/sender.py`), so the owner is pulled back even with the app closed —
  exactly the Task runner's completion path (`runner.py:242-259`), but emitted
  **per round**, not once.

Cadence = per round + key transitions (started, gap round, synthesizing, done).
**Deferred (§8):** *live, in-place* streaming into an already-open surface between
turns — no per-session standing channel exists today; building one (a session-
keyed SSE mirroring `NotifyBus`, or keeping a `_LiveTurn`-style broker alive for
the run) is a separate transport project. The transcript-append + nudge is the
shipped-substrate path and is sufficient for R6.

---

## §4. Security — the trust model is deep_research's; the new work is bounding amplification

`tree.py:16-19` closed `depth≥1` spawning for **two** reasons: the model
"wouldn't use it reliably" (a *value* concern, folded into the R2 gate) and the
"**brief-laundering** surface." Reopening it is a real security decision, and the
red-team's core correction stands: the draft's claim *"a laundered brief can only
cause more sandboxed web research, not exfiltration"* was **wrong and is retracted**
— an exfil channel for brief text does exist (below). What the **owner decided**
(2026-07-22) is how to treat it: **deepest inherits `deep_research`'s exact trust
model** — sub agents share jerv's allowances through the parent⊆child clamp, and the
shipped web sandbox (SSRF private-host block, `no_memory`, empty domain + read scope,
no location, no KB) applies at *every* depth. **No bespoke egress control** (no
allowlist, no URL-substring guard, no forced topic-label rewrite). The residual is
accepted as `deep_research` already accepts it; R2's security work is bounding the
*amplification* the extra tier adds, not inventing a new guard.

**The residual, stated honestly (accepted, not eliminated).** The SSRF guard blocks
only private/loopback/reserved hosts (`web/fetch.py:275-289`); a routable **public**
host passes. A research child's brief already contains question-derived text, so a
poisoned page it reads could inject "fetch `attacker.com/?q=<brief text>`" — leaking
what is being researched. **This channel already exists in `deep_research` today**;
the two-tier fan *amplifies* it (more agents, more reads), it does not invent it.
Two things bound the harm and make the residual acceptable:
- **The firewalled domains never enter the sandbox at any depth** — health, finance,
  and location data are absent by construction (`no_memory`, empty `domain_scopes`,
  no location). Only *research-brief text* can leak; the sensitive tiers cannot.
- **Amplification is capped** — the per-run token/wall-clock ceiling and the caps in
  the controls below bound how much attacker-steered research a single run can do.

The owner accepts leakage of research-brief text as the price of an open-web research
tool, exactly as for `deep_research`. That is a deliberate risk acceptance on record,
not an oversight.

**What actually holds — the tool-clamp half.** `sub ⊆ task_effective ⊆
orchestrator` is real and monotone: a child loop's `ctx.agent_tools` is the
*already-clamped* set (`loop.py:470,480`), and `_run_child` passes
`tools_allow=child_tools` = the clamped intersection (`spawn.py:859`). A depth-2
sub agent cannot hold a tool its task agent lacks — **provided** the net-new
decomposition path clamps against the *task agent's effective* tools, not the
orchestrator's or the sub-persona's raw set. Fan-count amplification is also hard-
bounded — `can_admit` is a global counter (`tree.py:144`), so recursion cannot
explode agent count.

**The controls — amplification bounds, not a new egress guard.** Per the owner's
decision, egress and tool access are `deep_research`'s (the clamp + the shipped
sandbox); the R2 controls exist to bound the *amplification* the second tier adds and
to keep the spawn decision structured. Enabling depth-1 spawning **deletes the
belt-and-suspenders leaf guarantee** (personas hold no spawn tool, `spawn.py:18-19`),
so depth becomes the sole structural guard — these controls put the guard rails back.
They are R2 **build blockers**:

1. **Egress / tool access = `deep_research`'s model (owner decision).** Sub agents
   share jerv's allowances via the parent⊆child clamp; the shipped web sandbox applies
   at every depth. No new allowlist or URL guard. The residual (above) is accepted.
2. **Decomposition-only spawn** — the task-agent persona reaches spawning *only*
   through a structured decomposition tool that **refuses free-form/raw spawn
   args**. No raw spawn affordance at depth 1.
3. **Per-task-agent sub-fan cap `K`** — enforced as a **per-parent** counter in
   `TreeState` (not just `max_total_agents`). Moved out of §9 into an R2 blocker.
4. **One-shot decomposition** — a task agent gets **exactly one** decomposition
   round (a per-agent "already decomposed" flag on `TreeState`), so it cannot read
   sub-fan-1's fetched content and then spawn sub-fan-2 embedding it + an attacker
   URL (the lateral cross-fan exfil path). Structural, not prompt-enforced.
5. **Run-scoped `max_depth`, seed-guarded** — `spawn.py:427` and
   `deep_research.py:296` read the module constant today; both must read
   `tree.max_depth`, and the seeding paths (`api/agent.py:785`, the scheduled
   runner) must be *unable* to mint `max_depth>1`. A seed bug = silent global depth
   escalation.
6. **Two-tier reserve** — at two tiers, up to `max_parallel²` model calls can be
   in flight across the pool boundary (the single-tier overshoot bound,
   `tree.py:96-104`, squared), and `stage_reserve`'s single-level stepping
   (`DR_ANALYST_RESERVE`/`DR_CRITIQUE_RESERVE`) does not compose. Total spend stays
   hard-bounded by `tree_budget`; the *synthesis reserve* needs a tree-wide
   concurrency semaphore + a recursion-aware reserve redesign.

**The R2 security gate (100% coverage, `CLAUDE.md` rule 3)** — since egress is
`deep_research`'s (no new guard to test), the gate proves the clamp + firewalls hold
at the new depth and the amplification bounds bite:
- a **transitivity test** — orchestrator lacks tool X, both task and sub personas
  list X → assert the sub agent does **not** get X (the clamp composes across tiers).
- a **negative-depth test** — an ordinary (non-deepest) turn's `TreeState` has
  `max_depth==1` and a depth-1 child is refused spawning even with the depth-2 code
  compiled in (run-scoped depth, no global escalation).
- **firewall/sandbox isolation at depth 2** — a depth-2 sub agent has empty
  memory / domain-scope / read-scope and no location, exactly like a depth-1 child.
- **amplification-bound tests** — the per-parent sub-fan cap `K` and the one-shot
  decomposition flag each refuse the (K+1)th / second spawn from one task agent.

**Residual, quantified and accepted (settled decision 4).** With a resized tree
cap of ~N agents × `CHILD_MAX_COST_TOKENS` (900k), the worst case is *tens of
millions of attacker-steerable tokens per run*, bounded by the owner ceiling. The
owner approves that figure explicitly at kickoff. A tighter ceiling triggered by
an injection heuristic is **out of scope** (not detectable today) but named here.

---

## §5. Substrate grounding (corrected against the code, 2026-07-22)

**Reuse as-is (more than the first draft credited):**
- **Spawn fan** — `run_research_fan` (`spawn.py:572`), `_spawn_waves` (`:603`),
  the parent⊆child clamp (`:140`), the sandbox mint (`:526-534`), `_ChildResult`
  (`:110-137`).
- **Headless background run-context** — the draft called this "net-new
  scaffolding"; it is **not**. `TaskRunner.run` (`tasks/runner.py:150`) already
  drives a full `AgentLoop.run_stream` turn with **no HTTP request** —
  reconstructing `read_context`, minting a session, resolving the owner principal
  (`:159-213`). The *only* reason a task turn hits `ctx.tree is None → refuse`
  (`spawn.py:421`) is that `LoopTurnExecutor.run_turn` calls `run_stream(...)`
  **without** `tree=` (`runner.py:105-117`). Seeding the tree is one argument, not
  a subsystem.
- **Findings state is serializable** — the draft called this "the real work"; it
  is largely free. `_ChildResult` is a frozen dataclass of str/bool/tuple
  (`spawn.py:110-137`); `WebSource` is a Pydantic `BaseModel` (`contracts.py:136`).
  No live sessions/router handles are stored. Only `TreeState` needs work (below).
- **A resume precedent to mirror** — the workflow engine has no checkpoint/resume,
  but the media-analysis off-turn job does: a `status='running'` long job with a
  durable one-shot resume claim (`media_analysis_results` + `resumed_at`, migration
  0138, `agent/media_results.py:172-205`). `research_run_state` should copy its
  running-status + atomic-`claim_resume` shape.
- **Report library / view / composition root / notify + transcript paths** — as in
  §2.5, §3.5; the `deepest_research` tool wires late-bound like `DeepResearchRef`
  (`readtools.py:660,755-768`).

**Net-new (the genuine work, corrected):**
- **A concurrent detached execution lane.** "Off the worker" is necessary but
  **not sufficient** — both the job worker (`worker.py:338`) *and* the tasks loop
  (`tasks/scheduler.py:59-61`, `for task in due: await runner.run(...)`) run their
  work **sequentially**; a 30-min run in either blocks everything else. The
  requirement is a genuinely concurrent detached task (`asyncio.create_task`) with
  its **own** idle/wall-clock watchdog + cancellation — it runs outside both the
  `/chat` turn timeout (`api/agent.py`, `_MAX_TURN_WALL_CLOCK_S`) and the worker's
  machinery, so neither backstop covers it.
- **`TreeState` made restart-safe** — persist `deadline` as **absolute UTC**
  (monotonic `time.monotonic()+…`, `tree.py:132`, is meaningless after restart) and
  re-derive on rehydrate; commit `spent`/`agents_spawned` at **round boundaries**
  and rewind to the last committed round on resume (they never reset per round
  today, `tree.py:106,148`, so a many-round two-tier run blows past `12` and a
  re-run round double-counts → the run refuses its own fans).
- **`research_run_state` checkpoint table** (RLS `external`, + isolation test),
  round-boundary writes, rehydrate-and-continue.
- **The adaptive stability judge** (§3.3) [shipped in R1], the R2 gate tests (§4),
  the decomposition tool + per-parent cap + one-shot flag, the progress channel (§3.5).
  (Egress/tool access is inherited from `deep_research` per the owner decision — no
  new egress control to build.)
- **The trusted seed path** and its negative-depth guard (§4 control 5).

---

## §6. Waves

| Wave | Deliverable | Gate |
|---|---|---|
| **R0** | **Raise-the-constants probe** (no new code beyond constants + a bench harness). Raise `deep_research`'s `MAX_RESEARCH_ROUNDS`, gap-k, breadth to a ceiling; run a fixed benchmark of ≥8 genuinely-large questions; blind-rate vs today's default. | **The §0 kill gate.** Only a demonstrated coverage gap (bounded-at-ceiling still under-covers *for lack of rounds*, not tuning/search/synthesis) authorizes R1. |
| **R1** | **Adaptive loop spike** — the resource-bounded reflect→refill loop + the **new** diminishing-returns judge (a co-located `.prompt` + schema field, versioned per DEVELOPMENT.md), *in-request, depth-1 only*, as `deep_research(mode=deepest)`. | **Value, not just correctness.** On the R0 benchmark, blind-rate default vs bounded-at-ceiling vs adaptive on a **pre-registered** rubric. **KILL R2–R8 if:** the adaptive run is not preferred over bounded-at-ceiling on a clear majority; or its marginal gain per +1M tokens is below a pre-registered threshold; or the gaps trace to something other than round-depth. Loop also terminates on all three conditions in tests. |
| **R2** | **Two-tier recursion + amplification controls** — run-scoped `max_depth` on `TreeState`; the decomposition-only spawn tool (refuses free-form); per-parent cap `K`; one-shot-decomposition flag. Egress/tool access inherited from `deep_research` (owner decision, §4) — no new egress guard. | **Security gate at 100%:** transitivity (clamp composes across tiers), negative-depth (run-scoped), depth-2 firewall/sandbox isolation, and the K / one-shot amplification-bound tests (§4). **Plus a value check:** depth-1 decomposition reliably beats a flat fan of equal agent count. |
| **R3** | **Concurrent execution lane** — reuse `TaskRunner`/`LoopTurnExecutor` headless context + pass `tree=`; a detached concurrent task with its own watchdog + cancellation. | A deepest run does **not** block a concurrent scheduled task or a second deepest run (proves *concurrent*, not merely *elsewhere*). Watchdog cancels a runaway. |
| **R4** | **Trusted run-context + seed isolation** — the seed path for `max_depth=2`; all lane DB access on an **RLS-scoped session** via the storage abstraction. | Isolation test: the lane session cannot read another domain's rows and cannot mint a tree with any scope the interactive path lacks; a non-deepest seed cannot produce `max_depth>1`. |
| **R5** | **Checkpoint / resume** — `research_run_state` (RLS `external` + isolation test), round-boundary commit, `TreeState` absolute-deadline + counter-rewind, mirroring `media_analysis_results`/0138. | Kill-mid-run → resume **continues from the last committed round and produces a coverage-equivalent report** over the accumulated findings (assert findings/source superset + a completed report — **not** byte-equality; LLM calls are non-seeded). |
| **R6** | **Progress channel** (§3.5) — per-round transcript-append via `AgentTranscript.record_exchange` + a `NotifyBus`/FCM per-round nudge (`ref=session_id`). | A running deepest run posts a progress turn each round that renders in the initiating chat on reload; a nudge wakes a closed app; owner-RLS holds. |
| **R7** | **Kickoff tool + cost governance + report landing** — `deepest_research` `.tool` sidecar (enqueue-and-return); owner ceiling shown with the §4 residual; **tool-aware** dedup so deep/deepest coexist; persist the report at round-commit so a post-synthesis ceiling-hit still lands it. | Ceiling enforced as the terminal bound; a deep + deepest report on the same question coexist; owner notified on finish. |
| **R8** | **Frontend** — a run-screen entry for an in-flight deepest run + the finished-report provenance view (tiers/rounds/tokens/resume-count). | **GUI gate (PROCESS.md):** three interactive HTML mocks → owner picks → binding mock lands in `docs/mocks/` *before* implementation. |

Tests land with each wave (`CLAUDE.md` rule 5): real Postgres via testcontainers,
LLM + web faked, 80% backend coverage, security paths (the R2 + R4 isolation
tests) at 100%. **Dependencies:** R3 is additionally gated on `deep_research` Open
decision 2 (in-turn vs deferred) resolving toward "deferred" — until the base
tool's on-box timing forces deferral, the background lane is speculative.
**Dev-setup:** the lane is expected to add **no new runtime dependency** (it reuses
the existing async/process substrate); if that proves false, `scripts/dev-setup.sh`
is updated in the same PR (rule 8) and the dep flagged in the wave.

---

## §7. Relationship to `deep_research`

`deepest_research` does **not** replace `deep_research` — the bounded, single-turn
tool stays the default for most "research this properly" asks. `deepest_research`
is the escalation for a genuinely large, open question worth an hour and a real
budget. The R1 spike ships as a `mode=deepest` on the existing tool before the two
diverge into separate tools at R7. jerv's prompt gains one line steering a rare
deepest-worthy question to it. **This plan is grounded on an in-progress
substrate** (`deep_research` is In progress, not shipped); if R1 needs the
deep-research spine merged first, that is an explicit dependency.

---

## §8. Deferred past this plan

- **Live, in-place progress into an already-open chat surface** between turns. No
  per-session standing channel exists (`_LiveTurn` is per-run and dies with the
  turn; the PWA holds no standing per-session stream; `NotifyBus` SSE targets the
  native app, not the PWA chat). A session-keyed SSE/WS is a separate transport
  project; R6's transcript-append + nudge is the shipped-substrate path.
- **Depth 3+** — the two-tier cap is deliberate; a third tier reopens the §4
  argument for the added tier.
- **True free-form depth-1 spawning** — the structured decomposition (§4) is the
  only depth-1 affordance; a raw spawn tool would widen the exfil surface for no
  gain.
- **Injection-heuristic ceiling tightening** (§4 residual) and **KB-scoped deepest
  research** — both out of scope, named.

---

## §9. Open decisions + promotion checklist (on pickup)

**Open decisions (owner):**
1. **Blocking measurement — token ↔ wall-clock ↔ dollar.** The draft's "50–100M
   tokens / 30–60 min" is internally inconsistent with the base tool's measured
   ~8M tree / ~28 min (→ 50–100M ≈ 3–6 h, not 30–60 min). Resolve on-box *before*
   R0 is designed; the kickoff cost estimate and the "minutes-to-hours" framing
   depend on it.
2. **Default per-run ceiling** (token + wall-clock defaults + hard maxes), grounded
   in on-box cost.
3. **Per-tier caps** — the two-tier tree total and `K` (the §4 blocker) — sized
   from a real run once R2 lands.
4. **Run concurrency** — one deepest run at a time (a global lock) vs a small pool
   (affects R3).

**Promotion checklist (DOC_LIFECYCLE + proposed/README).** On pickup: flip
`Status: Scheduled`; `git mv` this file `proposed/ → plans/`; add a `ROADMAP.md`
slot (near the in-progress deep-research lines); add a `plans/README.md` row;
confirm each net-new artifact maps to its non-negotiable (the `research_run_state`
+ RLS isolation test, the R4 seed-isolation test, the `.prompt`/`.tool` sidecars
with the version-bump guard, storage-abstraction/RLS-scoped session for all lane
DB access, `dev-setup.sh` currency).

---

_Grounded in a substrate map of `backend/src/jbrain/{agent,workflow,tasks,external,
notify,push,api}` + `frontend/src/agent` and five adversarial reviews (security,
feasibility, cost/value, process, progress-transport), all 2026-07-22. Companion
to the in-progress `DEEP_RESEARCH_TOOL_PLAN.md`._
