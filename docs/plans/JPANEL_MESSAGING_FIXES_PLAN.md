# jpanel messaging — the fix plan after the adversarial review

> **Status:** Scheduled · **Last verified:** 2026-10-11 · **Waves:** W1◻️ W2◻️ W3◻️ F1◻️ F2◻️ F3◻️ F4◻️ P1◻️

Voice post between the twins' panels and the owner's PWA works on the happy path. It fails on
the paths a four-year-old actually takes: pressing again while it loads, walking away
mid-message, holding the pet while a message waits, talking over a TV, and a USB re-flash.

**The work behind this plan:**

- **Original review:** 32 findings. B6, B7 and B9 duplicate A3, A1 and A6; three of the 32 are
  lists of UX gaps.
- **Verification against the code:** two more defects, A8 and A9.
- **Cold review of this plan, round 1:** 15 blocking items and 26 non-blocking.
- **Round 2:** 17 blocking items and 33 non-blocking.

The round-2 blockers clustered into four groups:

- edge events between tasks that could be lost or reordered;
- several writers on one audio buffer;
- the blast radius of an identity migration;
- owner decisions silently overturned.

So this revision removes mechanisms instead of patching them:

- **The engine publishes one snapshot the UI reads every frame**, not events (§7).
- **The audio task is the only writer of the speaker** (§7.4).
- **Re-flash identity is fixed where re-flash happens**, with no RLS rewrite (§11).
- **Every owner decision is kept**, unless §4 lists it as a question whose default is the old
  behaviour.

The owner approved restructuring: *"I'm totally down for restructuring the whole UI state
machine to make sure that everything is done correctly."* The panel's interaction logic therefore
becomes one explicit, host-tested state machine (§8).

**Binding inputs:**

- `CLAUDE.md`;
- `docs/DOC_LIFECYCLE.md`;
- `docs/reference/PROCESS.md`;
- `docs/reference/DEVELOPMENT.md` ("Verifying a change");
- `docs/plans/JPANEL_PLAN.md`, called **JP** below.

The panels are placeholder-named **Quorra** and **Tamsin**. Function names are as of firmware
0.3.39. Line numbers are omitted because they drift.

## 1. Goal

A message to or from a child is never lost silently or replayed by a fault, and is never
reported as heard when it was not. Every press a pre-reader makes is answered in a way she can
interpret without reading.

The panel's behaviour is defined by one state machine. Every mode, deadline and drawn control is
exercised on the host, and the code that ships is the code that is tested.

## 2. Expectations, by journey

Each expectation is testable and is cited by its E-number in §3 and in the tests.

**J1 — a child sends**

- **E1.1** Every recording a panel can capture (`CAPTURE_MAX_MS`) is accepted by the box. A body
  one byte longer is refused, never truncated.
- **E1.2** After the tick or the hush auto-send, she hears exactly one outcome within the send
  deadline: **sent**, **nobody to send to**, **dropped** (the box trimmed it to nothing), or
  **didn't go**.
- **E1.3** While an upload is in flight the glass shows it (an F4 visual). A message that arrives
  meanwhile still gets its sound and its notice.
- **E1.4** Nothing during an upload alters the bytes being uploaded. Anything that would need the
  microphone is refused audibly.
- **E1.5** A recording she deliberately started and then left silent ends with the "dropped"
  sound. Every refusal to open the microphone, from any mode and by touch or voice, is audible.
- **E1.6** A hold on the pet's centre opens the "who?" menu:
  - when nothing is up;
  - when a waiting message shows as a badge;
  - when the again/reply pair has stood for 10 s (the default in §13 Q1).

  The hold keeps today's guards: the rim margin, maintenance tap counts, and a swipe.

**J2 — a child receives, plays, stops, replies**

- **E2.1** Pressing the notice plays the message whose sender's face the notice shows, chosen by
  id when the box sends ids.
- **E2.2** Stop is honoured at any moment after the press: the exit, the button, a face press, a
  swipe, or reply.
  - **Moments covered:** queued, waiting for the speaker, waiting for the response head, in the
    preroll, sounding, paused, draining, and between run messages.
  - **Speaker:** silent within one audio chunk (40 ms) plus the codec's DMA queue (~90 ms).
  - **Engine:** nothing chains on.
  - **Network:** the request is cancelled within one 250 ms link step (the WebSocket path; HTTPS
    limits are in §7.6).
- **E2.3** During playback, the top-left face is the sender of the message playing — including
  in the gap between run messages. A face press **cycles the selection** and a swipe **moves it
  by direction, clamped** (JP 0.3.37, kept). Both stop the sound. No message is skipped or
  replayed by the selection logic.
- **E2.4** A message counts as heard only if it sounded for `HEARD_MIN_MS` or 80% of a short
  message, and only from F3 on. A shorter stop hands it back: it stays waiting and the delivery
  attempt is refunded, idempotently and within a bound.
- **E2.5** A heard message is acknowledged exactly once, even through network loss. A lost
  acknowledgement never replays, skips, or leaves controls over silence.
- **E2.6** Every playback ends, whatever the network does, within the deadlines of §7.5 and §8.4.
  A failure says "didn't go" and restores the notice.
- **E2.7** Reply during playback opens a working recording to the sender at once, with the tick
  and cross live. The pair, a replay or the exit can never be live over a recording.
- **E2.8** Exit leaves playback with today's behaviour: stop, then the pair. The one-press exit
  is a question in §13 Q2.
- **E2.9** A message arriving during a pet conversation or a recording gets its chime when the
  microphone closes and the speaker is free. Its 15 s big notice counts from when the notice is
  first visible.
- **E2.10** A press that cannot be served always ends audibly, never silently.
- **E2.11** A message already announced is never announced as new again: not after a hand-back,
  and not after an acknowledgement retry.

**J3 — Dad sends, reads, clears**

- **E3.1** A message to an unknown panel is refused before speech is synthesised, with a
  readable error; the draft is kept.
- **E3.2** Every unplayed message to the owner is renderable. Reading counts after a dwell, and
  only while the PWA has focus.
- **E3.3** Each message carries a status: not yet seen by her panel; on her panel, not played;
  started, not finished; **Played**, plus "stopped after N s" when it was cut; or couldn't be
  delivered. Twin-to-twin messages carry the same statuses.
- **E3.4** An undelivered message can be retried or discarded.
- **E3.5** The clear's "kept" text names the right child.
- **E3.6** Twin-to-twin messages show in both children's threads.
- **E3.7** A stale poll never removes a just-sent bubble.
- **E3.8** Errors read as sentences. The composer shows its limit and says the message waits for
  a tap on the panel.
- **E3.9** The launcher's jpanel tile shows the unread count.
- **E3.10** Audio plays in iOS Safari. A malformed id gets 404, never 500.

**J4 — re-flash**

- **E4.1** A re-flash loses nothing. Unplayed mail follows the panel to its new key, and its
  history, badge and pet transcripts stay in one thread with no ghost chip.
- **E4.2** Threads of retired panels — revoked, or pre-0211 subjects — are visible under their
  name, can be cleared, and can be merged into a live panel by the owner, explicitly and never
  automatically.

**J5 — network loss**

- **E5.1** A message is acknowledged only if it streamed whole and sounded.
- **E5.2** An acknowledgement that was not delivered is retried with backoff. While it is owed,
  no fetch starts, and a press says "didn't go" with the notice kept.
- **E5.3** A failed send says so. It is not kept for resend (§5).
- **E5.4** Mixed releases keep every journey working, at worst with today's behaviour (§6).

## 3. Traceability

"Verified" records what reading 0.3.39 found. **✓** means confirmed. **✓\*** means confirmed
with a correction, given below the table.

| ID | Verified | Fix | Wave | Proving test(s) |
|---|---|---|---|---|
| A1 | ✓ | Stop generation; the audio task discards; network cancel; pause tagged with gen (§7) | F2 | `jpanel_tests: test_stop_at_every_interleave_point`, `stream_tests: test_stop_while_paused_drains`, `test_stop_during_drain_frees_speaker` |
| A2 | ✓ | Engine phases with deadlines; the UI reads the level, not edges (§7.2, §7.5) | F2 | `test_chain_fetch_failure_returns_to_idle` (×4) |
| A3 | ✓ | The box accepts the capture cap; caps pinned; `EMPTY` outcome | W1, F2 | `test_send_accepts_exactly_the_capture_cap`, `test_send_refuses_cap_plus_one`, `test_capture_cap_pinned_across_firmware_and_box` |
| A4 | ✓ | Serial engine; ack before the next command; chain by id (§7.3) | W3, F2 | `test_lost_ack_is_retried_and_never_replays`, `test_unreachable_ack_returns_to_idle_and_notice` |
| A5 | ✓ | Capture lease (§7.4) | F2 | `stream_tests: test_capture_open_refused_while_leased`, `test_upload_bytes_unchanged_under_listen_attempts` |
| A6 | ✓\* | Selection by id; hand-back (F3); bounded, idempotent `/returned` | W3, F2, F3 | `test_stop_in_preroll_returns_not_acks`, `test_returned_is_idempotent_and_bounded`, `sm_tests: test_face_cycles_without_skipping` |
| A7 | ✓ | One seqlock snapshot (§7.2) | F2 | `test_snapshot_is_never_torn` |
| A8 *(new)* | — | Shared `s_state` gone; the snapshot has one writer | F2 | `test_send_outcome_survives_concurrent_chain` |
| A9 *(new)* | — | Only the audio task resets ring cursors (§7.4) | F2 | `stream_tests: test_only_producer_and_consumer_touch_cursors` |
| A-tests | ✓ | Host foundations (§10) | F1 | `run_jpanel_tests`, `run_stream_tests` |
| B0 | ✓ | One state machine (§8) | F3 | `check_no_logic_in_display`, `sm_tests` |
| B1 | ✓ | Per-mode targets; reply opens RECORD at once; `PEND_REPLY` removed | F3 | `test_reply_during_playback_opens_a_working_recording` |
| B2 | ✓\* | Pair FRESH/STANDING; hold rule per mode (§8.3) | F3 | `test_hold_at_pet_centre_*` (×4) |
| B3 | ✓ | PAIR only while nothing waits; one quadrant owner per mode | F3 | `test_quadrant_face_matches_press` + I2 |
| B4 | ✓ | Speaker priorities; queued cues wait; notice clock from first visible (§7.4, §8.5) | F2, F3 | `stream_tests: test_cue_priorities`, `sm_tests: test_message_during_pet_reply_gets_chime_and_big_box` |
| B5 | ✓ | Sending is level state; the notice stays; a press on it is refused audibly | F3, F4 | `test_notice_shown_and_refused_during_send` |
| B6 | = A3 | — | — | — |
| B7 | = A1 | — | — | — |
| B8 | ✓ | Silent deliberate recording → "dropped"; every microphone refusal audible | F3 | `test_silent_recording_says_dropped`, `test_every_mic_refusal_sounds` |
| B9 | = A6 | — | — | — |
| B10 | ✓\* | Pending deadline > reply cap; follow-up yields; `due()`; "stop stop" kept | F2, F3 | `test_press_behind_pet_reply_is_served`, `test_deadlines_survive_u32_wrap` |
| B-UX | ✓ | §9 B-UX table | F3, F4 | per row |
| C1 | ✓ | Re-flash re-addresses unplayed mail; owner views group by subject; explicit merge (§11) | W2 | §11.3 |
| C2 | ✓ | `send_text` validates; the picker is grouped by subject | W1, W2 | `test_send_text_unknown_panel_404_before_tts`, vitest |
| C3 | ✓ | Owner retry and discard | W3, P1 | `test_retry_resets_deliveries`, `test_discard_only_owner_or_undelivered`, `test_discard_releases_blob` |
| C4 | ✓ | `heard_ms`, `offered_at`; honest copy | W3, F3, P1 | `test_played_records_heard_ms`, vitest status matrix |
| C5 | ✓ | Between rows in both threads, with status | W1, P1 | `test_between_message_in_both_threads` |
| C6 | ✓ | Per-thread limit plus every unplayed-to-owner row | W1 | `test_every_unplayed_owner_row_is_returned` |
| C7 | ✓ (Range plausible) | 206 Range; uuid → 404 | W1 | `test_audio_range_206`, `test_non_uuid_is_404` |
| C8 | ✓ | `errorDetail` handles lists; `maxLength`; TTS timeout 60 s | W1, P1 | `client.test`, `test_send_text_tts_timeout_60s` |
| C9 | ✓ | `kept_for` per recipient | W1 | `test_clear_kept_names_recipient` |
| C10 | ✓ | Owner audio over the cap → 413 | W1 | `test_owner_audio_over_cap_is_413` |
| C11 | ✓ | Request sequence plus mutation epoch | W1 | vitest stale-poll test |
| C-UX | ✓ | §9 C-UX table | W3, P1 | per row |

**Corrections to the original review.**

- **A6:** the face needs `waiting > 1` after `fetch_head` decrements the count. With exactly two
  messages waiting, the face is dead during the first; the skip needs three or more.
- **B10:** the recording branch of "stop stop" is dead because commands are muted while
  recording. JP 0.3.33 muted them deliberately, so a word inside a message cannot act. **Kept.**
- **B2 / B-UX:** "the pair never expires" was the owner's 0.3.25 decision. It is kept, and §8.3
  removes the harm instead.
- **C7:** the 500 is confirmed. The iOS Range failure is plausible; the fix is cheap regardless.
- **Comment and doc drift**, fixed in the waves that touch it:
  - `s_repeat_until`'s comment promises the pair is displaced by a new message, which is the root
    of B3;
  - `ui.c`'s exit case says "No cue" after `CUE_STOP`;
  - `jpanel_replay` claims it does not ask the box;
  - JP still says "Capped at 20 s".

## 4. Owner decisions

**Kept unchanged:**

- the down edge fires the press;
- a long press on a menu is just a press (0.3.35);
- the two discs are hit by quadrants (*"split the [screen] into quarters"*);
- the pair never expires (0.3.25);
- the face press cycles and the swipe moves by direction, clamped (0.3.37);
- the top-left face shows the sender of the message playing (reported three times);
- the button is the power control in every state (0.3.26);
- the screen-sleep rules;
- a message that cannot be delivered is still a message, and a bulk clear keeps unheard rows;
- two clears per thread;
- 30-day retention after `played_at`;
- "the other panel" refusal; sibling RLS; panels never read pet transcripts;
- the free silent drop of an accidental wake.

**Changed, because each is the defect itself:**

| Today | After | Why |
|---|---|---|
| A finger stop always acknowledges ("she heard it"), to avoid burning an attempt | From F3: a stop after `HEARD_MIN_MS` of real output acknowledges. Shorter stops hand the message back, with the attempt refunded idempotently. | Never burning attempts is kept. Writing `played_at` for a press in the preroll is the C4 lie. |
| `/next?at=` by position | `/waiting` carries ids; `/next?id=` | Positional addressing caused A4 and A6. Ids are no new exposure, because a panel reads its own rows. |
| A pair that never expires turns the pet's centre into a permanent reply button | The pair stays drawn forever. After `REPEAT_MS` (10 s, the owner's figure) it is hit by its disc reach only (§8.3). | E1.6. The quadrant rule still holds while it is fresh. The 10 s is §13 Q1. |
| `ui.c` plus `display.c`'s duplicate | One state machine | Owner-approved restructure (§8). |

**Not changed. Each becomes a question in §13 whose default is today's behaviour:**

- the one-press exit (Q2);
- a distinct "busy" refusal sound (Q3);
- push notifications (Q4).

## 5. Scope

- **Deferred:**
  - **Push notifications.** There is no web-push infrastructure; it would be a new dependency
    and a privacy decision that JP §5 left open.
  - **Resending a failed send.** Keeping the capture buffer would hold the lease, and with it
    the pet's microphone.
- **Out of scope:** child-speech STT quality.
- **Unchanged:** `endpoint_status`.

## 6. Version skew

Panels OTA from the box's `firmware/dist/`. `main.c` installs any version that **differs** from
the one it runs (`strcmp`). Two combinations follow:

- **A new box with old firmware** is routine. It lasts at least until each panel's next manifest
  poll (about 15 min), and indefinitely for a panel whose OTA keeps refusing.
- **An old box with new firmware** only follows a box revert. Ops → Update is forward-only: it
  fast-forwards `main` and runs `alembic upgrade head`. A rollback is therefore a revert PR that
  the owner applies with Ops → Update.

Two rules:

1. **Box changes are additive for panels.** cJSON ignores unknown fields, headers are matched by
   name, and no status that 0.3.39 acts on changes meaning.
2. **Firmware degrades to today's path** when a field, header or route is absent, and never
   shows the child an error.

| Change | New box + 0.3.39 | Reverted box + new firmware |
|---|---|---|
| `/send` raw ceiling = capture cap | 35 s recordings stop failing — **fixes A3 for every panel in the field** | 413 → `FAILED("too-long")` |
| `/waiting` `queue[].id` | Ignored. The worst-case body (8 × 14-character names, plus ids) stays ≤ 1023 bytes, the size of 0.3.39's buffer, pinned by test. | Absent → positional `?at=` and count-rise "new" (today's rules) |
| `/next?id=`, `X-Jpanel-Duration-Ms`, `X-Jpanel-Delivery` | Not sent | `id` ignored, `at` honoured; absolute `HEARD_MIN_MS`; no refund |
| `/played {id, heard_ms}` | `heard_ms` NULL → "Played" | Extra field ignored |
| `/returned {id, delivery}` | Never called | 404 → logged; one attempt burned (today's worst case) |
| `offered_at`, owner subject grouping, re-flash re-address | Box-side | n/a |

Pinned by `test_old_firmware_contract_*` (the backend, with exact 0.3.39 bodies) and
`test_against_0_3_39_box_*` (a firmware host test whose fake box omits new fields and 404s new
routes).

---

## 7. The messaging engine (F2)

### 7.1 Two arrows between tasks, and nothing else

**render → engine:** a command queue of depth 4. Each command is
`{kind PLAY|REPLAY|SEND, seq, gen, id[48], at, to}`. Only render posts.

- A failed `xQueueSend` is reported to the machine on the next frame as `in.post_failed = seq`.
- "Poll soon" (render, `main.c`'s nudge) is not a command. It is an atomic flag the engine
  reads, so polls never fill the queue.

**engine → render:** one **snapshot**, published with a seqlock and written only by the engine.
Render copies it once per frame. **Level, not edge:** nothing can be dropped or reordered. A frame
skipped for calibration simply reads a later snapshot.

```
eng_view {
  phase: IDLE | POLLING | FETCHING | SOUNDING | ACKING | SENDING,  phase_since
  done_seq                     // last command fully finished (any kind)
  cur  { seq, id, from[32], from_dad, run }   // the message being fetched or played
  next { id, from[32], from_dad }             // the next run message, if any
  play_outcome { seq, id, kind: heard|stopped|returned|short|empty|failed|cancelled, reason }
  send_outcome { seq, kind: SENT|NOBODY|EMPTY|FAILED, reason }
  waiting { count, known, q[8] { id[48], from[32], from_dad }, sibling[32] }
  ack_owed: bool
}
```

The machine derives everything else from these fields:

- **"Its command finished"** is `done_seq >= my_seq`.
- **"Its send's outcome"** is `send_outcome.seq == my_seq`.
- **"Sounding"** is `phase == SOUNDING`.
- **Before the request goes out**, the engine seeds `cur.from` from the waiting entry it is about
  to fetch, the way `seed_sender` does today. During a run, the face is right in the gap between
  messages.
- **There are no events**, so there is no RESYNC, no event overflow, and no ordering to get
  wrong.

### 7.2 The snapshot seqlock

- **Writer:** `s = load(relaxed); store(s+1, relaxed); __atomic_thread_fence(RELEASE);` write the
  fields; `store(s+2, RELEASE)`.
- **Reader:** `s1 = load(ACQUIRE)` (retry if odd); `memcpy`; `__atomic_thread_fence(ACQUIRE)`;
  `s2 = load(relaxed)`; accept if `s1 == s2`.
  - The plain `memcpy` races in formal C11 terms. It is validated and accepted here, as in common
    GCC/Xtensa practice.
  - At most 4 tries. On failure the reader keeps the previous frame's copy.
- `eng_view` and the parsed `/waiting` struct are **static**, never on the stack: the face task
  has 8 KB and the engine 6 KB.

### 7.3 The engine is serial

`jpanel_core_step(core, now)` runs this order on every pass:

1. **Finish the current stream first.** If a stream has ended, been drained or been discarded,
   classify it, arm the acknowledgement and attempt it **before** dequeuing anything. While it is
   unclassified, `stream_begin` refuses. This closes the window in which a new fetch reset the
   counters of a message that had not been judged yet.
2. **Acknowledge.** There is one slot, `{id, kind PLAYED|RETURNED, heard_ms, delivery}`. Each
   attempt has a 5 s timeout.
   - **204 (or 404/410: the row is gone):** clear the slot and continue.
   - **Anything else:** end the run, `ack_owed = true`, and retry with `due()` backoff of 1, 2,
     4 … 30 s.
   - **While the slot is owed:** the poll drops the owed PLAYED id from `waiting` (E2.11), and a
     PLAY first retries the acknowledgement. If that retry fails, the PLAY finishes with
     `play_outcome = failed("ack-pending")`.
3. **Continue a run.** The engine fetches the next message by **id**, from the run list it took
   when the run began, skipping ids played or returned in this run. It never fetches "the head",
   so a returned message cannot replay in the same run. With an old box (no ids) it fetches
   `?at=0`, as today.
4. **Dequeue a command.** A PLAY or REPLAY whose `gen` is stale finishes immediately as
   `cancelled`.
5. **Poll**, when due or flagged. The poll's request is cancellable and yields to a queued
   command.

Every exit sets `done_seq` and the matching outcome in a single publish.

- **The chain** (`run`) depends on step 2's result. The snapshot shows ACKING with `next` set,
  so the UI draws "between messages" (the next sender's face, the controls live) rather than
  controls over silence.
- **The send** acquires the capture lease, makes up to 2 attempts (`SEND_ATTEMPTS`), releases the
  lease, and **then** publishes `send_outcome`. Releasing first means the next frame's microphone
  gate does not see a lease that is about to go away.

### 7.4 One owner of the speaker: the audio task

The audio task is the **only** writer of `s_play`, of the ring's read cursor, and of ring resets.
Everything else sends it a request:

| Request | From | Writer of the request | Priority |
|---|---|---|---|
| Pet reply `{ptr, len, seq}` | talk task (replaces `talk.c`'s `audio_play`) | talk; the audio task copies, then sets `accepted` | 1 (highest) |
| Press cue (one-slot mailbox) | render | render | 2 — dropped while a reply sounds (JP: a reply outranks an acknowledgement); otherwise preempts a cue and sounds instead of the ring's next chunk, exactly as `s_play` is served before the ring today |
| Message stream (ring) | engine (producer: write cursor, `gen` stamp) | engine | 3 |
| Queued cues (outcome, refusal, new message; FIFO, depth 4) | render | FreeRTOS queue | 4 — waits while anything above sounds **and while the capture is open** (no chime punched into a recording, E2.9). No expiry while waiting. A press on the notice cancels a pending MESSAGE chime. |
| Silence (standby, deep sleep, stop) | render | atomic flag | Overrides everything once |

**A reply is never refused because a cue is playing.** It waits for that cue (≤ 500 ms). This
removes today's lost reply and red dash: `audio_play` refused while a cue held `s_play`, and
`talk.c` reported `TALK_NET_FAILED`.

**Stop and pause.**

- `s_stop_gen` is written only by render; the stream is stamped with `gen` by the engine.
- On every chunk, the audio task checks **gen before pause**. If the stream is stale, it discards
  (`r = w`, a consumer write), outputs nothing, and marks it `stale`. Pause is ignored for a stale
  stream, so a stop while paused drains.
- A pause request is `{gen, on}` and is honoured only while `gen` matches the current stream. A
  new stream therefore can never start paused, and nobody needs to clear the flag. Even so, every
  exit from PLAY in the machine emits `PAUSE(0)` (invariant I5).
- The audio task alone writes `{gen, out_ms}` — the samples actually handed to the codec for that
  stream — and the engine reads them to classify. `stream_begin` is accepted (and resets them)
  only once the previous stream has been classified (§7.3.1), so nothing resets what is still
  being judged.
- `ring.c`'s cursors gain `__atomic` release on publish and acquire on read, in the `stream.c`
  extraction.

**The capture lease** is a `uint32_t` acquired by CAS 0 → {1 engine, 2 talk}.
- `capture_open()` refuses while it is held.
- Engine: acquired before the send is posted; released when the send ends, and by the poster if
  the post fails.
- Talk: acquired at `talk_send`; released when the upload returns, before the reply plays, so the
  follow-up can open.

### 7.5 Every engine phase has a deadline

| Phase | Bound |
|---|---|
| POLLING | `JPANEL_HTTP_TIMEOUT_MS`; cancelled when a command is queued |
| FETCHING | the head within `JPANEL_HTTP_TIMEOUT_MS`; the speaker wait `STREAM_WAIT_MAX_MS` |
| SOUNDING | **producer stall**: no bytes for `LINK_STALL_MS` → cut. **Consumer stall**: no output progress for 3 s while not paused → discard. **Pause**: `AUDIO_PAUSE_MAX_MS` → auto-resume (kept). |
| ACKING | 5 s per attempt |
| SENDING | `SEND_ATTEMPTS × JPANEL_HTTP_TIMEOUT_MS` |

The UI backstops these with its own deadlines (§8.4), so even an engine fault cannot strand a mode.

### 7.6 Network cancel

- **`link_types.h`** — split out of `link.h`, which cannot build on the host — gains
  `bool (*cancelled)(void *ctx)`.
- **WebSocket path:** `ws_request` polls it in the head wait and in both body loops, every
  `LINK_STEP_MS` (250 ms), then calls `wsp_cancel`. That decision function,
  `wsp_wait_step(now, deadline, cancelled) → WAIT|CANCEL|TIMEOUT`, moves into the host-built
  `wsproto.c`, so the WebSocket cancel is tested on the real code.
- **HTTPS fallback:** checked in `http_request`'s read loop and before `fetch_headers`. The head
  phase of `esp_http_client_perform`, `LINK_UP_WAIT_MS` (8 s) and the `s_http_lock` wait cannot
  be interrupted. They are bounded by their timeouts, and the UI's deadlines turn them into an
  audible "didn't go".
- **URL sizes** for `/next?id=<36>&at=N`: `rel[96]`, `path[112]`. The `/waiting` buffer grows to
  1536 bytes.

### 7.7 Writers

| State | Writer |
|---|---|
| `s_stop_gen`, press-cue mailbox, silence flag, pause request, command queue (send) | render |
| poll-soon flag | any task (an atomic store) |
| snapshot, run list, ack slot, ring write cursor, stream `gen` | engine |
| `s_play`, ring read cursor and reset, `{gen, out_ms, stale}` | audio |
| reply mailbox | talk (post) / audio (accept) |
| capture lease | CAS by the acquirer; released by the holder |

**Telemetry:** `ack_owed`, `ack_retries`, `returned`, `lease_refusals`, `snapshot_retries`,
`post_failed`, `jpanel_stack_free`, `sm_violations`.

**F2 changes no intended behaviour.**

- What disappears are defects: A1, A2, A4, A5, A7, A8, A9, the A3 mapping, and the lost pet reply.
- Every policy change waits for F3: the hand-back, id selection, cue rules, and the per-mode
  targets. F2 keeps acknowledge-on-stop.
- The old `display.c` reads the snapshot through thin wrappers in `jpanel.c`:
  - `jpanel_state()` is derived from the phase plus unseen outcome seqs;
  - `jpanel_clear()` marks the outcome as seen;
  - `jpanel_running`, `jpanel_fetching` and `jpanel_in_from` read `cur`.
- The wrappers keep refusing a play while a send holds the lease, so the old UI never draws
  controls over an upload.
- `capture_open()` returning false is handled in the old `start_listening` and
  `start_send_recording` (as "busy").
- F3 deletes the wrappers.

---

## 8. The panel state machine (F3)

### 8.1 Shape

`firmware/main/panel_sm.{c,h}` replaces `ui.{c,h}`. It is pure and host-built.

- **The step:** `sm_step(sm, &in, &out)` runs **once per frame**, on the device and in the tests
  alike: sample → step → execute → draw `out.view`.
- **`in`**, sampled before the step:
  - `now`;
  - the engine snapshot;
  - `post_failed`;
  - touch edges with their points, and whether the down point is on the pet (`TALK_MARGIN_PX`);
  - `gest_taps`, a swipe;
  - the voice command, the button edges;
  - `reply_sounding`, `hearing`, `capture_ms`, `leased`, `net`, the sleep stage;
  - `calibrating`.
- **`out`** is an ordered effect list plus `sm_view_t`.
  - `in` and `out` are static in `display.c`, not on the stack.
  - `out` carries the telemetry fields the machine now owns: dashes and swipes/sel.
- **`display.c` keeps:** sampling; the executor, keeping the `sound` gate on every cue; drawing
  `sm_view_t`; the sleep stages; and calibration.
- **Pet-side effects are pass-through** and keep today's guards: the poke (label hit, zones,
  variants, colour, deferral to a pet reply), and the `VOCAB_FORM/COLOUR/ACTION` commands.
- **Calibration:**
  - its **entry** is an input: the machine leaves any mode for PET, emitting `CAPTURE_DROP`,
    `PAUSE(0)` and `STOP`;
  - during calibration the step only applies deadlines;
  - on exit there is nothing to resync, because the engine state is a level.
- **`make check_no_logic_in_display`** (in `make test`) fails if `display.c` declares any static
  — file-scope **or function-local** — whose name, with the `s_` stripped, matches a field of
  `sm_t`. The field list is extracted from `panel_sm.h` by awk. It also fails if `display.c`
  calls any `jpanel_*` but `jpanel_snapshot`, `jpanel_post` or `jpanel_poll_soon`.

### 8.2 State

```
sm_t {
  mode: PET | GRID | LISTEN | THINK | RECORD | PLAY | PAIR,  since
  play: { seq, phase: PENDING|WAITING|ON, paused, pause_wanted, run_pos }
  pair: { since, id, from, from_dad }        // FRESH while !due(now, since + REPEAT_MS)
  rec:  { to, heard, hush, deliberate, started_by_down }
  talk: { voice, heard, hush, lead, follow_armed, follow_turns, dash... }
  grid_until
  send: { seq, deadline }                    // sending iff seq > send_outcome.seq
  standby
  notice: { first_visible }
  sel_id, sel_index (old-box fallback), seen_ids[16]
  down: { at, x, y, on_pet, target, swiped }
}
```

**Invariants.** They are asserted after every host step, and counted on the device: a violation
recovers to PET with `CAPTURE_DROP`, `PAUSE(0)` and `STOP`.

- **I1** One mode. The microphone is open ⇔ the mode is LISTEN or RECORD.
- **I2** The top-left face is:
  - in PET, the selected waiting sender, which is what a press there plays;
  - in PLAY, `cur.from` — the message playing, or the one being fetched;
  - in PAIR, the last sender, which is what again/reply addresses.
- **I3** The drawn controls are exactly the current mode's targets.
- **I4** Every press answers with a cue or a view change, except the listed claimed-and-ignored
  presses.
- **I5** `paused` ⇒ PLAY with the engine SOUNDING. Every exit from PLAY emits `PAUSE(0)` before
  `STOP`.
- **I6** Every non-rest mode has a deadline (§8.4). PET and PAIR are the rest modes; PAIR never
  expires, by the owner's decision.
- **I7** All time goes through `due(now, t) = (int32_t)(now - t) >= 0`.

### 8.3 Targets and transitions

**Global — every mode, checked first.**

- **Button short press:**
  - from standby → wake;
  - otherwise → `CUE_STOP`, `CAPTURE_DROP`, `PAUSE(0)`, `STOP`, standby, PET.
- **Button hold** → deep sleep (as today).
- **The wake phrase or "tell sister/dad":**
  - outside PET/PAIR → refused (`CUE_STOP`, queued);
  - in PET/PAIR → through the **microphone gate** below.
- **"stop":** as today's `VOCAB_STOP`, in LISTEN and THINK. It is muted in RECORD (see the
  corrections to the review).
- **Dark wake** → PET, the pair cleared, the notice restarted big. This is JP's rule, extended to
  the pair.

**The microphone gate.** Requires the mode to be PET, PAIR or GRID, `!leased`,
`!reply_sounding` and `net != BUSY`. A refusal is `CUE_STOP` (the queued class, so it sounds even
behind a reply).

| Mode | Targets, in priority order | Hold | Deadline |
|---|---|---|---|
| PET | NOTICE (the big box, or the badge quadrant) · PET | down on PET, on the pet, no gesture taps, not swiped → GRID | — |
| GRID | the four cells · swallow | — | `SENDTO_MS` |
| LISTEN (voice) / RECORD | CROSS quadrant · TICK quadrant · swallow | — | the lead, hush and cap; `TALK_TIMEOUT_MS` |
| THINK | PET (flinch only) | — | `TALK_TIMEOUT_MS` |
| PLAY | EXIT (after `EXIT_GRACE_MS`) · FACE (if `count > 1`) · PAUSE quadrant · REPLY quadrant · swallow | — | per phase (§8.4) |
| PAIR, fresh | EXIT · AGAIN quadrant · REPLY quadrant · PET | — | — |
| PAIR, standing | EXIT · AGAIN disc reach · REPLY disc reach · PET | as PET → GRID | — |

The **standing** pair's discs are `CONFIRM_HIT_R` (82) around (92, `confirm_cy`) and (276,
`confirm_cy`). The pet centre (184, 224) lies ≈ 171 px from either disc when the panel is upright
and ≈ 139 px in a quarter turn, so it is PET.

**PET**

- **Notice press:**
  - if sending → `CUE_STOP` (refused); the notice stays;
  - otherwise → `CUE_PLAY`, then PLAY{PENDING, `sel_id`}.
- **Swipe** → moves the selection by direction, clamped, with `CUE_HEARD` (0.3.37).

**RECORD**

- **Tick, or hush, or the cap:**
  - if she was heard → the `SEND` effect, `CUE_HEARD`, PET;
  - otherwise → drop, `CUE_OOPS` (today's tick-into-silence, kept).
- **Cross** → drop, `CUE_STOP`, PET.
- **Lead silence** → drop, plus `CUE_STOP` if the recording was deliberate (B8).
- **A swipe** in the same press that opened the recording from PLAY's REPLY → drop the recording
  silently, apply the swipe, and go to PET. A drag is not a reply; today the same swipe cancels
  `PEND_REPLY`.

**LISTEN / THINK** — today's behaviour, ported, with one change: the follow-up does not open if
a press is pending or an unseen message arrived during the turn.

**PLAY.** Phases: PENDING (waiting for the cue and any pet reply), WAITING (posted, the engine
has not started `seq`), ON (the engine's `cur.seq == seq`).

- **PENDING:**
  - when `!reply_sounding` → post PLAY{seq, id, gen} → WAITING;
  - if `post_failed == seq` → `CUE_OOPS`, PET.
- **ON** — derived every frame from the snapshot:
  - the controls draw the paused or playing state;
  - `next` is shown in the gap between messages.
- **Finished** (`done_seq >= seq`, with `phase ∉ {FETCHING, SOUNDING, ACKING}` for this run):
  - outcome heard, stopped, or played with ack pending, **and** `waiting.count == 0` → PAIR;
  - outcome failed, short or empty → `CUE_OOPS`, PET;
  - otherwise → PET;
  - every exit emits `PAUSE(0)`.
- **EXIT** → `PAUSE(0)`, `STOP`, `CUE_STOP`, then as today: PAIR when nothing waits, otherwise
  PET. (The one-press exit is §13 Q2.)
- **FACE** → `PAUSE(0)`, `STOP`, `CUE_HEARD`, selection to the next id cyclically, then PET
  (0.3.37: "stop what is sounding, drop a deferred play"). **Swipe** → the same, by direction,
  clamped.
  - The cycle and the swipe run over the waiting list **plus `cur`** while it plays. Next after
    the playing message is the first waiting entry after it in run order, so the two-message case
    works and nothing is skipped.
  - From F3, a stop under `HEARD_MIN_MS` returns the message to the waiting list in its old place.
- **PAUSE** — while ON, toggles with `CUE_PAUSE`/`CUE_RESUME`. Before ON it sets `pause_wanted`,
  applied when ON begins.
- **REPLY** → `PAUSE(0)`, `STOP`, `CUE_REPLY`, then the microphone gate → RECORD{to = `cur`}.
  There is no pending reply: the stop silences the speaker within a chunk. If the gate refuses →
  `CUE_STOP`, PET.

**PAIR**

- **AGAIN** → `CUE_PLAY`, PLAY{PENDING, replay id}.
  - If the replay fails → `CUE_OOPS`, **back to PAIR** (as today: the pair stays).
- **REPLY** → `CUE_REPLY`, then the microphone gate → RECORD.
- **EXIT** → `CUE_STOP`, PET.
- **Hold while standing** → GRID.
- **An unseen id in `waiting`** → PET; the notice takes the quadrant (B3).

### 8.4 Deadlines (I6)

| Mode / phase | Deadline | On expiry |
|---|---|---|
| PLAY PENDING | `PENDING_MS = PLAY_BUF_MS + 2000` (`_Static_assert`) | `CUE_OOPS`, PET (EXIT stays live, so the window can be left) |
| PLAY WAITING | `5 s (ack) + JPANEL_HTTP_TIMEOUT_MS + 2 s`, from post | `STOP`, `CUE_OOPS`, PET |
| PLAY ON | the engine's SOUNDING bounds (§7.5), plus a backstop: `phase_since` older than `AUDIO_PAUSE_MAX_MS + LINK_STALL_MS + 5 s` with no progress | `STOP`, `CUE_OOPS`, PET |
| sending | `SEND_ATTEMPTS × JPANEL_HTTP_TIMEOUT_MS + 5 s` | treated as FAILED (`CUE_OOPS`) |
| GRID, LISTEN, RECORD, THINK | today's constants | today's exits |

### 8.5 Selection, "new", cues, sleep

**Selection.**
- `sel_id` follows a message by id. When the selected message leaves the list, the selection
  moves to the next one in order.
- An old box sends no ids; the machine falls back to `sel_index` and today's rules.

**"New".**
- New means an id not in `seen_ids` (the last 16). It drives `CUE_MESSAGE`, pair displacement,
  the notice clock, and the sleep stage's `arrived` wake.
- A returned message, or one whose acknowledgement is owed, therefore never chimes, never wakes
  a panel in standby, and never resets the selection (E2.11).
- An old box falls back to a count rise.

**Cues.** The machine tags each cue with a class: press (mailbox) or queued (§7.4). Refusals,
outcomes and the new-message chime are queued; acknowledgements of presses are press cues.
`test_every_cue_has_a_class` enforces the tagging.

**Sleep.**
- `sm_busy()` is true in every mode except PET and PAIR, while sending, and while the engine is
  not IDLE — as broad as today's `used` test.
- The `arrived` wake reads the machine's unseen-id output.

### 8.6 Tests (`firmware/host/sm_tests.c` → `run_sm_tests`)

1. **Port every scenario in `ui_tests.c`** to `sm_step`, keeping each one's comment with the
   owner's words.
2. **Rewrite exactly these checks.** Each one asserts a defect, and the F3 PR names each change:
   - `test_the_controls_are_pressable_for_as_long_as_they_are_drawn` and
     `test_every_playback_control_answers_the_finger` — the `PEND_REPLY` checks, because reply
     opens the recording immediately;
   - `test_reply_records_to_whoever_just_spoke` — the deferred path is removed;
   - `test_a_long_press_on_a_menu_is_just_a_press` — the pair case splits into fresh (still "just
     a press") and standing (`GRID` from the pet centre);
   - `test_the_pair_waits_for_a_finger_not_a_clock` — adds that a new message displaces it (B3);
   - `test_the_notice_shrinks_to_a_badge_and_a_second_one_does_not_restore_it` — the clock starts
     at first visible;
   - `test_the_queue_count_only_sounds_on_the_way_up` — rises become unseen ids;
   - `test_what_the_box_said_about_a_message_is_answered_in_sound` — outcome cues are queued, not
     dropped.

   Unchanged and still passing:
   - `test_pressing_the_face_cycles_through_the_waiting_messages`;
   - `test_a_swipe_stops_the_message_it_moves_off`;
   - `test_a_tick_pressed_into_silence_refuses_to_send`;
   - `test_the_face_follows_the_message_that_is_playing`;
   - `test_the_top_right_corner_ends_a_message_while_it_plays`;
   - `test_a_short_button_press_darkens_blanks_and_mutes`, extended to every mode.
3. **Journey tests** named in §3 and §9, plus `test_hold_at_pet_centre_*` with a badge, in a
   fresh pair, in a standing pair, and during PLAY.
4. **`test_storm`.** A seeded run of 300k frames of random inputs, with the engine snapshot
   **dropped** (the old copy re-read), **delayed** (an older copy) and **duplicated**. Touches
   are dropped and repeated, clocks jump (including the u32 wrap), and calibration is entered at
   random. Every frame asserts I1–I7; liveness requires each non-rest mode to end by its deadline.
5. **End to end.** `jpanel_tests` can also run `sm_step` against the real `jpanel_core` and
   `stream` on fakes, so A1, A6 and B1 are proven across both halves.

### 8.7 Landing and rollback

F3 replaces the old logic in one wave. A shadow run of both machines was rejected: the old
path's known bugs make its divergences mostly intended, which turns parity into noise, and a
shadow doubles state on a stack-limited render task.

**Protection in the field:**
- the host gate in §8.6;
- the `sm_violations` count, with recovery;
- the OTA rollback gate for crashes;
- an owner on-glass checklist in the F3 PR.

**Rollback** is a source revert PR: revert F3 (and F4 too, if it has landed — F4 builds on F3),
bump `version.txt`, and commit the rebuilt `dist/`. `firmware.yml` byte-compares `dist/` with a
fresh build, so re-committing an old image is not possible. The owner applies it with Ops →
Update; the panels then OTA because the version differs. No terminal is involved.

---

## 9. Per-finding detail

Where this section and §7/§8 overlap, §7/§8 hold the design; this section adds cause and tests.

- **A1.**
  - **Cause:** `fetch_head` reset `s_stopped`/`s_run` after `stream_begin_waiting`; the exit's
    `CUE_STOP` made the wait succeed late; `audio_stream_begin` cleared pause.
  - **Fix:** §7.1, §7.4, §7.6, and `pause_wanted`.
  - **Tests:**
    - `test_stop_at_every_interleave_point`, at these points: queued, speaker wait, head, after
      claim, per chunk, paused, drain, pre-ack, chain gap;
    - `test_stop_with_no_stream_then_play_starts`;
    - `test_stop_while_paused_drains`;
    - `test_stop_during_drain_frees_speaker`;
    - `test_stop_during_5s_head_delay_cancels_request` (against `wsproto.c`);
    - `test_next_queued_during_drain_classifies_previous_first`;
    - `test_pause_before_first_byte_holds`.
- **A2.**
  - **Cause:** `!p.began` exits left `s_run` set.
  - **Fix:** the engine publishes a phase, so the UI cannot be stranded; each phase has a
    deadline.
  - **Test:** `test_chain_fetch_failure_returns_to_idle` over {link, 500, 204, speaker busy}.
- **A3.**
  - **Cause:** the 35 s capture against the box's 30 s raw check, made before trimming → 413.
  - **Fix (W1):** the raw ceiling becomes `PANEL_AUDIO_MAX` (`endpoint.py`, 35 s).
  - **Fix (F2):** 400 → `EMPTY` ("dropped"), 409 → `NOBODY`, 413 → `FAILED("too-long")`.
  - **Tests:** exactly the cap → 200; cap + 1 → 413; the caps are pinned across `audio.c`,
    `endpoint.py`, `panel_ws.py` and `voiceMessage.ts`; `test_old_firmware_contract_send_35s`.
- **A4.**
  - **Cause:** `do_played` fires once; the chain then re-fetches the head; the local list shifts
    by position.
  - **Fix:** §7.3; `fetch_head` removes by id; `/waiting` ids (W3).
  - **Tests:**
    - `test_lost_ack_is_retried_and_never_replays`;
    - `test_unreachable_ack_returns_to_idle_and_notice`;
    - `test_owed_played_id_filtered_from_waiting`;
    - backend `test_waiting_queue_carries_ids`, `test_next_by_id_serves_that_row_or_204`,
      `test_waiting_body_fits_0_3_39_buffer`.
- **A5.**
  - **Cause:** `start_listening` and the follow-up open the capture during an upload.
  - **Fix:** the lease (§7.4).
  - **Tests:** `test_capture_open_refused_while_leased`, `test_lease_released_on_post_failure`,
    `test_upload_bytes_unchanged_under_listen_attempts`.
- **A6.**
  - **Cause:** a stop acknowledged regardless of how long the message sounded; "next" ran against
    a count missing the playing message; the selection reset on every count change.
  - **Fix:**
    - F3's hand-back: heard iff `out_ms ≥ min(HEARD_MIN_MS, 0.8 × duration)`.
    - `/returned {id, delivery}`, where `delivery` comes from `X-Jpanel-Delivery` on `/next`:
      `UPDATE … SET deliveries = deliveries-1, returns = returns+1 WHERE id=:id AND played_at IS
      NULL AND deliveries = :delivery AND returns < 5`. A retry after a lost 204 matches nothing,
      which makes it idempotent, and `returns` keeps 0209's bound.
    - The cycle runs over waiting plus `cur` (§8.3).
  - **Tests:**
    - `test_stop_in_preroll_returns_not_acks`;
    - `test_stop_after_threshold_acks_with_heard_ms`;
    - `test_returned_is_idempotent_and_bounded`;
    - `test_returned_refunds_only_own_unplayed` (integration, sibling case);
    - `test_face_cycles_without_skipping` (2, 3 and 8 messages);
    - `test_run_never_replays_a_returned_message` (`jpanel_tests`).
- **A7, A8, A9.**
  - **Fix:** §7.2, §7.1, §7.4.
  - **Tests:**
    - `test_snapshot_is_never_torn` — mid-write the reader fails and keeps its previous copy; at
      quiet points it succeeds;
    - `test_send_outcome_survives_concurrent_chain`;
    - `test_only_producer_and_consumer_touch_cursors`.
- **B0:** §8.
- **B1.**
  - **Cause:** `PEND_REPLY` and the PLAYING branch re-armed the pair in one pass; PAIR outranked
    CONFIRM.
  - **Fix:** per-mode targets; reply opens RECORD immediately.
  - **Test:** `test_reply_during_playback_opens_a_working_recording`.
- **B2.**
  - **Cause:** the hold was gated by `!menu_up`. With the pair up, (184, 224) falls in the reply
    quadrant.
  - **Fix:** the hold rule per mode, and FRESH/STANDING pairs.
  - **Tests:** `test_hold_at_pet_centre_*` (×4).
- **B3.**
  - **Fix:** PAIR only while `count == 0`; an unseen id ends PAIR. This settles JP's open
    "face collision".
  - **Test:** `test_quadrant_face_matches_press`.
- **B4.**
  - **Fix:** §7.4 priorities; the notice clock starts at `notice.first_visible`.
  - **Tests:**
    - `stream_tests: test_cue_priorities` — a reply waits for a cue and is never refused; queued
      cues wait for a stream and for a recording;
    - `sm_tests: test_message_during_pet_reply_gets_chime_and_big_box`;
    - `test_heard_then_oops_both_sound`.
- **B5.**
  - **Fix:** sending is a level; the notice stays; a press on it is refused audibly. F4 draws the
    overlay.
  - **Test:** `test_notice_shown_and_refused_during_send`.
- **B8.**
  - **Fix:** a deliberate recording dropped for silence → `CUE_STOP`; the microphone gate's
    refusal is queued `CUE_STOP` from every path.
  - **Tests:** `test_silent_recording_says_dropped`, `test_every_mic_refusal_sounds`.
- **B10.**
  - **Fix:** §8.4 `PENDING_MS`; the follow-up yields (§8.3); `due()` everywhere; "stop stop" kept
    (see the corrections in §3).
  - **Tests:** `test_press_behind_pet_reply_is_served`, `test_follow_up_yields_to_pending_press`,
    `test_deadlines_survive_u32_wrap`.

**B-UX**

| Gap | Decision | Wave |
|---|---|---|
| X takes two presses | **Unchanged by default**; §13 Q2 | — |
| Every failure sounds like OOPS | SENT / NOBODY (new `CUE_NOBODY`) / EMPTY (`CUE_STOP`) / FAILED (`CUE_OOPS`) | F2 mapping; F4 contour (GUI gate, WebAudio mock) |
| No sending feedback | Sending overlay; `CUE_SENT` contour | F4 |
| Text instructions for pre-readers | Icons and pips from the chosen mock; names stay for the adult | F4 |
| Badge has no count or animation | Pips and a pulse; **no reminder sound** (JP: nagging) | F4 |
| "1/2" counts the remaining queue | `run_pos / (run_pos + remaining)` | F3 `test_run_position_counts_the_playing_message` |
| A swipe during playback stops | **Kept** (0.3.37) | — |
| Arrivals mid-turn miss the big box | Notice clock (B4) | F3 |
| The pair blocks, survives sleep | STANDING; cleared on a dark wake | F3 `test_pair_cleared_on_dark_wake_not_dim` |
| No hint for stopping a recording | Familiar tick/cross glyphs plus a hush ring | F4 |

**C findings**

- **C1** — §11.
- **C2.**
  - **Fix:** `send_text` validates against the roster **before** TTS (404). The PWA refetches
    when no thread matches, and keeps the draft on 404. W2's subject grouping removes ghost chips.
  - **Tests:** `test_send_text_unknown_panel_404_before_tts` (the TTS fake is never called);
    vitest.
- **C3.**
  - **Fix:** `POST /messages/{id}/retry` (unplayed and at the cap → `deliveries = 0, returns =
    0`, nudge). `DELETE /messages/{id}` (owner-sent or undelivered rows only; releases the blob
    through `blob_referenced`, as `sweep.py` does). Both reject a non-uuid id with 404.
  - **Tests:** `test_retry_resets_deliveries`, `test_retry_refused_when_deliverable`,
    `test_discard_only_owner_or_undelivered`, `test_discard_releases_blob`.
- **C4.**
  - **Fix:** W3 columns `heard_ms` and `offered_at`. `/waiting` stamps `offered_at` once per
    message. The PWA copy follows E3.3; an empty room cannot be detected, so it says "Played".
  - **Tests:** `test_played_records_heard_ms`, `test_old_firmware_played_body_leaves_heard_ms_null`,
    `test_waiting_stamps_offered_once`, `test_offered_at_rls_own_rows_only` (sibling case).
- **C5.**
  - **Fix:** `between` rows go in both threads, and the unplayed count is unchanged. P1 shows the
    status on out and between rows.
  - **Test:** `test_between_message_in_both_threads`.
- **C6.**
  - **Fix:** the limit applies per thread (`row_number()` partitioned by thread). The response is
    unioned with every unplayed panel→owner row, and `?before=&device=` pages older history.
  - **Tests:** `test_every_unplayed_owner_row_is_returned`,
    `test_per_thread_limit_keeps_quiet_panel_visible`.
- **C7.**
  - **Fix:** single-range 206, `Accept-Ranges`, 416. A uuid guard on the audio, read and C3
    routes.
  - **Tests:** `test_audio_range_206`, `test_audio_range_416`, `test_non_uuid_is_404`.
- **C8.**
  - **Fix:** `errorDetail` joins `detail[].msg`; `maxLength={600}` (W1). The counter and hint
    land in P1. TTS timeout 60 s. The 600-character cap is kept (§13 Q5).
- **C9** — `kept_for` per recipient.
- **C10** — owner audio over the cap → 413.
- **C11** — a sequence ref and an epoch ref; a stale refresh is discarded; vitest.

**C-UX**

| Gap | Decision | Wave |
|---|---|---|
| No notification to Dad | Launcher badge (`GET /jpanel/summary`); push is §13 Q4 | W3, P1 |
| "Not heard yet" is ambiguous | `offered_at` / `deliveries` statuses | P1 |
| Composer hint and limit | "Waits on her panel until she taps it", plus a counter | P1 |
| Read at 60% visibility | Visible for 1.5 s **and** `document.hasFocus()` | P1 |
| Reading starts the 30-day sweep | Policy kept; the footer says so | P1 |
| Renames rewrite past names | **Not fixed.** Renames are mostly corrections, and snapshots would freeze typos (§13) | — |
| Two clears | Kept; the confirm text names what remains | P1 |

---

## 10. Host foundations (F1, behaviour-preserving)

1. **`link_types.h`**; the wait-decision function moves into `wsproto.c` (§7.6).
2. **`stream.c`**: the ring, stream and capture bookkeeping, extracted from `audio.c` **as they
   are today**. `audio.c` calls it.
3. **`jpanel_core.c`**, behind `jpanel_io_t`:
   - the request;
   - stream ops (`stream.c` on the host);
   - SHA-256;
   - the clock and sleep;
   - `yield(point)`;
   - the snapshot publish.

   `jpanel.c` keeps the task, the queue, cJSON parsing (field names are pinned by backend
   tests), mbedtls, and the real io.
4. **`run_jpanel_tests` and `run_stream_tests`** join `make test`. Their fakes: a scripted box
   (including a 0.3.39 mode), the real `ring.c`/`stream.c`, a fake clock, a test-only
   public-domain SHA-256 (no new tool, so `dev-setup.sh` is unchanged), and yield hooks.
5. **Characterisation tests** assert 0.3.39's behaviour, **bugs included**, each named with its
   finding ID. F2 inverts them, so every bug is reproduced before it is fixed.
6. **Backend tests that grep `jpanel.c`** retarget to `jpanel_core.c` in the same PR.

The host proves logic under every interleaving the hooks name. It cannot prove dual-core memory
ordering, which is reviewed against the fences in §7 and watched through the telemetry.

## 11. Identity (W2) — no RLS change

**The problem (C1).** `DevicesRepo.reflash` keeps the subject but revokes and mints principals.
Messages are addressed to principal ids, and RLS reads `app.principal_id`. Unplayed mail
addressed to a revoked key is then never served, never swept, and invisible to the owner.

### 11.1 The fix: principal keying stays, re-flash carries the mail

Panel routes, RLS, nudge, `panel_ws`, `_waiting_count`, `/converse`'s `pet_turn` insert, rename
and revoke are all **unchanged**, because nothing about principal keying changes. Three pieces
of work instead:

1. **`reflash`** runs one extra statement in the same owner-context transaction:
   `UPDATE app.jpanel_message SET recipient_device = :new WHERE played_at IS NULL AND
   recipient_device IN (SELECT id::text FROM app.principals WHERE subject_id = :sid AND id <>
   :new)`.
   - **Only keys on the same subject.** Mail never moves across subjects, so the twins can never
     be merged.
   - **Subject selection** changes from "oldest subject by name and role" to "the subject owning
     the most recently created key, live or revoked, for this name and role". A revoke followed
     by a re-flash then lands on the subject the panel last used, not on an older empty one.
2. **The owner's views group by subject**, in owner context and read-only:
   - `messages()`, `pet_chats` and the unplayed counts join `principals` on `subject_id`. A
     principal with NULL `subject_id` is its own group.
   - A thread for a subject with a live jpet key uses that key's principal id as `device_id`, so
     `send_text`, rename and clear work unchanged.
   - A subject without one is `retired: true`, with `device_id` = the subject id.
   - `clear_history`, `clear_pet_chats` and the badge resolve the id to the subject's principals.
   - Turns from `display`-role devices are excluded from the children's threads. "Clear all"
     still removes them.
3. **Pre-0211 subjects**, where one physical panel has several subjects, are **never merged
   automatically.**
   - P1 shows them as retired threads. The owner merges one explicitly: `POST
     /jpanel/panels/{retired_subject}/merge {into: device_id}` (owner only).
   - The merge re-addresses that subject's unplayed mail to the target's live principal, and
     re-points its revoked keys' `subject_id` so the history groups with the target.
   - It **tombstones** the retired subject's `display_name` (`merged:<uuid>`), so `reflash` can
     never match it.
   - Until P1 ships, W2 hides retired threads from the picker rather than drawing them as live
     chips.

**One-time data migration (W2).** No schema change. Unplayed rows addressed to a revoked key are
re-addressed to the **same subject's** single live jpet key, where one exists; everything else is
left for the merge.

**Rollback.** None is needed: every row stays principal-keyed, and old code reads it correctly.
`alembic` runs the migration in a transaction, so if it fails the box stays on the old head. The
owner sees the failure in Ops → Update.

### 11.2 Why not the subject/RLS rewrite (round 1)

That design needed every panel-context site rewritten, a second id space, a revert revision, and
— as round 2 showed — name-based grouping that could merge two children's inboxes. Carrying the
mail at re-flash time uses the one moment the box knows both identities for certain: the subject
and the keys it revokes.

### 11.3 Tests (CLAUDE.md #3)

There are no new tables or policies; the existing RLS isolation tests stand unchanged.

- `test_reflash_carries_unplayed_mail_to_new_key` — also asserts that played rows and the
  **sibling's** rows are untouched.
- `test_reflash_after_revoke_lands_on_last_used_subject`.
- `test_owner_threads_group_by_subject_across_reflash` — history, badge, pet turns, no ghost
  chip.
- `test_retired_subject_thread_listed_and_clearable`.
- `test_merge_moves_mail_and_tombstones` — afterwards `reflash` cannot match the tombstone. A
  merge whose source subject still holds a live key is refused with 409. The target is always the
  owner's explicit choice, never inferred from a name.
- `test_identity_backfill_pg` — seeds today's shape: 13 principals, a subject with a revoked-key
  inbox, and a pre-0211 extra subject left untouched.
- `test_send_after_reflash_delivers` (integration, a real panel key).

## 12. Waves

One PR per wave (PROCESS.md). Each package runs its **whole** local gate set from its own
directory (CLAUDE.md #11):

- **backend:** `ruff check .`, `ruff format --check .`, `pyright`, `pytest`.
- **frontend:** `npm run lint`, `npm run typecheck`, `npm run test`, `npm run build`.
- **firmware:** `make -C firmware/host test`.
- **supervisor:** the full gate set (`ruff check .`, `ruff format --check .`, `pyright`,
  `pytest`), because `firmware/dist/**` triggers the supervisor job.

**Every firmware wave:**

- bumps `firmware/version.txt`;
- commits a rebuilt `dist/` (`scripts/firmware-setup.sh` once, `idf.py build`,
  `scripts/firmware-dist.sh`);
- keeps the bootloader byte-identical.

**Every wave:**

- runs `scripts/docs-freshness.sh`;
- updates this plan's markers, JP §3b, the firmware READMEs and the ROADMAP/plans README rows.

**Constraints that hold throughout:**

- no new dependency or setup step — if one becomes necessary, that PR updates `dev-setup.sh`
  (#8);
- no shell-only operator step: Ops → Update, OTA, the PWA and debug-API telemetry (#10).

| Wave | Contents | Depends on | CI |
|---|---|---|---|
| **W1** box/PWA, invisible | A3 box; C2 validation; C5; C6; C7; C8 (except visuals); C9; C10; C11; skew pins. **Fixes A3 for all panels already in the field.** | — | backend, frontend, docs |
| **W2** identity | §11 (reflash, owner grouping, merge route, data migration, retired chips hidden) | W1 | backend, frontend, docs |
| **W3** contract | `/waiting` ids + `offered_at`; `/next?id=` + duration + delivery headers; `/played {heard_ms}`; idempotent bounded `/returned`; C3 routes; `/summary`; columns `heard_ms`, `offered_at`, `returns` (additive) | W2 | backend, docs |
| **F1** host foundations | §10; no behaviour change | — (parallel) | firmware, backend, supervisor, docs |
| **F2** engine | §7 in full. Defects fixed, no policy changed; the old UI runs through wrappers. | F1, W3 | firmware, backend (pins), supervisor, docs |
| **F3** state machine | §8; every B fix; hand-back on; wrappers deleted | F2 | firmware, supervisor, docs |
| **F4** panel visuals | Sending overlay, pips, pulse, glyphs, hush ring, `CUE_NOBODY`, `CUE_SENT`. **GUI gate:** three interactive mocks (with WebAudio) first; the chosen one goes to `docs/mocks/jpanel-panel-messaging/` | F3 | firmware, supervisor, docs |
| **P1** PWA | C3 buttons; C4 copy; C5 status; composer counter and hint; launcher badge; read dwell; retention line; retired threads and merge; "Load earlier". **GUI gate:** three mocks first | W3, W2 | frontend, docs |

Order: W1 → W2 → W3 → F1 → F2 → F3 → F4. P1 can run any time after W3, and F1 can run in
parallel with the W waves. Each wave ships on its own, and none changes intended behaviour
before the wave that owns that change.

## 13. Risks, losses, questions

**Risks.**

- **F3 replaces the UI logic in one wave.** Mitigations: the §8.6 gate, recovery counting in
  the field, and source-revert rollback (§8.7).
- **Memory ordering** is reviewed, not tested on the host.
- **HTTPS fallback.** Cancel cannot interrupt the head, `LINK_UP_WAIT_MS` or the http lock, so a
  stop or skip in that window ends as "didn't go" after a bounded wait.
- **Update window.** The W2 data migration runs before the new API starts. The old API keeps
  working on the re-addressed rows, because they stay principal-keyed.

**What the owner loses.**

- **Push notifications** (Q4).
- **Resending a failed send.**
- **"Played" means played, not heard.** The PWA cannot know who was in the room.
- **Renames rewrite past names** in old messages.
- **A stop under 2 s puts the message back** in the queue, and the notice returns for it.
- **The notice can't be played during her own upload.**
- **Retired panels** need one explicit merge each.
- **Pet chats from display-role devices** are hidden from the children's threads.

**Questions.** Each has a default that the plan executes unless the owner overrules it.

1. **The pair goes STANDING after 10 s** (`REPEAT_MS`, his own figure). After that the pet's
   centre opens the menu, and only the drawn discs replay or reply. **Default: yes**, because
   without it E1.6 is impossible while the pair stands.
2. **Exit in one press** (stop and leave, no pair)? **Default: today's behaviour** — stop, then
   the pair, then a second press.
3. **A distinct "busy" sound** for refusals, separate from "dropped"? **Default: `CUE_STOP`, as
   today.** F4's mock offers the alternative.
4. **Web push** (VAPID, a service-worker subscription, a new dependency). **Default:
   deferred.**
5. **The typed-message cap** stays at 600 characters (about 50 s spoken). **Default: kept.**
6. **`HEARD_MIN_MS = 2 s`.** **Default: 2 s.**

## 14. Review log

**Round 1** (15 blocking, 26 non-blocking). Every item was accepted after verification. Its
outcomes that still stand:

- the consumer-owned discard;
- correct seqlock fences;
- `stream.c` on the host;
- network cancel;
- the single acknowledgement slot;
- one step per frame;
- the hold vs pair design;
- audible deadlines;
- cue classes;
- "new" keyed by id;
- per-mode targets;
- the call-site census;
- forward-only rollback;
- no new table.

Three of those (the subject migration, its revert revision, and the event queue) were replaced in
round 2 by simpler mechanisms.

**Round 2** (17 blocking, 33 non-blocking). Every claim was checked against the code, and all
were accepted. Each blocking item and its resolution:

| Item | Resolution |
|---|---|
| FW-1: an outcome was lost, or charged to the wrong message, when a new fetch arrived during the drain | The engine is serial: it classifies and acknowledges the previous stream before dequeuing, and `stream_begin` refuses until then. The audio task alone writes `{gen, out_ms}` (§7.3, §7.4). |
| FW-2: pause vs discard wedged the ring | gen is checked before pause; pause requests are gen-tagged; every PLAY exit emits `PAUSE(0)`; F2 keeps today's semantics through the wrappers. |
| FW-3: the order of `PLAY_ENDED` vs the acknowledgement was undefined | Events removed. The snapshot shows ACKING with `next`, and the UI draws "between messages" (§7.1). |
| FW-4: SOUNDING and `sending` had no liveness bound; lost events | Level snapshot; deadlines for every engine phase and UI mode; the storm test drops, delays and duplicates input (§7.5, §8.4, §8.6). |
| FW-5: shared `s_play` writers; a lost reply | The audio task is the only owner of the speaker; replies go through a mailbox and wait for a cue (§7.4). |
| FW-6: the chain could replay a returned message | The run list is chained by id, excluding played or returned ids (§7.3). |
| UX-1: the transition list dropped behaviour (button, swipe, auto-resume, hold guards, voice outside PET) | A global block; the PET swipe; the pause deadline; `gest_taps` and `on_pet` inputs (§8.3). |
| UX-2: face and swipe semantics, and the top-left face, were overturned silently; an incomplete rewrite list | Owner semantics restored (cycle or select, then stop); I2 per mode; `cur` seeded before the request; swipe-after-reply defined; the full rewrite list (§8.3, §8.6). |
| UX-3: pause stranded a message | I5, gen-tagged pause. |
| UX-4: `sending` stuck; calibration hand-off | Level `send_outcome`; `post_failed`; a sending deadline; calibration entry is an input (§8.1). |
| UX-5: the F2 hand-back regression; the sleep `arrived` wake | Hand-back only from F3; `arrived` = an unseen id; `sm_busy` covers PLAY, sending and an engine that is not idle (§8.5). |
| UX-6: the `s_play` race and refusal cues | The speaker owner; refusals are queued cues (§7.4, §8.3). |
| UX-7: rollback was impossible as written | Source revert, version bump and rebuilt dist; F4 couples to F3 (§8.7). |
| BE-1: name grouping could merge twins | Removed: no automatic cross-subject grouping; an explicit owner merge (§11). |
| BE-2: non-canonical subjects stranded mail | No re-pointing; reflash selects the last-used subject; tombstone on merge. |
| BE-3: rename/revoke ids; socket disconnect | Principal keying is unchanged, so both keep working as today. |
| BE-4: the forward revert was wrong | No revert is needed; the rows stay principal-keyed. |

**Round-2 non-blocking items** — all accepted. Each lands as follows:

- **Firmware engine:**
  - polls are a flag rather than a command, and the post failure surfaces as `post_failed`;
  - the WAITING deadline is sized from the real serial path, with the inline acknowledgement
    capped at 5 s;
  - the 40 ms chunk and the ~90 ms DMA queue are stated;
  - HTTPS cancel is checked in the read loop and before the headers, and the waits it cannot
    interrupt are named;
  - the wait-decision function moves into `wsproto.c`;
  - `ring.c` gains atomics;
  - the lease is released before the outcome is published;
  - F2's wrappers handle the refusals;
  - the step's structs are static, not on the stack;
  - the seqlock copy's formal race is stated.
- **UX:**
  - `sending_hides` is removed;
  - seq matching is by level;
  - the run order is engine-owned and keyed by id;
  - a failed replay returns to PAIR;
  - the WAITING deadline is measured from the post, and covers the acknowledgement and the head;
  - `check_no_logic` strips `s_` and covers function-local statics;
  - recovery emits its effects;
  - telemetry sources move into the machine;
  - pet and VOCAB effects pass through;
  - the `CUE_STOP` overload is §13 Q3;
  - the chime waits while the microphone is open;
  - the quarter-turn distance is stated.
- **Backend:**
  - NULL subjects are their own group;
  - the owner-branch and AST-guard items and the `*_principal` binding are obsolete, because
    nothing panel-side changes;
  - the update window is noted;
  - `/returned` is idempotent through the delivery number;
  - DELETE releases the blob;
  - the supervisor job is added to the CI column;
  - the questions are updated;
  - the old buffer limit is pinned.
