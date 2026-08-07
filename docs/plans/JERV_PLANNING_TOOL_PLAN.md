# jerv Planning Tool — owner-approved plans, executed across turns

> **Status:** In progress · **Last verified:** 2026-08-06 · **Waves:** P1✅ P2✅ P3✅ P4◻️ (P1 = the planning tool: table + RLS, read_plan/write_plan, owner-only approval, per-turn re-injection, prompt. P2 = the auto-continuation runtime: the settle hook, the in-process sweep, the `pause` control, the /chat hooks. P3 = the PWA surfaces: the `plan_card` view, the composer-foot plan pill, the Chats badge, the auto-resume countdown. P4 = **live + tidy** (on-branch, pre-merge): continuation turns now STREAM live into the chat via the reattach broker (§5), and the plan surface moved off the transcript — the inline card is draft-only, the in-work plan lives behind the composer pill's popover (§6). Shipped with unit + RLS-isolation + integration + vitest coverage.)

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

## 2. Data model — `app.agent_session_plans` (migration 0155)

One row per chat, keyed by `agent_sessions.id` (`ON DELETE CASCADE`), owner-only RLS
(`app.is_owner()`, FORCE) like `archivist_memory` — a plan is conversation-local, **not**
knowledge-base data, so it carries no domain and jerv (empty-scoped) still reaches it
because the firewall is ownership, not a domain scope.

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
  `await_owner` (blocked — stop the loop until the owner replies).

Both are added to `JERV_TOOLS`; the `web` class means `curator` never sees them. A tool
result returns a `plan_card` **view** so the card renders inline (data-only, never
model-authored markup — invariants #1/#9).

## 4. Approval + re-injection

- **Owner approval** is a UI gesture / endpoint (`POST /api/plans/{id}/approve`), never a
  jerv tool. The owner may edit the draft first (`/edit`). Approval also **arms the first
  continuation**, so the sweep starts jerv on the plan (~15s) — approval alone otherwise
  just sets the status and the plan sits, because nothing tells the agent it was approved.
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
  re-arms and retries next sweep.
- **Terminate:** the chain stops when the checklist is fully `- [x]`, the status leaves
  `in_work`, jerv sets `await_owner`, the owner sends anything, or `MAX_CONTINUATIONS` (20)
  is hit. Each hop is a discrete, separately-recorded, guardrailed turn — bounded per the
  "no unbounded autonomous loop" invariant.

Owner controls (`/api/plans/{id}/stop|continue`) cancel the pending continuation or fire
the next step now.

## 6. PWA surfaces (P3, retargeted in P4)

The plan's live state + controls live in one shared hook/component pair
(`usePlanState` + `PlanBody`, `agent/views/registry.tsx`): the body (Markdown) + a
flag-enum status chip + the checklist; owner **Approve**/**Edit** when `not_approved`;
the live **auto-resume countdown** (polling `GET /api/plans/{id}` → `continuation_due_at`)
with **Continue now** / **Stop** when `in_work`; a distinct "Waiting for you" state when
`awaiting_owner`; and a derived **"complete"** state (all steps `- [x]`) the stored status
can't carry.

**Where it renders (P4 — off the transcript):** the big card was crowding the chat and
fighting other tool views (deep-research cards), so it no longer lives inline once approved:

- **`plan_card` tool-view — draft only.** `FullBrainSurface`'s `viewsToRender` drops the
  inline `plan_card` unless the live `plan_status === "not_approved"`, so the card shows only
  while the owner still needs to **Approve/Edit** the draft, then disappears.
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
cascade delete, the approval state machine). Continuation coverage
(`test_plan_continuation`: schedule + atomic claim, the awaiting/non-in-work/cap guards,
the settle gate, the owner reset, the `await_owner` opt-out, and the **live-streaming**
regression — a continuation registers a real `_LiveTurn` keyed by run_id, emits `data:`
frames, and is discoverable via the registry, then drained). Unit coverage
(`test_plantools`: the tool guards, web-gating/jerv-only, the checklist helper;
`test_loop_turn_executor`: the `acc`/`on_event` streaming sink on `run_turn`); the
sidecar + prompt version pins are updated in the same change. Frontend (vitest):
`useFullBrain` (the live-continuation discovery poll), `FullBrainSurface` (the inline
`plan_card` is draft-only), `Omnibox` (the plan pill is a tappable popover trigger),
and the existing `registry` `plan_card` suite.
