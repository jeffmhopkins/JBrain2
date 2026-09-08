> **Status:** Research · **Last verified:** 2026-09-08

# B5 — Runtime and orchestration for the agent-ingest conversation

Where a per-note agent conversation runs, how it shares one 128 GB box with the
owner's own chat, and what has to exist before the deterministic
extract → Integrator → arbiter → apply pipeline can be deleted.

Scope: runtime + orchestration only. What the tools *do* to the graph, the prompt,
and the confidence split are other B-docs. Every claim below is either **verified**
(a `path:line` I read on this branch, `claude/agent-predicate-db-redesign-xog54v`)
or explicitly marked **assumed**.

---

## 0. Recommendation, first

**Build a new agent-turn runner, host it in the worker process, dispatch it one
turn at a time through the existing Postgres job queue, and enter it through the
workflow engine's existing `note.ingested` trigger. Do not model the conversation
itself as a workflow pipeline.**

Concretely:

| layer | what it is | why |
|---|---|---|
| **entry** | the shipped engine trigger `note.ingested → integrate pipeline` (`backend/migrations/versions/0040_seed_event_triggers.py:49`), with its one action swapped from `integrate_note` to `ingest_converse` | the engine already owns "a note finished ingesting"; nothing new is needed to *start* a conversation, and the Automations screen keeps showing it (`backend/src/jbrain/api/ops.py:190`) |
| **state** | a new durable `ingest_conversations` row per note: status, turn count, budget spent, model/prompt version, the open question, the owner's answer | modelled on `app.research_run_state` (`backend/src/jbrain/external/research_run_state.py:1-10`) and `app.intake_sessions` (`backend/src/jbrain/intake/repo.py:255`) — both already do "durable background agent run, resumable after restart, one in-flight turn claimed atomically" |
| **execution** | one `app.jobs` row = **one bounded agent turn**, not the whole conversation | keeps `STALE_LOCK`'s "no handler runs anywhere near 10 minutes" assumption true (`backend/src/jbrain/queue.py:28-30`); a turn that runs out of budget re-enqueues a continuation, exactly as plan continuations already chunk long work (`backend/src/jbrain/agent/continuation.py:1-24`) |
| **turn engine** | `AgentLoop.run_stream` via a `LoopTurnExecutor`-shaped adapter (`backend/src/jbrain/tasks/runner.py:98-181`) | the guardrails, tool dispatch, error-as-observation and usage accounting already exist (`backend/src/jbrain/agent/loop.py:1-8`) |
| **transcript** | `agent_sessions` + `agent_turns` (`backend/src/jbrain/models/agent.py:26,160`), one session per note | reopening the conversation in the PWA is then the *existing* chat surface, not a new one |
| **question** | a `review_items` row of a new `kind` (`backend/src/jbrain/models/analysis.py:208`), surfaced by `GET /api/review` (`backend/src/jbrain/api/analysis.py:137`) | the review inbox is already a **silent** badge-count queue with no push — this is the owner's stated requirement, already built |

### Why not the workflow engine for the conversation itself

The engine is a *dispatch* layer, not an execution substrate — its own docstring
says so: *"It owns no new execution machinery … each step is enqueued through the
existing `queue.enqueue` exactly as a hardcoded trigger would"*
(`backend/src/jbrain/workflow/scheduler.py:6-9`). Four properties make it a bad fit
for an interactive, resumable, human-in-the-loop conversation:

1. **Pipelines are linear, statically-parameterised action lists.** A step is
   `{action, action_version, params}` with params *"bound at definition time"* and
   *"a DAG is deferred"* (`backend/src/jbrain/workflow/contracts.py:81-92`). A
   conversation's next step is chosen by the model at run time. You would be
   encoding "the agent decides" as a one-step pipeline that loops on itself — the
   engine adds ceremony and zero structure.
2. **There is no suspended-run state.** A `runs` row is
   `running | done | error | superseded` (`backend/src/jbrain/workflow/runlog.py:139-149`,
   `backend/src/jbrain/models/agent.py:112`). "Waiting three months for the owner"
   has no representation, and a run left `running` is exactly what the stranded-run
   reaper is built to kill (`backend/src/jbrain/agent/runlog.py:38` `STRANDED_AFTER_SECONDS = 3900`).
3. **"Latest run wins" is actively wrong here.** `supersede_running_runs`
   cancels a pipeline's still-queued jobs and marks its runs `superseded` whenever a
   newer fire of the same pipeline starts (`backend/src/jbrain/workflow/runlog.py:103-122`).
   A second note arriving would supersede the run holding an unanswered question.
4. **The engine's per-run bookkeeping assumes fan-out-then-finish.**
   `finalize_job_step` closes the parent run *"once ALL its steps' jobs are terminal"*
   (`backend/src/jbrain/workflow/runlog.py:206-222`). A conversation has an
   open-ended, run-time-determined number of steps.

The engine *did* carry the ingest/integration cutover (dispatcher is LIVE by
default, `backend/src/jbrain/workflow/dispatcher.py:56-65`), and that is precisely
why it should keep the **entry** seam: `note.created → ingest_note` and
`note.ingested → <the conversation starter>` stay data-defined, operator-visible,
and re-fireable from Ops (`POST /ops/triggers/{id}/run`,
`backend/src/jbrain/api/ops.py:290`). Everything after turn 0 is not pipeline-shaped.

### Why not the job queue alone

The queue is the right *executor* and the wrong *model*. It gives, verified:
`SELECT … FOR UPDATE SKIP LOCKED` claim with a stale-lock reaper
(`backend/src/jbrain/queue.py:369-429`), exponential backoff capped at an hour
(`backend/src/jbrain/queue.py:26,146-151`), a **defer that burns no attempt**
(`backend/src/jbrain/queue.py:496-527`), a scope stamp so a job can run narrowed
rather than as system (`backend/src/jbrain/queue.py:46-61`,
`backend/src/jbrain/worker.py:113-122`), per-job token + log capture
(`backend/src/jbrain/worker.py:207`), and a box-hold pause that stops every
model-loading background job while the coder or a jmolt night owns the box
(`backend/src/jbrain/worker.py:472-478,549`).

What it does **not** give, and what this design must add:

- **No priority.** The claim is `ORDER BY run_after` over an index of
  `(status, run_after)` (`backend/src/jbrain/queue.py:389`,
  `backend/migrations/versions/0003_jobs_chunks_ingest_state.py:49`). Interactive
  work never entered `app.jobs`, so none was needed. It is needed now.
- **No conversation identity.** Dedup is payload-keyed string matching
  (`has_active`, `backend/src/jbrain/queue.py:190-219`).
- **A 10-minute liveness assumption.** `STALE_LOCK` is justified by *"no handler
  runs anywhere near 10 minutes"* (`backend/src/jbrain/queue.py:28-30`). One
  bounded turn honours that; a whole conversation would not.

---

## 1. Process placement: worker, not api

The api process owns the interactive agent machinery — `live_turns`
(`backend/src/jbrain/main.py:313`), the `turn_starting` marker
(`backend/src/jbrain/main.py:315`), `WarmKeeper` (`backend/src/jbrain/main.py:1375`),
the plan-continuation sweep (`backend/src/jbrain/main.py:1323`), the deepest lane
and its boot resume (`backend/src/jbrain/main.py:1126`), the stranded-run reaper
(`backend/src/jbrain/main.py:1144`). The worker owns the job loop and nothing
agent-shaped: `backend/src/jbrain/worker.py:740-878` wires 30-odd handlers and
imports no `ToolRegistry`.

**Recommendation: worker.** Reasons, in order:

1. The worker is the only process that already **stops for a box reservation**
   (`backend/src/jbrain/worker.py:472-478`). Ingestion is exactly the work that must
   yield the box; putting it in the api would mean re-deriving that pause.
2. The worker is **single-threaded by design** (`backend/src/jbrain/worker.py:3`) —
   one background local turn at a time is the serialization property we want, free.
3. Token + structured-log capture per unit of work is already wired around
   `process_one` (`backend/src/jbrain/worker.py:207-287`), which is what makes the
   run-log honest instead of a 0-token placeholder.

**Cost of that choice (must be paid explicitly):** a `ToolRegistry` and the graph
tools must be constructed in `worker.py` alongside the existing handler wiring, and
the worker cannot use `_LiveTurn` — that broker is in-process, api-only
(`backend/src/jbrain/agent/live_turn.py:1-11`). See §5 for what replaces it.

**Rejected alternative:** a detached `asyncio.Task` lane in the api, like
`DeepestRunLane` (`backend/src/jbrain/agent/deepest_lane.py:36-72`). It is a proven
pattern with a wall-clock watchdog and a boot resume, but it deliberately runs
*outside* the worker's job machinery (`deepest_lane.py:10-12`) — which means outside
the box-hold pause, outside the precondition gate, outside the run-log's token
tally, and with `max_concurrent = 1` refusing rather than queueing
(`deepest_lane.py:30-33,67-69`). For bursty note capture, "refused, not queued" is
the wrong answer.

---

## 2. Serialization against the single resident model

This is the hard part, and it is mostly a **vision** problem, not a text problem.

### 2.1 What already exists (verified)

| capability | where | what it actually does |
|---|---|---|
| single evictor | `backend/src/jbrain/llm/residency.py:1-32` | llama-swap never evicts (`swap: false` group); the app's `ResidencyCoordinator` is the sole evictor. `ensure_room` evicts biggest-first until the new model fits under the free-RAM floor (`residency.py:855`) |
| cross-process eviction lock | `backend/src/jbrain/llm/residency.py:85-101`, key at `backend/src/jbrain/llm/admission.py:31` | a Postgres transaction advisory lock around the **eviction decision only** — deliberately *not* held across the load (`residency.py:60-67`: holding it across a load self-deadlocked the box on 2026-08-23) |
| declaration-based admission | `backend/src/jbrain/llm/admission.py:1-19`, ledger at `backend/src/jbrain/llm/ledger.py` | admits against declared rows, never live measurement; `DRAINING` keeps its full charge |
| device guard | `backend/src/jbrain/llm/gpu_guard.py`, raised at `backend/src/jbrain/worker.py:217-266` | `GpuBudgetError`; `permanent=True` (infeasible) fails the job, otherwise it **defers with no attempt burned** |
| box hold | `backend/src/jbrain/settings_store.py:1049-1053`, honoured at `backend/src/jbrain/worker.py:472-478` and `backend/src/jbrain/worker.py:663` | code mode OR a jmolt night pauses *all* background work and refuses *any* background load |
| "don't force a swap" precondition | `backend/src/jbrain/workflow/preconditions.py:46-73`, wired at `backend/src/jbrain/worker.py:920-922` | `model_already_loaded` — the job runs only if the model it will resolve to is already in `gateway.running()`; unmet → `queue.defer` (`worker.py:317`), fixed 5-minute retry (`preconditions.py:30`), no attempt burned. **Used by exactly one action today** (`triage.classify`) |
| interactive concurrency caps | `backend/src/jbrain/api/agent.py:123,725,727` | 4 concurrent detached turns box-wide; 409 on a second turn for the same session |
| primed interactive prefix | `backend/src/jbrain/llm/warm_keeper.py:1-30`, `backend/src/jbrain/llm/kv_prefix.py:1-10` | the keeper keeps `agent.turn`'s ~29k-token persona+tools prefix hot **and** saves it to disk for a ~2 s restore instead of a ~60 s re-prefill |
| live per-task routing | `backend/src/jbrain/settings_store.py:827-842`, router precedence at `backend/src/jbrain/llm/router.py:428-500` | `llm_task_overrides` outranks tier and task default; on the live box **all 19 selectable tasks resolve to on-box models — 16 to `gpt-oss-120b`, 3 vision to `qwen3.8-27b-abliterated`** (`docs/reference/MODEL_ACCESS_INVENTORY.md` §E) |
| prefill diagnostic | `backend/src/jbrain/llm/prefill.py:1-30`, wired at `backend/src/jbrain/worker.py:709` and `backend/src/jbrain/llm/router.py:861` | the box's longest silence; a load's priming warm-up measured at **118 s of a 198 s load span** |

### 2.2 The load-bearing observation

**An ingestion conversation on `gpt-oss-120b` causes no model swap.** 16 of 19
live tasks already resolve there (§E of the inventory), including `agent.turn`. If
the ingest conversation is routed to a *new* task name that resolves to the same
served model, background ingestion never evicts anything: it contends for **slots
and GPU time**, not for residency. That is the single largest serialization win
available and it costs nothing but a routing decision.

The corollary is the whole priority design:

- **Text ingestion → slot/time contention.** Solve with ordering + an
  interactive-presence gate.
- **Vision (OCR, captions, video) → swap contention.** ~69 GB out, ~22-33 GB in,
  then back. Solve with **batching**, not ordering.

### 2.3 What is missing (the gap list)

**G1 — no priority column on `app.jobs`.** Claim is `ORDER BY run_after`
(`backend/src/jbrain/queue.py:389`). *Add* `priority smallint NOT NULL DEFAULT 100`
and claim `ORDER BY priority, run_after`, with the index widened to
`(status, priority, run_after)`. The rank-then-time shape is already blessed in this
codebase: `INTEGRATION_BACKFILL_ORDER_BY` is
`"(n.provenance = 'untrusted_origin'), n.created_at"`
(`backend/src/jbrain/queue.py:573-580`) — owner-ahead ordering, live since Phase 7.
Proposed bands: `10` owner-answered conversation resume · `50` vision batch drain ·
`100` fresh ingest conversation · `200` reconcilers/housekeeping.

**G2 — the worker cannot see that the owner is chatting.** `live_turns` and
`turn_starting` are in-process api state (`backend/src/jbrain/main.py:313-315`);
the worker has only `queue.running_kinds` (`backend/src/jbrain/queue.py:260`), which
looks the wrong way. *Add* a cross-process presence marker — the cheapest correct
version is the pattern already used for the box hold: a settings row the api stamps
at `/chat` turn start (right where `turn_starting` is set,
`backend/src/jbrain/api/agent.py:734`) carrying an expiry, read by the worker.
Express it as a **precondition** (`no_interactive_turn`) on the ingest-turn action,
*not* as a second `held` pause: a precondition defer costs no attempt, surfaces its
reason on the run's progress note (`backend/src/jbrain/worker.py:316-318`), and is
per-action, so a busy chat day does not also stop embedding, purges and reconcilers
the way `box_hold_names` does (`backend/src/jbrain/worker.py:472-478` pauses
*everything*).

**G3 — vision work is unbatched, one job per attachment.** `ingest/pipeline.py:321`
enqueues one `ocr_attachment` per image (or per scanned PDF,
`ingest/pipeline.py:259`); each handler independently resolves `vision.ocr` and can
force a swap (`backend/src/jbrain/worker.py:748-750`). Forty photo notes syncing at
once is up to forty swap pairs. Two-part fix, both reusing shipped primitives:

- *Don't start a swap you weren't going to pay for*: give the vision action the
  existing `model_already_loaded` precondition shape
  (`backend/src/jbrain/workflow/preconditions.py:46`), keyed on `vision.ocr`.
- *Once you've paid, amortize it*: a **drain window**. The first vision job in a
  window performs the load and writes a `vision_resident_until` marker; while the
  marker holds, the precondition is met for every other vision job and they claim
  in a row (G1's priority band `50` puts them ahead of fresh ingest conversations).
  The window closes on an empty vision queue, a deadline, or an interactive
  preempt. This is the piece with **no existing analogue** — `model_already_loaded`
  gives the "don't force it" half only.

**G4 — background text turns can evict the primed interactive KV prefix.**
`kv_prefix.py:4-8` is explicit: *"a dedicated second slot keeps background traffic
from evicting it — but … a single-slot configuration … loses the prefix to any
background task."* Slot count is an operator setting
(`llm_local_parallel_slots`, read at `backend/src/jbrain/worker.py:630`). On a
single-slot box, every ingest turn costs the owner's next message a ~60 s prefill,
recoverable to ~2 s only because the disk store exists. **Assumed, not measured on
this box:** that ingest turns on the same served model will in practice evict the
jerv prefix at `-np 1`. Worth an explicit console measurement before shipping (§6).

**G5 — residency admission is first-come-first-served.** `ensure_room` has no
notion of *who* is asking (`backend/src/jbrain/llm/residency.py:855-914`). The only
priority mechanism on the box is the all-or-nothing box hold. This is tolerable
*given* §2.2 (ingest doesn't swap) and G3 (vision batches), and I would **not**
add priority to residency in this change — the risk of touching the admission
arithmetic is high and the plan's own history says so
(`backend/src/jbrain/llm/admission.py:1-19`: three prior attempts, three
double-counts).

**Not a gap:** whisper is a **separate llama-swap in the `tts-stt` container** with
its own models dir and `ttl: 300` — the config comment claiming otherwise is
recorded as a known contradiction (`docs/reference/MODEL_ACCESS_INVENTORY.md`,
"Contradictions" table). Audio transcription does not contend through the main
residency budget the way vision does. Embeddings are TEI in a 1 GB-capped CPU
container with no load/unload path in our code (same doc, §B.2).

### 2.4 The admission policy, stated

```
claim order        : ORDER BY priority, run_after          (G1)
interactive gate   : ingest-turn action carries precondition `no_interactive_turn`
                     -> defer 5 min, no attempt burned      (G2, reusing worker.py:290-319)
swap gate          : vision action carries `vision_model_loaded_or_window_open`
                     -> defer, or claim the whole batch     (G3)
box gate           : existing box_hold_names pause          (worker.py:472-478, unchanged)
memory gate        : existing ledger + gpu_guard;
                     transient refusal -> defer, infeasible -> permanent fail
                                                            (worker.py:217-266, unchanged)
routing            : ingest.converse -> the SAME served model as agent.turn,
                     so a background turn never evicts       (§2.2)
```

Owner turns never wait behind a backlog because owner turns **never enter
`app.jobs`** — `/chat` runs detached in the api (`backend/src/jbrain/api/agent.py:707`
onward). The queue backlog is invisible to them. What they *can* wait behind is a
single in-flight background turn holding a slot; that is bounded by the per-turn
budget in §3, and by the fact that the worker runs one job at a time.

---

## 3. Budgets, timeouts, non-termination, idempotency

### 3.1 Turn and tool-call budgets — reuse, don't reinvent

`Guardrails` already carries `max_steps` / `max_cost_tokens` /
`max_consecutive_tool_errors` (`backend/src/jbrain/agent/loop.py:125-133`, defaults
20 / 200 000 / 3), sized by effort and a per-agent multiplier
(`loop.py:199-224`), with a soft "you're nearly out, synthesize" nudge
(`loop.py:186-192`) and a forced-final synthesis at exhaustion (`loop.py:165-183`).
Per-tool ceilings exist as `ToolCallBudget` (`loop.py:225-260`), the mechanical
backstop for a persona whose prompt states a budget the model ignores.

For an ingest turn, **do not** use the supervised ceilings
(`SUPERVISED_MAX_STEPS = 500`, `loop.py:162-163`) — those exist because a human is
watching and can press Stop, which is exactly what an ingest turn lacks. Use the
ordinary effort-sized budget, and add two conversation-level caps that have no
existing analogue:

- `max_turns_per_conversation` — the ceiling on *continuations*, modelled on
  `MAX_CONTINUATIONS = 20` (`backend/src/jbrain/agent/continuation.py:64`).
- `max_cost_tokens_per_conversation` — modelled on the intake session's cumulative
  cap, which is enforced **in the same conditional UPDATE that claims the turn**
  (`backend/src/jbrain/intake/repo.py:255-284`). That is the right shape: one
  statement is simultaneously the concurrency cap, the turn cap and the cost cap,
  DB-enforced so it holds across processes.

### 3.2 Wall clock

`ASSISTANT.md` specifies `wall_clock_timeout (~60s interactive; slower work defers
to the job queue)` (`docs/reference/ASSISTANT.md:151-156`). The deepest lane
enforces a wall clock with `asyncio.wait_for` in its supervisor
(`backend/src/jbrain/agent/deepest_lane.py:74-91`). The job queue's own liveness
bound is `STALE_LOCK = 10 minutes` (`backend/src/jbrain/queue.py:30`).

**Set the ingest-turn wall clock below `STALE_LOCK`** — 5 minutes is the natural
number, matching `RETRY_AFTER` (`backend/src/jbrain/workflow/preconditions.py:30`).
A turn that overruns is cancelled by its own watchdog and re-enqueued as a
continuation, so the stale reaper never sees it and never burns a reclaim attempt
(`backend/src/jbrain/queue.py:139-143`).

### 3.3 Non-termination

Four independent stops, three of them already built:

1. step cap → forced-final synthesis (`loop.py:906-912`);
2. consecutive-tool-error cap (`loop.py:904-909`);
3. conversation turn cap (new, §3.1);
4. **an explicit terminal tool.** The model must be able to say "done" and
   "I need to ask" as *tool calls*, so termination is a state transition the
   harness records, not a text pattern it parses. `end_turn` alone is not enough
   — a conversation that ends every turn with prose and no state change would
   re-enqueue forever until the turn cap. Recommend `finish_note(summary)` and
   `ask_owner(question, context)`; the latter sets the conversation
   `awaiting_owner` and **does not re-enqueue**, which is what makes "silent
   queue" free rather than a feature.

This stays inside `ASSISTANT.md`'s *"no unbounded autonomous loop"* refusal
(`docs/reference/ASSISTANT.md:127`) by the same argument the plan continuation
makes for itself: each hop is a discrete, separately-recorded, step-capped turn
(`backend/src/jbrain/agent/continuation.py:19-24`).

### 3.4 Idempotency when a turn dies mid-tool-call

This is the sharpest new risk. Today the arbiter commits deterministically at the
end of a validated plan (`docs/reference/ARCHITECTURE.md:109-130`,
`backend/src/jbrain/analysis/pipeline.py:305-309`). Under the new design **each
graph-write tool commits on its own**, so a crashed turn leaves partial writes
already durable and the retry re-runs the same turn from the same conversation
state.

The queue cannot help here — it retries the *job*, and the job is now
side-effecting mid-flight. Three mechanisms, in order of importance:

1. **Persist tool results before the turn ends.** The transcript today is written
   once, after the turn completes (`backend/src/jbrain/agent/transcript_store.py:1-8`,
   "a completed turn writes the user message then the assistant answer … in one
   transaction"). For ingest, append each tool call **and its result** as it
   happens, so a retry resumes *after* the writes that landed instead of repeating
   them. This is the durable-checkpoint pattern `research_run_state` already uses:
   *"the run writes its committed state after each round, so a worker/box restart
   mid-run rehydrates and CONTINUES from the last committed round"*
   (`backend/src/jbrain/external/research_run_state.py:1-9`).
2. **Idempotency keys on the write tools.** Every graph write carries
   `(conversation_id, turn_index, call_index)`; the write is an upsert keyed on it.
   Then a duplicated call is a no-op rather than a duplicate fact. The
   `ActionSpec.dedup_key_expr` field exists as advisory metadata for exactly this
   idea but *"the handler still enforces write-once"*
   (`backend/src/jbrain/workflow/registry.py:44-50`) — so the enforcement is ours
   to write.
3. **Claim the turn atomically.** `intake/repo.py:255-284`'s single conditional
   UPDATE — with a stale-lock reclaim so a crashed turn never locks the
   conversation forever — is the exact pattern; copy it rather than inventing one.

**Verified constraint that shapes all of this:** the DB is disposable (owner
decision), and notes remain the sole source of truth
(`docs/reference/ARCHITECTURE.md:159-161`, "everything traces to a note"). So a
partial graph write is *recoverable by re-running the conversation from turn 0* in
the worst case. That materially lowers the bar — but only if a re-run is
convergent, which requires (2).

---

## 4. Resumption three months later

What must survive, and where it lives:

| what | store | status |
|---|---|---|
| the note | `app.notes` | exists |
| the conversation transcript | `app.agent_turns`, cascade-deleted with the session (`backend/src/jbrain/models/agent.py:160-183`) | exists |
| the open question | `app.review_items` (`backend/src/jbrain/models/analysis.py:208-224`) | exists |
| the model + prompt version the conversation ran under | `runs.prompt_version` + `runs.call_stamp` (`backend/src/jbrain/models/agent.py:119-125` — model, provider, effort, window, tool names, persona, all as one JSONB blob) | exists |
| conversation status / budget spent / turn count | — | **new table** |
| the raw tool-call / tool-result messages | — | **gap, see below** |

### 4.1 The rehydration gap

`ChatMessageIn` is documented as *"Only the text is carried — tool calls live
inside a single turn-loop, not across them"* (`backend/src/jbrain/api/agent.py:127-131`),
and `_conversation` rebuilds history as flat `UserMessage`/`AssistantMessage`
(`backend/src/jbrain/api/agent.py:657-682`). `AgentTurn.tools` is a rendered
"Worked" display shape, not provider tool-call messages
(`backend/src/jbrain/models/agent.py:178-179`).

So resuming an ingest conversation from the stored transcript **loses the tool
call/result structure**. For a chat that is fine — the answer text carries the
meaning. For ingestion it is not: "I already wrote three facts and asked about the
fourth" is exactly the state that must survive.

**Recommendation: don't rehydrate the old conversation. Re-derive it.** On resume:

1. Re-read the note body and its attachments' extracts from the DB (cheap,
   deterministic).
2. Re-read **what this conversation already wrote to the graph**, by
   `conversation_id` provenance on the facts/edges it created.
3. Build a *fresh* turn-0 context: `note + already-written + the question I asked +
   the owner's answer`, and run one bounded turn.

This makes resume immune to model change, prompt change, and tool-schema change,
because nothing from the old context is replayed into the model — the only carried
state is durable rows the current prompt can read. It is also the only version that
survives "the model was swapped out from under this conversation", which on a
three-month horizon is the likely case, not the edge case.

Record the transition: stamp the new run's `call_stamp` as usual, and if
`prompt_version` differs from the conversation's stored one, note it on the run so
the owner (and a future eval) can see the conversation changed hands. `runs`
already carries `prompt_version` non-null for `kind='agent'` by DB CHECK
(`backend/src/jbrain/models/agent.py:83-86`).

### 4.2 The stale-question problem

A three-month-old question may be unanswerable or moot — the note may have been
superseded by a later note, or deleted. Two shipped invariants apply:

- **Purge is total** (`docs/reference/ASSISTANT.md:101-104`): note deletion
  cascades to agent episodic memory. A deleted note's conversation must be deleted,
  not orphaned. `AgentSession → AgentTurn` already cascades; the new conversation
  row needs the same FK and a purge test (`backend/src/jbrain/analysis/purge.py`
  is the existing sweep, run at boot, `backend/src/jbrain/worker.py:526`).
- **Untrusted-origin content never triggers a background job**
  (`docs/reference/ASSISTANT.md:94-98`). Scope is owner notes only (owner
  decision), which satisfies this — but the resume path must re-check it, not
  assume the origin from three months ago.

---

## 5. Streaming to the PWA, and the offline phone

**Correction to a common assumption:** `backend/src/jbrain/stream.py` is *not* the
SSE layer — it is URL-sourced video/stream sampling for the `analyze_stream` tool
(`backend/src/jbrain/stream.py:1-30`). The SSE machinery is
`backend/src/jbrain/agent/live_turn.py` (the per-run frame buffer + fan-out broker)
plus the `/chat` endpoint and `GET /chat/runs/{id}/stream?after=N`
(`docs/reference/ASSISTANT.md:159-186`).

`_LiveTurn` is **in-process** and lives in `app.state.live_turns` in the api
(`backend/src/jbrain/main.py:313`, `live_turn.py:1-11`). A worker-hosted ingest turn
cannot register there.

**Recommendation for v1: do not live-stream background ingestion.** Surface it
through the two channels that already work cross-process:

1. **`runs.progress_note`** — a free-text "processed X of Y" line a long-running
   job updates, polled by the Ops "Runs" screen
   (`backend/src/jbrain/models/agent.py:114-116`, written by
   `set_run_progress`, `backend/src/jbrain/workflow/runlog.py:185-203`, invoked via
   the worker's `report_progress` (`backend/src/jbrain/worker.py:189-195`) for any
   handler that declares a `progress` parameter, `worker.py:125-150`). An ingest
   turn can write "turn 3/8 · wrote 2 facts · reading attachment 1/3".
2. **The finished transcript** — `agent_turns` rows, read by the existing chat
   surface. The owner opens the conversation and sees everything.

**When live streaming is genuinely wanted** (the owner taps a note and wants to
watch it being read), the honest options are: (a) move the ingest runner into the
api and reuse `_LiveTurn` directly — the plan-continuation precedent, which was
extracted from `api/agent.py` *precisely so a headless turn could register a real
streamable turn* (`live_turn.py:9-11`, `continuation.py:15-21`); or (b) persist
frames to a table and have the api serve SSE from it. (a) is far cheaper and
already proven; it costs the worker's box-hold pause and single-threading (§1).
Defer the decision — it is not needed for correctness.

**What an offline phone sees:** nothing, and that is correct. Notes queue locally
and sync later; the conversation starts server-side when the note lands. On
reconnect the PWA sees the review badge (silent, no push —
`GET /api/review`, `backend/src/jbrain/api/analysis.py:137`) and the note's
integration state. The push path (`NotifyBus`,
`backend/src/jbrain/notify/bus.py:31-56`) must **not** be wired to ingest
questions: `notify_owner` is opt-in per subsystem, so silence is the default and
requires no work.

---

## 6. Observability, and what the debug console must gain

### 6.1 What already lands, free

- **Run log.** One `runs` row per pipeline dispatch + one `run_steps` row per
  enqueued job (`backend/src/jbrain/workflow/runlog.py:45-100`), finalized with the
  job's real outcome, token cost and captured structured-log trace
  (`runlog.py:206-256`, driven from `worker.py:322-343`). Read surface:
  `GET /api/runs`, `/runs/{id}`, `/runs/queue-depth`, `/runs/stats`
  (`backend/src/jbrain/api/runs.py:92,114,120,132`).
- **Box events.** `app.box_events`, cross-process by design because *"the confusing
  loads (a deferred transcription, an ingest) are mostly the worker's"*
  (`backend/src/jbrain/box_events.py:1-27`). Kinds include `model_load`,
  `model_unload`, `prefill`, `job_refused_no_room`
  (`box_events.py:51-88`). A permanent capacity refusal is already narrated onto
  the owner's surface precisely because *"they have no terminal"*
  (`backend/src/jbrain/worker.py:241-250`).
- **LLM usage.** One `app.llm_usage` row per adapter call, tallied into the active
  per-job `TokenScope` at the single chokepoint every call passes through
  (`backend/src/jbrain/usage.py:73-100`, `LlmRouter._record` at
  `backend/src/jbrain/llm/router.py:690-696`). Surfaced at `GET /ops/llm-usage`
  (`backend/src/jbrain/api/ops.py:1112`).
- **Debug console.** Read-only SQL under an owner RLS context inside a
  `SET TRANSACTION READ ONLY` transaction, plus gateway load/unload/slots,
  llama-swap and llama-server log tails, host memory, and `/complete-async` for
  calls that outlive the tunnel timeout (`docs/runbooks/DEBUG_ACCESS.md`,
  "What the token can do"). So the owner can already *query* `app.jobs`.

### 6.2 What a no-terminal owner cannot do today, and must be able to

The failure the owner will actually hit is **"a note has been sitting there for an
hour"**. Answering it today requires knowing to write SQL against `app.jobs` and
knowing which of `queued`/`running`/`deferred-with-a-reason-in-last_error` means
what. `last_error` is the *only* diagnostic column, and a defer writes its reason
there prefixed `deferred:` with `status` still `queued`
(`backend/src/jbrain/queue.py:504-527`). Nothing projects it.

Required additions, in priority order:

1. **A per-note ingestion status, in the PWA.** For a note: its conversation's
   status, turn count, tokens spent, the open question if any, and — when it is
   waiting — *why*, in the defer reason's own words. Everything needed is already
   written; nothing reads it.
2. **`GET /api/debug/jobs`** — a first-class projection of `app.jobs`:
   status, kind, attempts, `run_after`, `last_error`, and (new) priority, filtered
   and sorted. Note `/api/debug/jobs/{id}` today is the *debug completion* job map
   (`backend/src/jbrain/api/debug.py:977-1057`), unrelated to `app.jobs` — pick a
   different path or rename that one.
3. **`GET /api/debug/box/lanes`** — one call answering "what is resident, what is
   in flight, what is deferred and on what precondition, is an interactive turn
   present, is a vision window open". Today this is three or four separate calls
   (`/llm/local-models/{id}/slots`, `/host`, `/sql`, `/logs/…`) and a lot of
   inference. The inventory itself records that *"the debug console cannot see
   resident model memory"* was a real gap
   (`docs/reference/MODEL_ACCESS_INVENTORY.md`, §E heading at line 140).
4. **A "kick this note" control** — re-fire the conversation for one note without a
   shell. The engine's `POST /ops/triggers/{id}/run`
   (`backend/src/jbrain/api/ops.py:290`) fires a whole *pipeline*, not one note;
   per-note re-fire needs its own endpoint. This is exactly the class of thing
   CLAUDE.md rule 10 says must be designed out rather than answered with a shell
   step.

---

## 7. Backpressure: 40 notes sync at once

Sequence, with what actually happens today plus what changes:

| stage | today (verified) | under the new design |
|---|---|---|
| notes land | 40 `note.created` events → engine dispatcher tick every 2 s (`backend/src/jbrain/workflow/dispatcher.py:667`) → 40 `ingest_note` jobs | unchanged |
| attachments | one `ocr_attachment` per image (`backend/src/jbrain/ingest/pipeline.py:321`); integration is **deferred while any is outstanding** (`backend/src/jbrain/queue.py:617-624`) and while a note still expects unuploaded attachments, for at most `INTEGRATION_ATTACHMENT_SETTLE_SECONDS = 300` (`queue.py:562-571`) | vision jobs coalesce into a drain window (G3); the settle gate is unchanged and is *right* — it already prevents a body-only conversation from starting before the OCR text exists |
| integration | 40 `integrate_note` jobs, drained one at a time by the single-threaded worker | 40 `ingest_converse` jobs at priority 100; each runs **one turn**, then either finishes, asks, or re-enqueues a continuation |
| backlog bound | `backfill_pending_integration` is bounded at 100 per call, oldest-first, owner-notes-ahead (`backend/src/jbrain/queue.py:562-643`) | keep exactly this shape for the conversation backfill |
| owner opens chat | api turn runs detached, never queued | precondition `no_interactive_turn` defers the next ingest turn ≤5 min; the in-flight one finishes (bounded by §3.2's 5-minute wall clock) |
| box refuses memory | transient → defer, no attempt burned; infeasible → permanent fail + a `job_refused_no_room` box event (`backend/src/jbrain/worker.py:217-266`) | unchanged, and now the right behaviour by construction |
| code mode / jmolt night | whole background loop pauses (`backend/src/jbrain/worker.py:472-478,549`); `cancel_running` terminally fails in-flight jobs so they can't requeue (`backend/src/jbrain/queue.py:274-305`) | **needs care**: `cancel_running` forces `attempts = max_attempts`, and its docstring notes *"a cancelled report does not resurrect"*. An ingest conversation must resurrect — it is not a report. Give the conversation row its own reconcile sweep (the `reconcile_*` pattern, `backend/src/jbrain/workflow/scheduler.py`, live at 300 s per §E) so a cancelled turn re-enqueues from durable state |

**The genuinely new failure mode:** 40 conversations that each ask a question
produce 40 review items in one burst. That is the silent queue working as designed
— but it needs a cap and a grouping story, or the first real sync makes the review
inbox useless. **Assumed, not designed here:** that B-other-docs cover question
batching/grouping. Flagged as an open question below.

**Worst realistic burst cost:** 40 notes × N turns × a `gpt-oss-120b` turn. With
no swaps (§2.2) the cost is GPU time, not minutes-per-note of load. With vision
unbatched it is up to 40 swap pairs at ~200 s each — **~2.2 hours of pure model
loading**, which is the number that justifies G3 on its own.

---

## 8. Verified vs assumed

**Verified** (read on this branch): every `path:line` above; the queue's claim /
backoff / defer / stale-reaper semantics; the absence of a priority column; the
worker's single-threadedness and box-hold pause; the precondition gate and its
single live user; the engine's dispatch-only role, linear pipelines, run status
vocabulary and supersede semantics; `AgentLoop` guardrails and the supervised
lift; the plan-continuation chunking pattern; `research_run_state` checkpointing
and `deepest` boot resume; the intake atomic turn claim; `_LiveTurn`'s
in-process, api-only nature; `stream.py` being video sampling, not SSE; the
transcript's text-only cross-turn carry; run-log / box-events / llm-usage
plumbing; the review inbox as a silent queue; residency's single-evictor role,
box advisory lock, ledger and gpu-guard refusal paths; whisper/TEI/ComfyUI being
separate memory consumers.

**Verified from `MODEL_ACCESS_INVENTORY.md` (live-box reads, 2026-08-22, not
re-verified today):** all 19 tasks route local, 16 → `gpt-oss-120b`, 3 vision →
`qwen3.8-27b-abliterated`; `auto_restore: false`; `free_ram` fraction 0.05;
16 live schedules; measured peak footprint `gpt-oss-120b` 69.26 GB; prefill
measured at 118 s of a 198 s load span.

**Assumed** (stated as such, needs measurement or an owner call):
- that an ingest turn on the same served model will evict the primed jerv KV
  prefix at `-np 1` (G4);
- the ~200 s figure for a vision swap pair, extrapolated from load spans in the
  inventory rather than measured for `qwen3.8-27b-abliterated` specifically;
- that question batching/grouping is covered by another B-doc (§7);
- that the graph-write tools can be made idempotent on
  `(conversation_id, turn, call)` — this depends on the tool design, not on
  anything read here;
- that `ingest.converse` can be given its own task name without perturbing the
  live `llm_task_overrides` row (it would need adding there, or it falls back to
  `TASK_DEFAULTS`, which on a fresh box is a **cloud** spec —
  `backend/src/jbrain/llm/router.py:51-117`, all 20 entries `"xai:grok-4.3"`.
  A new task that nobody overrides would route to a provider this design says
  does not exist. **This is a real trap**, not a hypothetical).

---

## Open questions for the owner

1. **Worker or api?** The worker gets the box-hold pause, single-threading and
   token accounting for free but cannot live-stream. The api can stream (reusing
   `_LiveTurn`, as the plan continuation already does) but sits outside the pause.
   Do you want to *watch* a note being read, or is the review badge enough?
2. **How much of the box may background ingestion take while you are using it?**
   Options: (a) hard yield — no ingest turn starts while a chat turn is live
   (simplest, and what §2.4 proposes); (b) soft — ingest keeps a second slot, your
   turns run slower but ingestion keeps up; (c) a nightly window only. (a) means a
   heavy chat day drains no backlog.
3. **Vision batching aggressiveness.** Hold OCR until N attachments are waiting or
   T minutes pass, then drain the lot in one window? What are N and T? A window
   that drains 40 photos means the vision model is resident — and `gpt-oss-120b`
   is not — for the whole drain.
4. **Question-burst cap.** If 40 synced notes each want to ask something, do you
   want all 40 questions, a capped N per burst with the rest committing
   low-confidence, or the conversations to hold their questions until the backlog
   settles?
5. **Re-run semantics.** The DB is disposable, so a conversation *could* be
   re-run from turn 0 after any failure rather than resumed mid-flight. Is
   "re-read the note from scratch" acceptable as the universal recovery path? It
   is much simpler than partial-write recovery — at the cost of re-spending tokens
   and possibly asking you a question you already answered.
6. **Does an ingest question ever earn a push?** The decision is "silent queue".
   Confirming that includes *never* — even for a health-domain note, even after a
   week unanswered — settles a lot of design.
7. **Conversation retention.** Ingest conversations accumulate one agent session
   per note forever. Prune the transcript after the note is finished (keeping the
   run-log and the graph writes), or keep everything?
8. **Deleting the old pipeline.** `integrate_note`, the arbiter and the review-card
   machinery are load-bearing for existing data. Cut over per-note behind a
   setting (both paths registered, one trigger flipped — the engine already
   supports exactly this shape), or a hard swap on a wiped DB?
