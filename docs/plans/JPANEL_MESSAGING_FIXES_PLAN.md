# jpanel messaging — the fix plan after the adversarial review

> **Status:** Scheduled · **Last verified:** 2026-10-11 · **Waves:** W1◻️ W2◻️ W3◻️ F1◻️ F2◻️ F3◻️ F4◻️ P1◻️

Voice post between the twins' panels and the owner's PWA works on the happy path. It fails on
the paths a four-year-old actually takes: pressing again while it loads, walking away
mid-message, holding the pet while a message waits, talking over a TV, and a USB re-flash.

**Where the findings came from.**

| Source | Findings |
|---|---|
| Original review | 32 findings (B6, B7, B9 duplicate A3, A1, A6) |
| Verification against the code | 2 new defects (A8, A9) |
| Cold review, round 1 | 15 blocking + 26 non-blocking |
| Cold review, round 2 | 17 blocking + 33 non-blocking |
| Cold review, round 3 | 18 blocking + 30 non-blocking |
| Cold review, round 4 | 16 blocking + 40 non-blocking |

**How the plan responds.** The core model has held through the last two rounds:

- one level snapshot from the engine;
- a serial engine;
- generation-stamped streams;
- the audio task as the sole writer of the speaker.

Round 4 found three recurring *classes* of defect. Each is now closed structurally, so new
instances cannot appear:

1. **Time bounds are true by construction.** Every request carries an absolute deadline that
   `link.c` enforces. The engine publishes the deadline of whatever it is doing *now*. The UI
   never adds durations together (§7.5).
2. **Existing behaviour is preserved mechanically.** Every `ui_tests.c` test and every
   owner-decision comment the machine touches is listed with a disposition (Appendices A and B).
   Anything not decided in §4 keeps today's behaviour.
3. **A cue's lifetime starts when the cue becomes eligible to play.** The new-message chime is
   enqueued on the edge where it can actually sound (§7.4, §8.5).

The owner has approved the restructure (*"totally down for restructuring the whole UI state
machine to make sure that everything is done correctly"*) and has decided three questions
(§4).

Binding inputs: `CLAUDE.md`, `docs/DOC_LIFECYCLE.md`, `docs/reference/PROCESS.md`,
`docs/reference/DEVELOPMENT.md`, and `docs/plans/JPANEL_PLAN.md` (**JP**).

The panels are placeholder-named **Quorra** and **Tamsin**. Names in this plan are as of
firmware 0.3.39. Line numbers are omitted because they drift.

## 1. Goal

A message to or from a child is never:

- lost silently;
- replayed by a fault;
- reported as heard when it was not.

Every press a pre-reader makes gets an answer she can understand without reading. Every mode,
deadline and drawn control is exercised on the host, so the code that ships is the code that is
tested.

## 2. Expectations

Each expectation is testable and is cited by its number in §3 and in the tests.

**J1 — a child sends**

- **E1.1** The box accepts every recording a panel can capture. A body more than 0.5 s over the
  cap is refused, never truncated.
- **E1.2** After the tick or the hush auto-send she hears exactly one outcome: sent, nobody to
  send to, dropped (silence trimmed to nothing), or didn't go. It comes within the engine's
  published deadline. An outcome arriving after the UI's backstop is logged and never sounded.
- **E1.3** An upload in flight is shown on the glass (F4). A message arriving meanwhile still
  chimes and shows its notice.
- **E1.4** Nothing alters an upload's bytes. Anything that needs the microphone during an upload
  is refused with the busy sound.
- **E1.5** Lead-silence on a deliberate recording ends with "dropped". The tick pressed into
  silence keeps today's "didn't go". Every refusal, from any mode and by touch or voice, plays
  the busy sound — including one refused behind a pet reply, which sounds when the reply ends.
- **E1.6** A hold on the pet centre opens the "who?" menu in all three of these cases: with
  nothing up, with a badge up, and with a pair that has stood for 10 s. Today's rim,
  maintenance-tap and swipe guards stay.

**J2 — a child receives, plays, stops, replies**

- **E2.1** Pressing the notice plays the message whose sender the notice shows.
- **E2.2** A stop — exit, button, face, swipe or reply — is honoured at any point after the
  press. The speaker falls silent within one 40 ms chunk plus about 90 ms of codec DMA, and
  nothing chains on. Over WebSocket the request is cancelled within 250 ms (HTTPS: §7.6).
- **E2.3** The top-left face is drawn every frame from the same function that addresses reply
  (§8.2 I2). A face press cycles the selection and a swipe moves it, clamped (JP 0.3.37). Both
  stop the sound. Nothing is skipped.
- **E2.4** From F3, when the fetch carried `X-Jpanel-Delivery`:
  - a message is "heard" after `HEARD_MIN_MS`, or 80 % of a short message;
  - a shorter stop hands it back, refunded idempotently and within a cap.

  Without that header, a stop acknowledges as it does today. A fetch cancelled before its head
  may burn one attempt; that is bounded by the cap of 5 and recoverable through retry (C3).
- **E2.5** A heard message is acknowledged at most once. A lost acknowledgement is retried for
  up to 10 minutes. After that, or after a reboot (the slot lives in RAM), the box may offer the
  message again, bounded by its delivery cap. It never replays or skips within a run.
- **E2.6** Every playback ends by the engine's published deadline. A failure says "didn't go"
  and restores the notice.
- **E2.7** Reply is live only while the message is audibly playing (ON, §8.2). It opens a
  working recording to the drawn face, with the tick and cross live after a short press grace.
- **E2.8** Exit during playback is **one press**: it stops and leaves (owner). A message that
  ends by itself is followed by the pair, as today.
- **E2.9** A message that arrives during a conversation, a recording or a pet reply chimes when
  the microphone closes and the reply ends. Its 15 s big box counts from first visibility. A
  second arrival does not restore the big box; a wake from dark does (JP).
- **E2.10** A press that cannot be served always ends audibly.
- **E2.11** A message already announced is never announced as new again.
- **E2.12** PAUSE and REPLY are claimed and ignored until ON (JP 0.3.36). EXIT is dead for
  `EXIT_GRACE_MS` after the press that started the playback, in every phase (0.3.29).

**J3 — Dad sends, reads, clears**

- **E3.1** An unknown panel answers 404 before text-to-speech runs; the draft is kept.
- **E3.2** Every unplayed message to the owner can be rendered. A message is marked read after a
  dwell, while the page has focus.
- **E3.3** Statuses:
  - not yet seen by her panel — `offered_at` is stamped when the panel's `/waiting` first
    describes the message;
  - on her panel;
  - started;
  - Played, or "stopped after N s";
  - couldn't be delivered.

  Twin-to-twin messages show the same statuses.
- **E3.4** Retry and discard are available on undelivered messages.
- **E3.5** The "kept" count after a clear names the right child.
- **E3.6** A twin-to-twin message appears in both threads. Clearing either thread deletes it,
  unless it is still unheard.
- **E3.7** A stale poll never removes a just-sent bubble.
- **E3.8** Error messages read as sentences. The composer shows its limit and a hint. A
  recording made at the cap is sent.
- **E3.9** The launcher badge equals what the screen renders.
- **E3.10** iOS Safari can play the audio. A malformed id answers 404.

**J4 — re-flash**

- **E4.1** Unplayed mail follows a re-flash or a rotation to the new key, including mail
  inserted while the re-flash was in flight. The panel's history stays in one thread.
- **E4.2** Retired panels stay visible and clearable, and can be merged explicitly — from the
  debug API from W2, and from the PWA from P1. Nothing is merged automatically.

**J5 — network loss**

- **E5.1** A message is acknowledged only if it was played whole, or stopped after
  `HEARD_MIN_MS`. A message that streamed short is never acknowledged.
- **E5.2** A 4xx other than 408 or 429 is terminal. Every other failure retries with backoff,
  for up to 10 minutes.
- **E5.3** A failed send says so and is not kept for resending (§5).
- **E5.4** Mixed releases keep every journey working, at worst with today's behaviour (§6).

## 3. Traceability

"Verified" records what reading 0.3.39 found. ✓ means confirmed; ✓\* means confirmed with a
correction, listed after the table.

| ID | Verified | Fix | Wave | Proving test(s) |
|---|---|---|---|---|
| A1 | ✓ | Stop generation; arbiter discard; sink checks gen; network cancel | F2 | `test_stop_at_every_interleave_point`, `stream: test_stop_while_paused_drains` |
| A2 | ✓ | Every command ends; the UI backstops on the engine's published deadline | F2 | `test_chain_fetch_failure_returns_to_idle` |
| A3 | ✓ | Box accepts the capture cap (+0.5 s slack); caps pinned | W1, F2 | `test_send_accepts_exactly_the_capture_cap`, `test_send_refuses_well_over_cap`, `test_capture_caps_pinned` |
| A4 | ✓ | Serial engine; ack before the next command; run by id | W3, F2 | `test_lost_ack_is_retried_and_never_replays`, `test_ack_4xx_is_terminal` |
| A5 | ✓ | Render takes the lease before posting | F2 | `test_lease_taken_before_post` |
| A6 | ✓\* | Hand-back gated on the delivery header; selection over waiting + current | W3, F2, F3 | `test_stop_in_preroll_returns_not_acks`, `test_against_0_3_39_box_short_stop_acks`, `sm: test_face_cycles_without_skipping` |
| A7 | ✓ | Seqlock snapshot | F2 | `test_snapshot_is_never_torn` |
| A8 *(new)* | — | `s_state` shared by two writers → removed | F2 | `test_send_outcome_survives_concurrent_chain` |
| A9 *(new)* | — | No ring reset; only producer and arbiter write the cursors | F2 | `stream: test_cursor_writers` |
| A-tests | ✓ | Host foundations (§10) | F1 | `run_jpanel_tests`, `run_stream_tests` |
| B0 | ✓ | One state machine (§8) | F3 | Appendix A; `check_no_logic_in_display` |
| B1 | ✓ | Per-mode targets; reply only in ON | F3 | `test_reply_during_playback_opens_a_working_recording` |
| B2 | ✓\* | Standing pair (owner) | F3 | `test_hold_at_pet_centre_*` |
| B3 | ✓ | Pair offer hidden while a message waits; one face function | F3 | every-frame face assertion in `test_storm` |
| B4 | ✓ | Cue expiry from eligibility; chime on the eligibility edge | F2, F3 | `stream: test_cue_expiry_starts_when_eligible`, `sm: test_message_during_30s_recording_chimes_after` |
| B5 | ✓ | Sending is a level; notice and AGAIN answer "busy" | F3, F4 | `test_notice_and_again_refused_during_send` |
| B6 | = A3 | — | — | — |
| B7 | = A1 | — | — | — |
| B8 | ✓ | Lead-silence → "dropped"; every refusal → `CUE_BUSY` | F3 | `test_every_refusal_sounds_busy` |
| B9 | = A6 | — | — | — |
| B10 | ✓\* | Pending wait longer than the reply cap; follow-up yields; `due()`; "stop stop" kept | F2, F3 | `test_press_behind_pet_reply_is_served`, `test_deadlines_survive_u32_wrap` |
| B-UX | ✓ | §9 | F3, F4 | per row |
| C1 | ✓ | `readdress_orphans`; owner grouping by subject; explicit merge (§11) | W2 | §11.2 |
| C2 | ✓ | Validation; picker grouped by subject | W1, W2 | `test_send_text_unknown_panel_404_before_tts` |
| C3 | ✓ | Retry and discard | W3, P1 | `test_retry_resets_deliveries`, `test_discard_releases_blob` |
| C4 | ✓ | `heard_ms`, `offered_at`; honest copy | W3, F3, P1 | `test_played_records_heard_ms` |
| C5 | ✓ | Between rows in both threads | W1, P1 | `test_between_message_in_both_threads` |
| C6 | ✓ | Per-thread limit + every unplayed owner row | W1 | `test_every_unplayed_owner_row_is_returned` |
| C7 | ✓ (Range plausible) | 206 Range; uuid → 404 everywhere | W1, W3 | `test_audio_range_206`, `test_non_uuid_is_404` |
| C8 | ✓ | `errorDetail`; `maxLength`; TTS 60 s | W1, P1 | `client.test` |
| C9 | ✓ | `kept_for` | W1 | `test_clear_kept_names_recipient` |
| C10 | ✓ | PWA clamps to the cap; box refuses only over cap + slack | W1 | `voiceMessage.test: clamps_to_cap`, `test_owner_audio_at_30_02s_is_accepted` |
| C11 | ✓ | Request sequence + epoch | W1 | vitest |
| C-UX | ✓ | §9 | W1, P1 | per row |

**Corrections to the review:**

- **A6:** the face needs `waiting > 1` *after* the decrement, so with two messages it is dead
  during the first one.
- **B10:** "stop stop" is muted while recording — a deliberate rule (JP 0.3.33), kept.
- **B2:** "the pair never expires" is the owner's own rule (0.3.25). The owner chose the
  standing variant.
- **C7:** the 500 is confirmed; the Range behaviour is plausible but unconfirmed.
- **Comment drift** to fix in the waves that touch these files:
  - `s_repeat_until` ("displaced by a new message");
  - `ui.c` exit ("No cue");
  - `jpanel_replay` ("without asking the box");
  - `audio.c` ("Ten seconds", "thirty");
  - `panel_ws.py` ("`MAX_MESSAGE_MS` is 30 s");
  - JP ("Capped at 20 s").

## 4. Owner decisions

**The rule.** Every owner decision the machine touches is listed in Appendix B. Each is kept
unless this section changes it.

**Decided by the owner on 2026-10-11:**

| Question | Decision |
|---|---|
| Standing pair | Yes. After `REPEAT_MS` (10 s) the pair is hit only by its drawn discs (`CONFIRM_HIT_R`), and a hold on the pet centre opens the menu. |
| Exit during playback | One press: it stops the message and leaves, with no pair step. The pair still follows a message that ends by itself. |
| Refusal sound | A new, distinct `CUE_BUSY` ("busy, try later") for every refusal. `CUE_STOP` stays "cancel / dropped". Designed in F4; F3 ships a placeholder pinned by `test_cue_busy_distinct`. |

**Changed because the old behaviour is itself a finding:**

- **Ack-on-stop.** From F3, and only when the fetch carried a delivery number, a stop shorter
  than `HEARD_MIN_MS` hands the message back (C4 / A6). Otherwise a stop acknowledges, as today,
  in every wave.
- **`/next?at=` → by id** (A4, A6).
- **The notice clock** counts from first visibility instead of arrival (B4). The second-arrival
  and dark-wake rules are unchanged.
- **The tap order** puts CONFIRM above PAIR. In practice the pair and the tick/cross are never in
  the same mode (B1).

## 5. Scope

- **Deferred:**
  - **Web push.** It needs a new dependency and is a privacy decision (JP §5).
  - **Resend after a failed send.** Keeping the recording would hold the capture lease, and with
    it the pet's microphone.
- **Out of scope:** speech-to-text for children's voices.
- **Unchanged:** `endpoint_status`.

## 6. Version skew

Panels OTA from the box's `firmware/dist/`. `main.c` installs any version that **differs** from
the running one. So:

- **New box + old firmware is routine.** It lasts at least until each panel's next manifest
  poll (~15 min), and indefinitely for a panel whose OTA keeps refusing.
- **Old code + new firmware happens only after a box rollback.** Ops → Update runs
  `git reset --hard @{u}` and then `alembic upgrade head`. A rollback reverts code only. **It
  never deletes a migration revision file.** W1 adds a CI check that rejects deleting any file
  under `backend/migrations/versions/`. W2 is data-only and W3's columns are nullable or
  defaulted, so old code runs against the new schema
  (`test_w3_schema_accepts_pre_w3_box_insert_shapes`).

Two rules hold across every change:

- **Box changes are additive.**
- **Firmware degrades to today's path** whenever a field, header or route is missing.

| Change | New box + 0.3.39 | Old code + new firmware |
|---|---|---|
| `/send` ceiling = capture cap + 0.5 s | A3 fixed for every panel in the field | 413 → `FAILED("too-long")` |
| `/waiting` gains `queue[].id` | ignored; ~848 B ≤ 0.3.39's 1024 B buffer (`test_waiting_body_fits_0_3_39_buffer`, W3) | positional `?at=`; "new" = the count rose |
| `/next?id=`, `X-Jpanel-Duration-Ms`, `X-Jpanel-Delivery` (from `UPDATE … RETURNING`) | not sent | `at` honoured; **no delivery header → ack on stop, as today** |
| `/played {id, heard_ms}` | `heard_ms` is NULL | the extra field is ignored |
| `/returned` (tunnelled via `_ALLOWED`) | never called | never called (no header) |
| Re-addressing, owner grouping, `offered_at` | box-side | the data stays principal-keyed |

---

## 7. The engine (F2)

### 7.1 Two arrows, and nothing else

**render → engine: a command queue.**

- Depth 4. Each command is `{kind, seq, gen, expires_at, id[48], at, to}`. Only render posts.
- `in.post_failed = seq` reports a post that failed.
- "Poll soon" is an atomic flag, not a command (`jpanel_poll_soon()` keeps its name and is
  called from render and from `main.c`'s nudge).
- Command expiry: PLAY and REPLAY at post + 10 s; SEND at post + 60 s.

**engine → render: one seqlock snapshot**, written only by the engine and copied by render once
per frame. It is a level, not a stream of events, so nothing can be dropped or reordered.

```
eng_view {
  phase IDLE|POLLING|FETCHING|SOUNDING|ACKING|SENDING
  claimed_seq                        // command the engine is working on now (0 = none)
  deadline_ms                        // absolute: when what it is doing NOW must have ended
  done_seq                           // every command ends here exactly once
  cur  { seq, stream_id, id, from[32], from_dad, run_continues }
  play_outcome { seq, id, kind heard|heard_unacked|stopped_heard|returned|short|empty
                                |cancelled|failed, reason }
  send_outcome { seq, kind SENT|NOBODY|EMPTY|FAILED, reason }
  waiting { count, known, q[8]{ id, from, from_dad }, sibling }
  ack_owed, hb                       // hb: incremented every engine step (telemetry only)
}
```

- `seq_ge(a, b) = (int32_t)(a - b) >= 0`.
- **The engine claims a command** — it publishes `claimed_seq`, `cur` and `deadline_ms` —
  **before** it blocks on anything.
- It never claims a command whose `expires_at` has passed. Such a command ends at once:
  `failed("expired")` or `cancelled` (stale gen).
- For the next message in a run, the engine seeds `cur` from the waiting entry before the
  request (today's `seed_sender`).
- **Audio facts are not in the snapshot.** Render reads `{stream_id, out_ms, arb_hb}` directly
  from the arbiter's atomics (§7.4). The engine's copy could be 20 s stale while it blocks.

### 7.2 Seqlock

**Writer:**
1. `s = load(relaxed)`;
2. `store(s+1, relaxed)`;
3. `fence(RELEASE)`;
4. write the fields;
5. `store(s+2, RELEASE)`.

**Reader:**
1. `s1 = load(ACQUIRE)`; if odd, retry;
2. `memcpy` the fields;
3. `fence(ACQUIRE)`;
4. `s2 = load(relaxed)`;
5. accept if equal.

The formal C11 race in the `memcpy` is validated and accepted. After 4 failed tries the reader
keeps its previous copy. All big structs are static (the stacks are 8 KB face, 6 KB engine).

### 7.3 The serial engine

`jpanel_core_step(core, now)` runs in this order:

1. **Finish the current stream.**
   - Classify it. Publish its `play_outcome`, and `run_continues`: the remaining run ids
     (§7.3.3) exist and the gen is fresh. Publishing this **before** the acknowledgement lets the
     UI leave a finished run without waiting on it.
   - Arm the ack slot and attempt it. `stream_begin` refuses until classification is done.
2. **Acknowledge.** One slot: `{id, kind PLAYED|RETURNED, heard_ms, delivery, first_at}`.
   - Request deadline: 10 s.
   - **204** → clear. Also clear on **409** (refund not applicable) and **404** (row gone, or
     route absent — counted as `no_refund`).
   - **Other 4xx except 408/429** → terminal: clear and log.
   - **Anything else** → end the run, set `ack_owed`, back off 1, 2, 4 … 30 s; give up after 10
     minutes.
   - **While owed:**
     - the poll drops the owed PLAYED id from `waiting`;
     - a PLAY first retries the ack, and on failure ends `failed("ack-pending")`.
3. **Continue a run, by id.**
   - The run list is: the waiting list at the start, plus ids that arrive mid-run (JP: "press
     once, hear everything"), minus ids played or returned in this run.
   - Continue only if `cur.gen == load(s_stop_gen)`.
   - With no ids, fall back to `?at=0`.
   - A RETURNED message goes back into the local `waiting` at its old position, and a poll is
     flagged.
4. **Dequeue.** A command with a stale gen or past its expiry ends at once. Expiry is checked
   again before each request attempt (the `/next`, the second send attempt, the 422 retry).
5. **Poll** when due or flagged. A queued command cancels a poll (§7.6).

Every exit publishes `done_seq` and the outcome in one write.

**Lease.** Render acquires the capture lease with a CAS (0 → 1) before posting a SEND. The
engine releases it after the last send attempt and before publishing `send_outcome`. If the
post fails, render releases it.

**Stops in F2** (no policy change):

| Stop when | With delivery header | Without |
|---|---|---|
| `out_ms > 0` | PLAYED | PLAYED (as today) |
| `out_ms == 0` (nothing audible) | RETURNED | PLAYED (today's ack-on-stop; nothing is lost) |

A stream that ended **short** (unverified) is never acknowledged. That is today's rule.

### 7.4 The audio arbiter: `stream_tick`, pure and host-tested

Every per-chunk decision lives in **`stream.c`**: `stream_tick(&st, now) → { WRITE(ptr, len) |
IDLE, capture: COPY|SILENCE|NONE }`. That covers today's `s_play`-before-ring rule, the preroll,
pause, deafness, the capture copy, and request intake.

- `audio_task` does codec I/O and nothing else. `check_no_logic_in_audio` is part of `make test`
  and fails if `audio.c` touches any field of `stream_t`.
- **Cues are rendered by the arbiter**, on its own task, into its own buffer (`cue_render` is
  pure, in `cue.c`). Callers pass only a cue id. **Render never writes `s_play`.**

**Requests, by priority:**

| Request | From | Rule |
|---|---|---|
| Pet reply `{ptr, len, id}` | talk | Played from talk's own buffer. `reply_done == id` is set on every disposal: played, silenced, or standby. It waits out a sounding cue (≤ 749 ms, `cue.h`) and is never refused. While standby is held, a reply is disposed of unplayed. |
| Press cue (one-slot mailbox) | render | Preempts a cue. Dropped while a reply is pending or sounding (JP: a reply outranks an acknowledgement). Plays over a stream chunk by chunk, as today. |
| Queued cues: refusals, outcomes, new message | render | SPSC array of depth 4 in `stream.c`. **Eligible** only when no reply is pending or sounding and the capture is closed. **Expiry counts from first eligibility:** refusal 2 s, outcome 15 s, new message 20 s (a press on the notice cancels the chime). On overflow the oldest MESSAGE is dropped first, then the oldest entry; drops are counted. Plays over a stream. |
| Message stream (ring) | engine (producer) | Lowest priority; yields to everything above. |
| Silence | render (flag) | Cuts the cue and reply and discards the stream. |

**Stop and pause:**

- `s_stop_gen` is written only by render.
- Each stream carries `{gen, stream_id}`.
- Every tick checks the gen **before** pause. A stale stream is discarded: `r = w` (a consumer
  write), and pause is ignored for it.
- **Pause belongs to the machine.** Render writes `pause_req {stream_id, on}`, and the arbiter
  honours it only for that stream.
- **The sink checks the gen on every chunk.** `pump` returns non-zero on a stale gen, so a stale
  body is never drained at network speed.

**The ring:**

- Cursors are monotonic and **never reset**. `w` belongs to the producer and `r` to the arbiter,
  with `__atomic` release/acquire.
- `stream_begin` waits until the arbiter has published `seen_w == w && r == w` for the previous
  stream.
- The arbiter alone writes `{stream_id, out_ms, arb_hb}`. `arb_hb` advances every tick.

**The capture lease** is a `uint32_t` CAS: 1 for the engine's send, 2 for talk.

- `capture_open()` refuses while the lease is held.
- Talk releases it when its upload returns.
- Every capture opener handles the refusal: F2's wrappers (`start_listening`,
  `start_send_recording`, the follow-up open in `display.c`) and F3's microphone gate.

### 7.5 Deadlines are true by construction

**(a) `link.c` enforces an absolute deadline.** `link_req_t.deadline_ms` replaces `timeout_ms`
for everything up to and including the response head, uploads included:

- **The socket wait.** `LINK_UP_WAIT_MS` becomes `min(8 s, left)`.
- **The WebSocket control frame.** `ws_send`'s `s_ws_lock` take and its send both get `left`
  instead of `LINK_SEND_MS`; that wait was missing from the round-3 arithmetic.
- **Upload loops.** Every frame is checked against the deadline.
- **The head wait.**
- **HTTPS.** The `s_http_lock` take gets `left`. `esp_http_client`'s timeout is set to `left`
  before `open`, before each `write`, and before `fetch_headers`, and the deadline is checked
  between steps.
- **Response bodies.**
  - Buffered bodies (`buf`) are bounded by the deadline too.
  - **Streamed bodies** (`sink`: `/next`, `/message/{id}/pcm`) are bounded by `LINK_STALL_MS` per
    *network* gap, as today. Time the sink spends blocked (the ring is full, or the stream is
    paused) does not count.
- Over-run per request is ≤ one `LINK_STEP_MS` of slop.
- The pure decision helpers are host-tested in `wsproto.c`: `wsp_wait_step(now, deadline,
  cancelled)` and `link_left(now, deadline)`.

**(b) The engine publishes the deadline of what it is doing now.**

| Engine phase | `deadline_ms` |
|---|---|
| POLLING, FETCHING, ACKING, SENDING | the current request's `deadline_ms`, published before the request starts |
| SOUNDING | `now + LINK_STALL_MS + 2 s`, refreshed while bytes arrive **or** the arbiter's `out_ms` advances **or** (paused and `arb_hb` advances) |

A wedged arbiter or a stuck engine therefore stops refreshing, and the published deadline
passes.

**Request deadlines:**

| Request | Deadline |
|---|---|
| poll | 20 s |
| `/next` head | 20 s |
| send attempt | 120 s — covers 1.1 MB at ~10 KB/s, and the 20 s control-frame lock |
| ack | 10 s |

**(c) The UI never adds durations.** Its only backstops:

- **Claimed:** `claimed_seq == mine` and `due(now, deadline_ms + 2000)`.
- **Unclaimed:** `!seq_ge(done_seq, mine)`, not claimed, and `due(now, expires_at + 1000)`.

An engine that blocks inside a call past its own deadline leaves `deadline_ms` stale in the
snapshot, so the backstop fires. A backstop emits `STOP`, `CUE_OOPS` and goes to PET. A later
real outcome increments `late_outcome` and is never sounded.

**(d) No codec-wedge rule.** A wedged arbiter stops `out_ms` and `arb_hb`. The SOUNDING deadline
stops refreshing, and the UI's claimed backstop stops it.

**Test.** `test_engine_meets_published_deadlines` runs a fake link that models every real wait:

- the 8 s socket wait;
- the 20 s control-frame lock;
- the `s_http_lock` wait;
- a slow upload at 10 KB/s;
- a slow head;
- body gaps.

It asserts that every claimed step ends by `deadline_ms + LINK_STEP_MS`, and that every command
reaches `done_seq`. `test_storm` (§8.6) asserts that no UI backstop fires while the fake engine
honours its own deadlines.

### 7.6 Network cancel

- `link_types.h` (split from `link.h`) adds `deadline_ms` and `bool (*cancelled)(void *ctx)`.
- **WebSocket waits** call `wsp_wait_step` every 250 ms and send `wsp_cancel` when cancelled.
- **HTTPS** is checked between steps and in the read loop.
- The waits that cannot be interrupted are still bounded by `left` (§7.5a).
- Buffers: `rel[96]`, `path[112]`; `/waiting` buffer 1536 B.
- **Gap.** cJSON parsing stays on the device. Host tests feed parsed structs (including the
  shape with no ids). The field names are pinned by backend tests.

### 7.7 Writers

| State | Writer |
|---|---|
| `s_stop_gen`; mailboxes (press cue, silence); `pause_req`; command queue; send-lease acquire | render |
| poll flag | any task (atomic) |
| snapshot; run list; ack slot; ring `w`; stream `{gen, id}`; send-lease release | engine |
| ring `r`, `seen_w`, `{stream_id, out_ms, arb_hb}`, `reply_done`, `s_play`, all output | arbiter, on the audio task |
| reply request; talk lease | talk |

**Type headers.** `jpanel_types.h` is split from `jpanel.h`, whose `cfg.h` pulls in
`esp_err.h`, so that `panel_sm.c` and `jpanel_core.c` build on the host.

**New telemetry:** `ack_owed`, `ack_retries`, `no_refund`, `returned`, `lease_refusals`,
`snapshot_retries`, `post_failed`, `late_outcome`, `cue_drops`, `jpanel_stack_free`,
`sm_violations`.

**F2 changes no intended behaviour.**

- The old `display.c` reads the snapshot through thin wrappers in `jpanel.c`:
  - `jpanel_state()` is derived from the phase and from outcome seqs not yet seen;
  - `jpanel_clear()` marks an outcome seen;
  - `running`, `fetching` and `in_from` come from `cur`;
  - play is refused while the send lease is held.
- **Outcome-to-cue mapping in F2 is today's:** `EMPTY` reports as `FAILED` (OOPS).
- F3 deletes the wrappers.

---

## 8. The panel state machine (F3)

### 8.1 Shape

`panel_sm.{c,h}` replaces `ui.{c,h}`. `sm_step(sm, &in, &out)` runs once per frame, both on the
device and in the tests.

**Inputs (`in`):**

- `now`, `snap`, `audio {stream_id, out_ms}`, `post_failed`;
- touch edges with their points;
- `on_pet`, `gest_taps`, `swipe`;
- the voice command, `cmds_muted`;
- button edges, `standby`;
- `reply_sounding`, `speaker_busy`, `hearing`, `capture_ms`, `leased`, `net`;
- sleep stage, `calibrating`.

**Outputs (`out`):** ordered effects, `sm_view_t`, and the dashes and swipes telemetry.

**`display.c` keeps:**

- sampling;
- the executor (with the `sound` gate on every cue);
- drawing;
- the sleep stages — `used` keeps its non-machine terms (speaking, actions, gestures, movement)
  and replaces its talk and jpanel terms with `sm_busy()`;
- calibration;
- the pet-side pass-through effects: the poke, `VOCAB_FORM`/`COLOUR`/`ACTION`, the label.

**Calibration:**

- Entering calibration is an input. The machine goes to PET with `CAPTURE_DROP`, `PAUSE(0)` and
  `STOP`. Any pair offer is **kept**.
- While calibrating, the machine is not stepped.
- On the first frame after calibration, outcomes are processed **before** backstops are
  evaluated.

**`make check_no_logic_in_display`** (run by `make test`) fails on any of these in `display.c`:

- a file-scope or function-local static whose name, with `s_` stripped, matches an `sm_t` field
  (extracted by awk from `panel_sm.h`);
- a call to any `jpanel_*` other than `jpanel_snapshot`, `jpanel_post` or `jpanel_poll_soon`.

A renamed duplicate would pass this gate; the wave review catches that.

### 8.2 State and invariants

```
sm_t {
  mode PET|GRID|LISTEN|THINK|RECORD|PLAY, since       // THINK includes the 2.5 s FAILED dash
  play { seq, target{id, from, from_dad, replay}, since /* the press */, run_pos,
         pause_req{stream_id, on, since} }
  offer { id, from, from_dad, since }                 // the again/reply pair; independent of
                                                      // mode, drawn only in PET with count == 0
  rec  { to, heard, hush, deliberate, opened_at }
  talk { voice, heard, hush, lead, follow_armed, follow_turns, dash... }
  grid_until; send { seq, posted_at, expires_at }; notice { first_visible, big_shown }
  chime_owed; sel { id, index }; seen_ids[16]; down { at, x, y, on_pet, target, swiped }
}
```

**ON** = `snap.claimed_seq == play.seq && audio.stream_id == snap.cur.stream_id &&
audio.out_ms > 0`. That is, the message is audibly playing.

**The pair is an offer, not a mode.** That is today's semantics: a flag that survives a
conversation, a grid, a recording started by voice, and calibration.

- **Cleared by:**
  - EXIT on the pair;
  - REPLY on the pair;
  - the button's standby;
  - a new playback finishing, which replaces it.
- **Hidden** (not cleared) while `waiting.count > 0`.
- **Standing** once `due(now, offer.since + REPEAT_MS)`.

**Invariants.** Asserted after every host step and counted on the device. A violation recovers
to PET with `CAPTURE_DROP`, `PAUSE(0)` and `STOP`, plus `CUE_OOPS` if a recording was dropped.

- **I1** One mode. The microphone is open ⇔ the mode is LISTEN or RECORD.
- **I2** One function, `sm_face(sm, snap)`, both draws the top-left face and addresses reply:
  - PLAY, claimed (`snap.claimed_seq == play.seq`, including the gap between messages of a run):
    `snap.cur`;
  - PLAY, not yet claimed: `play.target`;
  - PET with a visible offer: `offer`;
  - PET otherwise: the selected waiting entry.

  The harness asserts the drawn face on **every** frame.
- **I3** The drawn controls are exactly the current mode's targets.
- **I4** Every press answers with a cue or a view change, except the claimed-and-ignored presses
  listed in §8.3.
- **I5** `play.pause_req.on` ⇒ mode is PLAY and `pause_req.stream_id == snap.cur.stream_id`.
  This uses the machine's own request, never the snapshot. Every exit from PLAY emits `PAUSE(0)`
  and then `STOP`.
- **I6** Every mode other than PET has a deadline:

  | Mode | Deadline |
  |---|---|
  | PLAY, SEND | §7.5c |
  | GRID | `SENDTO_MS` |
  | LISTEN, THINK | today's |
  | RECORD | `CAPTURE_MAX_MS + 5 s`, absolute |
  | Pause | `AUDIO_PAUSE_MAX_MS` → auto-resume |
  | PENDING | `PENDING_MS` |

- **I7** Time uses `due()` and seqs use `seq_ge()`.

### 8.3 Targets and transitions

**Global (any mode):**

- **Button, short press:**
  - in standby → wake;
  - otherwise → `CUE_STOP`, `CAPTURE_DROP`, `PAUSE(0)`, `STOP`, disarm the follow-up, clear the
    offer, standby, PET.
- **Button, hold** → deep sleep.
- **Voice "stop"** runs today's `VOCAB_STOP` handler in whatever mode it is *delivered*:
  - RECORD/LISTEN → drop;
  - THINK → `talk_clear`;
  - everywhere → disarm the follow-up and set turns to MAX.

  Delivery is governed by today's mute (LISTEN, RECORD, standby), so only the reachability is
  unchanged.
- **Wake phrase and "tell sister/dad"** go through the microphone gate.

**The microphone gate** requires:

- mode PET, GRID, or PLAY-ON-REPLY (after its `STOP`);
- not standby, not leased, `!reply_sounding`, `net != BUSY`.

A refusal plays a queued `CUE_BUSY`.

**Targets per mode.** Listed in priority order; *ignored* = claimed with no effect.

| Mode | Targets | Hold |
|---|---|---|
| PET (offer hidden or none) | NOTICE (big box or badge quadrant) · PET | from PET with `on_pet`, free taps, not swiped → GRID |
| PET, fresh offer | EXIT · AGAIN quadrant · REPLY quadrant · PET | as PET (the quadrants claim most of the centre) |
| PET, standing offer | EXIT · AGAIN disc reach · REPLY disc reach · PET | as PET → GRID at the pet centre |
| GRID | four cells · swallow | — |
| LISTEN (held) | none; the release ends it | — |
| LISTEN (voice), RECORD | CROSS · TICK quadrants (both *ignored* for `EXIT_GRACE_MS` after a press opened the recording) · swallow | — |
| THINK | PET (flinch) | — |
| PLAY | EXIT (*ignored* until `play.since + EXIT_GRACE_MS`, **in every phase**) · FACE (live while `count + (claimed ? 1 : 0) > 1`) · PAUSE / REPLY quadrants (*ignored* until ON) · swallow | — |

- The pet centre (184, 224) is ≈171 px from either disc upright, and ≈139 px on a quarter turn.
  So in a standing offer it is PET.
- A replay draws the pair layout with the playing glyph (`test_a_replay_still_reads_as_playing`,
  unchanged).

**PET**

- **Notice:**
  - while sending → `CUE_BUSY`;
  - otherwise → `CUE_PLAY`; PLAY{target = selection, since = now}.
- **AGAIN:**
  - while sending → `CUE_BUSY`;
  - otherwise → `CUE_PLAY`; PLAY{replay `offer.id`}.
- **REPLY** → microphone gate → `CUE_REPLY`, clear the offer, RECORD{to = `sm_face`}.
  - Refused → `CUE_BUSY`; the offer is kept.
- **EXIT** → `CUE_STOP`, clear the offer.
- **Swipe** → select, clamped, `CUE_HEARD`.
- **An unseen id appears:**
  - the selection goes back to the oldest (today's rule);
  - the id is added to `seen_ids`;
  - `chime_owed` is set.

**RECORD**

- **Tick, or hush, or cap:**
  - heard → effect `SEND` (the executor CASes the lease and posts), `CUE_HEARD`, PET;
  - tick, not heard → drop, `CUE_OOPS` (today).
- **Cross** → drop, `CUE_STOP`.
- **Lead-silence** → drop, plus `CUE_STOP` if the recording was deliberate.
- **A swipe** in the press that opened the recording → drop it silently and apply the swipe.

**LISTEN / THINK:** today's behaviour, except that the follow-up does not open while a press is
pending or `chime_owed` is set.

**PLAY**

- **Pending** (not yet posted): post once `!speaker_busy` (today's rule — the press cue
  finishes first). `PENDING_MS = PLAY_BUF_MS + 2000`, checked by `_Static_assert`. Expiry →
  `CUE_OOPS`, PET.
- **`post_failed`** → `CUE_OOPS`, PET.
- **Finished** (`seq_ge(snap.done_seq, play.seq)`, or `play_outcome.seq == play.seq` with
  `!run_continues`):

  | Outcome | Then |
  |---|---|
  | `heard` / `heard_unacked`, nothing waiting | `offer` = last `cur`, PET |
  | `failed` / `short` / `empty` | `CUE_OOPS`, PET (a failed replay keeps the offer) |
  | otherwise | PET |

- **EXIT** → `PAUSE(0)`, `STOP`, `CUE_STOP`, PET. No offer (owner).
- **FACE / swipe** → `PAUSE(0)`, `STOP`, `CUE_HEARD`; the selection cycles, or moves clamped,
  over `waiting` plus the current message; PET (JP 0.3.37).
- **PAUSE** (ON) → toggles `pause_req` for `cur.stream_id`, `CUE_PAUSE` / `CUE_RESUME`.
- **REPLY** (ON) → `PAUSE(0)`, `STOP`, microphone gate:
  - opened → `CUE_REPLY`, RECORD{to = `sm_face`};
  - refused → `CUE_BUSY`, PET.

**SEND**

- `post_failed` for a send → `CUE_OOPS` at once, clear `send`; render has already released the
  lease.
- When the outcome arrives:

  | Outcome | Cue |
  |---|---|
  | SENT | `CUE_SENT` |
  | NOBODY | `CUE_OOPS` until F4 adds `CUE_NOBODY` |
  | EMPTY | `CUE_STOP` |
  | FAILED | `CUE_OOPS` |

- Backstop per §7.5c.

### 8.4 New messages and the notice

- **"New"** = an id not in `seen_ids`. With no ids from the box, a rise in the count stands in.
- **The chime.** `chime_owed` turns into a queued `CUE_MESSAGE` on the **first frame** where:
  - the capture is closed;
  - `!reply_sounding`;
  - the mode is not PLAY.

  The arbiter's expiry counts only from eligibility, so a message that arrives early in a 30 s
  recording still chimes.
- **`notice.first_visible`** is stamped on the first frame the notice is actually drawn, **once
  per wait** — a wait starting when the count goes from 0 to above 0. A second arrival does not
  restart it. A wake from dark restarts it (JP).
- **`sm_busy()`** = mode ≠ PET, or sending, or `snap.phase ∈ {FETCHING, SOUNDING}`. Polls and
  acks never hold the screen.
- **The sleep `arrived` wake** reads the machine's "new" edge.

### 8.5 Tests (`sm_tests.c` → `run_sm_tests`; part of `make test`)

- **Appendix A** gives the disposition of every `ui_tests.c` test.
- **Journey tests** (§3, §9).
- **`test_storm`.** 300k seeded frames:
  - snapshots dropped (the old copy kept), delayed, and duplicated;
  - touches dropped and repeated;
  - clock jumps, including the u32 wrap;
  - random calibration;
  - the fake engine at its published deadlines;
  - 30 s recordings with arrivals during them.

  Every frame asserts I1–I7 and the drawn face. Every non-PET mode must end by its deadline, and
  no backstop may fire while the fake engine honours its deadlines.
- **End to end.** `jpanel_tests` drives `sm_step` against the real `jpanel_core` and `stream.c`.

### 8.6 Landing and rollback

F3 replaces the logic in one wave. Shadow mode is rejected: divergences are mostly the intended
fixes, and a shadow would double the state on a task with a small stack.

**Rollback** is a source-revert PR covering F3, and F4 too if it has landed (it builds on F3). It
bumps `version.txt` and commits a rebuilt `dist/`, because `firmware.yml` byte-compares the image.
The owner applies it from Ops → Update, and the panels OTA because the version differs.

---

## 9. Per-finding detail

§7 and §8 carry the design. This section adds the causes and the tests.

**A1**

- **Cause:** `fetch_head` resets `s_stopped`/`s_run`; the exit's `CUE_STOP` makes the wait late;
  `audio_stream_begin` clears pause.
- **Tests:**
  - `test_stop_at_every_interleave_point` — queued, speaker wait, head, after claim, chunk,
    paused, drain, pre-ack, run gap;
  - `test_stop_with_no_stream_then_play_starts`;
  - `test_stop_while_paused_drains`;
  - `test_stop_during_drain_frees_speaker`;
  - `test_stale_body_not_drained` (the sink gen check);
  - `test_stop_during_5s_head_delay_cancels_request` (`wsproto.c`);
  - `test_next_queued_during_drain_classifies_previous_first`;
  - `test_run_gap_stop_never_acks_unsounded`.

**A2**

- **Test:** `test_chain_fetch_failure_returns_to_idle` (link / 500 / 204 / speaker busy).

**A3**

- **W1:** raw ceiling `PANEL_AUDIO_MAX` + 0.5 s.
- **F2:** status mapping 400 → `EMPTY`, 409 → `NOBODY`, 413 → `FAILED("too-long")`.
- **Pins:**
  - `test_capture_caps_pinned`: `PANEL_AUDIO_MAX == CAPTURE_MAX_MS × 32`, and
    `panel_ws.MAX_BODY > PANEL_AUDIO_MAX` — the panel's cap;
  - `voiceMessage.MAX_MESSAGE_MS == jpanel.MAX_MESSAGE_MS` — the owner's cap, pinned separately.

**A4**

- **Tests:**
  - `test_lost_ack_is_retried_and_never_replays`;
  - `test_ack_4xx_is_terminal`;
  - `test_ack_gives_up_after_10_min`;
  - `test_owed_played_id_filtered_from_waiting`;
  - backend `test_waiting_queue_carries_ids`;
  - backend `test_next_by_id_serves_that_row_or_204`.

**A5**

- **Tests:** `test_lease_taken_before_post`, `test_lease_released_on_post_failure`,
  `test_upload_bytes_unchanged_under_listen_attempts`, `test_follow_up_open_handles_refusal`.

**A6**

- **F3:** a message counts as heard iff `out_ms ≥ min(HEARD_MIN_MS, 0.8 × duration)`, and only
  with a delivery header.
- **W3:** `POST /returned {id, delivery}` runs
  `UPDATE … SET deliveries = deliveries-1, returns = returns+1 WHERE id=:id AND played_at IS NULL
  AND deliveries=:delivery AND returns<5`. It answers 204 or 409, and 404 for a non-uuid id. It
  is added to `panel_ws._ALLOWED`.
- **Tests:**
  - `test_stop_in_preroll_returns_not_acks`;
  - `test_stop_after_threshold_acks_with_heard_ms`;
  - `test_returned_is_idempotent_and_bounded`;
  - `test_returned_refunds_only_own_unplayed` (sibling case);
  - `test_panel_ws_tunnels_returned_and_new_headers`;
  - `test_returned_404_counts_no_refund`;
  - `test_returned_reinserted_and_polled`;
  - `test_face_cycles_without_skipping` (2, 3 and 8 messages);
  - `test_run_never_replays_a_returned_message`;
  - `test_message_arriving_mid_run_joins_it`.

**A7, A8, A9**

- **Tests:** `test_snapshot_is_never_torn`, `test_send_outcome_survives_concurrent_chain`,
  `test_cursor_writers`.

**B1**

- **Test:** `test_reply_during_playback_opens_a_working_recording`.

**B2**

- **Tests:** `test_hold_at_pet_centre_with_badge`, `…_fresh_offer_is_reply`,
  `…_standing_offer_opens_grid`, `…_during_play_is_swallowed`.

**B3**

- **Fix:** I2. The offer is hidden while a message waits, which settles JP's open "face
  collision".

**B4**

- **Tests:**
  - `test_cue_expiry_starts_when_eligible`;
  - `test_refusal_behind_reply_sounds_after_it`;
  - `test_message_during_30s_recording_chimes_after`;
  - `test_message_during_pet_reply_gets_chime_and_big_box`;
  - `test_heard_then_oops_both_sound`.

**B5**

- **Test:** `test_notice_and_again_refused_during_send`.

**B8**

- **Tests:** `test_silent_recording_says_dropped`, `test_every_refusal_sounds_busy`.

**B10**

- **Tests:** `test_press_behind_pet_reply_is_served`, `test_follow_up_yields_to_pending_press`,
  `test_deadlines_survive_u32_wrap`.

**B-UX**

| Gap | Decision | Wave |
|---|---|---|
| The exit takes two presses | One press (owner) | F3 |
| Every failure sounds the same | SENT / NOBODY / EMPTY / FAILED / BUSY (§8.3) | F3 (placeholders), F4 (contours) |
| No sign that a send is in progress | Overlay; `CUE_SENT` contour | F4 |
| Text instructions for pre-readers | Icons and pips; names kept | F4 |
| The badge | Pips and pulse; no reminder sound (JP) | F4 |
| "1/2" counts the remaining messages | `run_pos / (run_pos + remaining)` | F3 |
| Arrivals during a conversation | Chime on eligibility; notice from first visible | F3 |
| The pair blocks the hold | Standing offer (owner) | F3 |
| No hint for ending a recording | Glyphs and a hush ring | F4 |

**C1–C11**

- **C1:** §11.
- **C2:** `send_text` validates before TTS and returns 404. The PWA refetches and keeps the
  draft. W2's grouping removes ghost chips.
- **C3:** `POST /messages/{id}/retry` and `DELETE /messages/{id}`. The delete applies only to
  owner-sent or undelivered rows, and frees the blob via `blob_referenced`. Both reject non-uuid
  ids with 404.
- **C4:** columns `heard_ms` and `offered_at`. The copy says "Played".
- **C5:** a between row appears in both threads. Clearing either thread deletes it unless it is
  unheard — the thread deletes exactly the rows it shows.
- **C6:** per-thread `row_number()` limit, UNION every unplayed owner row, and `?before=`.
  **W1 owns the query function and `/summary`**, so the badge always equals what is rendered.
- **C7:** 206/416 and `Accept-Ranges`. uuid guards on: audio, owner-read, the C3 routes,
  `/next?id=`, `/returned`, and panel `/played`. A firmware 404 on `/played` means "row gone" →
  clear.
- **C8:** `errorDetail` joins `detail[].msg`; `maxLength={600}`; TTS timeout 60 s; the counter
  and hint come in P1.
- **C9:** `kept_for` per recipient.
- **C10:** the PWA clamps PCM to `MAX_MESSAGE_MS × PANEL_RATE` before upload (vitest
  `clamps_to_cap`). The box refuses only bodies over cap + 0.5 s (`test_owner_audio_at_30_02s_is_accepted`,
  `test_owner_audio_well_over_cap_is_413`).
- **C11:** request seq plus epoch.

**C-UX**

| Gap | Decision | Wave |
|---|---|---|
| Notification | Launcher badge (W1 `/summary`); push deferred | W1, P1 |
| "Not heard" is ambiguous | `offered_at` / `deliveries` statuses | P1 |
| Composer | Hint + counter | P1 |
| Read-marking | 1.5 s dwell + `document.hasFocus()` | P1 |
| Sweep | Kept; footer note | P1 |
| Renames rewrite the past | Not fixed: renames are corrections | — |
| Two clears | Kept; confirm text names what remains | P1 |

W1/W2 PWA changes (kept draft, hidden retired chips) are behaviour only, with no new surface or
visual design. So they sit outside the GUI gate; P1's visible changes go through it.

---

## 10. Host foundations (F1, behaviour-preserving)

1. **`link_types.h`, `jpanel_types.h`, and `wsp_wait_step` / `link_left` in `wsproto.c`.**
2. **`stream.c` with `stream_tick`.** Extract today's per-chunk decisions **as they are**;
   `audio.c` keeps I/O only; add `check_no_logic_in_audio`.
3. **`jpanel_core.c` behind `jpanel_io_t`.** `jpanel.c` keeps the task, the queue, cJSON,
   mbedtls and the real io.
4. **Suites `run_jpanel_tests` and `run_stream_tests`, plus both `check_*` gates**, all wired
   into `make test`. CI runs only `make -C firmware/host test`. The fakes:
   - a scripted box (with a 0.3.39 mode);
   - the real ring and stream;
   - a fake clock;
   - a test-only SHA-256 (no new tool, so `dev-setup.sh` is unchanged);
   - yield hooks.
5. **Characterisation tests** assert 0.3.39's behaviour, bugs included, each named with its
   finding. F2 inverts them.
6. **The hooks' reach is engine-side only.** Cross-core ordering is reviewed against the fences
   in §7.2 and watched through telemetry.

**Backend tests that read firmware.** Found by
`grep -rnoE '"[a-z_0-9]+\.(c|h)"' backend/tests` (one false hit, `test_web.py` `"a.c"`) plus the
`_fw()` and `const()` helpers. Each retarget lands in the wave that moves the code it reads.

| Test (file :: function) | Reads | Retarget |
|---|---|---|
| `test_jpanel_api` :: `test_the_firmware_builds_exactly_that_base` | `jpanel.c`, `link.c` | F1 (`jpanel_core.c`, `link_types.h`) |
| `test_jpanel_api` :: `test_both_ends_agree_on_the_name_of_the_integrity_header`, `test_the_cap_fits_the_buffer_the_panel_receives_it_in`, `test_the_panel_asks_for_a_position_the_way_this_route_reads_one`, `test_the_panel_can_hold_the_longest_poll_the_box_will_send`, `test_the_panel_retries_only_the_status_a_retry_can_fix` | `jpanel.c` | F1 → `jpanel_core.c`; poll buffer checks 1536 B in F2 and 1024 B for 0.3.39 in W3 |
| `test_jpanel_api` :: `test_the_panel_reads_the_queue_this_poll_serves`, `test_the_panel_reads_the_sibling_name_this_poll_serves` | `jpanel.c`, `display.c` | F1 (`jpanel.c` stays, for cJSON) / F3 (`panel_sm.c`) |
| `test_jpanel_api` :: `test_the_cap_fits_the_bubble_the_panel_draws_it_in` | `display.c`, `font.h`, `ui.h` | F3 (`ui.h` → `panel_sm.h`; draw width stays in `display.c`) |
| `test_jpanel_api` :: `test_the_queue_cap_is_the_same_number_on_both_sides` | `jpanel.h`, `ui.h` | F1 (`jpanel_types.h`), F3 (`panel_sm.h`) |
| `test_jpanel_api` :: `test_the_firmware_counts_repeats_in_sixteen_bits` | `font.c` | none planned |
| `test_jpanel_api` :: `test_dads_voice_is_male` | `speech.c` | none planned |
| `test_jpanel_api` :: `test_the_firmware_sends_the_repeat_count_and_the_raw_decode`, `test_the_settings_poll_carries_the_waiting_count` | `main.c`, `ota.c` (pins `jpanel_poll_soon()`) | name kept; none |
| `test_endpoint_report_now` :: `test_a_replay_that_ends_re_arms_the_buttons`, `test_every_way_of_starting_a_message_waits_for_the_speaker`, `test_the_scan_found_a_realistic_number_of_fields` | `jpanel.c` | F1 → `jpanel_core.c` |
| `test_endpoint_report_now` :: `test_a_cue_does_not_shorten_a_recording` | `audio.c` (deafen block) | F1 → `stream.c` |
| `test_endpoint_report_now` :: `test_every_talk_failure_the_firmware_can_hit_carries_a_name` | `talk.c` | F2 (reply request) |
| `test_endpoint_report_now` :: `test_an_open_microphone_is_deaf_to_commands`, `test_commands_are_muted_while_a_message_is_recorded`, `test_every_icon_reaches_the_same_path_the_voice_does`, `test_the_button_toggles_and_is_debounced` | `display.c`, `speech.c/h` | F3 (mute stays in `display.c`; paths → `panel_sm.c`) |
| `test_endpoint_report_now` :: `test_both_halves_of_the_dash_are_counted_and_told_apart` | `ui.c`, `display.c` (`st->dashes++`, `s_dashes++`, "timeout"/"net", `talk_state() ==`) | F3 → `panel_sm.c` (one counter) |
| `test_endpoint_report_now` :: `test_a_failed_fetch_cannot_look_like_a_change`, `…_retried_until_it_sends`, `test_the_firmware_adopts_before_it_acts`, `test_the_panel_can_stop_believing_its_own_radio`, `test_the_panel_reports_why_it_could_not_reach_the_box`, `test_the_registers_the_datasheet_cleared_are_left_alone`, `test_the_box_serves_the_key_the_firmware_reads` | `main.c`, `ota.c` | none planned |
| `test_endpoint_api` :: `test_the_ceiling_is_the_panel_s_buffer` | `talk.c` (`audio_play`) | F2 |
| `test_endpoint_api` :: `test_the_panel_waits_longer_than_the_question_it_allows`, `test_the_recording_window_outlasts_the_silence_it_waits_for` | `audio.c` (`CAPTURE_MAX_MS`), `display.c`, `talk.c` (`TALK_HTTP_TIMEOUT_MS`), `ui.h` | F1 (only if the cap constant moves to `stream.c`), F3 (`LISTEN_LEAD_MS` / `LISTEN_HUSH_MS` / `TALK_TIMEOUT_MS` → `panel_sm.h`) |
| `test_endpoint_api` :: `test_the_panel_actually_sends_the_screen_stage`, `test_the_panel_s_whole_payload_fits_the_buffer_it_is_built_in`, `test_it_strips_the_phrase_…`, `test_the_listen_entry_…` | `main.c`, `vocab.c` | none planned |
| `test_panel_ws` :: `test_the_firmware_spells_the_same_route_and_frame` | `link.c`, `wsproto.h` | F1 (`link_types.h`) |
| `test_nudge` :: two tests; `test_g2p` | `nudge.c/h`, `vocab.c` | none planned |

## 11. Identity (W2): no RLS change

**Problem (C1).** `reflash` keeps the subject but replaces its principals. Unplayed mail
addressed to a dead key is stranded.

**Fix.** Panel routes, RLS, nudge, `_waiting_count`, `/converse`, rename and revoke are
unchanged. `panel_ws` changes only by W3's `_ALLOWED` row.

1. **`readdress_orphans(session)`**, idempotent, owner context. It re-addresses unplayed rows
   whose recipient is a revoked key to **the same subject's** single live jpet key, if one
   exists. It never crosses subjects.
   - **Called from** `reflash` and `rotate` (in their transaction; the INSERT uses `RETURNING
     id`), the retention loop (`sweep.py`, every cycle), and right after each insert in `send`,
     `send_text` and `send_audio`, through a narrow internal owner context.
   - This closes the race in which a message is inserted while a re-flash is in flight.
   - Test: `test_send_racing_reflash_is_carried`.
2. **`reflash` picks a subject in this order:**
   1. the one owning the most recent key (live or revoked) for that name and role;
   2. the oldest;
   3. a new one.
3. **Owner views group by subject** (owner context, read-only). `messages()`, `pet_chats`,
   `/summary` and the counts join `principals.subject_id`.
   - A subject with a live jpet key uses that key's id as `device_id`, so send, rename and clear
     keep working.
   - A subject without one is marked `retired` (`device_id` = the subject id) and is listed only
     if it has rows.
   - A NULL subject or a hard-deleted principal goes to **"Unknown panel"** (`device_id` =
     `unknown`), which can be cleared.
   - `DELETE /messages`, **`DELETE /chats`**, the badge and `/summary` resolve the id to the
     subject's principals.
   - Display-role turns get their own **"Displays"** group.
4. **Explicit merge.** `merge_subject(source, into)` refuses unless:
   - the source and target have the same `device_role`;
   - the target has a live key;
   - the source has none.

   It re-addresses the source's unplayed mail, re-points its revoked keys, and tombstones its
   name as `merged:<into>:<source>`, which `reflash` can never match and `DevicesRepo.list`
   hides. It is logged. It is exposed twice, both from W2:
   - owner: `POST /jpanel/panels/{id}/merge`;
   - **debug:** `POST /api/debug/jpanel/panels/{id}/merge` under `_OWNER_CTX` (as
     `/endpoint/flash` is), with a `debug-connect.sh jpanel-merge` verb, a
     `docs/runbooks/DEBUG_ACCESS.md` row, and `test_debug_merge_route` (CLAUDE.md #10).
5. **The one-time data migration is `readdress_orphans`.** No schema change.

**Rollback.** None is needed: the rows stay principal-keyed, and revision files are never
removed (§6). Alembic is transactional, so a failed migration leaves the old head in place, and
Ops → Update shows the failure.

**This touches principal scope:** it re-points keys and moves mail between principals. So W2 is
**escalated to the owner, and its wave gate includes a red-team review** (PROCESS.md).

### 11.2 Tests

There are no new tables or policies, so the existing RLS isolation tests still apply. New tests:

- `test_reflash_carries_unplayed_mail_to_new_key` — played rows and **sibling** rows are
  untouched;
- `test_rotate_carries_unplayed_mail`;
- `test_send_racing_reflash_is_carried`;
- `test_readdress_orphans_idempotent`;
- `test_reflash_after_revoke_lands_on_last_used_subject`;
- `test_owner_threads_group_by_subject_across_reflash`;
- `test_unknown_panel_group_clearable`;
- `test_retired_listed_only_with_rows`;
- `test_clear_pet_chat_covers_old_keys`;
- `test_merge_rules_and_tombstone`;
- `test_debug_merge_route`;
- `test_identity_backfill_pg`;
- `test_send_after_reflash_delivers`.

## 12. Waves

One PR per wave. Each package runs its **whole** gate set from its own directory
(CLAUDE.md #11):

- **backend:** `ruff check .`, `ruff format --check .`, `pyright`, `pytest`. This also runs on
  **every firmware wave**, because the backend CI filter covers `firmware/main/**`.
- **frontend:** `npm run lint`, `npm run typecheck`, `npm run test`, `npm run build`.
- **supervisor:** the full set, because the `firmware/dist/**` filter triggers it.
- **firmware:** `make -C firmware/host test`.

Every firmware wave:

- bumps `version.txt`;
- commits a rebuilt `dist/` (`scripts/firmware-setup.sh` once, `idf.py build`,
  `scripts/firmware-dist.sh`);
- keeps the bootloader byte-identical;
- applies the §10 retargets it owns.

Every wave also:

- runs `scripts/docs-freshness.sh`;
- updates this plan, JP §3b, the firmware READMEs, and the ROADMAP and `plans/README` rows;
- adds no new dependency without updating `dev-setup.sh` (#8);
- adds no shell-only operator step (#10).

F3 additionally updates JP's exit, pair and refusal text.

| Wave | Contents | Depends on | Gates |
|---|---|---|---|
| **W1** box and PWA, invisible | A3 box; C2; C5; C6 (query function + `/summary`); C7 audio route; C8 (non-visual); C9; C10 (+ PWA clamp); C11; skew pins; CI check against deleting migration files | — | backend, frontend, docs |
| **W2** identity | §11 (owner + debug merge, `readdress_orphans`); retired chips hidden. **Escalation + red-team review.** | W1 | backend, frontend, docs |
| **W3** contract | `/waiting` ids + `offered_at`; `/next?id=` + headers; `/played {heard_ms}`; `/returned` + `_ALLOWED` + tunnel test; C3 routes; remaining uuid guards; columns `heard_ms`, `offered_at`, `returns`; the 0.3.39 buffer pin | W2 | backend, docs |
| **F1** host foundations | §10. No behaviour change. | — (parallel) | firmware, backend, supervisor, docs |
| **F2** engine | §7. Defects only; wrappers. | F1, W3 | firmware, backend, supervisor, docs |
| **F3** state machine | §8; Appendix A; Q1–Q3 (`CUE_BUSY` placeholder); hand-back on | F2 | firmware, backend, supervisor, docs |
| **F4** panel visuals | Overlay, pips, pulse, glyphs, hush ring; `CUE_NOBODY` / `CUE_SENT` / `CUE_BUSY` contours. **GUI gate:** three interactive mocks (WebAudio) first, the chosen one saved to `docs/mocks/jpanel-panel-messaging/`. | F3 | firmware, backend, supervisor, docs |
| **P1** PWA | C3 buttons; C4 copy; C5 status; composer; launcher badge; read dwell; retention line; retired threads + merge button; "Displays"; "Load earlier"; delete/revoke copy. **GUI gate:** mocks first. | W2, W3 | frontend, docs |

Order: W1 → W2 → W3 → F1 → F2 → F3 → F4, with P1 any time after W3. F1 can run in parallel.

## 13. Risks, losses, questions

**Risks**

- **F3 is a one-wave replacement.** Mitigated by §8.5 and §8.6.
- **Cross-core ordering is reviewed, not tested.**
- **HTTPS fallback waits run until their deadlines**, which are published and bounded.
- **`check_no_logic_in_display` matches names**, so it is backed by review.

**What the owner loses**

- Web push.
- Resend after a failed send.
- "Played" instead of "heard".
- Renames rewrite the past.
- A stop under 2 s returns the message.
- No notice or replay during her own upload ("busy").
- A cancel before the head can cost one attempt.
- Each pre-0211 subject needs one explicit merge.
- An ack given up after 10 minutes may replay later, within the delivery cap.

**Questions, with the defaults the plan runs on**

1. `HEARD_MIN_MS = 2 s`.
2. The 600-character text cap stays.
3. Web push stays deferred.
4. PLAY waits only for a free speaker, not for a pet turn in flight (today's behaviour). A late
   pet reply preempts the stream, which then resumes.

## 14. Review log

**Rounds 1–3** (50 blocking, 89 non-blocking). Every item was verified and accepted. What
still stands is listed in the intro. The mechanisms they introduced and later removed:

- the event queue;
- the subject/RLS rewrite and its revert;
- the busy token;
- `PEND_REPLY`;
- the stall and codec-wedge discards;
- summed bounds.

**Round 4** (16 blocking). Every claim was checked against the code. Examples:

- `ws_send` waits up to `LINK_SEND_MS` for both the lock and the send;
- upload loops bound only individual frames;
- HTTPS takes a fresh `timeout_ms` per step;
- `ui.c` stamps `controls_since` on `PEND_PLAY`;
- the replay draws the pair layout;
- the debug API has no jpanel route, and `owner_only` returns 403 to non-owners;
- `voiceMessage.ts` stops on a timer;
- `send` resolves the recipient before transcribing.

All were accepted and resolved **by class**:

| Class | Items | Structural fix |
|---|---|---|
| 1 Time bounds | FW-1/2/3/4, UX-3/8 | `link.c` enforces an absolute deadline; the engine publishes its current deadline; the UI never adds durations; no codec rule; ON from audio facts; pause belongs to the machine (§7.5, §8.2) |
| 2 Dropped behaviour | UX-1/6/7 and their kin | Appendices A and B, enumerated mechanically; the pair is an offer that survives what it survives today; exit grace from the press in every phase; the voice-stop handler is today's |
| 3 Cue lifetime | FW-5, UX-4 | Expiry from eligibility; chime on the eligibility edge; refusals behind a reply sound after it |
| Other | UX-2, UX-5; BE-1/2/3 | `sm_face` = `snap.cur` while claimed (every frame asserted); SEND `post_failed`; AGAIN "busy" while sending; debug merge route; PWA clamp + slack; `readdress_orphans` everywhere |

**Round-4 non-blocking items (40), all accepted:**

- **Engine and audio:**
  - `make test` wiring;
  - `jpanel_types.h`;
  - the SPSC cue store and arbiter-rendered cues;
  - sink gen check;
  - old-box stop policy;
  - audio facts read directly;
  - RETURNED re-insert;
  - 10 s ack deadline;
  - 60 s send expiry;
  - PLAY vs talk turn (Q4).
- **UI machine:**
  - reply-bounce grace;
  - FACE liveness;
  - "short" never acked;
  - outcome before ack;
  - notice clock wording;
  - THINK includes FAILED;
  - `used` terms;
  - a name-based gate backed by review;
  - RECORD deadline;
  - violation cue;
  - tick-into-silence decided;
  - F2/F3 cue mapping;
  - calibration order;
  - standby disposes of a reply;
  - voice stop in RECORD.
- **Backend and docs:**
  - full census;
  - revision CI check;
  - E2.4/E2.5 reworded;
  - `DELETE /chats` and "Unknown panel";
  - C5 clear rule;
  - `offered_at` semantics;
  - uuid guards;
  - `RETURNING` delivery;
  - W2 escalation;
  - `/summary` ownership;
  - test rename;
  - `audio.c` drift;
  - GUI-gate exemption stated.

---

## Appendix A — every `ui_tests.c` test (51)

**Dispositions:**

- **U** — unchanged: ported to `sm_step` with the same assertions.
- **R** — rewritten, with the new assertion stated.

No test is deleted.

| # | Test | Disp. | Note / new assertion |
|---|---|---|---|
| 1 | `test_the_order_is_a_list_and_ends_with_the_pet` | R | Each mode's target table is asserted literally, and each ends in PET or swallow. |
| 2 | `test_the_grid_is_modal_and_outranks_everything` | U | |
| 3 | `test_the_notice_is_tappable_only_in_one_quadrant_today` | U | |
| 4 | `test_a_cue_no_longer_decides_what_a_press_means` | U | |
| 5 | `test_the_exit_ends_a_message_while_it_is_audible` | U | |
| 6 | `test_the_notice_is_painted_through_its_own_arrival_cue` | U | |
| 7 | `test_a_hold_after_a_poke_still_opens_the_menu` | U | |
| 8 | `test_the_controls_are_pressable_for_as_long_as_they_are_drawn` | U | The transport is claimed (and ignored) from the press. |
| 9 | `test_a_second_press_cannot_cancel_what_the_first_one_started` | U | Exit grace from the press, every phase. |
| 10 | `test_the_face_follows_the_message_that_is_playing` | U | Plus the every-frame face assertion. |
| 11 | `test_a_notice_does_not_show_through_the_playback_screen` | U | |
| 12 | `test_a_swipe_changes_which_waiting_message_is_offered` | U | |
| 13 | `test_one_waiting_message_cannot_be_swiped_away` | U | |
| 14 | `test_a_swipe_stops_the_message_it_moves_off` | U | |
| 15 | `test_a_swipe_is_not_also_a_hold` | U | |
| 16 | `test_a_long_press_on_a_menu_is_just_a_press` | U | Fresh pair; the standing case is a new test. |
| 17 | `test_the_selected_message_is_the_one_that_plays` | R | Asserts the posted **id** (index on an old box). |
| 18 | `test_the_numeral_says_which_of_how_many` | U | Plus a new during-playback count test. |
| 19 | `test_a_new_message_sends_the_selection_home` | U | |
| 20 | `test_every_playback_control_answers_the_finger` | R | Reply mid-message → `mode == RECORD` and `CUE_REPLY` once the microphone opens (was `pending == UI_PEND_REPLY`). |
| 21 | `test_the_draw_layers_are_a_list` | R | The per-mode view composition is asserted; the grid is still on top. |
| 22 | `test_a_notification_arrives_and_a_tap_plays_it` | U | The post waits for the free speaker. |
| 23 | `test_a_press_does_not_evaporate_behind_its_own_cue` | U | `PENDING_MS` is larger; same assertions. |
| 24 | `test_a_press_during_the_arrival_cue_plays_the_message` | U | |
| 25 | `test_the_run_controls_appear_from_the_press_not_the_stream` | U | |
| 26 | `test_the_top_right_corner_ends_a_message_while_it_plays` | R | Second half: after the exit, mode PET with **no offer** (owner Q2). |
| 27 | `test_the_pair_waits_for_a_finger_not_a_clock` | U | Plus new: standing after 10 s and still drawn; hidden while a message waits. |
| 28 | `test_reply_records_to_whoever_just_spoke` | U | The recipient comes from `sm_face` (the same value). |
| 29 | `test_sending_does_not_raise_the_playback_controls` | U | |
| 30 | `test_a_short_button_press_darkens_blanks_and_mutes` | U | Extended to every mode; asserts no `CAPTURE_OPEN` after standby. |
| 31 | `test_a_five_second_hold_requests_deep_sleep_exactly_once` | U | |
| 32 | `test_a_hold_on_the_pet_opens_the_grid_at_every_free_tap_count` | U | |
| 33 | `test_the_grid_reaches_all_four_people` | U | |
| 34 | `test_the_grid_closes_itself_but_a_stray_finger_does_not` | U | |
| 35 | `test_a_tick_pressed_into_silence_refuses_to_send` | U | `CUE_OOPS` is kept. |
| 36 | `test_the_cross_discards_and_the_pet_is_not_poked` | U | |
| 37 | `test_a_named_turn_with_nobody_speaking_costs_nothing` | U | |
| 38 | `test_a_failed_turn_says_so_rather_than_going_quiet` | U | |
| 39 | `test_every_dash_is_counted_and_its_two_causes_told_apart` | U | |
| 40 | `test_a_reply_arms_a_follow_up_and_the_cap_ends_it` | U | |
| 41 | `test_stop_said_out_loud_leaves_every_state` | U | Today's handler, including the RECORD drop. |
| 42 | `test_pressing_the_face_cycles_through_the_waiting_messages` | U | |
| 43 | `test_the_face_is_not_a_control_with_one_message` | U | |
| 44 | `test_pressing_replay_twice_does_not_pause_what_never_started` | U | |
| 45 | `test_a_held_stream_resumes_rather_than_being_thrown_away` | U | |
| 46 | `test_the_notice_shrinks_to_a_badge_and_a_second_one_does_not_restore_it` | U | Plus new: hidden-at-arrival starts the clock at first visibility. |
| 47 | `test_the_notice_stays_painted_while_a_cue_sounds` | U | |
| 48 | `test_the_queue_count_only_sounds_on_the_way_up` | U | Count-only (old box) mode; plus a new id-based test. |
| 49 | `test_what_the_box_said_about_a_message_is_answered_in_sound` | R | Inputs come from snapshot outcome seqs (was `jstate`). EMPTY → `CUE_STOP`; refusals → `CUE_BUSY`; SENT / NOBODY / FAILED as today. |
| 50 | `test_a_replay_still_reads_as_playing` | U | |
| 51 | `test_the_action_list_never_overflows` | R | The worst frame rebuilt from F3's effects (no `UI_PEND_REPLY`); bound unchanged. |

**`tests.c` groups the plan touches** — all U:

- **ring:** `test_an_empty_ring_*`, `test_a_read_run_never_crosses_the_seam`,
  `test_the_cursors_survive_their_own_wrap`, `test_a_reset_empties_*`,
  `test_what_goes_in_comes_out_*`, `test_a_partial_write_*`, `test_a_lone_byte_*`. `ring_reset`
  stays; `stream.c` simply does not call it.
- **confirm / who-grid:** `test_the_tick_and_the_cross_split_the_bottom_half`,
  `test_the_transport_discs_do_not_move_between_play_and_pause`, `test_the_who_grid_*`,
  `test_the_targets_follow_a_turned_panel`. A new `test_confirm_disc_reach` is added for the
  standing offer.
- **wsproto:** frames, window, fallback, reconnect. New tests are added for `wsp_wait_step` and
  `link_left`.
- **cue:**
  - the sweeps (`test_every_cue_renders_something_audible`,
    `test_no_two_cues_are_the_same_sound`, …) cover the new `CUE_BUSY` and `CUE_NOBODY`
    automatically;
  - new: `test_cue_busy_distinct`;
  - new: `test_cue_store_*` (in `stream_tests`).

## Appendix B — owner decisions the machine touches

**K** means kept. **C** means changed: only by the §4 decisions (marked ★), or because the old
behaviour is itself the finding (marked †).

| Source | Decision | Disp. |
|---|---|---|
| JP Part B; `display.c` "pop-up box" | A pop-up when a message waits; it does not auto-play | K |
| `ui.h` / `display.c` POPUP_BIG ("very large … 15 seconds … smaller one top left") | Big for 15 s, then a badge; a second arrival does not restore it | K (clock from first visibility †) |
| JP sleep rules; `display.c` dark wake | The big box is restored after dark; waiting does not hold the screen awake | K |
| JP "multiple messages are one press, and a finger gets out" | One tap plays all of them; a finger stops it | K (mid-run arrivals join) |
| JP 0.3.33 "the count became a position" | `2/4` position, swipe line | K (count includes the playing message †) |
| `ui.h` / `display.c` REPEAT ("replay button … 10 seconds") / 0.3.25 ("no more waiting for it to time out") | The pair never expires | K; **standing after 10 s ★** |
| 0.3.25 "the face stays for the whole exchange" (`display.c`) | The sender's face is shown with the pair | K |
| 0.3.25 "The new message takes the corner" | A waiting message displaces the pair's corner | K (offer hidden) |
| `display.c` "top right where there's no icon" | Exit corner | K; **one press during playback ★** |
| 0.3.29 "pause icon for half a second" (`EXIT_GRACE_MS`) | The exit refuses for 700 ms after the press | K, every phase |
| 0.3.28 "exit swallowed presses aimed at the notification" (`notice_offered` guard) | The notice outranks the exit | K |
| 0.3.32 "every playback control answers the finger" | Distinct cues for play / pause / resume / reply / stop | K |
| 0.3.24 "as soon as I click it … show right away" | Controls appear from the press | K |
| 0.3.27 "pause button over a message that did not exist" | No controls during a send | K |
| 0.3.30–0.3.32 "little girl icon … versus the dad icon" (×3) | The face shows the playing message's sender | K (`sm_face`) |
| JP 0.3.33 "notification showed in the background of the menu" | No notice during playback | K |
| JP 0.3.33 swipe ("swipe left and swipe right … icon updates … number") | Swipe changes the selection, clamped; the selection goes home on new arrivals | K |
| `ui.c` "handle stopping the current playing message" | Swipe and face stop the sound | K |
| JP 0.3.37 "press the icon on the top left … cycle" | Face cycles | K |
| JP 0.3.36 "click the play button sometimes it just pauses" | Transport ignored until it sounds | K |
| JP 0.3.35 "long press treated as a normal press of menu items" | A hold on a control = a press | K |
| `display.c` / `ui.h` "make sure long press will pull up the menu even after multiple presses"; "change long press … pull up the same thing" | Hold → grid at any free tap count | K |
| JP 0.3.33 "occasionally … commands get recognized" | Commands muted in LISTEN / RECORD / standby | K |
| `ui.h` / `display.c` "a wake word … as if I held the panel" | Hands-free listen | K |
| `ui.h` / `display.c` "after the text-to-speech … turn" (follow-up) | Auto-continue, capped at 6 | K (yields to pending/chime) |
| `display.c` "it wants to keep going even if I say stop" | Voice stop ends the auto-continue | K |
| `display.c` "anytime we get a red … logged" | Dash counted and named | K |
| `ui.h` "the babies keep getting cut off" (hush 1.8 s, lead) | Hush and lead timings | K |
| JP 0.3.38 "reply … doesn't actually get sent" | A failed send says so | K |
| JP 0.3.33 "it doesn't really play every single time" (wait for the speaker) | The press waits out its cue; `PENDING_MS` | K (window lengthened †) |
| `display.c` 0.3.26 "single press blinks the screen and stops listening"; "hold … 5 seconds … lowest power" | Button = standby / deep sleep, from any mode | K |
| `display.c` "it still just shows a frozen robot" (black frame) | Dark is painted | K |
| JP "Dim is a number"; "voice does not hold the screen awake"; "the pet answering still holds it" | Sleep `used` terms | K |
| JP "A long press seems to just reset the device" (`display.c`) | Release-based button | K |
| JP "Dad's voice is male"; "PWA can send audio"; "one child, one conversation"; "two clears"; "conversation window, clear history"; "panel picker radio" | PWA / box decisions | K |
| `jpanel.c` ack-on-stop ("she heard it and chose to stop") | A stop acknowledges | C † (hand-back below 2 s, only with a delivery header) |
| — | Refusal sound | **C ★** (`CUE_BUSY`) |
