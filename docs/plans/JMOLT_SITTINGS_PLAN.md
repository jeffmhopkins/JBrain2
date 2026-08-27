# jmolt sittings — a mechanical way to use the full hour, no summarizer

> **Status:** In progress · **Last verified:** 2026-08-27 · **Waves:** W1✅ W2◻️ W3✅ W4✅

Make jmolt actually use its unsupervised hour, without the fabrication and
injection risks a live LLM summarizer would add. The night runs as a sequence of
bounded **sittings** — each its own fresh-context turn seeded from jmolt's file
scratchpad plus a live countdown — instead of one ever-growing turn. Split out of
`CONTEXT_COMPACTION_PLAN.md` after a four-lens cold review concluded that, for the
one caller that reads attacker-authored text unsupervised, in-place context
compaction (summarization) is the worst fit and a mechanical reset is strictly
safer.

## 1. The problem, from the live box

jmolt's first night (2026-08-25) ran one agent turn: created 07:00:13 UTC, wrote
its notebook at 07:04, ended `stop_reason=end_turn` at 9 steps / ~46k tokens. It
did not run out of anything — it *chose* to stop after ~4 minutes. Two causes:

1. **No sense of time.** The turn is seeded once with `now_block(tz)` +
   a prologue that mentions "the hour" in prose (`agent/jmolt_night.py`), but jmolt
   is never told how long the hour is, how much has elapsed, or how much remains.
   The 3600 s wall-clock watchdog (`JMOLT_NIGHT_WALL_CLOCK_S`) is an external kill
   it cannot see. So nothing makes it pace across the hour.
2. **Context can only grow.** A single night-turn accumulates every feed read,
   thread, and profile. To *actually* fill the hour (read → reply → post → read
   over dozens of steps) the turn would eventually hit the model's context window —
   before the clock — and today an overflow just kills the turn.

## 2. Why sittings, not compaction

The cold review of `CONTEXT_COMPACTION_PLAN.md` (§0 there) found that summarizing
jmolt's in-turn context re-imports every risk the jmolt threat model works to
remove: a fabrication-prone 120B rewriting attacker-influenced text; a summary
minted in the *trusted* `UserMessage` channel; the prose `_FENCE` stripped; and a
new **intra-night** re-elevation of fenced memory (defeating M2/A3 from the
inside). jmolt is the *best* possible caller for a mechanical reset instead,
because **its memory is already externalized** to a 16-file / 128 KB scratchpad
(`agent/jmoltscratchtools.py`, `JmoltScratchRepo`) governed by "whatever is not
written down is gone" (`jmolt_night.py`). That is exactly the durable
context-reset substrate a fresh-context handoff needs — already built.

A sitting boundary reuses the **same re-fenced reload** the threat model already
blessed for the night boundary (M2): each sitting reloads the scratchpad as fenced
DATA, no more trusted than the forum text it quotes. So sittings add **no new
injection surface** — they run the trusted reload jmolt already does between
nights, just more often. No summarizer, no fence-stripping, nothing attacker-authored
ever passes through a rewrite step.

## 3. The design

Replace the single `run_turn` in `JmoltNightRunner.run` with a **sittings loop**:

As built (`JmoltNightRunner.run`), with the constant names it ships:

```
woke_at = clock()
sitting, empty_retries, reflection_due = 0, 0, False
while True:
    now = clock()
    if now - woke_at >= JMOLT_NIGHT_WALL_CLOCK_S - JMOLT_LAST_SITTING_MARGIN_S:
        break                                           # hour nearly up
    if sitting > 0 and killed:                          # M6 kill between sittings
        break
    reflection_due = reflection_due or (                # LATCHES (survives an empty retry)
        sitting >= JMOLT_MAX_SITTINGS                   # feed budget spent, OR
        or now - woke_at >= JMOLT_NIGHT_WALL_CLOCK_S - JMOLT_REFLECTION_MARGIN_S)
    sitting += 1
    conversation = [ now_block(tz), sitting_preamble(now) + prologue(sitting, reflection_due) ]
    done, summary, error, empty = run one bounded agent turn (its own recorded run + transcript)
    if empty and empty_retries < JMOLT_MAX_EMPTY_RETRIES:   # a no-work sitting: retry, don't count
        empty_retries += 1; sitting -= 1; continue          # re-run the SAME number, with a nudge
    # jmolt reads its scratchpad, does a chunk of the night, writes its scratchpad
    if reflection_due: break                            # the reflection sitting closes the night
```

- **Empty-sitting retry** — a sitting that comes back with no final text at ≤1 model step and
  `end_turn`, or that took ≤1 step and billed **zero tokens** (`_is_empty_sitting`), is re-run
  with a concrete first-move nudge WITHOUT consuming a slot. Bounded by
  `JMOLT_MAX_EMPTY_RETRIES` **consecutive** empties, reset only by a PRODUCTIVE sitting (an
  empty one that already spent the budget must not rearm it, or a wedged model gets three
  fresh retries per slot instead of three in a row). A transient provider fault is retried the
  same way — but note that a sitting is a whole multi-step turn, so a fault on a later step
  arrives after earlier steps have already staged rows; what makes the re-run safe is the
  done-tonight block below, which shows it what the failed attempt did.

  The zero-token arm only widens the ≤1-step case, deliberately. Zero usage is not proof of a
  dead turn: the adapter documents that a local server may omit the usage chunk on a complete
  turn, and `test_openai_stream_plain_text_handles_missing_usage_chunk` pins that as supported.
  Treating cost alone as decisive would discard real multi-step work and re-run a sitting whose
  tool calls had already committed.

  **What these actually are — the earlier explanation here was wrong.** This was recorded as
  "gpt-oss's harmony format intermittently ends a turn with an empty final channel; the model
  wakes, thinks a half-sentence, and stops." It does not. The model produced its tool call
  every time; **our own adapter threw it away.** llama.cpp cut the SSE body after the
  reasoning deltas and before the `tool_calls`/`finish_reason`/usage chunks, and
  `openai_compat.converse_stream` yielded its final `LlmTurn` unconditionally — `stop_reason`
  defaulting to `end_turn`, usage defaulting to zero — so a stream cut mid-generation was
  byte-identical, to every caller, to a model choosing to say nothing. Measured on the box on
  2026-08-27: nine of sixteen sittings, each 1.70–1.76 s (the same wall-clock as a *successful*
  first step), llama-swap logging `no valid JSON data found in stream` 1:1 with them, truncated
  bodies at 2,274 bytes against 3,802 for the successful ones — the missing ~1.5 KB being
  exactly the tool call. The same call **non-streaming succeeded 12/12** against the same model,
  tools and prompt. The zero `cost_tokens` was the tell, and it was real: no usage chunk ever
  arrived. The adapter now raises `LlmStreamTruncatedError` when a stream ends without a
  `finish_reason`, and the router recovers the round once, non-streaming.

- **`time_header`** — a small, inert block built from the **local trusted clock**
  (M4), injected at the top of every sitting: "You woke at 23:00. It is now 23:34.
  About 26 minutes remain in your hour. This is sitting 3." Gives jmolt a real,
  mechanical countdown to pace against — the pacing fix, independent of context.
  It is derived data (owner-local time math), never platform time.
- **`prologue(sitting)`** — sitting 1 uses today's ritual / returning-night
  prologue; sittings 2+ use a short **continue** prologue: "You've been on Moltbook
  for a while tonight; your notes are loaded above. Keep going — read, reply, post,
  vote — and as the hour closes, make sure everything you want to keep is written
  down." The "flush before the hour ends" instruction moves to *every* sitting's
  close, so there is never much un-persisted state.
- **Reflection sitting** — the hour reserves ONE closing sitting for thinking and
  tending files, not the feed (`_REFLECTION_PROLOGUE`), and it is the night's last. It
  is owed once **either** the feed budget is spent (`sitting ≥ JMOLT_MAX_SITTINGS`)
  **or** the hour is nearly closing
  (`elapsed ≥ JMOLT_NIGHT_WALL_CLOCK_S − JMOLT_REFLECTION_MARGIN_S`) — whichever lands
  first — and the flag latches, so an empty-sitting retry (which hands the slot back)
  re-runs it as a reflection sitting rather than dropping to the feed prologue. The
  budget bounds the FEED sittings; the closing one is extra.
  **Why both arms:** the budget used to be a plain loop bound with the reflection gated
  on elapsed time alone, so a night of quick sittings spent all 12 slots before the time
  window opened and simply exited. Measured on the live box, the reflection sitting had
  then never run once — 13 sittings across two real nights, zero reflections; the
  2026-08-26 night used its budget by minute 40 at ~200 s a sitting and stopped 20
  minutes early. The stretch reserved for jmolt to think was the one it never got, and
  its scratchpad showed it: a minute-by-minute log of what it did and nothing about what
  it thought. It is the structural forcing-function for jmolt to DEVELOP — form and
  record a view, work out what only it can (its handle, what it makes of this place),
  leave itself real threads for tomorrow — rather than spend the whole hour reacting to
  the feed and leaving a bare activity log. Recovered sitting-capacity (from the empty
  retry) buys reflection, not more comments.
- **Note + done-tonight ride every sitting** — the owner's advisory note and a list of what
  jmolt has already DONE tonight (`_done_tonight_block`, from the **action ledger**) are
  re-injected into every sitting's prologue, not just sitting 1. Each sitting is
  fresh-context with no memory of the last, so anything left only on sitting 1 is gone for
  the rest of the night.

  The block lists **targets, not counts**, and is scoped to the night by `woke_at`. It
  replaced a counts-by-kind read of the outbox that could not do the job: "2 comments, 1
  vote" is a number, and a number cannot stop a duplicate — only *which post* can. That read
  also filtered to `queued`/`released`, and with the drip publishing 20–45 s after staging,
  rows fell out of it almost immediately and it reported near-nothing.

  The deeper reason it has to exist at all: **jmolt's writes are invisible to its reads.**
  Writes go stage → release → drip while reads go to the live site, so within a night nothing
  it has written comes back to it. Measured on 2026-08-26 it read one thread nine times
  across five sittings, was shown the same two comments by other agents every time, and put
  seventeen of its own on that post without ever seeing one of them. The ledger is the only
  exact account of what it did, and it was right the whole time — jmolt just had no way to
  read it.
- **Per-sitting bound** — each sitting is capped by the existing per-turn step/cost
  guardrails plus a wall-clock slice, so no single sitting's context can approach
  the window. The outer 3600 s watchdog stays as the hard ceiling.
- **The scratchpad is the handoff.** No summarizer, no in-context carry-over
  between sittings beyond what jmolt itself wrote to its files. This makes the
  "whatever is not written down is gone" contract *structural* rather than a prompt
  hope: flush-then-reset means no un-persisted note sits at a sitting boundary.

## 4. Recording & the history browser

Each night stays **one `agent_session`** (`agent='jmolt'`); each sitting records
its own run + transcript turns under that session (reusing the run-log /
transcript already wired in `JmoltNightRunner.run`). The owner-facing history
browser (`api/moltbook_settings.py`, the jmolt-screen Nights card) then needs to
**aggregate multiple runs per session** — sum steps/cost, show sitting count, and
list each sitting's transcript in order. Small, additive change to the `nights`
endpoint's `LEFT JOIN` (aggregate the runs) and the transcript view (group by
run/sitting).

## 5. Threat-model reconciliation (binding)

- **M2/M4 preserved, no new surface.** The sitting boundary is the re-fenced
  scratchpad reload M2 already governs; the countdown uses the local trusted clock
  M4 already mandates. No summarizer, so none of the cold review's summarization
  blockers apply.
- **M6/M7 unchanged.** The global kill and the autonomy switch still gate the loop;
  a kill mid-night stops launching further sittings.
- **Irreversible-loss window closed structurally.** Because each sitting ends by
  flushing to the scratchpad and the next starts by reloading it, there is no
  un-persisted state at a boundary — the hazard in-place compaction had to tiptoe
  around does not exist here.
- This is a change to a shipped, adversarially-reviewed component, so it reconciles
  with `../plans/JMOLT_PLAN.md` and `../research/jmolt/THREAT_MODEL.md` in the same
  PR (a note that the night is now multi-sitting; M2/M4/M6/M7 re-verified).

## 6. Waves

- **W1 ✅ — the sittings loop + countdown + continue-prologue, backend.** Shipped:
  the sittings loop in `JmoltNightRunner.run` (one `agent_session`, a recorded run
  per sitting, launching until the hour is nearly up or a mid-night kill lands, with
  a runaway `JMOLT_MAX_SITTINGS` backstop); the `_sitting_preamble` live countdown
  from the local trusted clock (M4); the `_CONTINUE_PROLOGUE` for sittings 2+ (reload
  the scratchpad as fenced DATA, M2); an injectable clock so the sittings loop is
  deterministically testable; and the history aggregation (§4) — the owner `nights`
  endpoint sums steps/cost and counts sittings per session, the jmolt-screen Nights
  card shows the sitting count. Tests (real Postgres + faked LLM): a multi-sitting
  night records N runs under one session, a mid-night kill halts further sittings, and
  the history endpoint aggregates. No new dependency.
- **W2 — on-box tuning.** Sitting length, how many sittings fill an hour, the
  continue-prologue wording, and whether jmolt actually paces better with the
  countdown — measured against a real gpt-oss-120b hour on the box.
- **W3 ✅ — box reservation for the night (night hold).** The full hour is only
  worth having if nothing else is fighting jmolt for the box. A **night hold** (a
  persisted settings set of served-model names, cloned from the code-mode hold) is
  set to jmolt's served local model when a night starts and cleared in a `finally`
  when it ends (with a tick-level self-heal for a crashed night). While it is set:
  the worker pauses its background job loop, the `WarmKeeper` keeps gpt-oss primed
  and loads nothing else, and the owner-task scheduler + plan-continuation sweep
  each yield (claim nothing this hour). Deliberately **Option A**: the night hold
  does NOT feed the residency coordinators (those stay code-mode-only), so an owner
  turn at 03:00 can still load another local model — jmolt yields, and the
  `WarmKeeper` restores gpt-oss afterward. `settings_store.box_hold_names` is the
  single union (code-mode ∪ night) every honouring reader consults. Tests (real
  Postgres + faked LLM): the hold is set for the duration of the sitting and cleared
  after (even when a sitting raises); a dangling hold self-heals on the next idle
  tick; the scheduler and continuation sweeps early-return while it is set. No new
  dependency.
- **W4 ✅ — pacing the hour (prologue + `time_left`).** The countdown fixes *knowing*
  the time; two changes push jmolt to *use* it well. (1) The returning-night prologue is
  rewritten to push using the whole hour on substance — read deeply, contemplate before
  posting ("one post you mean beats three you don't"), and actively *organize* the
  notebook (consolidate, retitle, connect, prune) rather than only append — while keeping
  the persona's "on your own terms / never pad / a quiet night is a full night" spine.
  (2) A jmolt-only **`time_left`** tool lets jmolt check mid-turn how much of its hour
  remains, computed from the trusted local clock (M4) so it never asks the 120B to do the
  arithmetic: the night stamps its end time in settings at run start and clears it in the
  `finally` (beside the night hold), and the tool reads it under jmolt's own
  (`is_owner()`-satisfying) context. The per-sitting countdown injection stays; this adds
  a within-sitting check jmolt drives itself. Tests: the pure `time_left_message` (minutes
  remaining / clamps to over / "not running"), the handler over a fake store, and the
  night stamping + clearing the deadline. No new dependency.

## 7. Open decisions for the owner

1. **Sitting length / count** — a fixed per-sitting time slice (e.g. ~10 min → ~5–6
   sittings), a fixed sitting count, or "keep going until the hour is nearly up"
   with each sitting bounded only by the step/cost budget? (Recommend a time slice;
   tune in W2.)
2. **Countdown granularity** — inject the time header once per sitting (simplest),
   or also refresh it every few steps within a sitting so a long sitting stays
   time-aware? (Recommend once per sitting for W1; revisit if sittings run long.)
3. **First-night behaviour** — keep the first night deliberately short (the ritual
   says "lurking is a full first night"), or let it run the full sittings loop like
   any night? (Recommend: run the loop, but the ritual prologue keeps sitting 1
   low-pressure.)

## 8. Non-negotiable reconciliation (when promoted)

No LLM summarizer at all, so no fabrication/fence surface (the whole point). All
scratchpad I/O via the storage abstraction (#2) and jmolt's existing RLS-scoped
write context (#3 — no new table, so no new isolation test). Tests land with the
code (#5). Conventional Commits + PR + green CI (#6). Docs travel with the code:
reconcile `../plans/JMOLT_PLAN.md` + `../research/jmolt/THREAT_MODEL.md`, and give
this a `../ROADMAP.md` slot on promotion (#9).
