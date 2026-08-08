# jerv Planning Tool — owner-approved plans, executed across turns

> **Status:** In progress · **Last verified:** 2026-08-08 · **Waves:** P1✅ P2✅ P3✅ P4◻️ P9✅ P9.1✅ P9.2✅ P9.3✅ P9.4✅ P9.5✅ (P1 = the planning tool: table + RLS, read_plan/write_plan, owner-only approval, per-turn re-injection, prompt. P2 = the auto-continuation runtime: the settle hook, the in-process sweep, the `pause` control, the /chat hooks. P3 = the PWA surfaces: the `plan_card` view, the composer-foot plan pill, the Chats badge, the auto-resume countdown. P4 = **live + tidy** (on-branch, pre-merge): continuation turns now STREAM live into the chat via the reattach broker (§5), and the plan surface moved off the transcript — the inline card is draft-only, the in-work plan lives behind the composer pill's popover (§6). Shipped with unit + RLS-isolation + integration + vitest coverage. **P4.1 = visibility + budget** (§5, §6, §9): the original draft card is kept on its own turn as the approved record, the between-steps wait shows an interruptible countdown on the status line, and a supervised (foreground-watched) turn earns a lifted per-turn budget. **P4.2 = draft approvability fix** (§3, §4, §6): a `not_approved` draft is always approvable — `await_owner` on a draft is a no-op, approval clears any stale flag, and the card's `not_approved` state dominates `await_owner` — so jerv pausing the draft can never strand the owner with no way to approve. **P5 = step-results scratchpad + instant start** (§9): an append-only, index-ordered results scratchpad (`write_plan_result`) each step records its synthesis to and the final step reads to write the deliverable — visible as a per-step Results section in the card — plus approve/Continue now start the step immediately (an on-demand kick + a prompt busy-retry) instead of idling the ~60s window. **P6 = execution correctness + polish** (§10): recording a result now deterministically ticks the step's box and flips `in_work`; written plans are normalized to a flat `- [ ]` checklist (the Step-4-heading render bug); one step per turn; the card's countdown no longer flashes on an immediate start; and the Results section is collapsible + Markdown-rendered in the card and the modal. **P7 = multiple independent plans per conversation** (§11): the table is re-keyed on a per-plan `plan_id` (migration 0158) so a chat holds many plans without crosstalk — an old card keeps its own results, the omnibox tracks the active (latest) plan, a fresh `not_approved` draft over a running plan starts a separate plan, and the per-step Results now fold one collapsible per step. Also (§12): the context-usage meter no longer freezes during continuations — `run_turn` passes the window into the loop so UsageEvents stream, and the continuation persists the fill for reopen. **P8 = per-step turn acknowledgement** (§13): a non-final continuation step now writes a one-line recap of what it produced as its visible reply (jerv v38), so the turn shows progress instead of rendering empty; the final step still writes the full deliverable. **P9 = plan steps can research + Stop truly halts** (§5): a continuation turn is seeded as a sub-agent fan ROOT (`root_tree=True` through `run_turn`), so a plan whose steps ARE `deep_research`/`compare_candidates` runs no longer refuses every step with "only available in an interactive owner turn"; and Stop is fixed — the continuation runs its turn as a cancellable task on `live.task` so the composer Stop actually cancels it, a cancelled step marks the plan `awaiting_owner` (no re-arm), and `/api/plans/{id}/stop` halts durably + cancels the in-flight step instead of resetting the budget and letting it re-arm. **P9.1 = a notes section stays prose** (§10): a trailing `**Notes**`/`## Caveats` section's bullets are no longer flattened into phantom checklist steps (the 0-of-8-not-5 bug), and jerv v39 keeps caveats in its reply rather than the plan body. **P9.2 = per-step timing** (§10): each step records how long it took (`step_started_at` stamped as the step's turn begins, `complete_step` writes the elapsed `secs` onto the step's results entry), shown as a compact duration on each step in the card + omnibox modal. **P9.3 = the omnibox modal renders plan markdown like the card** (frontend): the plan Sheet is portaled outside `.fb-shell`, so `.plan-sheet` was added to the `.md-*` style scope — inline code/tables in a step render identically in the popover and the inline card. **P9.4 = step-advance safety net** (§10): a `done` continuation turn that finished the work but recorded no result (jerv skipped `write_plan_result` after a deep_research step) now auto-records the turn's answer so the box ticks and the plan advances, instead of the loop re-running the same step forever. **P9.5 = reopen reconcile** (§5): a PWA backgrounded mid-plan doesn't remount and its `activeId` doesn't change, so the id-keyed transcript reload never re-fired on resume — continuation turns that landed while hidden stayed missing and a stale "still researching…" bubble lingered until the owner switched chats. `useFullBrain` now reloads the active chat's transcript on `visibilitychange`→visible (unless a turn is streaming live into it).)

> Reconciled with the root `CLAUDE.md` non-negotiables: the plan is an owner-only
> `app.agent_session_plans` row behind `app.is_owner()` RLS (FORCE), with the mandated
> per-table RLS-isolation test; every continuation turn runs through the **LLM adapter**
> via the shared `LoopTurnExecutor` (never a provider SDK) and all DB access is on an
> RLS-scoped session; the continuation loop is **episodic, human-anchored, and
> step-capped** (the ASSISTANT.md "no unbounded autonomous loop" refuse-rule); and the
> feature is **jerv-only** — `curator` never sees the tools (the opt-in `web` class).

A per-conversation **plan**: jerv drafts a plan when the owner asks, the owner approves
it, and jerv then executes the plan's checklist **across turns** — so a multi-step task
that can't finish inside one turn's guardrails still completes, while staying steerable.

---

## 1. Why

jerv is a sandboxed web chatbot with no mutating knowledge-base tools, so this is about
**alignment, not blast-radius**: agree an approach, then follow it. Two problems it solves:

1. **Agreeing before doing.** "Make a plan and let me approve it" — a shared, editable
   plan document the owner signs off on, so a big task starts from a mutual understanding
   rather than jerv guessing.
2. **The single-turn ceiling.** A long approved plan can't finish in one turn (the
   `max_steps` / cost / wall-clock guardrails cut it off). Execution is **chunked across
   turns**, with a fresh guardrail budget each step — and a brief owner-interruptible
   pause between steps so the run stays steerable (avoiding "plan drift with no recovery").

**Owner-initiated only.** jerv does *not* volunteer plans. Planning mode is entered only
when the owner explicitly asks ("make a plan", "plan this out"); otherwise jerv just does
the task. (Enforced in the jerv prompt and the `write_plan` tool description.)

## 2. Data model — `app.agent_session_plans` (migrations 0155, 0157, 0158)

MANY plans per chat (P7, migration 0158): each row is keyed by its own `plan_id uuid`, with
`session_id` a NOT-NULL cascade FK to `agent_sessions.id` (non-unique — many plans per chat).
Owner-only RLS (`app.is_owner()`, FORCE) like `archivist_memory` — a plan is conversation-local,
**not** knowledge-base data, so it carries no domain and jerv (empty-scoped) still reaches it
because the firewall is ownership, not a domain scope. The **active** plan of a session is the
latest-created one (`ORDER BY created_at DESC`, backed by a `(session_id, created_at)` index) — the
one the tools, the re-injection, the continuation loop, and the omnibox all operate on.

- `body text` — the plan text (Markdown, a `- [ ]` / `- [x]` checklist for steps).
- `status text` CHECK ∈ (`not_approved`, `approved`, `in_work`).
- Continuation bookkeeping: `continuation_due_at timestamptz` (when the next step
  auto-fires; NULL = none pending), `awaiting_owner bool` (jerv's blocked opt-out),
  `continuations_used int` (the cap counter). A partial index on `continuation_due_at`
  keeps the sweep's claim cheap.

`PlanRepo` (`models/plan.py`) takes an already-RLS-scoped session; the owner-only firewall
is Postgres', not the repo's.

## 3. The tools (jerv-only, `web` class)

- **`read_plan`** — the current plan (body + status) for this chat.
- **`write_plan(body?, status?, pause?)`** — create/rewrite the body and/or move the
  status and/or hand off the turn. The **state machine is server-enforced**: jerv may set
  only `not_approved` / `in_work`, and `in_work` only once the owner has approved.
  **`approved` is the owner's alone** — so web content jerv reads can never talk it into
  self-approving. `pause`: `checkpoint` (finished a step, continue after the window) or
  `await_owner` (blocked — stop the loop until the owner replies). **`await_owner` on a
  `not_approved` draft is a deliberate no-op** (P4.2): a draft is already waiting for the
  owner (to approve it), so setting the flag would be meaningless and harmful — it would hide
  the Approve control on the card and, left set, block the loop even after approval. jerv
  reasonably reads "don't start until I approve" as a reason to pause; the handler drops the
  pause on a draft and returns a note teaching it that a draft already waits.

Both are added to `JERV_TOOLS`; the `web` class means `curator` never sees them. A tool
result returns a `plan_card` **view** so the card renders inline (data-only, never
model-authored markup — invariants #1/#9).

## 4. Approval + re-injection

- **Owner approval** is a UI gesture / endpoint (`POST /api/plans/{id}/approve`), never a
  jerv tool. The owner may edit the draft first (`/edit`). Approval (`PlanRepo.approve`) sets
  `approved` **and clears any stale `awaiting_owner`** (jerv may have paused the draft — see
  §3), then **arms the first continuation**, so the sweep starts jerv on the plan (~15s) —
  approval alone otherwise just sets the status and the plan sits, because nothing tells the
  agent it was approved. Clearing the flag is load-bearing: `claim_due_continuations` skips
  awaiting-owner plans, so a leftover flag would leave the approved plan armed-but-never-run.
  The loop fires on `approved` (the first step) as well as `in_work` (jerv flips the status
  itself as it executes).
- **On-plan across turns:** while a plan is `approved`/`in_work`, the `/chat` route
  re-injects it each turn as a DATA-framed operating block (`_plan_blocks`), beside the
  existing `read_artifact` / `read_research_report` pointers — the single highest-leverage
  way to keep the agent on-plan, matching how Claude Code / Cursor / OpenHands re-feed the
  plan file. A `not_approved` draft is deliberately **not** injected (not sanctioned yet).

## 5. Auto-continuation (the loop)

Composition of existing machinery, not a new loop (`agent/continuation.py`):

- **Schedule:** a jerv turn that ends with the plan `in_work` + unchecked steps + not
  awaiting-owner + under the cap arms a continuation `CONTINUATION_DELAY_S` (60s) out
  (`maybe_schedule_continuation`, called from `/chat`'s settle path and after each
  continuation turn). **Hybrid trigger:** any clean turn-end schedules it, and jerv can
  also force a boundary with `pause="checkpoint"` or opt out with `pause="await_owner"`.
- **Interrupt window:** an owner message supersedes — `/chat` calls `cancel_and_reset`
  (drops the due-time, clears await-owner, zeroes the cap counter), so the owner both
  steers and refreshes the budget (a `deferred_outcome` resume is a system event, skipped).
- **Fire:** an in-process sweep (`run_plan_continuation_loop`, ~15s cadence, restart-safe
  because the due-time lives in the DB) atomically claims due plans and runs one **fresh**
  jerv turn per plan via `LoopTurnExecutor` — the same engine `/chat` and tasks use —
  recorded with `record_answer` (no fake owner bubble), exactly like the deferred-tool
  auto-resume. **It STREAMS live (P4):** rather than register the old inert marker, the
  runner registers a real `_LiveTurn` (the shared frame-buffer + fan-out broker, extracted
  to `agent/live_turn.py`) keyed by the continuation's run_id, and passes an `on_event` sink
  into `run_turn` (`tasks/runner.py`) that emits each event as a `data:` SSE frame. Because
  the continuation flips the session's `last_run_status` to `running`, a foreground/reloaded
  client discovers the run (`GET /chat/sessions/{id}/live-run`) and follows it
  (`GET /chat/runs/{run_id}/stream`) — the very same reattach path an owner's detached turn
  uses — so the owner watches the step's thinking + tool calls token-by-token instead of it
  running invisibly. The frontend arms this via a poll on `useFullBrain` while the active
  plan is working. The runner registers in the shared `live_turns` registry, so an owner
  turn can't stack on a continuation (the `/chat` 409 guard) and vice versa; a busy session
  re-arms and retries next sweep. **It is a fan ROOT (P9):** `run_turn` is called with
  `root_tree=True`, so — exactly as an owner `/chat` turn does — the turn seeds a rooted
  `TreeState`. Without it `ctx.tree` is None and the fan-out tools
  (`deep_research`/`deep_produce`/`deepest_research`/`spawn_subagent`) refuse with "only
  available in an interactive owner turn", which broke a plan whose steps ARE deep-research
  runs (e.g. a candidate-report plan: `candidate_profile` per candidate, then
  `compare_candidates`).
- **Terminate:** the chain stops when the checklist is fully `- [x]`, the status leaves
  `in_work`, jerv sets `await_owner`, the owner sends anything, or `MAX_CONTINUATIONS` (20)
  is hit. Each hop is a discrete, separately-recorded, guardrailed turn — bounded per the
  "no unbounded autonomous loop" invariant.

Owner controls (`/api/plans/{id}/stop|continue`) halt the loop or fire the next step now.
**Stop truly halts (P9):** the continuation runs its turn as a cancellable task published on
`live.task`, so the composer's Stop (`POST /chat/runs/{run_id}/cancel` → `live.cancel()`) —
which was a silent no-op while `live.task` stayed None — now cancels the running step; a
cancelled step marks the plan `awaiting_owner` so the sweep can't re-claim it and the settle
helper can't re-arm. `/api/plans/{id}/stop` likewise sets `awaiting_owner` (the durable halt)
and cancels any in-flight step for the conversation, rather than `cancel_and_reset` (which
left `awaiting_owner=False`, so a settling turn re-armed and the loop "kept going"). The owner
resumes by sending a message (the owner-message reset re-opens the loop).

**Supervised budget (P4.1).** The per-turn guardrails (`max_steps` / `max_cost_tokens`) are a
sensible bound for an *unattended* run, but they cut off a legitimately long step mid-work
with "hit the budget" / "too many steps". A turn is **supervised** when a foreground PWA client
is up watching it stream and can Stop it — the human, not a fixed cap, is then the loop's
anchor — so it earns a much larger *finite* backstop (`guardrails_for_effort(..., supervised=True)`
→ `SUPERVISED_MAX_STEPS` / `SUPERVISED_MAX_COST_TOKENS`; the consecutive-error cap and these
ceilings still stop a genuinely wedged run, so it is never an unbounded loop). Two sources set
it: a **`/chat` turn is supervised by definition** (the owner just sent it from an open client);
a **continuation step is supervised only while a client is present** — proven by a recent hit on
the live-run poll (`session_live_run` stamps `app.state.plan_presence[session_id]`; the sweep's
`PlanContinuationRunner._client_present` treats it fresh within `PRESENCE_TTL_S`). A backgrounded
or closed app stops the ~4s poll, so presence ages out and the next step falls back to the
ordinary bounded budget — and a **scheduled background task never passes `supervised`**, so
headless runs keep their bounds.

## 6. PWA surfaces (P3, retargeted in P4)

The plan's live state + controls live in one shared hook/component pair
(`usePlanState` + `PlanBody`, `agent/views/registry.tsx`): the body (Markdown) + a
flag-enum status chip + the checklist; owner **Approve**/**Edit** when `not_approved`;
the live **auto-resume countdown** (polling `GET /api/plans/{id}` → `continuation_due_at`)
with **Continue now** / **Stop** when `in_work`; a distinct "Waiting for you" state when
`awaiting_owner`; and a derived **"complete"** state (all steps `- [x]`) the stored status
can't carry. The derived state gives **`not_approved` the highest precedence** (P4.2): a
draft always renders as a draft (Approve/Edit), even if a stray `awaiting_owner` is set on
it — the await-owner state must never suppress the Approve control and strand the owner.

**Where it renders (P4 — off the transcript):** the big card was crowding the chat and
fighting other tool views (deep-research cards), so it no longer lives inline once approved:

- **`plan_card` tool-view — kept on the drafting turn, dropped on step turns (P4.1).**
  `FullBrainSurface`'s `viewsToRender` keeps the inline `plan_card` whose OWN frozen payload
  status is `not_approved` — the card on the original turn where jerv drafted the plan and the
  owner approved it. That card stays put and, being live (`usePlanState` reconciles), shows the
  plan's current state (Approved → Working → Complete) as its anchored transcript record. The
  `plan_card`s jerv re-emits on later CONTINUATION step turns (each `write_plan` tick, emitted
  while `in_work`) are dropped, so the working plan doesn't stack a fresh card down the chat on
  every step — it lives behind the composer pill's popover and the status-line countdown below.
  (Earlier the gate keyed off the single live session status and dropped *every* card once
  approved, including the original; keying per-card off the view payload restores the original
  as the approved record while still hiding the step-turn churn.)
- **Between-steps countdown on the status line (P4.1).** While a continuation is armed but not
  yet firing, the status line above the composer (`AgentStatusLine`) — normally "Thinking / a
  tool / Answered" — shows an interruptible **"Starting next step in m:ss"** with an inline
  **Stop**, so the auto-continuation is visible and steerable right where the owner is looking,
  not only in the plan popover. A live turn phase always wins the line; the countdown takes over
  only once the turn has settled (`planWaitingStatus`, fed the live `usePlanState` countdown by
  `HomeScreen`).
- **Composer-foot plan pill → popover.** The always-visible pill now reads the LIVE derived
  state (so it flips to **"plan complete"** instead of stalling on "working to plan"), and is
  a **button**: tapping it opens the plan in a bottom **`PlanSheet`** (the shared `Sheet`) —
  the in-work status, checklist, countdown, and Continue/Stop — which the owner dismisses.
  One `usePlanState` instance (in `HomeScreen`) drives both the pill and the popover.
- **Chats-picker badge** — the other out-of-card status surface, driven by
  `SessionOut.plan_status`, refreshed after each settled turn.

## 7. Known limitations (accepted, bounded)

- **Post-approval body edits are not re-gated.** Once a plan is `approved`/`in_work`,
  jerv may rewrite `body` (e.g. tick a step, or add a new `- [ ]`) without a fresh
  approval — a deliberate softness, because auto-reverting on *any* body change would
  break the legitimate check-off flow. The blast radius is bounded: jerv holds no
  knowledge-base or owner data, its web egress is already direct, the live `plan_card` is
  owner-visible, and any owner message resets the loop. jerv's prompt is told to set
  `not_approved` again for changes big enough to need re-sign-off. A future hardening (a
  body hash captured at approve time surfaced as a "changed since approval" signal on the
  card) is noted but not built.
- **A narrow owner-turn / continuation race is closed structurally**, not merely by
  timing: `/chat` marks `turn_starting` the instant it passes its concurrency guard (with
  no `await` before the mark), and the sweep's guard-and-reserve is likewise await-free, so
  the two atomic sections can't interleave to start two turns on one session. A marker a
  failed setup leaks ages out via `TURN_STARTING_TTL_S`.

## 8. Tests

Real Postgres via testcontainers; LLM faked. The mandated RLS-isolation test
(`test_agent_session_plans_rls`: owner round-trip, non-owner sees nothing / can't write,
cascade delete, the approval state machine, and the **P4.2 draft-approvability** guards —
pausing a draft never sets `awaiting_owner`, and `approve` clears a stale flag). The
frontend precedence (a `not_approved` draft with a stale `awaiting_owner` still renders
Approve, never "Waiting for you") is covered in the `registry` `plan_card` vitest suite.
Continuation coverage
(`test_plan_continuation`: schedule + atomic claim, the awaiting/non-in-work/cap guards,
the settle gate, the owner reset, the `await_owner` opt-out, the **live-streaming**
regression — a continuation registers a real `_LiveTurn` keyed by run_id, emits `data:`
frames, and is discoverable via the registry, then drained — and the **supervised-budget**
gate: a step run with a fresh `client_presence` entry passes `supervised=True` to `run_turn`,
a stale/absent one keeps the bounded budget, and `session_live_run` stamps the presence).
The supervised guardrails themselves are unit-covered in `test_agent_loop`
(`guardrails_for_effort(..., supervised=True)` lifts to the finite `SUPERVISED_MAX_*` ceilings,
ignores `scale`, and keeps the error cap). Unit coverage
(`test_plantools`: the tool guards, web-gating/jerv-only, the checklist helper;
`test_loop_turn_executor`: the `acc`/`on_event` streaming sink on `run_turn`); the
sidecar + prompt version pins are updated in the same change. Frontend (vitest):
`useFullBrain` (the live-continuation discovery poll), `FullBrainSurface` (the original
draft-emitted `plan_card` is kept and reconciles after approval while a step-turn card is
dropped; and `AgentStatusLine` renders the between-steps countdown with an interruptible Stop),
`status` (`planWaitingStatus`), `Omnibox` (the plan pill is a tappable popover trigger), and the
existing `registry` `plan_card` suite.

## 9. Step-results scratchpad + instant start (P5)

Two gaps surfaced once plans ran end to end: a completed step's findings lived only in that
turn's collapsed tool trace (invisible in the chat, unavailable to later steps as a clean
synthesis), and approve/Continue idled the ~60s window before the first step actually started.

**Results scratchpad (append-only, index-ordered).** A `results jsonb` column on
`agent_session_plans` (migration 0157) holds an array of `{heading?, note}` entries.
`write_plan_result(note, heading?)` (jerv-only, `web` class) APPENDS one entry per call and
never rewrites an earlier index — so no later step can erase a prior step's results from view
(the owner's explicit requirement; simpler than a diff/version store, and each entry is its own
per-step attribution). `read_plan` returns the whole scratchpad, and it is re-injected each
turn (`_plan_blocks` for owner turns, `_continuation_conversation` for continuation steps), so
every step reads all prior results and the FINAL step reads the whole thing to write the
deliverable. The jerv prompt + the continuation seed tell jerv to record each finished step's
synthesis before ticking it `- [x]`. It renders as a per-step **Results** section in the plan
card (`PlanBody`), so a research step's findings are visible in one place instead of buried in
its tool trace. `PlanRepo.append_result` is read-modify-write (safe — turns are serialized per
plan by the single-turn guard + the sweep's atomic claim); an entry is capped at
`_MAX_RESULT_CHARS`.

**Instant start.** Approve arms the first continuation due-now, but the periodic sweep
(`SWEEP_INTERVAL_S`) meant up to a sweep's delay before it ran — and, worse, if the owner
approved while the draft turn was still finishing, `_run_one` found the session busy and
re-armed the FULL `CONTINUATION_DELAY_S` (the observed ~60s wait). Two fixes: the busy/cap
re-arm now uses `BUSY_RETRY_DELAY_S` (due-now, retry next sweep — it isn't pausing for the
owner, just waiting for the session to free), and the approve/Continue endpoints call
`PlanContinuationRunner.schedule_kick()` — an on-demand, fire-and-forget sweep tick (task
reference held so it isn't GC'd; the atomic claim keeps it from double-running with the
periodic sweep) — so the step starts immediately when the session is free.

Tests: RLS integration (`test_results_scratchpad_is_append_only`: append order + no-clobber via
the repo and the `write_plan_result` handler, `read_plan` surfaces it, empty-note refused);
continuation integration (`test_schedule_kick_runs_a_due_step_immediately`,
`test_busy_session_rearms_promptly_not_the_owner_window`); vitest (the card renders the
append-only Results section). The jerv prompt version pin is bumped in the same change.

## 10. Execution correctness + polish (P6)

Live testing surfaced that a plan could tick nothing and produce nothing: jerv recorded a
result but never checked the box off, never flipped `in_work`, ran multiple steps in one turn,
and wrote a `- **Step 4 – Write Guide**:` heading with nested sub-checkboxes that the card
couldn't render. Fixes:

- **Recording a result IS the step completion.** `write_plan_result` → `PlanRepo.complete_step`
  appends the entry AND deterministically advances the plan: `tick_next_step` flips the first
  unchecked `- [ ]` to `- [x]`, and `approved` flips to `in_work` on the first recorded step. So
  progress no longer depends on jerv remembering a separate `write_plan` check-off (the observed
  failure: body stayed all `- [ ]`, status stuck at `approved`). The tool description + prompt
  say so, and tell jerv to do exactly one step per turn then end it.
- **Plan-body normalization.** `normalize_plan_body` (applied when jerv writes the body) turns
  a plan into a FLAT `- [ ]` checklist — every list item (a bare bullet, an indented sub-item,
  or an existing checkbox at any depth) becomes a top-level step; headings/prose pass through;
  checked state is preserved; idempotent. This fixes the step-as-a-heading render bug and makes
  every step tickable. **Exception — a trailing notes section (P9.1):** once the body hits a
  prose-section heading (`_PROSE_HEADING`: a bare `**Notes**` / `## Caveats` / `Notes:` label),
  the rest of the body is jerv's explanatory prose (caveats, reminders), so its bullets pass
  through UNCONVERTED — they render as notes via the frontend's `parsePlanBody` prose bucket, not
  as steps. Before this, a plan's `**Notes**:` bullets ("Await your approval before starting")
  each became a phantom checklist step, inflating the count (0 of 8 instead of 0 of 5) and feeding
  non-actions into the loop. The jerv prompt (v39) also tells jerv to keep caveats in its reply,
  not the body, so the common case has no notes section in the body at all.
- **Per-step timing (P9.2).** The plan row carries a nullable `step_started_at` (migration 0159).
  The continuation stamps it (`mark_step_started`) as a step's turn begins — right after counting
  the fire — and `complete_step` records the step's elapsed `secs` onto that step's results entry,
  then clears the stamp. The card + omnibox modal show it as a compact duration ("2m 14s") on each
  step, so the owner sees the time spent on each (a candidate-report step is a multi-minute
  deep-research run). A step finished with no stamp (e.g. a foreground turn) simply carries no time.
- **Step-advance safety net (P9.4).** The loop advances a step only when its `- [ ]` box ticks —
  which happens via `write_plan_result`. If jerv finished a step's work but skipped that call
  (common when the step's action was a single `deep_research` call that already produced+persisted
  a report, so jerv just recapped), the box never ticked and the continuation re-ran the SAME step.
  Now: after a `done` continuation turn that produced a visible answer but recorded no result (the
  `results` array didn't grow), the continuation deterministically records that answer via
  `complete_step` — ticking the box, keeping `results` index-aligned to steps, and advancing.
  Skipped when jerv did record, paused for the owner, or produced nothing; the seed also reminds
  jerv that running the tool doesn't tick the box, only `write_plan_result` does.
- **PWA polish.** The card's between-steps countdown/controls no longer flash on an immediate
  start: the countdown shows only for a real wait (`remainingMs >= MIN_COUNTDOWN_MS`), and `now`
  is re-anchored on every plan fold so an approve/Continue armed due-now reads ~0. The step-text
  is inline Markdown (so a step's `**bold**` renders, not literal asterisks). The Results section
  is collapsible, **default closed**, and Markdown-rendered (wide tables scroll) in both the
  inline card and the omnibox modal (they share `PlanBody`).

Tests: unit (`test_plantools`: `tick_next_step`, `normalize_plan_body` flatten + idempotent, and
a notes section stays prose + real "Note…" step text is not mistaken for a heading (P9.1));
RLS integration (`write_plan_result` appends + ticks + sets in_work with no clobber; `write_plan`
normalizes a heading/nested body to a flat checklist; `mark_step_started` stamps + `complete_step`
records/clears the elapsed `secs`, and an unstamped step carries no time (P9.2); the settle
auto-records a step when jerv skipped write_plan_result, and does NOT double-record when it did
(P9.4)); vitest
(`registry`: the due-now countdown is suppressed; the Results section defaults collapsed and opens
on click; a step's duration renders when timed (P9.2)). jerv prompt version + digest and the
`write_plan_result.tool` sidecar pin bumped in the same change.

## 11. Multiple independent plans per conversation (P7)

Live testing surfaced crosstalk: drafting a NEW plan in a chat that already ran one showed the
OLD plan's results and the omnibox pill didn't track the new plan — because the table was
primary-keyed by `session_id` (one plan per chat), so a second plan overwrote the first's row and
inherited its scratchpad. Fix: **re-key the table on a per-plan `plan_id`** (migration 0158) so a
conversation holds MANY plans, each with its own body, status, and results.

- **Data model.** `agent_session_plans.plan_id uuid` is the PK (default `gen_random_uuid()`);
  `session_id` becomes a NOT-NULL cascade FK (now non-unique), with a `(session_id, created_at)`
  index. The **active** plan of a session is the latest-created one. RLS/grants are unchanged (the
  owner-only FORCE policy rides the same table).
- **Repo.** `PlanRepo` is plan-id-native for reads/mutations (`get_by_id`, `approve`,
  `complete_step`, `schedule_continuation`, …), with `get_active`/`active_plan_id` resolvers for
  the session-scoped callers (the tools, the re-injection, the settle hook). `create` starts a new
  plan AND clears the previous active plan's pending continuation — so only the active plan
  auto-continues (the invariant that lets the session-scoped loop and "the active plan" never
  disagree). `claim_due_continuations` returns `(plan_id, session_id)` pairs; `_run_one` re-reads
  by `plan_id` and skips a plan that is no longer the session's active one (a newer plan was
  drafted after the claim — its tools would otherwise do cross-plan work).
- **write_plan create-vs-edit.** A fresh `not_approved` body written while the active plan is
  already `approved`/`in_work` starts a SEPARATE plan (empty results); refining a still-unapproved
  draft, or jerv rewriting the running plan's checklist, edits in place. The prompt (v37) tells
  jerv a fresh `not_approved` draft is what begins a new plan.
- **API.** Reads/mutations key on `plan_id` (`GET/POST /plans/{plan_id}…`), plus
  `GET /plans/session/{session_id}/active` for the omnibox to resolve the current plan. `PlanOut`
  and the `plan_card` view payload carry `plan_id`. approve/continue arm the loop only when the
  plan is the session's ACTIVE one (approving a superseded plan would arm a countdown the sweep's
  active-guard then drops). The Chats-list `plan_status` badge subquery resolves the ACTIVE plan
  (`ORDER BY created_at DESC LIMIT 1`) — a bare scalar over many plan rows would 500 the list.
- **PWA.** `usePlanState` runs in two modes: an inline card is BOUND to its own `plan_id` (polls
  `getPlan(planId)`, so an old card keeps its own history after a newer plan supersedes it); the
  omnibox pill/popover RESOLVES the active plan each poll (`getActivePlan(sessionId)`), so it
  follows a newly-created plan. Also: the per-step Results scratchpad now folds **per step** —
  each finished step is its own collapsible (default closed), rather than one combined section.

Tests: RLS integration (a new draft over a running plan starts a fresh plan with empty results and
clears the old plan's continuation; refining an unapproved draft stays one plan; cascade deletes
all a chat's plans); continuation integration (claim returns `(plan_id, session_id)`; a superseded
plan is not run); vitest (`registry`: each step-result is its own collapsible, opening one leaves
the others folded). Migration 0158 + jerv prompt v37 digest bumped in the same change.

## 12. Context-usage meter during continuations (P7)

The context meter at the foot of the omnibox froze during plan work: it stayed at the last
foreground `/chat` value while continuations ran, and could show near-empty on reopen. Root
cause: `LoopTurnExecutor.run_turn` (the engine continuations + scheduled tasks share) never passed
`context_window` into `AgentLoop.run_stream`, and the loop **suppresses every `UsageEvent` when
the window is `None`** — so a reattached client streaming a continuation saw no usage frames, and
nothing persisted the fill for reopen. Fix:

- **run_turn resolves and passes the window.** It now `await router.context_window("agent.turn")`
  (exactly as `/chat` does) and threads it into `run_stream`, so the loop emits `UsageEvent`s on
  the continuation/task path — a reattached client's meter moves live again. It captures the
  fullest step's `input+output` and returns `context_used`/`context_window` on `ExecutedTurn`.
- **The continuation persists the reopen seed.** `PlanContinuationRunner` gained a `sessions`
  (`AgentSessionRepo`, wired in `main.py`); after a turn settles it calls `record_context` so a
  client that reopens the chat AFTER the live stream is gone sees the true fill, not a stale
  value. `TaskRunner` does the same for the session a scheduled task creates.

Tests: unit (`test_loop_turn_executor`: run_turn passes the resolved window into the loop and
returns the fullest step's fill); continuation integration (a continuation persists
`context_tokens`/`context_window` onto the AgentSession row).

## 13. Per-step turn acknowledgement (P8)

Live testing showed a non-final continuation step rendering an EMPTY agent turn: the step's
synthesis went into the `write_plan_result` tool call, and the continuation seed told jerv to
"produce its real output … then END YOUR TURN" and NOT to write a status line — so the visible
reply was blank except the system's "next step will be processed automatically" line, reading as
a stall. Fix (seed + jerv prompt v38): for a NON-final step, jerv's visible reply must be ONE
short, specific sentence recapping what that step produced (e.g. "Step 1 done — compared 5
carry-ons; front-runners are …"), so the turn shows progress at a glance — while the full findings
stay in the recorded result, not the chat. The FINAL step is unchanged: it still writes the whole
deliverable in full (never a one-line recap). The earlier "no status line" guard was really
against replying *instead of* doing the work; a one-line recap of completed, recorded work is
wanted. Tests: `test_continuation_seed` asserts the non-final recap directive alongside the
final-deliverable directive; jerv v38 digest + version pin bumped.
