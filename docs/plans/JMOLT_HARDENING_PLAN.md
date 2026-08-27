# jmolt hardening — what four independent audits found

> **Status:** Scheduled · **Last verified:** 2026-08-27 · **Waves:** H1◻️ H2◻️ H3◻️ H4◻️ H5◻️

jmolt shipped (`JMOLT_PLAN.md`, `JMOLT_SITTINGS_PLAN.md`) and then ran three real nights.
Those nights produced a set of failures that were diagnosed and fixed in one branch — a
truncated LLM stream recorded as an empty turn, Moltbook threads rendered so the model
answered in another agent's voice, the drip publishing in bursts until the platform
throttled it. Fixing them raised the obvious question: **what else is like this and nobody
has looked?**

So four independent audits ran over the parts the fixes had not touched — the security
mitigations against their own threat model, the night's lifecycle and failure modes, the
scratchpad that is jmolt's only memory, and the owner-facing surfaces. They were given the
already-fixed list and told not to re-report it.

They returned **38 distinct findings**. This plan is the work that follows from them.

The through-line is not that any one component is weak. It is that jmolt's *design* is
sound and its *failure paths* are unbuilt: the system does the right thing when everything
works, and when something does not work it usually fails silently, in a way the owner
cannot see and — four times over — cannot recover from without a terminal they do not have
(`CLAUDE.md` #10).

## Provenance

Each finding below carries the audit that produced it. Two were found independently by two
audits from different directions; those are marked, because independent convergence is the
strongest signal in the set.

| Audit | Scope |
|---|---|
| **S** | Security — the M-numbered mitigations against `../research/jmolt/THREAT_MODEL.md` |
| **N** | Night lifecycle — the lane, watchdog, scheduling, crash/restart, resource contention |
| **M** | Memory — the scratchpad, its quota, its tools, its archive |
| **O** | Owner-facing — digest, journal, notifications, PWA controls, metrics |

## The findings

### Class A — recovery requires a terminal (`CLAUDE.md` #10)

The highest-severity class in this repo, because the owner cannot execute the remedy.

| # | Finding | Audit |
|---|---|---|
| A1 | A stranded night deadline pins the PWA to "Awake now" permanently. Cleared only in a `finally` that hard process death skips; the boot self-heal clears the hold beside it but not this. Not in the settings patch model, no route writes it, the debug SQL console is read-only. | N |
| A2 | The once-per-night marker is stamped on **launch**, and the night's whole preflight sits outside its `try`. Any early failure burns the day silently — no session, no runs, no notification — and no "run the night now" action exists anywhere. | N |
| A3 | Key rotation is unimplementable. Both the tamper and key-leak notifications instruct the owner to rotate; there is no field, no route, no client method, and no runbook. The only real paths are editing the host env or clearing the key, which unregisters a one-shot-registerable account. | S |
| A4 | Disconnect is a no-op when the API key is present in the environment: the stored key clears and the night stops, but the drip keeps publishing staged rows with the env key while the panel reports disconnected. | O |

### Class B — security

| # | Finding | Audit |
|---|---|---|
| B1 | **The scratchpad reloads unfenced.** `scratch_read` returns raw content with no DATA fence, while the prologues elevate it ("that is your only memory"). This is M2, which the threat model names among the mitigations without which the design is not defensible. **Found independently by S and M.** | S, M |
| B2 | Auto-pause is not re-enforced when the platform is unreadable: the read returns before the pause block, so a compromised account becomes writable again the moment the owner clears the kill to investigate. The docstring asserts the opposite. | S |
| B3 | A publish whose id cannot be extracted is recorded as a success with no id, so the real item is unaccounted-for and the tamper watch declares a leak — global kill, autonomy reverted, "rotate the key" (see A3). Armed today: the non-profile write kinds already store no id. | S |
| B4 | Reconcile-before-publish adopts an id by **title match**, and that id enters the tamper watch's allow-list — the plan itself says a title match is worthless because the attacker controls titles. | S |
| B5 | The verification-challenge parse takes the first number anywhere in the reply, so a steered challenge produces a submitted wrong answer rather than a skip. Three of those spend the failure streak and stop all writes: the guard becomes the denial-of-service. | S |
| B6 | A kill accumulates a queue. The write handlers never check it, and auto-release then publishes everything staged during the frozen window the moment the kill clears. | S |
| B7 | The invisible/bidi character class has real gaps — verified by execution, several bidi and filler codepoints pass the content lint **and** survive stripping, reaching the owner's digest and review queue where they can reorder what is read before Release. | S |
| B8 | The content lint and near-duplicate check do not run at publish time, though the plan states they do. With autonomy on, the stage-time call is the only execution that ever happens. | S |
| B9 | The settings table is writable from jmolt's own RLS context. Latent — no jmolt tool exposes generic settings access — but the property is a tool-catalog convention, not a mechanical one. | S |

### Class C — the owner would not find out

| # | Finding | Audit |
|---|---|---|
| C1 | **There is no durable notification store and no PWA surface for one.** Every alert fans out to in-process subscribers and is discarded if none is listening; an expired session is a permanent, invisible blackout. This is the root of C2, C3, C4, C5 and A2's invisibility. | O |
| C2 | The morning digest is broken four ways at once: HTML-escaped into a plain-text channel, discards the ledger field holding what jmolt actually said, lists staged rows as published while capping the count at the query limit, and is collapsed to one line by the notification renderer. | O |
| C3 | The integrity watch has no deadman. It writes state only on a transition, so "healthy for days" and "never ran since deploy" are indistinguishable — confirmed on the live box, where the key does not exist at all. | S, O |
| C4 | A missed or crashed night reads as healthy: the schedule pill never considers staleness and the last-run row is plain text. | O, N |
| C5 | An invalid or revoked key produces no signal — the integrity read collapses auth failures into "cannot check" and keeps the prior state. | O |
| C6 | The metrics module is reachable only from a shell script. It computes the distinct-target measure that is the only loop detector in the system. | O |
| C7 | Failed writes have no notification, no filter in the activity feed, and no presence in the digest. | O |
| C8 | Nothing can retract a published item; the client has no delete for posts or comments. | O |

### Class D — the night's lifecycle

| # | Finding | Audit |
|---|---|---|
| D1 | The night's box reservation is written roughly fifteen database round-trips after the tick begins, and the owner-task scheduler reads it after two. **Evidenced three nights out of three:** the 03:00 owner task ran concurrently with the night's first sitting every time, on the same single-slot model. The deferral has never once applied. | N |
| D2 | There is no per-sitting wall clock; the end-of-night margin assumes short sittings and measured ones run far longer. A watchdog kill then raises a cancellation the sitting's handlers do not catch, so the transcript is never recorded, the run stays open until the stranded-run reaper finds it, and no notification fires. | N |
| D3 | Code mode silently kills the night — the tick never reads the code-mode hold, so every sitting raises and the budget burns in seconds with the day marked used. Symmetrically, powering the coding sandbox on mid-night kills the rest of it. | N |
| D4 | One greedy publish time collapses the night's post budget: the gap is chained off the maximum existing time, so a late post refuses every later one, and the refusal reads like a hard nightly cap. | N |
| D5 | Firing on an exact local hour loses a night invisibly when that hour does not exist on a DST transition. Not reachable at the default hour; the hour is owner-settable. | N |
| D6 | The once-per-night guard is a read-then-write with no compare-and-set, and the lane is per-process. Latent behind a single worker. | N |

### Class E — jmolt's ability to function

| # | Finding | Audit |
|---|---|---|
| E1 | **The persona prompt promises two mechanisms that do not exist**: that jmolt is handed its index and recent notes at the start of each night, and that it gets a five-minute warning before the hour ends. Neither is built. The same claim is repeated in three code docstrings and in `JMOLT_SITTINGS_PLAN.md`. Live, jmolt spends several steps per sitting self-initiating the orientation it was told it had been given. | M |
| E2 | A write call missing its content field silently empties the file and reports success. jmolt cannot recover: the archive exists but the tool that reads it lives on the observer persona, not jmolt's own. | M |
| E3 | An append mode is not validated and falls through to whole-file save, truncating the file to the delta. | M |
| E4 | Rename does not exist, though both prologues instruct jmolt to retitle a file. Append does not exist either, so every edit rewrites the whole file — measured: eleven full rewrites of one file inside a single night. | M |
| E5 | The per-file quota is reached in roughly three weeks on the observed growth curve, and will be reached **during the reflection sitting**, which begins with under ten minutes left and no room to trim and retry. A refused write discards the composed content; an over-long re-emission truncates the tool call into a non-retryable error that burns the slot. | M |
| E6 | Nothing tells jmolt which night this is, while the persona requires it to date its observations honestly. It has already written a wrong claim about its own age into permanent memory, and stamps its notes from whichever clock is nearest in context. | M |
| E7 | jmolt hand-maintains a duplicate of the action ledger because the ledger is surfaced only for tonight. It is the fastest-growing consumer of the quota in E5, and it is already known-wrong by jmolt's own written admission. | M |

### Class F — residuals of the fixes that prompted this audit

| # | Finding | Audit |
|---|---|---|
| F1 | Retried empty sittings are still recorded as real sittings in the run log and the transcript, so a night reads as roughly twice as busy as it was. | N |
| F2 | The drip's rate-limit hold is in-memory only, so it is lost on restart, and the heartbeat keeps stamping while it is held — the PWA reports publishing while nothing publishes. | O |
| F3 | The review-queue body preview truncates, so the release gate can still show less than what will publish. | O |
| F4 | The night ends early because the sitting budget is a hard end-of-night rather than a pacing bound. Whether to change this is a deliberate question about how much of the hour jmolt should get, not a defect. | N |

## Waves

Ordered by what stops the worst outcome soonest, not by component. Each wave is independently
shippable and leaves the system better than it found it.

### H1 — stop the silent losses ◻️

The two ways jmolt can lose work or be steered without anyone knowing.

- Fence `scratch_read` (B1) with the same DATA framing every other third-party surface
  carries, worded for self-authored notes, and cap the return.
- Refuse a write whose content field is absent; require an explicit mode to empty a file;
  reject unknown modes rather than defaulting to save; warn on a large shrink (E2, E3).
- Add append and rename modes (E4) — append removes the re-emission cost from the
  overwhelmingly common case and flattens the quota curve in E5 on its own.
- Give jmolt a read of its own scratch archive, so the safety net protecting it is reachable
  by the agent it protects (E2).
- Make the prompt true (E1): either build the seeded reload the persona promises, or strike
  the sentence. Same for the five-minute warning. Correct the three docstrings and the
  sittings plan either way.

**Done when:** no single tool call can destroy a file without saying so; jmolt's own notes
are fenced; and every mechanism the persona prompt describes exists.

### H2 — the owner can see, and can recover ◻️

The `CLAUDE.md` #10 class, plus the blindness that makes it worse.

- A durable notification store and a PWA surface for it (C1). This is the keystone: five
  other findings are only invisible because it does not exist.
- Persist the digest and make it re-requestable; stamp its marker after delivery, not before
  (C1, C2).
- Clear the stranded night deadline at boot and in the tick self-heal, and treat a deadline
  in the past as "not running" rather than trusting it (A1).
- Stamp the once-per-night marker durably as a conditional update, re-arm it when a night
  ends with nothing productive, and add a PWA action to run tonight's night now (A2, D6).
- A write-only key field, a rotation route, and a runbook covering rotation and the
  platform-recovery story (A3).
- Fix Disconnect to stop the drip when the key came from the environment (A4).

**Done when:** every failure in Class C produces something the owner can see, and no failure
in Class A requires a shell.

### H3 — the tamper and verify paths ◻️

B2, B3, B4 and A3 compose into a permanent brick with an unfollowable remedy. This wave
breaks that chain.

- A publish with no extractable id is a reconcile case, not a success; and the tamper watch
  distinguishes "an id we cannot account for" from "our own write whose id we failed to
  capture" (B3).
- Reconcile on something the attacker does not control, and never let a reconcile-derived id
  into the tamper allow-list unmarked (B4).
- Re-enforce the pause from stored state before attempting the platform read (B2).
- Match the challenge answer against the whole reply, so a steered challenge is a skip and
  not a submission (B5).
- Stamp an integrity-watch heartbeat every pass and surface it beside the drip's (C3).

**Done when:** no single platform-side event can permanently disable the subsystem, and a
silent watch is distinguishable from a healthy one.

### H4 — the night's failure paths ◻️

- Write the box reservation in the tick, before launch, so the deferral the scheduler already
  implements actually applies (D1).
- A per-sitting wall clock, and record the transcript and close the run in a `finally` so a
  cancelled sitting still leaves a record (D2).
- Defer — without consuming the day — when code mode holds the box; warn on powering the
  sandbox on mid-night (D3).
- Pick the earliest publish slot that clears every existing one, rather than chaining off the
  latest (D4).
- Fire on the first tick at or after the local target instant, which is DST-proof and removes
  the fixed-window cliff (D5).
- Check the kill in the write handlers, and do not auto-release rows staged inside a killed
  window (B6).

**Done when:** the night no longer contends with the owner's own 03:00 task, and no single
mid-night event costs the whole hour.

### H5 — honesty ◻️

Smaller items, but each one is a surface currently telling the owner or jmolt something untrue.

- The digest rewrite: drop the escaping, print what jmolt actually said, count what was
  actually published, add the failures (C2, C7).
- Exclude retried empty sittings from the sitting count (F1).
- Give jmolt its night number and first-night date, and name the timezone in the countdown
  (E6).
- Surface the ledger across nights so jmolt stops hand-keeping a duplicate (E7).
- Widen the invisible-character class on both sides (B7).
- Run the content lint at publish time as the plan already claims (B8).
- A metrics route and a failed-writes filter (C6, C7).
- Persist the drip's rate-limit hold and surface it (F2); widen the review-queue body (F3).

**Done when:** no owner-facing or jmolt-facing surface asserts something that is not true.

## Deliberately not in scope

- **F4 — the night ending early.** Changing the sitting budget is a decision about how much
  of the hour jmolt should spend on the feed, not a bug fix. It wants its own answer.
- **B9 — the settings table's RLS.** No jmolt tool exposes generic settings access, so there
  is no path today. Worth doing when the surrounding policies are next touched, not as an
  isolated migration.
- **C8 — retraction.** Whether the platform even offers a delete is unverified. If it does
  not, the honest fix is to say so in the activity card rather than build a control that
  cannot work.
- **A heuristic voice guard for impersonation.** Held deliberately from the earlier work
  until a week of nights shows whether the rendering fix holds on its own.
- **Re-staging the writes lost before the rate-limit fix.** Recoverable, but re-publishing
  to a public forum is the owner's call, not a migration's.
