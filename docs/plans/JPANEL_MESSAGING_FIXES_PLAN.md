# jpanel messaging — the fix plan after the adversarial review

> **Status:** Scheduled · **Last verified:** 2026-10-11 · **Waves:** W1◻️ W2◻️ W3◻️ F1◻️ F2◻️ F3◻️ F4◻️ P1◻️

Voice post between the twins' panels and the owner's PWA works on the happy path. It fails on
the paths a four-year-old actually takes: pressing again while it loads, walking away
mid-message, holding the pet while a message waits, talking over a TV, and a USB re-flash.

**Where the work came from**

| Source | Findings |
|---|---|
| Original review | 32 findings; B6, B7 and B9 duplicate A3, A1 and A6 |
| Verification against the code | 2 new defects (A8, A9) |
| Cold review, round 1 | 15 blocking + 26 non-blocking |
| Cold review, round 2 | 17 blocking + 33 non-blocking |
| Cold review, round 3 | 18 blocking + 30 non-blocking |
| Cold review, round 4 | 16 blocking + 40 non-blocking |
| Cold review, round 5 | 10 blocking + 30 non-blocking |

**What has held since round 3**

- One level snapshot from the engine.
- A serial engine.
- Generation-stamped streams.
- The audio task as the only writer of the speaker.
- Absolute deadlines enforced in `link.c`.

**Three recurring classes of defect, each closed structurally**

1. **Bounds.** Every deadline is enforced where it is waited on. The UI never adds durations
   together (§7.5).
2. **Preserved behaviour.** Appendices A and B list every existing test and owner decision with
   its disposition.
3. **Cues.** They are render-owned slots acknowledged by sequence number. There is no queue
   (§7.4).

The owner approved restructuring the UI state machine. He also decided three questions, recorded
in §4.

Binding inputs: `CLAUDE.md`, `docs/DOC_LIFECYCLE.md`, `docs/reference/PROCESS.md`,
`docs/reference/DEVELOPMENT.md`, and `docs/plans/JPANEL_PLAN.md` (**JP**). The panels are
placeholder-named **Quorra** and **Tamsin**. Names in this plan are as of firmware 0.3.39.

## 1. Goal

A message to or from a child is never lost silently or replayed by a fault, and is never
reported as heard when it was not. Every press a pre-reader makes gets an answer she can
understand without reading. Every mode, deadline and drawn control is exercised on the host.

## 2. Expectations

Each item is testable and is cited in §3 and in the tests.

**J1 — a child sends**

- **E1.1** The box accepts every recording a panel can capture. It refuses a recording more than
  0.5 s over the cap, and never truncates.
- **E1.2** After the tick or the hush auto-send, she hears exactly one send outcome: sent, nobody
  to send to, dropped (the box trimmed it to nothing), or didn't go.
  - It arrives within the send's published deadline.
  - A send is never cancelled by a stop, standby or calibration (§7.3).
  - An outcome that arrives after the UI's backstop is logged and never sounded.
- **E1.3** An upload in flight is shown on the glass (F4). A message that arrives during the
  upload is announced after the send ends; the serial engine cannot poll mid-send.
- **E1.4** Nothing alters an upload's bytes. Anything that needs the microphone during the upload
  is refused with the busy sound.
- **E1.5** A deliberate recording that times out with nobody speaking ends with "dropped". The
  tick pressed into silence keeps today's "didn't go". Every refusal plays the busy sound; one
  refused during a pet reply sounds after the reply ends.
- **E1.6** A hold on the pet centre opens the "who?" menu when nothing is up, when a badge is
  up, or when the again/reply pair has stood for 10 s.
  - Today's rim, maintenance-tap and swipe guards stay.
  - In a pair younger than 10 s, a press at the centre lands on the reply quadrant and opens a
    reply recording. That is today's behaviour; the recording shows the recipient's face and the
    cross cancels it.

**J2 — a child receives, plays, stops, replies**

- **E2.1** Pressing the notice plays the message whose sender the notice shows.
- **E2.2** A stop — the exit, the button, a face press or a swipe — is honoured at any point
  after the press.
  - The speaker is silent within 40 ms plus about 90 ms of DMA.
  - Nothing chains on.
  - A WebSocket request is cancelled within 250 ms (HTTPS: §7.6).
  - Sends are exempt (E1.2).
- **E2.3** The top-left face comes, every frame, from the same function that addresses reply
  (I2).
  - Face press cycles the selection and a swipe moves it, clamped (JP 0.3.37).
  - Either one stops the sound only if the selection actually moved.
  - No message is skipped.
- **E2.4** From F3, and only when the fetch carried `X-Jpanel-Delivery`:
  - a message counts as heard after `HEARD_MIN_MS`, or 80% of a short one;
  - a shorter stop hands the message back, refunded idempotently and within a cap.

  Otherwise, and in every earlier wave, a stop acknowledges as it does today. A fetch cancelled
  before its head may burn one attempt; that is bounded by the cap of 5 and retryable (C3).
- **E2.5** A heard message is acknowledged at most once.
  - A lost acknowledgement is retried for up to 10 minutes.
  - After that, or after a reboot (the slot lives in RAM), the box may offer it again, bounded
    by its cap.
  - It never replays or skips within a run.
- **E2.6** Every playback ends by the engine's published deadline. A failure says "didn't go" and
  restores the notice.
  - If an acknowledgement fails mid-run, the run ends without a cue and the notice returns.
    Nothing the child pressed went unanswered.
- **E2.7** Reply is live only while the message is audible (ON).
  - It opens a recording to the drawn face, with the tick and cross live after a press grace.
  - If the microphone would be refused (a pet reply sounding, an upload holding the lease), it
    answers "busy" and does **not** stop the message.
- **E2.8** The exit during playback is **one press**: it stops and leaves, and clears the pair
  offer (owner).
  - A message that ends by itself is followed by the pair. A replay that ends by itself keeps it.
- **E2.9** A message that arrives during a pet conversation, a recording, a pet reply or a
  playback chimes on the first frame the child can act on it. That is the PET or GRID screen,
  with the notice drawable, the microphone closed and no pet reply.
  - It never chimes for a message that was already played in the run it arrived during.
  - Its 15 s big box counts from first visibility. A second arrival does not restore it; a wake
    from dark does (JP).
- **E2.10** A press that cannot be served always ends audibly.
- **E2.11** A message already announced is never announced again.
- **E2.12** PAUSE and REPLY are claimed and ignored until ON (JP 0.3.36). The exit is dead for
  `EXIT_GRACE_MS` after the press that started the playback, in every phase (0.3.29).

**J3 — Dad sends, reads, clears**

- **E3.1** An unknown panel answers 404 before TTS runs, and the draft is kept.
- **E3.2** Every unplayed message to the owner is renderable. Reading counts after a dwell while
  the page has focus.
- **E3.3** The statuses are:
  - not yet seen by her panel (`offered_at` is stamped when the panel's `/waiting` first
    describes it);
  - on her panel;
  - started;
  - Played (or "stopped after N s");
  - couldn't be delivered — which **includes a recipient panel that no longer has a key**.

  Twin-to-twin messages get the same statuses.
- **E3.4** An undelivered message can be retried (when its panel is live) or discarded.
- **E3.5** The "kept" count after a clear names the right child.
- **E3.6** A twin-to-twin message appears in both threads. Clearing either thread deletes it
  unless it is still deliverable and unheard.
- **E3.7** A stale poll never removes a bubble that was just sent.
- **E3.8** Errors read as sentences. The composer shows its limit and a hint. A recording made at
  the cap is still sent.
- **E3.9** The launcher badge equals what the screen renders.
- **E3.10** iOS Safari can play the audio. A malformed id answers 404.

**J4 — re-flash**

- **E4.1** Unplayed mail follows a re-flash or a rotation to the new key, including mail
  inserted concurrently. History stays in one thread.
- **E4.2** From P1, retired panels are visible and clearable, and the owner can merge them
  explicitly; the debug API can merge them from W2. Until P1 they stay hidden, as today. Nothing
  merges automatically.

**J5 — network loss**

- **E5.1** A message is acknowledged only if it played whole or was stopped after
  `HEARD_MIN_MS`. A short stream is never acknowledged.
- **E5.2** A 4xx answer (except 408 and 429) is terminal. Everything else retries with backoff
  for up to 10 minutes.
- **E5.3** A failed send says so and is not kept for a resend (§5).
- **E5.4** Mixed releases keep every journey working, at worst with today's behaviour (§6).

## 3. Traceability

In the Verified column, ✓ means confirmed; ✓\* means confirmed with a correction, listed under
the table.

| ID | Verified | Fix | Wave | Proving test(s) |
|---|---|---|---|---|
| A1 | ✓ | Stop generation; arbiter discard; sink gen check; network cancel | F2 | `test_stop_at_every_interleave_point`, `stream: test_stop_while_paused_drains` |
| A2 | ✓ | Every command ends; the UI backstops on the engine's published deadline | F2 | `test_chain_fetch_failure_returns_to_idle` |
| A3 | ✓ | Box accepts the cap (+0.5 s); caps pinned | W1, F2 | `test_send_accepts_exactly_the_capture_cap`, `test_capture_caps_pinned` |
| A4 | ✓ | Serial engine; ack before the next command; run by id | W3, F2 | `test_lost_ack_is_retried_and_never_replays` |
| A5 | ✓ | Render leases the capture before posting | F2 | `test_lease_taken_before_post` |
| A6 | ✓\* | Hand-back (F3) gated on the delivery header; selection over waiting + current | W3, F3 | `test_stop_in_preroll_returns_not_acks`, `sm: test_face_cycles_without_skipping` |
| A7 | ✓ | Seqlock | F2 | `test_snapshot_is_never_torn` |
| A8 *(new)* | — | `s_state` shared → gone | F2 | `test_send_outcome_survives_concurrent_chain` |
| A9 *(new)* | — | No ring reset; two cursor writers | F2 | `stream: test_cursor_writers` |
| A-tests | ✓ | §10 | F1 | `run_jpanel_tests`, `run_stream_tests` |
| B0 | ✓ | One state machine (§8) | F3 | Appendix A; `check_no_logic_in_display` |
| B1 | ✓ | Per-mode targets; reply only in ON | F3 | `test_reply_during_playback_opens_a_working_recording` |
| B2 | ✓\* | Standing pair (owner) | F3 | `test_hold_at_pet_centre_*` |
| B3 | ✓ | One face function; offer hidden while waiting | F3 | every-frame face assertion |
| B4 | ✓ | Cue slots; chime on the actionable edge | F2, F3 | `stream: test_outcome_waits_out_reply`, `sm: test_message_during_30s_recording_chimes_after` |
| B5 | ✓ | Sending is a level; notice and AGAIN answer "busy" | F3, F4 | `test_notice_and_again_refused_during_send` |
| B6 | = A3 | — | — | — |
| B7 | = A1 | — | — | — |
| B8 | ✓ | Lead-silence → "dropped"; refusals → `CUE_BUSY` | F3 | `test_every_refusal_sounds_busy` |
| B9 | = A6 | — | — | — |
| B10 | ✓\* | Pending longer than the reply cap; follow-up yields; `due()`; "stop stop" kept | F2, F3 | `test_press_behind_pet_reply_is_served`, `test_deadlines_survive_u32_wrap` |
| B-UX | ✓ | §9 | F3, F4 | per row |
| C1 | ✓ | `readdress_orphans` (serialised); subject grouping; explicit merge (§11) | W2 | §11.2 |
| C2 | ✓ | Validation; subject-grouped picker | W1, W2 | `test_send_text_unknown_panel_404_before_tts` |
| C3 | ✓ | Retry/discard, including keyless recipients | W2, W3, P1 | `test_unheard_to_retired_panel_is_discardable` |
| C4 | ✓ | `heard_ms`, `offered_at` | W3, F3, P1 | `test_played_records_heard_ms` |
| C5 | ✓ | Between rows in both threads | W1, P1 | `test_between_message_in_both_threads` |
| C6 | ✓ | Per-thread limit + every unplayed owner row | W1 | `test_every_unplayed_owner_row_is_returned` |
| C7 | ✓ (Range plausible) | 206; uuid → 404 | W1, W3 | `test_audio_range_206`, `test_non_uuid_is_404` |
| C8 | ✓ | `errorDetail`; `maxLength`; TTS timeout 60 s | W1, P1 | `client.test` |
| C9 | ✓ | `kept_for` | W1 | `test_clear_kept_names_recipient` |
| C10 | ✓ | PWA clamps; box slack | W1 | `voiceMessage.test: clamps_to_cap`, `test_owner_audio_at_30_02s_is_accepted` |
| C11 | ✓ | Request seq + epoch | W1 | vitest |
| C-UX | ✓ | §9 | W1, P1 | per row |

**Corrections to the original review**

- **A6:** the face is dead with exactly two messages; the reported skip needs three.
- **B10:** "stop stop" is muted while recording, which is deliberate (JP 0.3.33). Kept.
- **B2:** the pair never expiring was the owner's 0.3.25 decision. He chose the standing
  variant.
- **C7:** the 500 is confirmed; the Range failure is plausible.
- **Comment drift** to fix in the waves that touch each file:
  - `s_repeat_until`;
  - the `ui.c` exit case ("No cue");
  - `jpanel_replay`;
  - `audio.c` "ten"/"thirty";
  - `panel_ws.py` "`MAX_MESSAGE_MS` is 30 s";
  - JP's "Capped at 20 s".

## 4. Owner decisions

Appendix B lists every owner decision the machine touches. Each is kept unless this section
changes it.

**Decided by the owner on 2026-10-11**

| Question | Decision |
|---|---|
| Standing pair | **Yes.** After `REPEAT_MS` (10 s) the pair is hit only by its drawn discs (`CONFIRM_HIT_R`), and a hold on the pet centre opens the menu. |
| Exit during playback | **One press.** It stops and leaves, and clears the offer. A message that ends by itself still produces the pair. |
| Refusal sound | **A new, distinct `CUE_BUSY`** for every refusal. `CUE_STOP` stays cancel/dropped. F4 designs it; F3 ships a placeholder (`test_cue_busy_distinct`). |

**Changed because the old behaviour is itself the finding**

- **Ack-on-stop.** From F3 only, and only when the fetch carried the delivery header, a stop
  below `HEARD_MIN_MS` hands the message back (C4/A6).
- **`/next?at=` → by id** (A4, A6).
- **The notice clock** counts from first visibility (B4).
- **A returned message reappears as a badge**, not a big box. The child has just dismissed it,
  and an id she has already seen does not start a new big-box wait.
- **The selection goes home on an unseen id, not on any count change.** This narrows today's rule:
  a count drop from playing no longer moves a selection still in range (A6).

## 5. Scope

- **Deferred:**
  - Web push — a new dependency and a privacy call (JP §5).
  - Resend after a failed send — it would hold the capture lease, and with it the pet's
    microphone.
- **Out of scope:** child-speech STT.
- **Unchanged:** `endpoint_status`.

## 6. Version skew

Panels OTA from the box's `firmware/dist/`. The settings poll (~3 s) carries the box's firmware
version, and `main.c` installs any version that **differs**. So:

- **New box + old firmware** lasts minutes per panel, and indefinitely for a panel whose OTA
  keeps refusing.
- **Old code + new firmware** happens only after a box rollback. Ops → Update runs
  `git reset --hard @{u}` and then `alembic upgrade head`. A rollback reverts code and **never
  deletes a migration revision file**: W1 adds a CI check that rejects deletions under
  `backend/migrations/versions/`.
  - W2 is data-only.
  - W3's columns are nullable or defaulted (`returns integer NOT NULL DEFAULT 0`), checked by
    `test_w3_schema_accepts_pre_w3_box_insert_shapes`.
  - Under rolled-back code, `merged:` tombstones show in the device list. This is cosmetic.

Every box change is additive. Firmware degrades to today's path whenever a field, header or route
is missing.

| Change | New box + 0.3.39 | Old code + new firmware |
|---|---|---|
| `/send` ceiling = cap + 0.5 s | A3 fixed for every panel in the field | 413 → `FAILED("too-long")` |
| `/waiting` `queue[].id` | ignored; ≤ 1011 B worst case (31-char names) < 0.3.39's 1024 B buffer (`test_waiting_body_fits_0_3_39_buffer`, W3) | positional `?at=`; "new" means a count rise |
| `/next?id=` (still `recipient_device = :me`) + `X-Jpanel-Duration-Ms` / `X-Jpanel-Delivery` (`UPDATE … RETURNING`) | not sent | the firmware also sends `at`, which a rolled-back box honours; with no delivery header a stop acks as today. The head frame stays ≤ 0.3.39's `LINK_HEAD_MAX` (1024 B), pinned by `test_next_head_fits_link_head_max`. |
| `/played {id, heard_ms}` | `heard_ms` NULL | extra field ignored |
| `/returned` (tunnelled through `_ALLOWED`) | never called | never called |
| Re-address, owner grouping, `offered_at` | box-side | the data stays principal-keyed |

---

## 7. The engine (F2)

### 7.1 Two arrows

**render → engine: a command queue of depth 4.**

```
cmd { kind PLAY|REPLAY|SEND, seq (never 0), gen, expires_at, id[48], at, to,
      from[32], from_dad }                                   // from/from_dad: REPLAY only
```

- Only render posts. A failed post is reported back as `in.post_failed = seq`.
- "Poll soon" is an atomic flag, not a command. `jpanel_poll_soon()` keeps its name.
- **Expiry before claim:**
  - PLAY and REPLAY expire 35 s after posting. That exceeds the longest predecessor that cannot
    be cancelled: an HTTPS poll (20 s) plus an ack (10 s), with slop. Both are enforced
    deadlines (§7.5).
  - SEND expires 60 s after posting.
- **A SEND carries no gen and is never cancelled by STOP.** Only expiry before claim ends it.

**engine → render: one seqlock snapshot.** Only the engine writes it; render copies it once per
frame.

```
eng_view {
  phase IDLE|POLLING|FETCHING|SOUNDING|ACKING|SENDING
  claimed_seq, deadline_ms               // what it is doing NOW and when that must have ended
  done_seq                               // every command ends here exactly once
  cur  { seq, stream_id, id, from[32], from_dad, replay, run_continues }
  play_outcome { seq, id, kind heard|heard_unacked|stopped_heard|returned|short|empty
                                |cancelled|failed, reason }
  send_outcome { seq, kind SENT|NOBODY|EMPTY|FAILED, reason }
  waiting { count, known, q[8]{ id, from, from_dad }, sibling }
  ack_owed, hb
}
```

- `seq_ge(a, b) = (int32_t)(a - b) >= 0`.
- **Claiming.** The engine claims a command — publishing `claimed_seq`, `cur` and `deadline_ms`
  — **before** it blocks on anything.
- **Stale commands.** It never claims an expired command, or a stale-gen PLAY/REPLAY. Such a
  command ends at once.
- **Sender seeding.** `cur` is seeded before the request goes out:
  - from the waiting entry, for a run message (today's `seed_sender`);
  - from the command's `{from, from_dad}`, for a REPLAY.

  The `/message/{id}/pcm` route sends no sender headers.
- **Audio facts are read directly** from the arbiter's atomics (§7.4), not from the snapshot.

### 7.2 Seqlock

**Writer:**
1. `s = load(relaxed)`;
2. `store(s+1, relaxed)`;
3. `fence(RELEASE)`;
4. write the fields;
5. `store(s+2, RELEASE)`.

**Reader:**
1. `s1 = load(ACQUIRE)`; if odd, retry;
2. `memcpy`;
3. `fence(ACQUIRE)`;
4. `s2 = load(relaxed)`;
5. accept if `s1 == s2`.

The memcpy race is validated and accepted. After four failed tries the reader keeps its previous
copy. All large structs are static: the stacks are 8 KB (face) and 6 KB (engine).

### 7.3 The serial engine

Each `jpanel_core_step(core, now)` runs these steps in order:

1. **Finish the current stream.** Classify it and publish its `play_outcome` and
   `run_continues` **before** acknowledging. Then arm the ack slot and attempt it.
   `stream_begin` refuses until classification is done.
   - `run_continues` is true when ids remain in the run and the gen is fresh. It is always false
     for a REPLAY.
2. **Acknowledge.** One slot: `{id, kind PLAYED|RETURNED, heard_ms, delivery, first_at}`.
   - An ack request has a 10 s deadline and is never gen-cancelled.
   - 204 → clear. 409 or 404 → clear (404 is counted `no_refund`).
   - Any other 4xx, except 408 and 429 → terminal: clear and log.
   - Anything else → end the run, set `ack_owed`, and back off 1, 2, 4 … 30 s, for up to
     10 minutes.
   - While an ack is owed, the poll drops the owed PLAYED id, and a PLAY retries the ack once
     first. If that retry fails, the PLAY ends `failed("ack-pending")`.
   - A **REPLAY is never acknowledged**; it was already acknowledged when first played.
3. **Continue a run** (PLAY only), by id.
   - The run list is the waiting list at the start, plus ids that arrive mid-run (JP), minus ids
     played or returned in this run.
   - The run continues only if `cur.gen` equals `s_stop_gen`.
   - Without ids it fetches `?at=0`.
   - A RETURNED message goes back into the local `waiting` at its old position, and a poll is
     flagged.
4. **Dequeue.**
   - A PLAY/REPLAY whose gen is stale, or any command past its expiry, ends at once.
   - A claimed SEND is never re-checked for expiry, so the 422 retry is always allowed.
5. **Poll** when due or flagged. A queued command cancels the poll (§7.6).

Every exit publishes `done_seq` and the outcome in one write.

**Lease.** Render acquires the capture lease (CAS 0 → 1) before it posts a SEND. The engine
releases it whenever a SEND ends — including one that ended expired at dequeue — and **before**
it publishes `send_outcome`. If the post fails, render releases the lease itself.

**Outcomes, by wave**

| Stop when | F2 (today's policy) | F3 with delivery header | F3 without |
|---|---|---|---|
| `out_ms ≥` heard threshold | PLAYED | PLAYED | PLAYED |
| below the threshold, or `out_ms == 0` | PLAYED (ack-on-stop) | RETURNED | PLAYED |
| stream ended short (unverified) | none | none | none |

**Replay outcomes** (no acknowledgement): `heard`, `stopped`, `short` (logged only, as today),
`failed` (`CUE_OOPS`, as today).

### 7.4 The audio arbiter: `stream_tick`, pure and host-tested

Every per-chunk decision lives in **`stream.c`**: `stream_tick(&st, now) → { WRITE(ptr, len) |
IDLE, capture: COPY|SILENCE|NONE }`. That covers the `s_play`-before-ring rule, the preroll,
pause, deafness, the capture copy, request intake, and cue rendering.

- `audio_task` does codec I/O only, enforced by `check_no_logic_in_audio`.
- `cue.c` renders a cue **in chunks** (`cue_render_chunk(c, variant, offset, n)`), with each
  cue's peak precomputed in a table. A tick never renders more than one chunk; the audio task has
  a 4 KB stack.
- `test_chunked_cue_equals_whole_render` checks the chunked output against the whole render.

**Requests are fixed slots, each written by one task and acknowledged by the arbiter through a
consumed sequence number.** There is no queue.

| Slot | Writer | Reader (arbiter writes only its `*_done` and its own play state) | Rule |
|---|---|---|---|
| `reply {ptr, len, id}` | talk | arbiter (`reply_done`) | Highest priority. Played from talk's buffer. Waits out a cue that is already sounding (≤ 749 ms). Never refused. `reply_done = id` on every disposal: played, silenced, or standby, where it is disposed of unplayed. |
| `outcome {seq, cue}` (send results, play failures) | render | arbiter (`outcome_done`) | Latest wins. Eligible once no reply is pending or sounding. Once started, nothing but Silence cuts it. Expires 15 s after it first becomes eligible; the arbiter stamps that time itself. |
| `refusal {seq}` (`CUE_BUSY`) | render | arbiter (`refusal_done`) | Coalesced. Same eligibility as `outcome`, played after it. Expires 2 s after first eligible. |
| `chime {seq}` (`CUE_MESSAGE`) | render | arbiter (`chime_done`) | Written only when the machine judges the chime actionable (§8.4). Plays when no reply, outcome or refusal is pending. Expires 20 s after first eligible. |
| `press {seq, cue}` (play/pause/resume/reply/heard/stop/listen, poke actions) | render | arbiter (`press_done`) | Plays at once over a stream, as today. **Never cuts an outcome, refusal or chime cue.** If one of those is sounding, the press waits up to 400 ms after it was written, then is dropped. Dropped while a reply is pending or sounding (JP: a reply outranks an acknowledgement). A newer press preempts an older press cue. |
| message stream (ring) | engine | arbiter | Lowest priority. Yields to every cue and to a reply, chunk by chunk. |
| `silence {seq}` | render | arbiter | Cuts any cue or reply and discards the stream, once. |

An outcome can only be cut by Silence (standby or deep sleep), and then the child has asked for
quiet. So there is no replay logic.

Each slot is a single packed word (`seq` and `cue`), or a seq written last with release ordering.

**Stop and pause.** `s_stop_gen` lives in `stream.c`; render writes it with `stream_stop()`,
called from the STOP effect. Each stream carries one packed atomic word holding `gen` (low 16
bits) and `stream_id`.

- The producer publishes that word (release) **before** its first write to `w`.
- The arbiter loads `w` (acquire) and then the word, and checks gen **before** pause:
  - a stale stream is discarded (`r = w`);
  - pause is ignored for it.
- **Pause belongs to the machine.** Render writes `pause_req` as one packed word,
  `{stream_id, on}`, which the arbiter honours only for that stream.
- The sink checks gen on every chunk.

**The ring.**

- Cursors are monotonic and never reset.
- The producer writes `w`; the arbiter writes `r`. Both use `__atomic` release/acquire.
- `stream_begin` waits until the arbiter has published `seen_w == w && r == w`.
- The arbiter alone writes `{stream_id, out_ms, arb_hb}` and the `speaker_busy` flags.

**The capture lease.** A `uint32_t` set by CAS: 1 for the engine's send, 2 for talk.

- `capture_open()` refuses while the lease is held.
- Talk releases its lease when its upload returns.
- Every opener handles the refusal: F2's wrappers, including the follow-up open in `display.c`,
  and F3's mic gate.

### 7.5 Deadlines are true by construction

**(a) `link.c` enforces `link_req_t.deadline_ms`.** Each step is clamped to the time left before
the deadline (`left`):

- the socket wait;
- `ws_send`'s lock and send;
- the HTTPS `s_http_lock`;
- `esp_http_client_open`, which gets `left/3` for each of its three timed phases (connect, TLS,
  header write);
- each `write`;
- `fetch_headers`.

**Phases and how each is bounded:**

| Phase | Bound |
|---|---|
| Upload (send) | **By progress:** each frame or write must complete within `LINK_STALL_MS`. The deadline is refreshed after each one, and the engine republishes it as the frames go out. |
| Head | A deadline fixed when the upload ends. For a send: `now + 60 s` (box whisper timeout, `_transcribe`) `+ 15 s` margin (trim, blob write, insert). For everything else: the request's own head budget. |
| Body, buffered or streamed | `LINK_STALL_MS` per network gap, as today. Time the sink is blocked (full ring, paused) does not count. On HTTPS the client timeout is reset to `LINK_STALL_MS` once headers arrive, so a late head does not shorten the body. |

Slop is at most one `LINK_STEP_MS`. The pure helpers `wsp_wait_step`, `link_left` and the `open`
split live in `wsproto.c` and are host-tested. `esp_http_client`'s own behaviour cannot be
host-tested; that limitation is stated, not hidden.

**Every `link_request` caller migrates from `timeout_ms` to `deadline_ms` with unchanged
semantics:**

- jpanel's;
- `talk.c` (deadline = now + `TALK_HTTP_TIMEOUT_MS` for the head; its reply body keeps today's
  stall rule);
- `ota.c`'s three sites.

**(b) The engine publishes the deadline of what it is doing now.**

| Phase | Published `deadline_ms` |
|---|---|
| POLLING, FETCHING, ACKING | the current step's deadline |
| SENDING | refreshed per upload frame, then the head deadline |
| SOUNDING, and any body | `now + LINK_STALL_MS + 2 s`, refreshed while bytes arrive, or `out_ms` advances, or (paused and `arb_hb` advances) |

**(c) The UI never sums durations.** It has exactly two backstops:

- **claimed:** `due(now, deadline_ms + 2000)`;
- **unclaimed:** `due(now, expires_at + 1000)`.

A PLAY backstop sends STOP plus `CUE_OOPS` and goes to PET. A SEND backstop clears only the send
state and sounds `CUE_OOPS`; the mode is unchanged. A late real outcome increments
`late_outcome` and is never sounded.

**(d)** A wedged arbiter stops refreshing, so the claimed backstop catches it. There is no
separate codec rule.

**Test.** `test_engine_meets_published_deadlines` uses a fake link that models every real wait:

- the 8 s socket wait;
- the 20 s `ws_send` lock plus send;
- the HTTPS lock;
- an upload at 10 KB/s;
- **60 s of box transcription before the head**;
- body gaps.

It asserts that every claimed step ends by `deadline_ms + LINK_STEP_MS`. `test_storm` keeps its
snapshot delays under the 2 s margin.

### 7.6 Network cancel

- `link_types.h` adds `deadline_ms` and `cancelled()`.
- On WebSocket, the wait loops call `wsp_wait_step` every 250 ms and send `wsp_cancel` when
  cancelled.
- On HTTPS, cancellation is checked between steps.
- **SEND requests pass no `cancelled`.** Sends are not stoppable.
- Buffers: `rel[96]`, `path[112]`, `/waiting` 1536 B.
- **Gap:** cJSON stays on the device. Host tests feed parsed structs, and backend tests pin the
  field names.

### 7.7 Writers

| State | Writer |
|---|---|
| `s_stop_gen`; cue slots `outcome`, `refusal`, `chime`, `press`, `silence`; `pause_req`; commands; send-lease acquire | render |
| poll flag | any (atomic) |
| snapshot; run list; ack slot; ring `w`; stream word; send-lease release | engine |
| ring `r`; `seen_w`; `{stream_id, out_ms, arb_hb}`; `speaker_busy`; every `*_done`; eligibility stamps; `s_play` | arbiter |
| `reply` slot; talk lease | talk |

`jpanel_types.h` is split out of `jpanel.h` so the new code can be built on the host.

**Telemetry:**

- `ack_owed`, `ack_retries`, `no_refund`, `returned`;
- `lease_refusals`, `snapshot_retries`, `post_failed`;
- `late_outcome`, `cue_expired`;
- `jpanel_stack_free`, `sm_violations`.

**F2 changes no intended behaviour.**

- The old `display.c` runs through thin wrappers in `jpanel.c`:
  - `jpanel_state()` is derived from the phase plus unseen outcome seqs;
  - `jpanel_clear()` marks an outcome seen;
  - `running`, `fetching` and `in_from` read `cur`;
  - play is refused while the send lease is held;
  - `audio_cue()` writes the `press` slot, or the `outcome` slot for SENT/OOPS. Same sounds as
    today.
- Outcome mapping is today's: EMPTY sounds as FAILED (OOPS).
- **F2 rollback:** a source-revert PR covering `link.c`, `stream.c` and `jpanel_core`, with a
  version bump and a rebuilt dist.

---

## 8. The panel state machine (F3)

### 8.1 Shape

`panel_sm.{c,h}` replaces `ui.{c,h}`. `sm_step(sm, &in, &out)` runs once per frame, both on the
device and in the tests.

**Inputs (`in`):**

- `now`, `snap`, `audio {stream_id, out_ms, speaker_busy, reply_busy}`, `post_failed`;
- touch edges with points, `on_pet`, `gest_taps`, `swipe`;
- the voice command and `cmds_muted`;
- button edges, `standby`;
- `hearing`, `capture_ms`, `leased`, `net`;
- the sleep stage and `calibrating`.

**Outputs (`out`):** ordered effects, `sm_view_t`, and dash/swipe telemetry.

**`display.c` keeps:**

- sampling;
- the executor, with the `sound` gate;
- drawing;
- the sleep stages, where `used` keeps its non-machine terms and replaces its talk/jpanel terms
  with `sm_busy()`;
- calibration;
- the pet-side pass-through effects (poke, `VOCAB_FORM/COLOUR/ACTION`, label), which keep today's
  guards — including *"SPEAKING, AND IT OUTRANKS THE TOY"*: a poke during a reply only flinches.

**Calibration.** Entering it is an input:

- the mode goes to PET, with `CAPTURE_DROP`, `PAUSE(0)` and `STOP`;
- **the offer is kept**, and any send continues;
- the machine is not stepped during calibration;
- on the first frame back, outcomes are processed before backstops.

**`make check_no_logic_in_display`** (run by `make test`) fails if either is true:

- `display.c` has any static, file-scope or function-local, whose name with `s_` stripped matches
  an `sm_t` field (extracted by awk);
- `display.c` calls a `jpanel_*` or `stream_*` function outside the executor's whitelist:
  `jpanel_snapshot`, `jpanel_post`, `jpanel_poll_soon`, `stream_stop`, `stream_pause_req`,
  `stream_cue_*`, `stream_silence`, `stream_audio_facts`.

A renamed duplicate would pass this check; the wave review covers that case.

### 8.2 State and invariants

```
sm_t {
  mode PET|GRID|LISTEN|THINK|RECORD|PLAY, since       // THINK includes the 2.5 s FAILED dash
  play { seq, target{id, from, from_dad, replay}, since /* the press */, run_pos,
         pause_req{stream_id, on, since} }
  offer { id, from, from_dad, since, hidden }         // the again/reply pair; not a mode
  rec  { to, heard, hush, deliberate, opened_at }
  talk { voice, heard, hush, lead, follow_armed, follow_turns, dash... }
  grid_until; send { seq, posted_at, expires_at }; notice { first_visible, big }
  chime_ids[8]; sel { id, index }; seen_ids[16]; down { at, x, y, on_pet, target, swiped }
}
```

**ON** means `snap.claimed_seq == play.seq`, `audio.stream_id == snap.cur.stream_id` and
`audio.out_ms > 0`: the message is actually audible.

**The offer**

- *Set* when a non-replay PLAY finishes with `heard`/`heard_unacked`, from the last `cur`. It is
  hidden if anything is still waiting.
- *Cleared* by:
  - EXIT on the pair;
  - REPLY on the pair;
  - button standby;
  - **every exit from PLAY other than a natural finish** — EXIT, FACE, swipe, button, backstop,
    calibration excepted;
  - **pressing to play a non-replay message** (cleared at the press).
- *Hidden, not cleared,* while `waiting.count > 0`.
- *Standing* once `due(now, offer.since + REPEAT_MS)`.

It survives a conversation, the grid, a recording started by voice, and calibration, as today.

**Invariants.** They are asserted after every host step and counted on the device. A violation
recovers to PET with `CAPTURE_DROP`, `PAUSE(0)` and `STOP`, plus `CUE_OOPS` if a recording was
dropped.

- **I1:** exactly one mode; the mic is open if and only if the mode is LISTEN or RECORD.
- **I2:** `sm_face(sm, snap)` both draws the top-left face and addresses reply. It returns:
  - in PLAY, claimed: `snap.cur` (for a replay, seeded from the offer);
  - in PLAY, not yet claimed: `play.target`;
  - in PET with a visible offer: the offer;
  - in PET otherwise: the selected waiting entry.

  The drawn face is asserted every frame.
- **I3:** the drawn controls are exactly the mode's targets.
- **I4:** every press gets a cue or a view change, except the presses §8.3 lists as ignored.
- **I5:** `pause_req.on` implies PLAY with `pause_req.stream_id == snap.cur.stream_id`.
  - Every exit from PLAY emits `PAUSE(0)`.
  - Every exit except a natural finish also emits `STOP`.
  - An ack never carries a cancel, so a STOP cannot cut it.
- **I6:** every mode except PET has a deadline:

  | Mode | Deadline |
  |---|---|
  | PLAY, SEND | §7.5c |
  | GRID | `SENDTO_MS` |
  | LISTEN, THINK | today's |
  | RECORD | `CAPTURE_MAX_MS + 5 s` |
  | pause | `AUDIO_PAUSE_MAX_MS` |
  | pending | `PENDING_MS` |

- **I7:** times compare with `due()`, seqs with `seq_ge()`.

### 8.3 Targets and transitions

**Global (any mode)**

- **Button short:**
  - in standby → wake;
  - otherwise → `CUE_STOP`, `CAPTURE_DROP`, `PAUSE(0)`, `STOP`, disarm the follow-up, clear the
    offer, standby, PET. **A send continues.**
- **Button hold** → deep sleep.
- **Voice "stop"** runs today's `VOCAB_STOP` handler wherever it is delivered:
  - RECORD or LISTEN → drop;
  - THINK → `talk_clear`;
  - always → disarm the follow-up and set turns to MAX.

  Delivery is gated by today's mute.
- **The wake phrase and "tell sister/dad"** go through the mic gate.

**The mic gate (today's rule).** The mic opens only if all of these hold:

- the mode is PET, GRID or PLAY (for a REPLY);
- not standby;
- not leased;
- `net != BUSY`;
- the speaker is quiet — no reply pending or sounding, no cue sounding, and no stream other than
  one this same transition is stopping.

The cue for the opening press itself sounds after the mic opens, inside the deaf window. A
refusal writes the `refusal` slot. This keeps *"NOT WHILE WE ARE SPEAKING"*.

**Targets, in priority order** (*ignored* = the press is claimed with no effect):

| Mode | Targets | Hold |
|---|---|---|
| PET (offer hidden or none) | NOTICE (big box, or the badge quadrant) · PET | from PET with `on_pet`, free taps and not swiped → GRID |
| PET, fresh offer | EXIT · AGAIN quadrant · REPLY quadrant · PET | as PET (most of the centre is quadrants) |
| PET, standing offer | EXIT · AGAIN/REPLY disc reach (r 82 at (92 \| 276, `confirm_cy`)) · PET | → GRID at the pet centre |
| GRID | four cells · swallow | — |
| LISTEN (held) | none (release ends it) | — |
| LISTEN (voice), RECORD | CROSS · TICK quadrants (both *ignored* for `EXIT_GRACE_MS` after a press opened the recording) · swallow | — |
| THINK | PET (flinch) | — |
| PLAY | EXIT (*ignored* until `play.since + EXIT_GRACE_MS`, every phase) · FACE (live while `count + (claimed ? 1 : 0) > 1`) · PAUSE/REPLY quadrants (*ignored* until ON) · swallow | — |

**The PLAY view** is today's:

- a PLAY draws the run bar;
- a replay draws the run bar until ON, then the pair layout with the playing glyph
  (`test_a_replay_still_reads_as_playing`).

**PET**

- **Notice:**
  - while sending → refusal;
  - otherwise → `CUE_PLAY`, clear the offer, PLAY{target = the selection, `since = now`}.
- **AGAIN:**
  - while sending → refusal;
  - otherwise → `CUE_PLAY`, PLAY{replay; target = the offer's `{id, from, from_dad}`, carried in
    the REPLAY command}. The offer stays.
- **REPLY** → mic gate:
  - opens → clear the offer, RECORD{to = `sm_face`}, `CUE_REPLY`;
  - refused → refusal; the offer stays.
- **EXIT** → `CUE_STOP`, clear the offer.
- **Swipe** → select, clamped; `CUE_HEARD` only if the selection moved.
- **An unseen id arrives:**
  - the selection goes home;
  - the id joins `seen_ids` and `chime_ids`;
  - a wait whose ids were all already seen (a returned message) starts as a badge.

**RECORD**

- **Tick, hush or cap, after she was heard** → effect `SEND` (the executor CASes the lease and
  posts), `CUE_HEARD`, PET.
- **Tick, nobody heard** → drop, outcome `CUE_OOPS` (today's).
- **Cross** → drop, `CUE_STOP`.
- **Lead-silence** → drop, plus `CUE_STOP` (to the outcome slot) if the recording was
  deliberate.
- **A swipe in the press that opened the recording** → drop it silently, then apply the swipe.

**LISTEN/THINK** — today's behaviour, except that the follow-up does not open while a press is
pending or `chime_ids` is non-empty.

**PLAY**

- **Pending.** The command is posted once `!speaker_busy` (today's: the press cue finishes
  first).
  - `PENDING_MS = PLAY_BUF_MS + 2000`, checked by `_Static_assert`.
  - When pending expires → outcome `CUE_OOPS`, PET.
  - On `post_failed` → `CUE_OOPS`, PET.
- **Finished**, meaning `play_outcome.seq == play.seq && !run_continues`, or `done_seq` reached:

  | Kind | Result |
  |---|---|
  | `heard` / `heard_unacked`, not a replay | set the offer from `cur` (hidden if anything waits), PET |
  | replay, any kind other than `failed` | PET, offer kept |
  | `failed` (a play or a replay) | outcome `CUE_OOPS`, PET; a replay keeps the offer |
  | `short` / `empty` | `CUE_OOPS`, PET |
  | otherwise | PET |

  A natural finish emits `PAUSE(0)` only.
- **EXIT** → `PAUSE(0)`, `STOP`, `CUE_STOP`, clear the offer, PET (owner).
- **FACE / swipe:**
  - if the selection over `waiting + current` moves → `PAUSE(0)`, `STOP`, `CUE_HEARD`, clear the
    offer, PET (JP 0.3.37);
  - at a clamped end → nothing.
- **PAUSE** (when ON) → toggle `pause_req` for `cur.stream_id`, with `CUE_PAUSE` or
  `CUE_RESUME`.
- **REPLY** (when ON) → mic gate, checked **before** stopping:
  - opens → `PAUSE(0)`, `STOP`, clear the offer, RECORD{to = `sm_face`}, `CUE_REPLY`;
  - refused → refusal, and the message keeps playing.

**SEND**

- On `post_failed` → `CUE_OOPS` at once, clear `send`. Render has already released the lease.
- On the outcome, written to the `outcome` slot:

  | Outcome | Cue |
  |---|---|
  | SENT | `CUE_SENT` |
  | NOBODY | `CUE_OOPS` (until F4's `CUE_NOBODY`) |
  | EMPTY | `CUE_STOP` |
  | FAILED | `CUE_OOPS` |

- A late outcome after the backstop is logged and not sounded.

### 8.4 New messages, the chime, sleep

- **"New"** is an id not in `seen_ids`. With no ids from the box, a rise in the count stands in.
- **The chime.** `chime_ids` holds the ids still owed a chime. The machine writes the `chime` slot
  on the first frame that meets **all** of these:
  - the mode is PET or GRID;
  - the notice is drawable — waiting > 0, and not hidden by a send;
  - the capture is closed and `!reply_busy`;
  - at least one of `chime_ids` is still in `waiting`.

  `chime_ids` is then cleared. Ids that have left `waiting` (played in the run they arrived
  during, or played elsewhere) are dropped from it **every frame**, so they never chime.
- **`notice.first_visible`** is stamped on the first frame the notice is drawn, once per wait:
  - a new wait starts when the count goes from 0 to above 0;
  - a second arrival does not restart it;
  - a dark wake does (JP).
- **`sm_busy()`** = mode ≠ PET, or sending, or `snap.phase ∈ {FETCHING, SOUNDING}`.
- **The sleep `arrived` wake** reads the machine's unseen-id edge.

### 8.5 Tests (`sm_tests.c` → `run_sm_tests`, in `make test`)

- **Appendix A** — every existing test, re-verified assertion by assertion.
- **The journey tests** named in §3 and §9, plus:
  - `test_exit_from_replay_is_one_press`;
  - `test_exit_does_not_resurrect_older_offer`;
  - `test_reply_during_replay_goes_to_offer_sender`;
  - `test_arrival_played_in_run_does_not_chime_after`;
  - `test_arrival_during_think_chimes_after_reply`;
  - `test_message_during_30s_recording_chimes_after`;
  - `test_poke_during_outcome_still_sounds_outcome`;
  - `test_standby_after_tick_still_sends`;
  - `test_reply_refused_during_pet_reply_keeps_playing`;
  - `test_swipe_at_clamped_end_stops_nothing`.
- **`test_storm`** — 300k seeded frames:
  - snapshots dropped, delayed (always under 2 s) or duplicated;
  - touches dropped or repeated;
  - clock jumps, including the u32 wrap;
  - random calibration;
  - an engine running at its published deadlines;
  - 30 s recordings with arrivals.

  Every frame asserts I1–I7 and the drawn face, and that no backstop fires.
- **End to end:** `jpanel_tests` drives `sm_step` against the real `jpanel_core` and `stream.c`.

### 8.6 Landing and rollback

F3 lands as one wave. A shadow-mode rollout was rejected: its divergences would be the intended
fixes, and it would double the state on a stack-limited task.

**Rollback:** a source-revert PR covering F3 (and F4, which is built on it, if it has landed),
plus a version bump and a rebuilt `dist/`, because `firmware.yml` byte-compares it. The owner
applies it through Ops → Update, and the panels OTA on the differing version.

---

## 9. Per-finding detail

§7 and §8 carry the design. This section records each finding's cause and its tests.

**A1**

- **Cause:** `fetch_head` resets `s_stopped`/`s_run`; the exit's `CUE_STOP` delays the wait; and
  `audio_stream_begin` clears pause.
- **Tests:**
  - `test_stop_at_every_interleave_point`;
  - `test_stop_with_no_stream_then_play_starts`;
  - `test_stop_while_paused_drains`;
  - `test_stop_during_drain_frees_speaker`;
  - `test_stale_body_not_drained`;
  - `test_stop_during_5s_head_delay_cancels_request`;
  - `test_next_queued_during_drain_classifies_previous_first`;
  - `test_run_gap_stop_never_acks_unsounded`;
  - `test_stale_word_never_eats_next_preroll`.

**A2** — `test_chain_fetch_failure_returns_to_idle`.

**A3**

- **W1:** the box accepts `PANEL_AUDIO_MAX` + 0.5 s.
- **F2:** 400 → `EMPTY`, 409 → `NOBODY`, 413 → `FAILED("too-long")`.
- **Pins:**
  - `test_capture_caps_pinned`: `PANEL_AUDIO_MAX == CAPTURE_MAX_MS × 32`, and
    `panel_ws.MAX_BODY > PANEL_AUDIO_MAX`;
  - `voiceMessage.MAX_MESSAGE_MS == jpanel.MAX_MESSAGE_MS` (the owner's cap).
- **Long sends:** `test_35s_send_on_slow_link_survives_transcription`.

**A4**

- `test_lost_ack_is_retried_and_never_replays`;
- `test_ack_4xx_is_terminal`;
- `test_ack_gives_up_after_10_min`;
- `test_owed_played_id_filtered_from_waiting`;
- `test_play_expiry_exceeds_ack_and_https_poll`;
- backend `test_waiting_queue_carries_ids`;
- backend `test_next_by_id_serves_that_row_or_204` (including an own-sent id → 204).

**A5**

- `test_lease_taken_before_post`;
- `test_lease_released_on_post_failure_and_expiry`;
- `test_upload_bytes_unchanged_under_listen_attempts`;
- `test_follow_up_open_handles_refusal`.

**A6**

- **W3:** `POST /returned {id, delivery}` runs
  `UPDATE … SET deliveries = deliveries-1, returns = returns+1 WHERE id=:id AND played_at IS NULL
  AND deliveries=:delivery AND returns<5`.
  - Answers 204, or 409 when nothing matches; a non-uuid id gets 404.
  - Added to `_ALLOWED`.
- **Tests:**
  - `test_stop_in_preroll_returns_not_acks`;
  - `test_stop_after_threshold_acks_with_heard_ms`;
  - `test_returned_is_idempotent_and_bounded`;
  - `test_returned_refunds_only_own_unplayed`;
  - `test_panel_ws_tunnels_returned_and_new_headers`;
  - `test_returned_reinserted_and_polled`;
  - `test_face_cycles_without_skipping`;
  - `test_run_never_replays_a_returned_message`;
  - `test_message_arriving_mid_run_joins_it`;
  - `test_replay_never_continues_a_run`.

**A7, A8, A9** — `test_snapshot_is_never_torn`, `test_send_outcome_survives_concurrent_chain`,
`test_cursor_writers`.

**B1** — `test_reply_during_playback_opens_a_working_recording`.

**B2** — `test_hold_at_pet_centre_with_badge`, `…_fresh_offer_is_reply`,
`…_standing_offer_opens_grid`, `…_during_play_is_swallowed`.

**B3** — I2, with the offer hidden while a message waits. This settles JP's "face collision".

**B4**

- `stream: test_outcome_waits_out_reply`;
- `test_refusal_coalesced_and_expires_from_eligibility`;
- `test_press_never_cuts_outcome`;
- `sm: test_message_during_pet_reply_gets_chime_and_big_box`;
- `test_heard_then_oops_both_sound`.

**B5** — `test_notice_and_again_refused_during_send`.

**B8** — `test_silent_recording_says_dropped`, `test_every_refusal_sounds_busy`.

**B10** — `test_press_behind_pet_reply_is_served`, `test_follow_up_yields_to_pending_press`,
`test_deadlines_survive_u32_wrap`.

**B-UX**

| Gap | Decision | Wave |
|---|---|---|
| Exit takes two presses | One press (owner) | F3 |
| Failures all sound alike | SENT / NOBODY / EMPTY / FAILED / BUSY (§8.3) | F3 placeholders, F4 contours |
| No sending feedback | Overlay; `CUE_SENT` contour | F4 |
| Text instructions for pre-readers | Icons and pips; names kept | F4 |
| Badge | Pips and pulse; no reminder sound (JP) | F4 |
| "1/2" counts remaining | `run_pos / (run_pos + remaining)` | F3 |
| Arrivals during a conversation | Chime on the actionable edge; notice clock from first visible | F3 |
| Pair blocks the hold | Standing offer (owner) | F3 |
| No stop hint for a recording | Glyphs and a hush ring | F4 |

**C findings**

- **C1** — §11.
- **C2** — `send_text` validates before TTS (404). The PWA refetches and keeps the draft. W2's
  grouping removes ghost chips.
- **C3** — `POST /messages/{id}/retry` (only if the recipient is live) and `DELETE
  /messages/{id}`.
  - Undeliverable means `deliveries ≥ cap` **or the recipient's subject has no live key**. That
    covers retired panels and the "Unknown panel" group.
  - The delete is allowed for owner-sent or undeliverable rows, and releases the blob via
    `blob_referenced`.
  - Both routes return 404 for a non-uuid id.
  - Tests: `test_unheard_to_retired_panel_is_discardable`,
    `test_unheard_to_unknown_panel_is_discardable`, `test_retry_refused_without_live_key`.
- **C4** — columns `heard_ms` and `offered_at`. The copy says "Played".
- **C5** — a between row is listed in both threads.
  - Clearing either thread deletes the rows it shows, except deliverable unheard ones.
  - Undeliverable rows are deleted, and the confirm text names them.
- **C6** — a per-thread `row_number()` limit, a UNION with every unplayed owner row, and
  `?before=`. **W1 owns the query function and `/summary`.**
- **C7** — 206/416 with `Accept-Ranges`. uuid guards on: the audio route, owner read, the C3
  routes, `/next?id=`, `/returned`, and panel `/played`. On the firmware side, a 404 from
  `/played` means "row gone", so it clears.
- **C8** — `errorDetail` joins the list `msg`s; `maxLength={600}`; TTS timeout 60 s; the counter
  and hint arrive in P1.
- **C9** — `kept_for` per recipient.
- **C10** — the PWA clamps PCM to `MAX_MESSAGE_MS × PANEL_RATE`. The box refuses only beyond the
  cap + 0.5 s (`test_owner_audio_at_30_02s_is_accepted`, `…_well_over_cap_is_413`).
- **C11** — request sequence numbers plus an epoch.

**C-UX**

| Gap | Decision | Wave |
|---|---|---|
| Notification | Launcher badge; push deferred | W1, P1 |
| Ambiguous "not heard" | Statuses | P1 |
| Composer | Hint and counter | P1 |
| Read-marking | 1.5 s dwell + focus | P1 |
| Sweep | Kept; footer | P1 |
| Renames rewrite the past | Not fixed: renames are corrections | — |
| Two clears | Kept; confirm names what remains | P1 |

The W1/W2 PWA changes — a kept draft, hidden retired chips — change behaviour only, not a surface,
so they are exempt from the GUI gate.

---

## 10. Host foundations (F1, behaviour-preserving)

1. `link_types.h`, `jpanel_types.h`, and `wsp_wait_step`/`link_left` in `wsproto.c`.
2. `stream.c` with `stream_tick`, extracted **as is**. `audio.c` keeps only I/O, and
   `check_no_logic_in_audio` is added.
3. `jpanel_core.c` behind `jpanel_io_t`. `jpanel.c` keeps the task, queue, cJSON, mbedtls and
   the real io.
4. `run_jpanel_tests`, `run_stream_tests` and both `check_*` gates, all in `make test` (CI runs
   only `make -C firmware/host test`). The fakes are:
   - a scripted box, with a 0.3.39 mode;
   - the real ring and stream;
   - a fake clock;
   - a test-only SHA-256, so `dev-setup.sh` is unchanged;
   - yield hooks.
5. Characterisation tests assert 0.3.39's behaviour — bugs included, each named by finding. F2
   inverts them.
6. The hooks reach engine-side code only; cross-core ordering is reviewed, not tested.

**Backend tests that read firmware.** Found with
`grep -rnoE '"[a-z_0-9]+\.(c|h)"' backend/tests` plus the `_fw`/`const` helpers. The only false
hit is `test_web.py` `"a.c"`. The census was independently verified complete in round 5.

| Test (file :: function) | Reads | Retarget |
|---|---|---|
| `test_jpanel_api` :: `test_the_firmware_builds_exactly_that_base` | `jpanel.c`, `link.c` | F1 |
| `test_jpanel_api` :: `test_both_ends_agree_on_the_name_of_the_integrity_header`, `test_the_cap_fits_the_buffer_the_panel_receives_it_in`, `test_the_panel_can_hold_the_longest_poll_the_box_will_send` | `jpanel.c` | F1 → `jpanel_core.c`; the poll buffer is 1536 B in F2 and 1024 B for 0.3.39 in W3 |
| `test_jpanel_api` :: `test_the_panel_asks_for_a_position_the_way_this_route_reads_one`, `test_the_panel_retries_only_the_status_a_retry_can_fix` | `jpanel.c` | F1 (move), **F2 (rewritten: `?id=&at=`; 422 retry without the expiry re-check)** |
| `test_jpanel_api` :: `test_the_panel_reads_the_queue_this_poll_serves`, `test_the_panel_reads_the_sibling_name_this_poll_serves` | `jpanel.c`, `display.c` | F1 / F3 |
| `test_jpanel_api` :: `test_the_cap_fits_the_bubble_the_panel_draws_it_in` | `display.c`, `font.h`, `ui.h` | F3 |
| `test_jpanel_api` :: `test_the_queue_cap_is_the_same_number_on_both_sides` | `jpanel.h`, `ui.h` | F1 (`jpanel_types.h`), F3 |
| `test_jpanel_api` :: `test_the_firmware_counts_repeats_in_sixteen_bits`, `test_dads_voice_is_male`, `test_the_firmware_sends_the_repeat_count_and_the_raw_decode`, `test_the_settings_poll_carries_the_waiting_count` | `font.c`, `speech.c`, `main.c`, `ota.c` | none (`jpanel_poll_soon` keeps its name) |
| `test_endpoint_report_now` :: `test_every_way_of_starting_a_message_waits_for_the_speaker`, `test_a_replay_that_ends_re_arms_the_buttons` | `jpanel.c` | F1 (move), **F2 (rewritten: `stream_begin_waiting`, the replay path)** |
| `test_endpoint_report_now` :: `test_the_scan_found_a_realistic_number_of_fields` | `jpanel.c` | F1 |
| `test_endpoint_report_now` :: `test_a_cue_does_not_shorten_a_recording` | `audio.c` | F1 → `stream.c` |
| `test_endpoint_report_now` :: `test_every_talk_failure_the_firmware_can_hit_carries_a_name` | `talk.c` | F2 |
| `test_endpoint_report_now` :: `test_an_open_microphone_is_deaf_to_commands`, `test_commands_are_muted_while_a_message_is_recorded`, `test_every_icon_reaches_the_same_path_the_voice_does`, `test_the_button_toggles_and_is_debounced` | `display.c`, `speech.c/h` | F3 |
| `test_endpoint_report_now` :: `test_both_halves_of_the_dash_are_counted_and_told_apart` | `ui.c`, `display.c` | F3 |
| `test_endpoint_report_now` :: `main.c`/`ota.c` tests (7) | `main.c`, `ota.c` | none |
| `test_endpoint_api` :: `test_the_ceiling_is_the_panel_s_buffer` | `talk.c` | F2 |
| `test_endpoint_api` :: `test_the_panel_waits_longer_than_the_question_it_allows`, `test_the_recording_window_outlasts_the_silence_it_waits_for` | `audio.c`, `display.c`, `talk.c`, `ui.h` | F1 (if the cap moves), F2 (`talk.c` deadline), F3 |
| `test_endpoint_api` :: `main.c`/`vocab.c` tests (4) | — | none |
| `test_panel_ws` :: `test_the_firmware_spells_the_same_route_and_frame` | `link.c`, `wsproto.h` | F1 |
| `test_nudge` (2), `test_g2p` | `nudge.c/h`, `vocab.c` | none |

## 11. Identity (W2): no RLS change

**Problem (C1).** A re-flash replaces a panel's principal, which strands any unplayed mail
addressed to the dead key.

Panel routes, RLS, nudge, `_waiting_count`, `/converse`, rename and revoke are unchanged.
`panel_ws` gets only W3's `_ALLOWED` row.

1. **`readdress_orphans(session)`** is idempotent and runs in owner context. It moves unplayed
   rows addressed to a revoked key onto the same subject's single live jpet key, and never
   crosses subjects.
   - Called from: `reflash` and `rotate`; the retention loop (`sweep.py`); and right after each
     insert in `send`, `send_text` and `send_audio`.
   - **Serialised.** `reflash` and `rotate` first take
     `LOCK TABLE app.jpanel_message IN SHARE ROW EXCLUSIVE MODE`, and the subject's `principals`
     rows `FOR UPDATE`. A concurrent send's INSERT (ROW EXCLUSIVE) then waits until the re-flash
     commits. Its post-insert readdress runs in a new transaction that sees the revoke, so it
     moves the row.
   - Test: `test_send_racing_reflash_is_carried`, with two real connections and a forced
     interleave (the re-flash holds its transaction open while the send inserts). It asserts the
     row lands on the new key without waiting for a sweep.
2. **`reflash` picks a subject in this order:** the one owning the most recent key (live or
   revoked) for this name and role; else the oldest; else a new one.
3. **Owner views group by subject**, read-only. `device_id` is:
   - the live jpet key's id, for a live panel;
   - the subject id plus `retired`, when there is no live key (listed only if it has rows);
   - `unknown`, for a NULL subject or hard-deleted principal ("Unknown panel").

   `DELETE /messages`, **`DELETE /chats`**, the badge and `/summary` resolve to the subject's
   principals. Display-role turns are their own "Displays" group. Rows to a recipient without a
   live key are **undeliverable** (C3/C5): discardable and clearable.
4. **Explicit merge, `merge_subject(source, into)`.** It requires the same `device_role`, a live
   key on the target, and none on the source.
   - It re-addresses mail, re-points revoked keys, tombstones the source as
     `merged:<into>:<source>`, and logs the merge.
   - Exposed as owner `POST /jpanel/panels/{id}/merge` and, from W2, as **debug
     `POST /api/debug/jpanel/panels/{id}/merge`** under `_OWNER_CTX`.
   - The debug route comes with `GET /api/debug/jpanel/panels` (subjects, retired groups, ids,
     row counts), the verbs `debug-connect.sh jpanel-panels` and `jpanel-merge`, a
     `DEBUG_ACCESS.md` row, and `test_debug_merge_route` (CLAUDE.md #10).
5. **The one-time migration is `readdress_orphans`.** No schema change.

**Rollback.** None is needed: rows stay principal-keyed, and revision files are never removed.
Under rolled-back code the `merged:` names appear in the device list.

**W2 touches principal scope, so it is escalated to the owner and its wave gate includes a
red-team review.**

### 11.2 Tests

No new tables or policies are added, so the existing RLS tests stand. New tests:

- `test_reflash_carries_unplayed_mail_to_new_key` (sibling and played rows untouched);
- `test_rotate_carries_unplayed_mail`;
- `test_send_racing_reflash_is_carried`;
- `test_readdress_orphans_idempotent`;
- `test_reflash_after_revoke_lands_on_last_used_subject`;
- `test_owner_threads_group_by_subject_across_reflash`;
- `test_unknown_panel_group_clearable`;
- `test_retired_listed_only_with_rows`;
- `test_unheard_to_retired_panel_is_discardable`;
- `test_clear_pet_chat_covers_old_keys`;
- `test_merge_rules_and_tombstone`;
- `test_debug_merge_route`;
- `test_identity_backfill_pg`;
- `test_send_after_reflash_delivers`.

## 12. Waves

Each wave is one PR. Each package runs its **whole** gate set, from its own directory (CLAUDE.md
#11):

- **backend:** `ruff check .`, `ruff format --check .`, `pyright`, `pytest`. It also runs on
  every firmware wave, because its path filter covers `firmware/main/**`.
- **frontend:** `npm run lint`, `npm run typecheck`, `npm run test`, `npm run build`.
- **supervisor:** its full set, triggered by `firmware/dist/**`.
- **firmware:** `make -C firmware/host test`.

**Every firmware wave also:**

- bumps `version.txt` and commits a rebuilt `dist/`;
- keeps the bootloader byte-identical;
- applies the §10 retargets it owns.

**Every wave also:**

- runs `docs-freshness`;
- updates this plan, JP §3b, the firmware READMEs, and the ROADMAP / `plans/README` rows;
- adds no dependency without updating `dev-setup.sh` (#8);
- leaves no shell-only operator step (#10).

F3 also updates JP's text on exit, the pair, and refusals.

| Wave | Contents | Depends on | Gates |
|---|---|---|---|
| **W1** box/PWA, invisible | A3 box; C2; C5; C6 (query function + `/summary`); C7 audio route; C8 (non-visual); C9; C10; C11; skew pins; revision-delete CI check | — | backend, frontend, docs |
| **W2** identity | §11 (serialised `readdress_orphans`, grouping, undeliverable rows, owner + debug merge and listing); retired chips hidden. **Escalation + red-team review.** | W1 | backend, frontend, docs |
| **W3** contract | `/waiting` ids + `offered_at`; `/next?id=` + headers; `/played {heard_ms}`; `/returned` + `_ALLOWED` + tunnel test; C3 routes; uuid guards; columns `heard_ms`, `offered_at`, `returns NOT NULL DEFAULT 0`; head and buffer pins | W2 | backend, docs |
| **F1** host foundations | §10, no behaviour change | — (parallel) | firmware, backend, supervisor, docs |
| **F2** engine | §7; defects only; wrappers | F1, W3 | firmware, backend, supervisor, docs |
| **F3** state machine | §8; Appendix A; Q1–Q3; hand-back | F2 | firmware, backend, supervisor, docs |
| **F4** panel visuals | Overlay, pips, pulse, glyphs, hush ring; `CUE_NOBODY` / `CUE_SENT` / `CUE_BUSY` contours. **GUI gate:** three interactive mocks (WebAudio) first, saved to `docs/mocks/jpanel-panel-messaging/`. | F3 | firmware, backend, supervisor, docs |
| **P1** PWA | C3 buttons; C4 copy; C5 status; composer; launcher badge; read dwell; retention line; retired threads + merge button; "Displays"; "Load earlier"; delete/revoke copy. **GUI gate.** | W2, W3 | frontend, docs |

**Order:** W1 → W2 → W3 → F1 → F2 → F3 → F4. P1 can land any time after W3; F1 can run in
parallel with the W waves.

## 13. Risks, losses, questions

**Risks**

- F3 is a one-wave replacement (§8.5–8.6).
- Cross-core ordering is reviewed, not tested.
- HTTPS waits last until their enforced deadlines.
- The name-based display gate needs review backing.

**What the owner loses**

- Web push.
- Resend after a failed send.
- "Played" instead of "heard".
- Renames rewrite past messages.
- A stop under 2 s returns the message (as a badge).
- No notice or replay during her own upload ("busy").
- A cancel before the head can cost one attempt.
- Each pre-0211 subject needs one explicit merge.
- An ack given up after 10 minutes may replay within the cap.
- An arrival during an upload is announced only after it.

**Questions** (the plan runs with these defaults):

1. `HEARD_MIN_MS = 2 s`.
2. The 600-character text cap stays.
3. Web push stays deferred.
4. PLAY waits for a free speaker, not for a pet turn in flight (today's behaviour). A late pet
   reply preempts the stream, which then resumes.

## 14. Review log

**Rounds 1–4** produced 66 blocking and 129 non-blocking items. All were verified and accepted.
Mechanisms they introduced and later removed:

- an event queue;
- a subject/RLS rewrite and its revert;
- a busy token;
- `PEND_REPLY`;
- stall and codec-wedge discards;
- summed bounds;
- a cue queue.

**Round 5** produced 10 blocking items. Every claim was checked against the code before it was
accepted, including:

- `/send` replies only after `_transcribe`, whose timeout is `min(whisper_timeout, 60)`;
- `panel_ws` cancels the handler when the panel cancels;
- `/message/{id}/pcm` sends no sender headers;
- `do_replay` relies on a stale `s_in_from`;
- `UI_TAP_ORDER_PLAYING_LEN == 5` is pinned in test #4;
- READ COMMITTED snapshots per statement;
- `clear_history` keeps every unplayed panel-addressed row.

All 10 were accepted:

| Item | Resolution |
|---|---|
| FW-1 the "SPSC" cue queue had a consumer write on the producer side | The queue is replaced by render-owned slots, each acknowledged through an arbiter-owned consumed seq (§7.4) |
| FW-2 a press cue cut outcomes | Press cues never cut outcome, refusal or chime cues; they wait 400 ms, then drop |
| FW-3 SEND was cancelled by STOP | SEND carries no gen and no `cancelled`; only expiry before claim ends it; the lease is released on every end; `test_standby_after_tick_still_sends` |
| FW-4 the send deadline ignored transcription | Progress-refreshed upload, then a head deadline of 60 s whisper + 15 s; modelled in the fake link |
| UX-1 the offer resurrected after an exit | Every non-natural exit from PLAY clears it; playing a non-replay message clears it at the press; two named tests |
| UX-2 replay sender and run behaviour were unspecified | REPLAY carries `{from, from_dad}` and the engine seeds `cur` from it; it never continues a run and is never acked; replay outcomes stated |
| UX-3 the chime could sound with nothing to press | Eligible only in PET/GRID with the notice drawable; ids that have left `waiting` are dropped every frame; two named tests |
| UX-4 five U rows could not hold | Every row re-verified against its assertions: #2, #3, #4, #5, #6, #8, #27 and #29 become R, with the protected property kept |
| BE-1 unheard mail to a keyless recipient was never clearable | It is undeliverable, so it is discardable and clearable; tests for retired and Unknown |
| BE-2 the re-flash race under READ COMMITTED | A table lock in `reflash`/`rotate` serialises the post-insert readdress; a two-connection interleave test |

**Round-5 non-blocking items (30)**, all accepted:

- **HTTPS link handling:**
  - body timeout reset after the head;
  - `open` split into `left/3`;
  - `wsproto` helpers;
  - `talk.c` and `ota.c` migrated to `deadline_ms`.
- **Engine timing:**
  - PLAY expiry of 35 s;
  - 422 retry without the expiry check;
  - packed atomic words and acquire order;
  - chunked cue rendering;
  - the mic gate covers a pending reply;
  - whitelist and `stream_stop`;
  - E1.3 reworded;
  - ack-failure run end stated;
  - seq skips 0;
  - F2 rollback;
  - storm delays under 2 s.
- **UI machine and Appendix B:**
  - Appendix B additions: SPEAKING, NOT WHILE WE ARE SPEAKING, the badge quadrant, confirm's
    fixed discs;
  - swipe stops only if the selection moved;
  - the SEND backstop leaves the mode alone;
  - a natural finish does not STOP;
  - the fresh-offer hold noted beside E1.6;
  - a returned message starts as a badge;
  - REPLY checks the mic gate before stopping.
- **Backend and docs:**
  - `returns NOT NULL DEFAULT 0`;
  - hand-back in F3 only;
  - the skew window reworded;
  - the census F2 rows;
  - `/next?id=` ownership and the own-sent test;
  - the head-frame pin;
  - the debug listing verb;
  - E4.2 timing;
  - the rollback tombstone note.

---

## Appendix A — every `ui_tests.c` test (51), re-verified assertion by assertion

**U** = ported to `sm_step` with the same assertions; only API names change, and every literal
assertion holds against the new machine. **R** = rewritten; the new assertion keeps the property
the test protects. No test is deleted.

| # | Test | Disp. | Note / new assertion (property protected) |
|---|---|---|---|
| 1 | `test_the_order_is_a_list_and_ends_with_the_pet` | R | Each mode's target table asserted literally; each ends in PET or swallow (*every press reaches something*). |
| 2 | `test_the_grid_is_modal_and_outranks_everything` | R | In GRID, every point (quadrants and centre) resolves to a cell or swallow, with a notice waiting and an offer standing. A run cannot coexist with GRID, so that part becomes "a hold during PLAY is swallowed" (*the menu is modal*). |
| 3 | `test_the_notice_is_tappable_only_in_one_quadrant_today` | R | Geometry and the "standing offer does not swallow a notice press" checks as before (offer hidden, still kept). The last step drives a real press into PLAY and, after grace, the top right → EXIT (*the exit returns once a message actually runs*). |
| 4 | `test_a_cue_no_longer_decides_what_a_press_means` | R | With `speaker_busy` from a cue, PET/GRID targets equal the quiet ones, and PLAY's targets do not change while a cue sounds (*a cue does not take the table away*). The table-length literal is dropped. |
| 5 | `test_the_exit_ends_a_message_while_it_is_audible` | R | Press into PLAY, run past grace, top right → EXIT ends the run with STOP (*the corner is the exit while a message is audible*). The `UI_TAP_ORDER*` literals become PLAY-table literals. |
| 6 | `test_the_notice_is_painted_through_its_own_arrival_cue` | R | Notice drawn while its own chime sounds; then a **press** into PLAY hides it (*a playing message owns the screen*), instead of setting a bare `stream_active`. |
| 7 | `test_a_hold_after_a_poke_still_opens_the_menu` | U | The hold needs no quiet speaker. |
| 8 | `test_the_controls_are_pressable_for_as_long_as_they_are_drawn` | R | From the press: controls drawn, transport claimed (ignored) through pending/waiting/claimed-not-ON; a replay likewise (*drawn ⇔ pressable*). Bare `jfetching`/`stream_active` inputs are not used. |
| 9 | `test_a_second_press_cannot_cancel_what_the_first_one_started` | U | Exit grace from the press, every phase. |
| 10 | `test_the_face_follows_the_message_that_is_playing` | U | `cur` is seeded across the chain; plus the every-frame assertion. |
| 11 | `test_a_notice_does_not_show_through_the_playback_screen` | U | Press-driven; the gap stays in PLAY. |
| 12 | `test_a_swipe_changes_which_waiting_message_is_offered` | U | |
| 13 | `test_one_waiting_message_cannot_be_swiped_away` | U | Stops only if the selection moved. |
| 14 | `test_a_swipe_stops_the_message_it_moves_off` | U | Includes "at the end of the queue stops nothing". |
| 15 | `test_a_swipe_is_not_also_a_hold` | U | |
| 16 | `test_a_long_press_on_a_menu_is_just_a_press` | U | Fresh offer: no grid. Standing case is a new test. |
| 17 | `test_the_selected_message_is_the_one_that_plays` | R | The posted command carries the selected **id** (index on an old box); `sm_face` before the claim = the sister (*the selected message plays, and its face shows from the press*). |
| 18 | `test_the_numeral_says_which_of_how_many` | U | The swipe during pending stops and returns to PET with sel moved; `sel_shown == 3` on the notice. |
| 19 | `test_a_new_message_sends_the_selection_home` | U | The harness sends no ids → the count-rise fallback; same results. |
| 20 | `test_every_playback_control_answers_the_finger` | R | Reply mid-message → `mode == RECORD` with `CUE_REPLY` (was `pending == UI_PEND_REPLY`); the other cue assertions stay (*every control answers with its own sound*). |
| 21 | `test_the_draw_layers_are_a_list` | R | Per-mode view composition; grid on top (*a menu is never covered*). |
| 22 | `test_a_notification_arrives_and_a_tap_plays_it` | U | Posted after the speaker is free. |
| 23 | `test_a_press_does_not_evaporate_behind_its_own_cue` | U | Larger `PENDING_MS`. |
| 24 | `test_a_press_during_the_arrival_cue_plays_the_message` | U | |
| 25 | `test_the_run_controls_appear_from_the_press_not_the_stream` | U | The pause glyph is drawn while ignored. |
| 26 | `test_the_top_right_corner_ends_a_message_while_it_plays` | R | Second half: after the exit, mode PET with **no offer**; one press (owner Q2). |
| 27 | `test_the_pair_waits_for_a_finger_not_a_clock` | R | `jpanel_clear >= 1` becomes "`play_outcome` heard consumed, offer set"; plus standing at 10 s, still drawn, and hidden while a message waits (*the pair waits for a finger*). |
| 28 | `test_reply_records_to_whoever_just_spoke` | U | The recipient comes from `sm_face` (the offer). |
| 29 | `test_sending_does_not_raise_the_playback_controls` | R | After SEND, `sending` with no transport or pair drawn; then a **press** on the notice is refused "busy", and a press after `SENT` raises the controls (*an outgoing message never shows playback controls*). |
| 30 | `test_a_short_button_press_darkens_blanks_and_mutes` | U | Extended to every mode; no `CAPTURE_OPEN` after standby; a send continues. |
| 31 | `test_a_five_second_hold_requests_deep_sleep_exactly_once` | U | |
| 32 | `test_a_hold_on_the_pet_opens_the_grid_at_every_free_tap_count` | U | |
| 33 | `test_the_grid_reaches_all_four_people` | U | |
| 34 | `test_the_grid_closes_itself_but_a_stray_finger_does_not` | U | |
| 35 | `test_a_tick_pressed_into_silence_refuses_to_send` | U | `CUE_OOPS`. |
| 36 | `test_the_cross_discards_and_the_pet_is_not_poked` | U | |
| 37 | `test_a_named_turn_with_nobody_speaking_costs_nothing` | U | |
| 38 | `test_a_failed_turn_says_so_rather_than_going_quiet` | U | |
| 39 | `test_every_dash_is_counted_and_its_two_causes_told_apart` | U | |
| 40 | `test_a_reply_arms_a_follow_up_and_the_cap_ends_it` | U | |
| 41 | `test_stop_said_out_loud_leaves_every_state` | U | Today's handler, including the RECORD drop. |
| 42 | `test_pressing_the_face_cycles_through_the_waiting_messages` | U | The face moves the selection → stop → PET (menu down). |
| 43 | `test_the_face_is_not_a_control_with_one_message` | U | |
| 44 | `test_pressing_replay_twice_does_not_pause_what_never_started` | U | The transport is ignored until ON; once ON it pauses. |
| 45 | `test_a_held_stream_resumes_rather_than_being_thrown_away` | U | |
| 46 | `test_the_notice_shrinks_to_a_badge_and_a_second_one_does_not_restore_it` | U | Plus new: hidden at arrival. |
| 47 | `test_the_notice_stays_painted_while_a_cue_sounds` | U | |
| 48 | `test_the_queue_count_only_sounds_on_the_way_up` | U | No-ids fallback; plus a new id-based test. |
| 49 | `test_what_the_box_said_about_a_message_is_answered_in_sound` | R | Inputs come from `send_outcome` seqs (was `jstate`). SENT → `CUE_SENT` + poll soon, consumed once; NOBODY → `CUE_OOPS` + caption; FAILED → `CUE_OOPS`, no caption; EMPTY → `CUE_STOP` (*each outcome its own sentence, consumed once*). |
| 50 | `test_a_replay_still_reads_as_playing` | U | The replay view is the run bar until ON, then the pair layout with the playing glyph. |
| 51 | `test_the_action_list_never_overflows` | R | The worst frame rebuilt from F3's effects (no `UI_PEND_REPLY`); same bound (*a frame never silently drops an effect*). |

**`tests.c` groups the plan touches** — all U:

- **ring**: `test_an_empty_ring_*`, the seam, wrap, reset, order, partial, lone-byte tests.
  `ring_reset` stays; `stream.c` does not call it.
- **confirm / who-grid**: tick/cross halves, transport discs, `test_the_who_grid_*`, turned panel.
  New: `test_confirm_disc_reach`.
- **wsproto**: frames, window, fallback, reconnect. New: `wsp_wait_step` and `link_left`.
- **cue**: the sweeps cover `CUE_BUSY`/`CUE_NOBODY`. New: `test_cue_busy_distinct`,
  `test_chunked_cue_equals_whole_render`.

## Appendix B — owner decisions the machine touches

**K** = kept. **C** = changed, either by a §4 owner decision (★) or because the old behaviour is
the finding (†).

| Source | Decision | Disp. |
|---|---|---|
| JP Part B; `display.c` "pop-up box" | Notice when a message waits; no auto-play | K |
| `ui.h`/`display.c` "very large … 15 seconds … smaller one top left" | Big for 15 s, then a badge; a second arrival does not restore it | K (clock from first visible †; a returned message starts as a badge †) |
| `display.c` "capture everything in that top left quadrant" | The badge's hit area is the whole quadrant | K |
| JP sleep rules; dark wake | Big box after dark; waiting does not hold the screen awake | K |
| JP "multiple messages are one press, and a finger gets out" | One tap plays all; a finger stops it | K (mid-run arrivals join) |
| JP 0.3.33 "the count became a position" | `n/m` and swipe | K (counts the playing message †) |
| `ui.h`/`display.c` "replay button … 10 seconds"; 0.3.25 "no more waiting for it to time out" | The pair never expires | K; **standing after 10 s ★** |
| `confirm.h` "the pair never moves between the two states" | Disc positions fixed | K (standing changes the hit area, not the drawing) |
| 0.3.25 "the face stays for the whole exchange" | Sender's face with the pair | K |
| 0.3.25 "the new message takes the corner" | A waiting message takes the corner | K (offer hidden) |
| `display.c` "top right where there's no icon" | Exit corner | K; **one press during playback ★** |
| 0.3.29 "pause icon for half a second" | Exit grace 700 ms from the press | K, every phase |
| 0.3.28 "exit swallowed presses aimed at the notification" | The notice outranks the exit | K |
| 0.3.32 "every playback control answers the finger" | Distinct play/pause/resume/reply/stop cues | K |
| 0.3.24 "as soon as I click it … show right away" | Controls from the press | K |
| 0.3.27 "pause button over a message that did not exist" | No controls during a send | K |
| 0.3.30–0.3.32 "little girl icon … versus the dad icon" (×3) | The face shows the playing message's sender | K (`sm_face`; replay seeded from the offer) |
| JP 0.3.33 "notification showed in the background of the menu" | No notice during playback | K |
| JP 0.3.33 swipe | Clamped swipe; the selection goes home on arrivals | K (home on unseen id †) |
| `ui.c` "handle stopping the current playing message" | Swipe and face stop the sound | K (only when the selection moved, as today) |
| JP 0.3.37 "press the icon on the top left … cycle" | The face cycles | K |
| JP 0.3.36 "click the play button sometimes it just pauses" | Transport ignored until it sounds | K |
| JP 0.3.35 "long press treated as a normal press of menu items" | A hold on a control = a press | K |
| `display.c`/`ui.h` "long press will pull up the menu even after multiple presses" | Hold → grid at any free tap count | K |
| JP 0.3.33 "commands get recognized" | Commands muted in LISTEN/RECORD/standby | K |
| `display.c` "NOT WHILE WE ARE SPEAKING" | The mic does not open while the panel makes sound | K (mic gate) |
| `display.c` "SPEAKING, AND IT OUTRANKS THE TOY" | A poke during a reply only flinches | K |
| `ui.h`/`display.c` "a wake word … as if I held the panel" | Hands-free listen | K |
| `ui.h`/`display.c` "after the text-to-speech … turn" | Auto-continue, capped at 6 | K (yields to pending/chime) |
| `display.c` "it wants to keep going even if I say stop" | Voice stop ends the auto-continue | K |
| `display.c` "anytime we get a red … logged" | Dash counted and named | K |
| `ui.h` "the babies keep getting cut off" | Hush and lead timings | K |
| JP 0.3.38 "reply … doesn't actually get sent" | A failed send says so | K |
| JP 0.3.33 "it doesn't really play every single time" | The press waits out its cue | K (window lengthened †) |
| `display.c` 0.3.26 "single press blinks the screen"; "hold … 5 seconds" | Button = standby / deep sleep, from any mode | K (a send continues) |
| `display.c` "frozen robot" | Dark is painted | K |
| JP "Dim is a number"; "voice does not hold the screen awake"; "the pet answering still holds it" | Sleep `used` terms | K |
| `display.c` "long press seems to just reset the device" | Release-based button | K |
| JP PWA decisions (male voice, PWA audio, one child one conversation, two clears, clear history, picker) | PWA/box | K |
| `jpanel.c` ack-on-stop | A stop acknowledges | C † (F3, with a delivery header only) |
| — | Refusal sound | **C ★** (`CUE_BUSY`) |
