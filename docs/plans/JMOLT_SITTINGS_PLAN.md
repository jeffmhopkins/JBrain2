# jmolt sittings — a mechanical way to use the full hour, no summarizer

> **Status:** In progress · **Last verified:** 2026-08-26 · **Waves:** W1✅ W2◻️ W3✅ W4✅

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
sitting, empty_retries, reflected = 0, 0, False
while sitting < JMOLT_MAX_SITTINGS:                     # runaway backstop
    now = clock()
    if now - woke_at >= JMOLT_NIGHT_WALL_CLOCK_S - JMOLT_LAST_SITTING_MARGIN_S:
        break                                           # hour nearly up
    if sitting > 0 and killed:                          # M6 kill between sittings
        break
    reflection = not reflected and \
        now - woke_at >= JMOLT_NIGHT_WALL_CLOCK_S - JMOLT_REFLECTION_MARGIN_S
    sitting += 1
    conversation = [ now_block(tz), sitting_preamble(now) + prologue(sitting, reflection) ]
    done, summary, error, empty = run one bounded agent turn (its own recorded run + transcript)
    if empty and empty_retries < JMOLT_MAX_EMPTY_RETRIES:   # a no-work sitting: retry, don't count
        empty_retries += 1; sitting -= 1; continue          # re-run the SAME number, with a nudge
    # jmolt reads its scratchpad, does a chunk of the night, writes its scratchpad
    if reflection: reflected = True; break              # the reflection sitting closes the night
```

- **Empty-sitting retry** — gpt-oss's harmony format intermittently ends a turn with an
  empty *final* channel right after its analysis: the model "wakes, thinks a half-sentence,
  and stops" with no tool call and no text (`_is_empty_sitting`: no final text, ≤1 model
  step, `end_turn`). A recent night lost ~1/3 of its sittings this way. Such a sitting is
  re-run — with a concrete first-move nudge — WITHOUT consuming a slot (the count is undone),
  bounded by `JMOLT_MAX_EMPTY_RETRIES` so a wedged model can't spin the hour on retries.

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
  tending files, not the feed (`_REFLECTION_PROLOGUE`, triggered once
  `elapsed ≥ JMOLT_NIGHT_WALL_CLOCK_S − JMOLT_REFLECTION_MARGIN_S`, then it is the
  night's last). It is the structural forcing-function for jmolt to DEVELOP — form and
  record a view, work out what only it can (its handle, what it makes of this place),
  leave itself real threads for tomorrow — rather than spend the whole hour reacting to
  the feed and leaving a bare activity log. Recovered sitting-capacity (from the empty
  retry) buys reflection, not more comments.
- **Note + pending ride every sitting** — the owner's advisory note and a one-line
  list of what jmolt has already staged (`_pending_block`, from the outbox) are
  re-injected into every sitting's prologue, not just sitting 1. Each sitting is
  fresh-context with no memory of the last, so a note or a pending-action list left only
  on sitting 1 is gone for the rest of the night; re-supplying them is what lets a note
  shape the whole hour and stops a fresh sitting re-staging what it cannot see it queued.
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
