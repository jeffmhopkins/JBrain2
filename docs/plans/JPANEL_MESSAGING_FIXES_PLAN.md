# jpanel messaging — the fix plan after the adversarial review

> **Status:** Scheduled · **Last verified:** 2026-10-11 · **Waves:** W1◻️ W2◻️ W3◻️ F1◻️ F2◻️ F3◻️ F4◻️ P1◻️

Voice post between the twins' panels and the owner's PWA works on the happy path. It fails on
the paths a four-year-old actually takes: pressing again while a message loads, walking away
mid-message, holding the pet while a message waits, talking over a TV, and a USB re-flash.

**Where the findings came from**

- The original review raised **32 findings**. Three are duplicates (B6 = A3, B7 = A1, B9 = A6).
- Verifying them against the code turned up **two more** (A8, A9).
- Three rounds of cold review of this plan added:
  - round 1: 15 blocking + 26 non-blocking;
  - round 2: 17 blocking + 33 non-blocking;
  - round 3: 18 blocking + 30 non-blocking.

  All of them are resolved below; §14 has the log.

**The core model, which round 3 found sound**

- The engine publishes **one level snapshot**, which the UI reads every frame.
- The engine is **serial**.
- Streams are **stamped with a generation**.
- The **audio task is the only writer of the speaker**.

**What the owner settled**

- On restructuring: *"totally down for restructuring the whole UI state machine to make sure
  that everything is done correctly."* So the panel's interaction logic becomes one explicit,
  host-tested state machine (§8).
- Three product questions are answered and recorded as decisions in §4:
  - the standing pair;
  - a one-press exit during playback;
  - a distinct "busy" refusal sound.

Binding inputs: `CLAUDE.md`, `docs/DOC_LIFECYCLE.md`, `docs/reference/PROCESS.md`,
`docs/reference/DEVELOPMENT.md`, and `docs/plans/JPANEL_PLAN.md` (**JP**). The panels are
placeholder-named **Quorra** and **Tamsin**. Names are as of firmware 0.3.39. Line numbers are
omitted because they drift.

## 1. Goal

A message, to or from a child, is never:

- lost silently;
- replayed because of a fault;
- reported as heard when it was not.

Every press a pre-reader makes gets an answer she can understand without reading. Every mode,
deadline and drawn control of the panel is exercised on the host, so the code that ships is the
code that is tested.

## 2. Expectations

Each expectation is a testable behaviour, cited in §3 and in the tests.

**J1 — a child sends**

- **E1.1** The box accepts every recording a panel can capture (`CAPTURE_MAX_MS`). One byte more
  is refused, never truncated.
- **E1.2** After the tick or the hush auto-send she hears exactly one outcome:
  - "sent";
  - "nobody to send to";
  - "dropped" — the box trimmed the recording to nothing;
  - "didn't go".

  It arrives within the engine's send bound (§7.5). An outcome that arrives after the UI's
  backstop is logged and never sounded, so she never hears two.
- **E1.3** The glass shows an upload while it is in flight (an F4 visual). A message arriving
  meanwhile still gets its sound and its notice.
- **E1.4** Nothing during an upload alters the bytes being uploaded. Anything that needs the
  microphone in that window is refused with the "busy" sound.
- **E1.5** A deliberate recording left silent ends with "dropped". Every refusal, from any mode
  and by touch or voice, plays the "busy" sound.
- **E1.6** A hold on the pet's centre opens the "who?" menu when:
  - nothing is up;
  - a badge is up;
  - the again/reply pair has stood for 10 s.

  The rim, maintenance-tap and swipe guards stay as they are today.

**J2 — a child receives, plays, stops, replies**

- **E2.1** Pressing the notice plays the message whose sender the notice shows.
- **E2.2** A stop — exit, the button, a face press or a swipe — is honoured at any moment after
  the press: queued, waiting for the speaker, waiting for the head, in the preroll, sounding,
  paused, draining, or between messages of a run. The speaker goes silent within one 40 ms chunk
  plus the codec's DMA queue (~90 ms). Nothing chains on. On the WebSocket transport the request
  is cancelled within 250 ms; HTTPS limits are in §7.6.
- **E2.3** The top-left face is the sender of whatever a press there, and reply, would address.
  This holds in every phase (§8.3). A face press **cycles** the selection; a swipe moves it **by
  direction, clamped** (JP 0.3.37). Both stop the sound. No message is skipped.
- **E2.4** Heard and returned messages (from F3, on a box that sends `X-Jpanel-Delivery`):
  - A message counts as heard if it sounded for `HEARD_MIN_MS`, or for 80% of a short one.
  - A shorter stop hands it back: it stays waiting and the attempt is refunded, idempotently and
    within a bound.
  - Without that header, a stop acknowledges, as today.
  - A fetch cancelled before its head can still burn one attempt. That is bounded by the
    5-attempt cap, and C3's retry recovers it.
- **E2.5** A heard message is acknowledged exactly once through network loss. A lost
  acknowledgement never replays a message, skips one, or leaves controls over silence.
- **E2.6** Every playback ends within the engine's bounds (§7.5). A failure plays "didn't go" and
  restores the notice.
- **E2.7** Reply works only while a message is actually sounding. It opens a working recording at
  once, to the person whose face is shown, with the tick and cross live.
- **E2.8** Exit during playback stops the message and leaves, in one press, with no pair
  (owner's decision). After a message ends by itself, the pair appears, as today.
- **E2.9** A message that arrives during a conversation or a recording chimes once the microphone
  closes. Its 15 s big notice counts from when it is first visible.
- **E2.10** A press that cannot be served always ends audibly, never in silence.
- **E2.11** A message already announced is never announced as new again.
- **E2.12** A press on PAUSE or REPLY before the message actually sounds is claimed and ignored
  (JP 0.3.36, kept).

**J3 — Dad sends, reads, clears**

- **E3.1** A message to an unknown panel is refused before speech is synthesised, with a readable
  error, and the draft is kept.
- **E3.2** Every unplayed message to the owner can be rendered. Reading counts only after a
  dwell, while the page has focus.
- **E3.3** The status of a message is one of:
  - not yet seen by her panel;
  - on her panel, not played;
  - started, not finished;
  - **Played** (+ "stopped after N s");
  - couldn't be delivered.

  Twin-to-twin messages use the same statuses.
- **E3.4** An undelivered message can be retried or discarded.
- **E3.5** The kept-back count after a clear names the right child.
- **E3.6** A twin-to-twin message appears in both children's threads.
- **E3.7** A stale poll never removes a bubble that was just sent.
- **E3.8** Errors read as sentences. The composer shows its limit and its hint.
- **E3.9** The launcher tile carries a badge that matches what the screen renders.
- **E3.10** Audio plays in iOS Safari. A malformed id answers 404.

**J4 — re-flash**

- **E4.1** A re-flash or a key rotation carries unplayed mail to the new key. History, the badge
  and pet transcripts stay in one thread, with no ghost chip.
- **E4.2** A retired panel's rows stay visible and clearable, and the owner can merge them
  explicitly. Nothing is ever merged automatically.

**J5 — network loss**

- **E5.1** A message is acknowledged only if it streamed whole and sounded.
- **E5.2** Acknowledgements are retried with backoff, for a bounded time. 4xx answers other than
  408 and 429 are terminal.
- **E5.3** A failed send says so. It is not kept for resend (§5).
- **E5.4** Mixed releases keep every journey working, at worst with today's behaviour (§6).

## 3. Traceability

Verified states what reading 0.3.39 found: **✓** confirmed as reported, **✓\*** confirmed with a
correction (listed below the table).

| ID | Verified | Fix | Wave | Proving test(s) |
|---|---|---|---|---|
| A1 | ✓ | Stop generation; the audio arbiter discards; network cancel (§7) | F2 | `test_stop_at_every_interleave_point`, `stream_tests: test_stop_while_paused_drains`, `test_stop_during_drain_frees_speaker` |
| A2 | ✓ | Level snapshot; every command gets a terminal outcome; engine bounds (§7.1, §7.5) | F2 | `test_chain_fetch_failure_returns_to_idle` (×4) |
| A3 | ✓ | Box accepts the capture cap; caps pinned; `EMPTY` outcome | W1, F2 | `test_send_accepts_exactly_the_capture_cap`, `test_send_refuses_cap_plus_one`, `test_capture_caps_pinned` |
| A4 | ✓ | Serial engine; ack before the next command; chain by id (§7.3) | W3, F2 | `test_lost_ack_is_retried_and_never_replays`, `test_ack_4xx_is_terminal` |
| A5 | ✓ | Lease taken by render before the post (§7.4) | F2 | `test_lease_taken_before_post`, `test_upload_bytes_unchanged_under_listen_attempts` |
| A6 | ✓\* | Hand-back (F3) gated on the delivery header; selection over waiting + current | W3, F2, F3 | `test_stop_in_preroll_returns_not_acks`, `test_against_0_3_39_box_short_stop_acks`, `sm_tests: test_face_cycles_without_skipping` |
| A7 | ✓ | Seqlock snapshot | F2 | `test_snapshot_is_never_torn` |
| A8 *(new)* | — | `s_state` shared by two tasks → gone | F2 | `test_send_outcome_survives_concurrent_chain` |
| A9 *(new)* | — | Ring cursors written only by producer and arbiter; no reset | F2 | `stream_tests: test_cursor_writers` |
| A-tests | ✓ | Host foundations (§10) | F1 | `run_jpanel_tests`, `run_stream_tests` |
| B0 | ✓ | One state machine (§8) | F3 | `check_no_logic_in_display`, `sm_tests` |
| B1 | ✓ | Per-mode targets; reply only in ON; `PEND_REPLY` removed | F3 | `test_reply_during_playback_opens_a_working_recording` |
| B2 | ✓\* | Standing pair (owner Q1) | F3 | `test_hold_at_pet_centre_*` (×4) |
| B3 | ✓ | PAIR only while nothing waits | F3 | `test_face_function_is_drawn_and_addressed` (every phase) |
| B4 | ✓ | Arbiter priorities; notice clock starts when first visible | F2, F3 | `stream_tests: test_cue_priorities`, `sm_tests: test_message_during_pet_reply_gets_chime_and_big_box` |
| B5 | ✓ | Sending is a level; the notice stays; a press on it plays "busy" | F3, F4 | `test_notice_shown_and_refused_during_send` |
| B6 | = A3 | — | — | — |
| B7 | = A1 | — | — | — |
| B8 | ✓ | Silence → "dropped"; every refusal → `CUE_BUSY` | F3, F4 | `test_silent_recording_says_dropped`, `test_every_refusal_sounds_busy` |
| B9 | = A6 | — | — | — |
| B10 | ✓\* | Pending deadline above the reply cap; follow-up yields; `due()`; "stop stop" kept | F2, F3 | `test_press_behind_pet_reply_is_served`, `test_deadlines_survive_u32_wrap` |
| B-UX | ✓ | §9 table | F3, F4 | per row |
| C1 | ✓ | Re-address on reflash/rotate; owner grouping by subject; explicit merge (§11) | W2 | §11.3 |
| C2 | ✓ | `send_text` validates; picker grouped by subject | W1, W2 | `test_send_text_unknown_panel_404_before_tts`, vitest |
| C3 | ✓ | Retry / discard | W3, P1 | `test_retry_resets_deliveries`, `test_discard_only_owner_or_undelivered`, `test_discard_releases_blob` |
| C4 | ✓ | `heard_ms`, `offered_at`; honest copy | W3, F3, P1 | `test_played_records_heard_ms`, vitest status matrix |
| C5 | ✓ | Between rows in both threads, with status | W1, P1 | `test_between_message_in_both_threads` |
| C6 | ✓ | Per-thread limit + every unplayed-to-owner row | W1 | `test_every_unplayed_owner_row_is_returned` |
| C7 | ✓ (Range plausible) | 206 Range; uuid → 404 | W1 | `test_audio_range_206`, `test_non_uuid_is_404` |
| C8 | ✓ | `errorDetail` lists; `maxLength`; TTS timeout 60 s | W1, P1 | `client.test`, `test_send_text_tts_timeout_60s` |
| C9 | ✓ | `kept_for` per recipient | W1 | `test_clear_kept_names_recipient` |
| C10 | ✓ | Owner audio over the cap → 413 | W1 | `test_owner_audio_over_cap_is_413` |
| C11 | ✓ | Request seq + mutation epoch | W1 | vitest stale-poll test |
| C-UX | ✓ | §9 table | W3, P1 | per row |

**Corrections to the review**

- **A6.** The face needs `waiting > 1` *after* the playing message has already been decremented
  out of the count. So with exactly two messages the face is dead during the first, and the
  reported skip needs three or more.
- **B10.** The recording branch of "stop stop" is dead because commands are muted while
  recording. That mute is a deliberate rule (JP 0.3.33), so it is **kept**.
- **B2.** The pair never expiring was the owner's 0.3.25 call. It is kept; the owner chose the
  standing variant (§4).
- **C7.** The 500 is confirmed. The Range failure is plausible; the fix is cheap either way.
- **Comment drift**, fixed in the waves that touch each file:
  - `s_repeat_until` promises that a new arrival displaces the pair;
  - `ui.c` exit: "No cue";
  - `jpanel_replay`: "without asking the box";
  - JP: "Capped at 20 s";
  - `panel_ws.py` says "`MAX_MESSAGE_MS` is 30 s" where it means the panel's 35 s.

## 4. Owner decisions

**Kept as they are:**

- the down edge fires;
- a long press on a menu is a press (0.3.35);
- quadrant hits for a fresh pair and for the transport;
- the pair never expires (0.3.25), including through sleep — which also means a dark wake does
  not clear it (only the notice restarts big, per JP);
- face cycles and swipes clamp by direction (0.3.37);
- the top-left face shows the sender of the message playing;
- the button is the power control in every state (0.3.26);
- the screen-sleep rules, and polls never keep the screen awake;
- voice "stop" behaves exactly as today in every unmuted state;
- PAUSE/REPLY are dead until the sound starts (0.3.36);
- "a message that cannot be delivered is still a message", and bulk clear keeps unheard rows;
- two clears per thread;
- 30-day retention;
- sibling RLS and "the other panel" refusal;
- the free silent drop of an accidental wake;
- "press once, hear everything" — messages arriving mid-run join the run.

**Decided by the owner (2026-10-11):**

| Q | Decision |
|---|---|
| Standing pair | **Yes.** After `REPEAT_MS` (10 s) the pair is hit only by its drawn discs (`CONFIRM_HIT_R`), and a hold on the pet centre opens the menu. |
| Exit during playback | **One press**: it stops the message and leaves. There is no pair step. The pair still follows a message that ends by itself. |
| Refusal sound | **A new, distinct `CUE_BUSY`** ("busy, try later") for every refusal. `CUE_STOP` stays cancel/dropped. Designed in F4's cue mock; until F4 lands, F3 uses a placeholder contour pinned by `test_cue_busy_distinct`. |

**Changed because the old behaviour is the defect itself:**

- **Ack-on-stop.** From F3, and only when the fetch carried `X-Jpanel-Delivery`, a stop shorter
  than `HEARD_MIN_MS` of real output hands the message back with a refund. Writing `played_at`
  for a stop during the preroll is exactly the lie in C4.
- **Positional `/next?at=` → by id.** Positional addressing caused A4 and A6. A panel already
  reads its own rows, so ids are no new exposure.
- **`ui.c` plus `display.c`'s duplicate → one machine.** The owner approved the restructure.

## 5. Scope

- **Deferred:**
  - **Web push.** It needs new infrastructure and a new dependency, and JP §5 left it open.
    The launcher badge is in scope.
  - **Resending a failed send.** It would need to hold the capture lease, which holds the pet's
    microphone.
- **Out of scope:** child-speech STT.
- **Unchanged:** `endpoint_status`.

## 6. Version skew

Panels OTA from the box's `firmware/dist/`. `main.c` installs any version that **differs** from
what it runs (`strcmp`). So:

- **New box + old firmware is routine.** It lasts at least until the next manifest poll
  (~15 min), and indefinitely for a panel whose OTA refuses.
- **Old code + new firmware only follows a box rollback.** Ops → Update runs
  `git reset --hard @{u}` and then `alembic upgrade head`, so a rollback is a revert PR the owner
  applies there.
  - **A rollback reverts code and never removes a migration revision file.** Removing one would
    leave the database stamped at a revision that no longer exists.
  - W2 is data-only and W3's columns are nullable or defaulted, so old code runs against the new
    schema. `test_w3_schema_accepts_0_3_39_insert_shapes` replays the pre-W3 insert column sets.

Two rules:

- **Box changes are additive.** cJSON ignores unknown fields, headers are matched by name, and no
  status 0.3.39 acts on changes meaning.
- **Firmware degrades to today's path** when a field, header or route is absent.

| Change | New box + 0.3.39 | Old code + new firmware |
|---|---|---|
| `/send` raw ceiling = capture cap | 35 s recordings stop failing — **fixes A3 for every panel in the field** | 413 → `FAILED("too-long")` |
| `/waiting` `queue[].id` | Ignored. The worst case (14-char names + ids) is ~901 B ≤ 0.3.39's 1024 B buffer, pinned by `test_waiting_body_fits_0_3_39_buffer` (W3) | Positional `?at=`; "new" by a rise in the count |
| `/next?id=`, `X-Jpanel-Duration-Ms`, `X-Jpanel-Delivery` | Not sent | `at` honoured; **no delivery header → ack on stop, as today** (`test_against_0_3_39_box_short_stop_acks`) |
| `/played {id, heard_ms}` | `heard_ms` NULL | Extra field ignored |
| `/returned` (new; tunnelled via `_ALLOWED`) | Never called | Never called, because there is no delivery header |
| Re-address, owner grouping, `offered_at` | Box-side | Data stays principal-keyed |

---

## 7. The engine (F2)

### 7.1 Two arrows

**render → engine: a command queue** (depth 4). Each command is
`{kind, seq, gen, expires_at, id[48], at, to}`.

- Render is the only poster.
- If the post fails, the next frame sees `in.post_failed = seq`.
- "Poll soon" is an atomic flag, not a command. Render and `main.c`'s nudge both set it.

**engine → render: one seqlock snapshot**, written only by the engine and copied by render once
per frame. It is a **level**: nothing can be dropped or reordered, and calibration simply skips
frames.

```
eng_view {
  phase: IDLE|POLLING|FETCHING|SOUNDING|ACKING|SENDING, phase_since
  done_seq                                      // every command ends here, exactly once
  cur  { seq, stream_id, id, from[32], from_dad, run }
  next { id, from[32], from_dad }
  progress { out_ms, last_progress_ms, paused }
  play_outcome { seq, id, kind, reason }        // heard|heard_unacked|stopped_heard|returned
                                                // |short|empty|cancelled|failed
  send_outcome { seq, kind SENT|NOBODY|EMPTY|FAILED, reason }   // reason incl. "expired"
  waiting { count, known, q[8]{ id, from, from_dad }, sibling }
  ack_owed
}
```

- **"Done"** is `seq_ge(done_seq, my_seq)`, where `seq_ge(a,b) = (int32_t)(a-b) >= 0`.
- **The run's next sender.** Before a run's next request goes out, the engine seeds `cur` from
  the waiting entry, as `seed_sender` does today.

### 7.2 Seqlock

- **Writer:** `s = load(relaxed); store(s+1, relaxed); __atomic_thread_fence(RELEASE);` write
  the fields; `store(s+2, RELEASE)`.
- **Reader:** `s1 = load(ACQUIRE)` (odd → retry); `memcpy`;
  `__atomic_thread_fence(ACQUIRE)`; `s2 = load(relaxed)`; equal → accept.
  - The memcpy's formal C11 race is validated by the seq check and accepted.
  - After 4 failed tries the reader keeps the previous copy.
- **Memory:** `eng_view`, the parsed `/waiting` struct, `sm_in_t` and `sm_out_t` are all
  **static**. The stacks are 8 KB (face) and 6 KB (engine).

### 7.3 Serial engine

`jpanel_core_step(core, now)` does the following, in order.

1. **Finish the current stream first.** Classify it, arm the acknowledgement and attempt it
   before dequeuing anything. `stream_begin` refuses until that is done.
2. **Acknowledge.** One slot: `{id, kind PLAYED|RETURNED, heard_ms, delivery, first_at}`.

   | Answer | Action |
   |---|---|
   | 204 | Clear the slot. |
   | `/returned` 409 (not applicable) | Clear the slot. |
   | `/returned` **404** (route absent) | Clear the slot; count `no_refund`. |
   | Other 4xx, except 408/429 | Terminal: clear the slot and log it. |
   | Anything else | End the run; `ack_owed`; back off 1, 2, 4 … 30 s; give up after 10 min (the box's delivery cap is the bound after that). |

   While the slot is owed:
   - the poll drops the owed PLAYED id from `waiting`;
   - a PLAY retries the acknowledgement first, and on failure ends `failed("ack-pending")`.
3. **Continue a run, by id.** The run list is the waiting list when the run began, plus every id
   that arrives during the run (JP: "press once, hear everything"), minus ids played or returned
   in this run.
   - The run continues only if `cur.gen == s_stop_gen`.
   - A box without ids continues with `?at=0`, as today.
4. **Dequeue a command.**
   - If its gen is stale, or `due(expires_at)`, it finishes at once: `cancelled` or
     `failed("expired")`.
   - The same expiry check runs again before each request attempt: the `/next`, the send's
     second attempt, and the 422 retry.
5. **Poll** when due or flagged. A queued command cancels the poll (§7.6).

**Every exit publishes `done_seq` and its outcome in a single snapshot write.**

**Sending.** The capture lease is **acquired by render before the post** (CAS 0 → 1) and is
released by the engine after the send's last attempt, *before* `send_outcome` is published. If
the post fails, render releases it immediately.

**Ack-on-stop in F2 is policy unchanged, with one defect fix:**

- `out_ms > 0` → PLAYED, as today.
- `out_ms == 0` (cancelled before any sound) → RETURNED when the fetch carried
  `X-Jpanel-Delivery`. Otherwise nothing is acknowledged, and the attempt is burned (bounded).

Nothing was heard, so writing `played_at` would be the C4 lie.

### 7.4 The audio arbiter: `stream_tick`, pure and host-tested

All per-chunk decisions move out of `audio_task` into **`stream.c`**, alongside `ring.c`. There,
`stream_tick(&st, now) → { WRITE(ptr, len) | IDLE, capture: COPY|SILENCE|NONE }` is a pure step
over:

- the requests (in the table below);
- the ring;
- the pause request;
- the deaf counter;
- the capture state.

`audio_task` is reduced to codec read/write around it. **`make check_no_logic_in_audio`** fails
if `audio.c` references any field of `stream_t`.

**Requests and priority:**

| Request | Owner | Rule |
|---|---|---|
| Pet reply `{ptr, len, id}` | talk task | Played **from talk's own buffer** (no copy). Talk must not reuse the buffer until `reply_done == id`. Every disposal sets it: played, silenced, or standby. A reply waits out a sounding cue (≤ 749 ms, `cue.h`) and is never refused. |
| Press cue (one-slot mailbox) | render | Preempts a cue. Dropped while a reply is pending or sounding (JP: a reply outranks an acknowledgement). Interleaves over a stream chunk-by-chunk, as `s_play` already does today. |
| Queued cues (FIFO, depth 4: refusals, outcomes, new message) | render (FreeRTOS queue) | Wait only behind a reply or an open capture, so no chime is punched into a recording. Otherwise they interleave over a stream like press cues. Expiry: refusal 2 s; outcome 15 s; new message 20 s, or cancelled by a press on the notice. On overflow the oldest `MESSAGE` is dropped first, then the oldest entry, and the drop is counted. |
| Message stream (ring) | engine (producer) | Lowest priority. Yields to any cue or reply chunk-by-chunk. |
| Silence | render (flag) | Cuts the cue and reply and discards the stream once. Used by standby and deep sleep. |

**Stop and pause**

- `s_stop_gen` is written only by render.
- Each stream carries `{gen, stream_id}`, stamped by the engine.
- Each tick checks **gen before pause**. A stale stream is discarded: `r = w`, a consumer write.
  Nothing is output, and pause is ignored.
- A pause request is `{stream_id, on}` and applies only to that stream. A new stream, including
  the next one in a run, never starts paused.

**The ring**

- Cursors are monotonic, so there is **no reset**.
- The producer writes only `w`; the arbiter writes only `r`, with `__atomic` release on publish
  and acquire on read.
- `stream_begin` succeeds only when the arbiter has published `seen_w == w && r == w` for the
  previous stream, so stale bytes can never play under a new gen.
- `{stream_id, out_ms, last_progress_ms}` is written by the arbiter alone, and is never reset by
  the producer.

**The capture lease** is a `uint32_t` CAS (0 → 1 engine-send, 2 talk). `capture_open()` refuses
while it is held.

- **Talk** acquires it at `talk_send` and releases it when the upload returns.
- **Every opener must handle the refusal:**
  - F2's wrappers: the old `start_listening`, `start_send_recording` and the follow-up open in
    `display.c`;
  - in F3, the machine's microphone gate.

### 7.5 Bounds: the engine is authoritative

Every request's worst case is derived from `link.c`'s real waits:

```
REQ_MAX(t) = LINK_UP_WAIT_MS (8 s, link_request waits for the socket)
           + t  (WS head wait; or HTTPS s_http_lock wait)
           + t  (HTTPS connect/head)
           + LINK_STALL_MS (20 s, per body gap)
```

- `REQ_MAX(20 s) = 68 s`, for polls, fetches and sends.
- `REQ_MAX(5 s) = 38 s`, for acks.
- `PRED_MAX = max(REQ_MAX(20 s), REQ_MAX(5 s) + 4 s ring drain) = 68 s` is the longest the engine
  can be busy before it dequeues.

| Command | Expiry (engine-enforced) | Terminal outcome no later than |
|---|---|---|
| PLAY/REPLAY | post + 10 s | post + max(10 s, PRED_MAX) + REQ_MAX(20 s) = **136 s**, then SOUNDING |
| SEND | post + 30 s | post + max(30 s, PRED_MAX) + 2 × REQ_MAX(20 s) = **204 s** |

The send row allows two attempts in case the second starts just before expiry. In practice
expiry is the bound, and the long tail exists only on the HTTPS fallback.

**While sounding:**

- **Producer stall:** no bytes for `LINK_STALL_MS` → cut (`short`).
- **Wedged codec:** primed, ring ≥ one chunk, and `out_ms` flat for 2 s → discard
  (`codec_wedged`). Starvation is **not** a stall — it is bounded by the producer stall. The old
  consumer-stall rule is gone.
- **Pause:** the machine auto-resumes at `AUDIO_PAUSE_MAX_MS` (kept). The box's
  `WINDOW_STALL_S` (90 s) stays above that.

**UI backstops** (§8.4) are strictly larger than these bounds. They fire only if the engine is
broken. A late outcome after a backstop increments `late_outcome` and is never sounded.

`test_engine_bounds_with_worst_link` uses a fake link that burns `LINK_UP_WAIT_MS`, the lock
wait, the head and the stall, and asserts each terminal outcome lands within the table.

### 7.6 Network cancel

- `link_types.h` (split from `link.h`, which cannot build on the host) adds
  `bool (*cancelled)(void *ctx)`.
- **WebSocket:**
  - `ws_request`'s head and body waits call
    `wsp_wait_step(now, deadline, cancelled) → WAIT|CANCEL|TIMEOUT` every `LINK_STEP_MS`
    (250 ms), then `wsp_cancel`;
  - `wsp_wait_step` lives in the host-built `wsproto.c`.
- **HTTPS fallback:** checked in `http_request`'s read loop and before `fetch_headers`.
- **Not interruptible, stated:** `LINK_UP_WAIT_MS`, the `s_http_lock` wait and the HTTPS head
  phase. They are bounded by §7.5.
- **Buffers:** `rel[96]`, `path[112]`, `/waiting` 1536 B.
- **Gap.** cJSON parsing stays device-side (it is an ESP-IDF component). Host tests feed the core
  parsed structs, including the "no ids" shape; the JSON field names are pinned by backend tests.

### 7.7 Writers

| State | Writer |
|---|---|
| `s_stop_gen`, press-cue mailbox, silence flag, pause request, commands, lease acquire (send) | render |
| poll flag | any (atomic) |
| snapshot, run list, ack slot, ring `w`, stream `{gen, id}`, lease release (send) | engine |
| ring `r`, `seen_w`, `{out_ms, last_progress}`, `reply_done`, everything output | arbiter (in audio task) |
| reply request, lease (talk) | talk |

**Telemetry:** `ack_owed`, `ack_retries`, `no_refund`, `returned`, `lease_refusals`,
`snapshot_retries`, `post_failed`, `late_outcome`, `codec_wedged`, `jpanel_stack_free`,
`sm_violations`.

**F2 changes no intended behaviour.**

- The old `display.c` runs on the snapshot through thin `jpanel.c` wrappers:
  - `jpanel_state()` is derived from the phase and unseen outcome seqs;
  - `jpanel_clear()` marks an outcome seen;
  - `running`, `fetching` and `in_from` come from `cur`;
  - a play is refused while the send lease is held.
- F3 deletes the wrappers.

---

## 8. The panel state machine (F3)

### 8.1 Shape

`panel_sm.{c,h}` replaces `ui.{c,h}`. It is pure and host-built.

**`sm_step(sm, &in, &out)`** runs once per frame, both on device and in the tests: sample → step
→ execute effects → draw `out.view`.

**`in`:**

- `now`, `snap`, `post_failed`;
- touch edges with points;
- `on_pet` (`TALK_MARGIN_PX`), `gest_taps`, the swipe;
- the voice command, `cmds_muted`;
- the button edges, `standby`;
- `reply_sounding`, `hearing`, `capture_ms`, `leased`, `net`;
- the sleep stage, `calibrating`.

**`out`:**

- ordered effects;
- `sm_view_t`;
- the telemetry the machine owns: dashes and swipes.

**`display.c` keeps:**

- sampling;
- the executor (the `sound` gate on every cue);
- drawing;
- the sleep stages;
- calibration;
- the pet-side effects as pass-through, with today's guards: the poke, `VOCAB_FORM/COLOUR/ACTION`,
  and the label.

**Calibration.**

- Entering it is an input: the machine goes to PET with `CAPTURE_DROP`, `PAUSE(0)` and `STOP`.
- While calibrating, the machine does not step.
- Afterwards it reads the current snapshot. That snapshot is a level, so a send outcome that
  arrived meanwhile is sounded normally (if before its backstop).

**`make check_no_logic_in_display`** (part of `make test`) fails on either of:

- any static in `display.c` — file-scope **or function-local** — whose name, with `s_` stripped,
  matches an `sm_t` field (the field names are extracted from `panel_sm.h` by awk);
- any call to a `jpanel_*` function other than `jpanel_snapshot`, `jpanel_post` or
  `jpanel_poll_soon`.

### 8.2 State and invariants

```
sm_t { mode PET|GRID|LISTEN|THINK|RECORD|PLAY|PAIR, since
       play { seq, target{id, from, from_dad, replay}, phase PENDING|WAITING|ON, posted_at,
              run_pos }
       pair { since, id, from, from_dad }       // standing when due(now, since + REPEAT_MS)
       rec  { to, heard, hush, deliberate, opened_by_down }
       talk { voice, heard, hush, lead, follow_armed, follow_turns, dash... }
       grid_until; send { seq, posted_at }; notice { first_visible }
       sel { id, index }; seen_ids[16]; down { at, x, y, on_pet, target, swiped } }
```

Invariants are asserted after every host step and counted on device. A violation recovers to
PET with `CAPTURE_DROP`, `PAUSE(0)` and `STOP`.

- **I1** — One mode. The microphone is open ⇔ the mode is LISTEN or RECORD.
- **I2** — **One face function**, `sm_face(sm, snap)`, used both for drawing the top-left face
  and for addressing reply:
  - PET: the selected waiting entry.
  - PLAY PENDING/WAITING: `play.target`.
  - PLAY ON: `snap.cur`.
  - PAIR: `pair`.
- **I3** — The drawn controls are exactly the current mode's targets.
- **I4** — Every press answers with a cue or a view change, except the listed
  claimed-and-ignored presses.
- **I5** — `paused` ⇒ PLAY ON. Every exit from PLAY emits `PAUSE(0)` and then `STOP`.
- **I6** — Every non-rest mode has an engine-backed deadline (§8.4). PET and PAIR are rest modes.
- **I7** — Time uses `due()` and seqs use `seq_ge()`.

### 8.3 Targets and transitions

**Global (any mode):**

- **Button short.**
  - In standby → wake.
  - Otherwise → `CUE_STOP`, `CAPTURE_DROP`, `PAUSE(0)`, `STOP`, disarm the follow-up, standby,
    PET.
- **Button hold** → deep sleep.
- **Voice "stop"** (when not muted, i.e. not LISTEN/RECORD/standby) — exactly today's
  `VOCAB_STOP`:
  - disarm the follow-up and set the turn count to the cap;
  - THINK → `talk_clear`.
- **The wake phrase and "tell sister/dad"** go through the microphone gate.

**The microphone gate** opens when all of these hold:

- the mode is PET, PAIR, GRID, or PLAY-ON-REPLY (after its `STOP`);
- not standby;
- not leased;
- `!reply_sounding`;
- `net != BUSY`.

Otherwise it refuses with `CUE_BUSY`.

**Targets** (in priority order; *ignored* = claimed with no effect):

| Mode | Targets | Hold | Deadline |
|---|---|---|---|
| PET | NOTICE (big box, or badge quadrant) · PET | down on PET + `on_pet` + `gest_taps` free + not swiped → GRID | — |
| GRID | four cells · swallow | — | `SENDTO_MS` |
| LISTEN (held) | none — the release ends the turn | — | `TALK_TIMEOUT_MS` |
| LISTEN (voice) / RECORD | CROSS · TICK quadrants · swallow | — | lead / hush / cap |
| THINK | PET (flinch only) | — | `TALK_TIMEOUT_MS` |
| PLAY PENDING/WAITING | EXIT · FACE · transport *ignored* | — | §8.4 |
| PLAY ON | EXIT (after `EXIT_GRACE_MS`) · FACE · PAUSE · REPLY · swallow | — | §8.4 |
| PAIR, fresh | EXIT · AGAIN · REPLY quadrants · PET | — | — |
| PAIR, standing | EXIT · AGAIN / REPLY disc reach (r 82 at (92 \| 276, `confirm_cy`)) · PET | as in PET → GRID | — |

A hold at the pet centre (184, 224) is ≈171 px (upright) or ≈139 px (quarter turn) from either
disc, so it lands on PET.

**FACE** is live while `waiting.count + (ON ? 1 : 0) > 1`.

**PET**

- **Notice:**
  - while sending → `CUE_BUSY`;
  - otherwise → `CUE_PLAY`, PLAY{PENDING, target = selected}.
- **Swipe** → select, clamped, `CUE_HEARD`.
- **Unseen id arrives** → the selection goes home to the oldest (today's rule) and the
  `CUE_MESSAGE` chime is queued.

**RECORD**

- **Tick, hush or cap:**
  - heard → `SEND`, `CUE_HEARD`, PET;
  - not heard (tick) → drop, `CUE_OOPS` (today's behaviour).
- **Cross** → drop, `CUE_STOP`.
- **Lead silence** → drop, plus `CUE_STOP` if the recording was deliberate.
- **A swipe** in the same press that opened the recording from REPLY → drop it silently and
  apply the swipe (today the same swipe cancels `PEND_REPLY`).

**LISTEN/THINK** — ported unchanged, except that the follow-up does not open when a press is
pending or an unseen message arrived during the turn.

**PLAY**

- **PENDING:** once `!reply_sounding`, post `{seq, gen, expires_at = now + 10 s, target}` →
  WAITING. If `post_failed` → `CUE_OOPS`, PET.
- **ON** is level-derived: `snap.cur.seq == play.seq && phase == SOUNDING`.
- **When the run finishes** (`seq_ge(done_seq, seq)` and the engine is no longer in this run's
  phases):
  - natural end (`heard` or `heard_unacked`) and `waiting.count == 0` → PAIR{from = the last
    `cur`};
  - `failed`, `short` or `empty` → `CUE_OOPS`, PET;
  - anything else → PET.
- **EXIT** (any phase) → `PAUSE(0)`, `STOP`, `CUE_STOP`, PET. One press, no pair (owner).
- **FACE / swipe** → `PAUSE(0)`, `STOP`, `CUE_HEARD`, then select (cyclic / clamped) over
  `waiting` + the current message, the next after it in run order → PET (0.3.37). No message is
  skipped. A stop under `HEARD_MIN_MS` returns the current message to its place.
- **PAUSE** (ON) → toggle, `CUE_PAUSE`/`CUE_RESUME`.
- **REPLY** (ON) → `PAUSE(0)`, `STOP`, then the microphone gate:
  - opens → `CUE_REPLY`, RECORD{to = `sm_face`};
  - refused → `CUE_BUSY`, PET.

**PAIR**

- **AGAIN** → `CUE_PLAY`, PLAY{PENDING, replay `pair.id`}. If the replay fails → `CUE_OOPS`,
  back to PAIR.
- **REPLY** → the microphone gate:
  - opens → `CUE_REPLY`, RECORD;
  - refused → `CUE_BUSY`, and the mode **stays PAIR**.
- **EXIT** → `CUE_STOP`, PET.
- **Standing hold** → GRID.
- **`waiting.count > 0`**, for any reason → PET. The notice owns the quadrant.

### 8.4 UI backstops

Each UI backstop is strictly greater than the engine's bound (§7.5) for the same step.

| Mode | Waits for | Backstop |
|---|---|---|
| PLAY PENDING | `!reply_sounding` | `PENDING_MS = PLAY_BUF_MS + 2000` (`_Static_assert`) → `CUE_OOPS`, PET. EXIT stays live. |
| PLAY WAITING | ON or done | 140 s after the post → `STOP`, `CUE_OOPS`, PET |
| PLAY ON | done | `now - progress.last_progress_ms > LINK_STALL_MS + 5 s` while not paused → `STOP`, `CUE_OOPS`, PET |
| sending | `send_outcome.seq == send.seq` | 210 s after the post → `CUE_OOPS`. A later real outcome is logged only. |
| GRID / LISTEN / RECORD / THINK | as today | as today |

### 8.5 "New", sleep, cues

- **"New"** means an id that is not in `seen_ids` (FIFO of 16). An old box falls back to a rise
  in the count. "New" drives:
  - `CUE_MESSAGE`;
  - the selection going home;
  - the notice clock (`first_visible`);
  - the sleep `arrived` wake.

  A returned message, or one whose acknowledgement is still owed, therefore never re-chimes,
  never wakes a panel in standby, and never resets the selection.
- **Sleep: `sm_busy()`** is true when any of these holds:
  - a mode other than PET or PAIR;
  - sending;
  - `snap.phase ∈ {FETCHING, SOUNDING}`.

  POLLING and ACKING never count, which matches today's `used` test.
- **Cues** carry a class: press (mailbox) or queued. Refusals are `CUE_BUSY`, queued.
  `CUE_REPLY` sounds only when the recording actually opens.

### 8.6 Tests (`sm_tests.c` → `run_sm_tests`)

1. **Port every `ui_tests.c` scenario** to `sm_step`, keeping each one's owner-quote comment.
2. **Rewrite exactly these checks.** Each is named in the F3 PR with its new assertion.

   | Test | Change |
   |---|---|
   | `test_the_top_right_corner_ends_a_message_while_it_plays`, second half | One-press exit (owner): PET, no pair. |
   | `test_the_controls_are_pressable_for_as_long_as_they_are_drawn`, `test_every_playback_control_answers_the_finger` | Reply opens RECORD in ON. No `PEND_REPLY`. |
   | `test_reply_records_to_whoever_just_spoke` | The recipient comes from `sm_face`. |
   | `test_the_action_list_never_overflows` | The worst frame rebuilt from F3's effects (no `UI_PEND_REPLY`). |
   | `test_a_long_press_on_a_menu_is_just_a_press` | The pair case splits: fresh = press, standing = GRID at the pet centre. |
   | `test_the_pair_waits_for_a_finger_not_a_clock` | Now also: `waiting.count > 0` → PET. |
   | `test_the_notice_shrinks_to_a_badge_and_a_second_one_does_not_restore_it` | The clock starts at `first_visible`. |
   | `test_the_queue_count_only_sounds_on_the_way_up` | A rise means an unseen id. |
   | `test_what_the_box_said_about_a_message_is_answered_in_sound` | Outcome cues are queued; refusals sound `CUE_BUSY`. |

   **Unchanged** — explicitly:
   - `test_pressing_replay_twice_does_not_pause_what_never_started`
   - `test_pressing_the_face_cycles_through_the_waiting_messages`
   - `test_a_swipe_stops_the_message_it_moves_off`
   - `test_a_new_message_sends_the_selection_home`
   - `test_a_tick_pressed_into_silence_refuses_to_send`
   - `test_the_face_follows_the_message_that_is_playing`
   - `test_stop_said_out_loud_leaves_every_state`
   - `test_a_held_stream_resumes_rather_than_being_thrown_away`
   - `test_a_short_button_press_darkens_blanks_and_mutes` — extended to every mode, and asserting
     no `CAPTURE_OPEN` after standby.
3. **Journey tests** named in §3 and §9, including:
   - `test_hold_at_pet_centre_*` — with a badge, a fresh pair, a standing pair, and during PLAY;
   - `test_face_function_is_drawn_and_addressed` — every PLAY phase and the run gap: the drawn
     face equals the reply recipient whenever REPLY is live.
4. **`test_storm`** — 300k seeded frames. It perturbs the inputs:
   - snapshots **dropped** (the old copy kept), **delayed** and **duplicated**;
   - touches dropped and repeated;
   - clock jumps, including the u32 wrap;
   - random calibration;
   - engine outcomes at its bounds.

   It asserts I1–I7 on every frame, that every non-rest mode ends by its bound, and that no
   backstop fires while the fake engine stays within §7.5.
5. **End to end.** `jpanel_tests` can drive `sm_step` against the real `jpanel_core` and
   `stream.c`. This covers A1, A6, B1 and E2.3 across both halves.

### 8.7 Landing and rollback

F3 replaces the old logic in one wave. A shadow mode was rejected: the old path's known bugs
make its divergences mostly intended, and a shadow would double the state on a stack-limited
task.

- **Field safety:**
  - the §8.6 gate;
  - `sm_violations` recovery;
  - the OTA rollback gate for crashes;
  - an owner on-glass checklist in the PR.
- **Rollback** is a source-revert PR: F3 (and F4, which builds on it, if it has landed), plus
  `version.txt` bumped and `dist/` rebuilt, because `firmware.yml` byte-compares `dist/` with the
  source. The owner applies it with Ops → Update, and the panels OTA because the version differs.

---

## 9. Per-finding detail

§7 and §8 hold the design; this section adds each cause and its tests.

- **A1.**
  - **Cause:** `fetch_head` resets `s_stopped`/`s_run`; the exit's `CUE_STOP` made the wait late;
    `audio_stream_begin` cleared pause.
  - **Tests:**
    - `test_stop_at_every_interleave_point` — queued, speaker wait, head, after claim, chunk,
      paused, drain, pre-ack, run gap;
    - `test_stop_with_no_stream_then_play_starts`;
    - `test_stop_while_paused_drains`;
    - `test_stop_during_drain_frees_speaker`;
    - `test_stop_during_5s_head_delay_cancels_request` (on `wsproto.c`);
    - `test_next_queued_during_drain_classifies_previous_first`;
    - `test_run_gap_stop_never_acks_unsounded`.
- **A2.**
  - **Cause:** `!p.began` exits left `s_run` set.
  - **Test:** `test_chain_fetch_failure_returns_to_idle`, over link / 500 / 204 / speaker busy.
- **A3.**
  - **W1:** the raw ceiling becomes `PANEL_AUDIO_MAX` (35 s).
  - **F2:** 400 → `EMPTY`, 409 → `NOBODY`, 413 → `FAILED("too-long")`.
  - **Pins:**
    - `test_capture_caps_pinned`: `PANEL_AUDIO_MAX == CAPTURE_MAX_MS × 32`, and
      `panel_ws.MAX_BODY > PANEL_AUDIO_MAX` — the panel's cap;
    - `voiceMessage.MAX_MESSAGE_MS == jpanel.MAX_MESSAGE_MS` — the owner's 30 s cap, a separate
      pin;
    - the `panel_ws.py` comment is corrected.
- **A4.**
  - **Fix:** §7.3; removal by id; W3 ids.
  - **Tests:**
    - `test_lost_ack_is_retried_and_never_replays`;
    - `test_ack_4xx_is_terminal`;
    - `test_ack_gives_up_after_10_min`;
    - `test_owed_played_id_filtered_from_waiting`;
    - backend `test_waiting_queue_carries_ids`, `test_next_by_id_serves_that_row_or_204`.
- **A5.**
  - **Fix:** §7.3 — the lease is taken by render before the post.
  - **Tests:**
    - `test_lease_taken_before_post`;
    - `test_lease_released_on_post_failure`;
    - `test_upload_bytes_unchanged_under_listen_attempts`;
    - `test_follow_up_open_handles_refusal` (the F2 wrapper).
- **A6.**
  - **F3:** heard iff `out_ms ≥ min(HEARD_MIN_MS, 0.8 × duration)`, otherwise RETURNED — only
    with a delivery header.
  - **W3:** `POST /returned {id, delivery}`:
    `UPDATE … SET deliveries = deliveries - 1, returns = returns + 1 WHERE id = :id AND
    played_at IS NULL AND deliveries = :delivery AND returns < 5`.
    - A match answers 204.
    - No match answers 409. That makes a retry after a lost 204 idempotent, and the cap keeps
      0209's bound.
    - The route is added to `panel_ws._ALLOWED`.
  - **Tests:**
    - `test_stop_in_preroll_returns_not_acks`;
    - `test_stop_after_threshold_acks_with_heard_ms`;
    - `test_returned_is_idempotent_and_bounded`;
    - `test_returned_refunds_only_own_unplayed` (integration, sibling case);
    - `test_panel_ws_tunnels_returned_and_new_headers` — a round trip of `/returned`,
      `/next?id=` and the `X-Jpanel-*` response headers through the socket;
    - `test_returned_404_counts_no_refund`;
    - `test_face_cycles_without_skipping` (2, 3 and 8 messages);
    - `test_run_never_replays_a_returned_message`;
    - `test_message_arriving_mid_run_joins_it`.
- **A7 / A8 / A9.** Tests: `test_snapshot_is_never_torn` (mid-write fails and keeps the previous
  copy; quiet points succeed), `test_send_outcome_survives_concurrent_chain`,
  `test_cursor_writers`.
- **B0.** §8.
- **B1.**
  - **Cause:** `PEND_REPLY` and the PLAYING branch re-armed the pair in the same pass, and PAIR
    outranked CONFIRM.
  - **Fix:** per-mode tables. REPLY is live only in ON and goes through the microphone gate.
  - **Test:** `test_reply_during_playback_opens_a_working_recording`.
- **B2.** The hold at (184, 224) fell in the reply quadrant. **Fix:** the standing pair.
- **B3.** **Fix:** PAIR only while `count == 0`, plus I2. This settles JP's open "face
  collision".
- **B4.**
  - **Fix:** §7.4 priorities, and the notice clock starts at `first_visible`.
  - **Tests:**
    - `test_cue_priorities` — a reply waits out a cue and is never refused; a press cue is
      dropped while a reply is pending; queued cues wait for capture and expire;
    - `test_message_during_pet_reply_gets_chime_and_big_box`;
    - `test_heard_then_oops_both_sound`.
- **B5.** Sending is a level; a press on the notice plays `CUE_BUSY`. **Test:**
  `test_notice_shown_and_refused_during_send`.
- **B8.** Silence → `CUE_STOP` (deliberate recordings); every refusal → `CUE_BUSY`.
- **B10.** §8.4 `PENDING_MS`; the follow-up yields; `due()` and `seq_ge()` throughout.
  **Tests:** `test_press_behind_pet_reply_is_served`, `test_follow_up_yields_to_pending_press`,
  `test_deadlines_survive_u32_wrap`.

**B-UX**

| Gap | Decision | Wave |
|---|---|---|
| X takes two presses | One press (owner) | F3 |
| All failures sound the same | SENT / NOBODY (`CUE_NOBODY`) / EMPTY (`CUE_STOP`) / FAILED (`CUE_OOPS`) / refusal (`CUE_BUSY`) | F2 mapping; F3 placeholder; F4 contours |
| No sending feedback | Overlay; `CUE_SENT` contour | F4 |
| Text instructions for pre-readers | Icons and pips (mock); names stay | F4 |
| Badge | Pips and a pulse. No reminder sound (JP) | F4 |
| "1/2" counts the remaining queue | `run_pos / (run_pos + remaining)` | F3 |
| Arrivals mid-turn | Notice clock | F3 |
| The pair blocks the hold | Standing pair (owner); survives sleep | F3 |
| No hint how to stop a recording | Glyphs and a hush ring | F4 |

**C findings**

- **C1.** §11.
- **C2.** `send_text` validates before TTS (404); the PWA refetches and keeps the draft; W2's
  grouping removes the ghost chips.
- **C3.** `POST /messages/{id}/retry` (at the cap → `deliveries = 0, returns = 0`, then a
  nudge). `DELETE /messages/{id}` (owner-sent or undelivered rows only, releasing the blob via
  `blob_referenced` as `sweep.py` does). Both answer 404 for a non-uuid id.
- **C4.** W3 columns `heard_ms` and `offered_at`. `/waiting` stamps `offered_at` once. The copy
  says "Played".
- **C5.** Between rows go in both threads; the unplayed count is unchanged; P1 adds the status.
- **C6.** A per-thread `row_number()` limit, `UNION` every unplayed panel→owner row, and
  `?before=` for older rows. `/summary` uses **the same query function**, so the badge always
  equals what renders (`test_summary_equals_rendered_unplayed`).
- **C7.** 206 / 416 / `Accept-Ranges`; a uuid guard on the audio, read and C3 routes.
- **C8.** `errorDetail` joins `detail[].msg`; `maxLength={600}`; TTS timeout 60 s; the counter
  and hint come in P1. The 600-character cap is kept.
- **C9.** `kept_for` per recipient.
- **C10.** Owner audio over the cap → 413.
- **C11.** Request seq plus mutation epoch.

**C-UX**

| Gap | Decision | Wave |
|---|---|---|
| No notification | Launcher badge (`/summary`); web push deferred (§5) | W3, P1 |
| Ambiguous "not heard" | `offered_at` / `deliveries` statuses | P1 |
| Composer | Hint plus counter | P1 |
| Read-marking too eager | Dwell 1.5 s and `document.hasFocus()` | P1 |
| 30-day sweep | Kept; stated in the footer | P1 |
| Renames rewrite past names | **Not fixed.** Renames are mostly corrections; snapshotting names would freeze typos (§13) | — |
| Two clears | Kept; the confirm text names what remains | P1 |

---

## 10. Host foundations (F1, behaviour-preserving)

1. **`link_types.h`.** The `wsp_wait_step` decision moves into `wsproto.c`.
2. **`stream.c`.** The ring, stream and capture bookkeeping, and the per-chunk decisions that are
   in `audio_task` today (`s_play` before the ring, the preroll, pause, deaf, the capture copy),
   extracted **as they are** into `stream_tick`. `audio.c` keeps codec I/O only, and
   `check_no_logic_in_audio` is added.
3. **`jpanel_core.c`**, behind `jpanel_io_t`: the request, stream ops, SHA-256, the clock and
   sleep, `yield(point)`, and the publish. `jpanel.c` keeps the task, the queue, cJSON, mbedtls
   and the real io.
4. **`run_jpanel_tests` and `run_stream_tests`** are added to `make test`. They use:
   - a scripted box, including a 0.3.39 mode;
   - the real `ring.c` and `stream.c`;
   - a fake clock;
   - a test-only public-domain SHA-256 (no new tool, so `dev-setup.sh` is unchanged);
   - yield hooks.
5. **Characterisation tests** assert 0.3.39's behaviour, **bugs included**, each named with its
   finding ID. F2 inverts them.
6. **Limit:** the yield hooks reach engine-side interleavings only. Cross-core ordering is
   reviewed against §7's fences and watched through telemetry.

**Backend tests that read firmware source.** This is a full census of `backend/tests`; each
retarget lands in the wave that moves the code it reads.

| Test (file:approx line) | Reads | Retarget in |
|---|---|---|
| `test_jpanel_api.py` 195, 523, 582, 602, 623, 671, 690, 772 | `jpanel.c` | F1 → `jpanel_core.c` |
| `test_jpanel_api.py` 204; `test_panel_ws.py` 445 | `link.c` | F1 (types → `link_types.h`; wait step → `wsproto.c`) |
| `test_endpoint_report_now.py` 290, 311 (`stream_begin_waiting(` count), 338, 667 | `jpanel.c` | F1 → `jpanel_core.c` |
| `test_endpoint_report_now.py` 407 (deafen block: `s_cap_on`, memset, `s_cap_used +=`) | `audio.c` | F1 → `stream.c` |
| `test_endpoint_api.py` 1603, 1634 (`CAPTURE_MAX_MS`) | `audio.c` | F1 (if the constant moves to `stream.c`) |
| `test_endpoint_api.py` 1539; `test_endpoint_report_now.py` 432 (`audio_play`, `goto done` / `why =`) | `talk.c` | F2 (reply request) |
| `test_jpanel_api.py` 543, 587, 677; `test_endpoint_report_now.py` 376, 474, 486, 503 (`speech_mute_commands`), 560, 722 (`s_dashes++`) | `display.c` | F3 → `panel_sm.c` / the executor |
| `test_jpanel_api.py` 555, 655 (`UI_QUEUE_MAX`, draw width); `test_endpoint_report_now.py` 717–721 (`st->dashes++` in `ui.c`); `test_endpoint_api.py` 1601–1602, 1635, 1660 (`LISTEN_LEAD_MS`, `LISTEN_HUSH_MS`, `TALK_TIMEOUT_MS` in `display.c`/`ui.h`) | `ui.c`, `ui.h`, `display.c` | F3 → `panel_sm.{c,h}` |
| `test_jpanel_api.py` 349, 425, 646, 736; `test_endpoint_report_now.py` 59–250, 497, 509, 622; `test_endpoint_api.py` 927, 1001, 1731–1751; `test_g2p.py`; `test_nudge.py` | `speech.c/h`, `main.c`, `jpanel.h`, `vocab.c`, `nudge.c/h` | Checked each wave; no move is planned |

## 11. Identity (W2): no RLS change

**Problem (C1).** `reflash` keeps the subject but mints and revokes principals. Messages are
principal-keyed, so unplayed mail addressed to a dead key is never served, never swept, and
never shown.

### 11.1 Fix

Panel routes, RLS, nudge, `panel_ws` (apart from W3's `_ALLOWED` entry), `_waiting_count`,
`/converse`, rename and revoke are all **unchanged**.

1. **`readdress_unplayed(session, sid, new_pid)`** — called by both `reflash` and `rotate`
   (`repo.py`), in the same owner-context transaction. The principal `INSERT` gains
   `RETURNING id` to bind `new_pid`.
   - It runs `UPDATE app.jpanel_message SET recipient_device = :new WHERE played_at IS NULL AND
     recipient_device IN (SELECT id::text FROM app.principals WHERE subject_id = :sid AND id <>
     :new)`.
   - It acts on the **same subject only**, so it never crosses between the twins.
2. **`reflash` subject choice:** the subject owning the most recently created key (live or
   revoked) for that name and role; otherwise the oldest; otherwise create a new one. A revoke
   followed by a re-flash therefore lands where the panel's history is.
3. **Owner views group by subject** (owner context, read-only). `messages()`, `pet_chats`,
   `/summary` and the counts join `principals.subject_id`.
   - A NULL subject, or a principal that `DevicesRepo.delete` hard-deleted, falls into one
     retired **"Unknown panel"** group that can be cleared.
   - A subject with a live jpet key uses that key as `device_id`, so `send_text`, rename and
     clear work unchanged.
   - A subject without one gets `retired: true` and `device_id` = the subject id. Only subjects
     **with rows** are listed.
   - Clear, the badge and `/summary` resolve the id to the subject's principals.
   - Display-role pet turns are their own non-messaging group, **"Displays"** — kept, not hidden.
4. **Pre-0211 extra subjects are never merged automatically.** The owner merges one explicitly
   with `POST /jpanel/panels/{retired_subject}/merge {into}`. The route:
   - refuses unless both subjects have the same `device_role`, the target holds a live key, and
     the source holds none;
   - re-addresses the source's unplayed mail to the target's live key and re-points the source's
     revoked keys;
   - tombstones the source's `display_name` as `merged:<target-id>:<source-id>` — an audit trail
     and a reversal record — so `reflash` can never match it;
   - is logged, and `DevicesRepo.list` hides tombstones.

   Until P1 adds the button, the route is reachable through the owner debug API
   (`docs/runbooks/DEBUG_ACCESS.md`). Nothing decays in the meantime, because unplayed rows are
   never swept.
5. **One-time data migration (no schema change).** Unplayed rows addressed to revoked keys are
   re-addressed to the same subject's single live jpet key, if one exists. Anything else waits
   for a merge.

**Rollback:** none is needed. The rows stay principal-keyed, and §6's rule applies (revision
files are never removed). Alembic is transactional, so a failed migration leaves the box on the
old head, and Ops → Update shows the failure.

### 11.2 Tests

There are no new tables or policies, so the existing RLS isolation tests stand.

- `test_reflash_carries_unplayed_mail_to_new_key` — played rows and **the sibling's** rows are
  untouched.
- `test_rotate_carries_unplayed_mail`.
- `test_reflash_after_revoke_lands_on_last_used_subject`.
- `test_owner_threads_group_by_subject_across_reflash`.
- `test_deleted_principal_rows_in_unknown_group`.
- `test_retired_listed_only_with_rows`.
- `test_merge_rules_and_tombstone`, covering:
  - a role mismatch is refused;
  - a target without a live key is refused;
  - a source with a live key is refused;
  - `reflash` cannot match a tombstone.
- `test_identity_backfill_pg`, using today's shape (13 principals, a dead-key inbox, an extra
  pre-0211 subject left untouched).
- `test_send_after_reflash_delivers` (a real panel key).

## 12. Waves

One PR per wave (PROCESS.md). Each package runs its **whole** gate set, from its own directory
(CLAUDE.md #11):

- **backend:** `ruff check .`, `ruff format --check .`, `pyright`, `pytest`.
  **The backend job runs on every firmware wave**, because `firmware/main/**` is in its filter.
- **frontend:** `npm run lint`, `npm run typecheck`, `npm run test`, `npm run build`.
- **supervisor:** the full set, because `firmware/dist/**` triggers it.
- **firmware:** `make -C firmware/host test`.

**Every firmware wave also:**

- bumps `version.txt`;
- commits a rebuilt `dist/` (`scripts/firmware-setup.sh` once, `idf.py build`,
  `scripts/firmware-dist.sh`);
- keeps the bootloader byte-identical;
- applies the §10 retargets it owns.

**Every wave:**

- runs `scripts/docs-freshness.sh`;
- updates this plan's markers, JP §3b, the firmware READMEs and the ROADMAP / `plans/README`
  rows. F3 also updates JP's exit, pair and refusal-cue text to the owner's decisions.

**Never:**

- a new dependency without updating `dev-setup.sh` (CLAUDE.md #8);
- a shell-only operator step (CLAUDE.md #10).

| Wave | Contents | Depends on | CI / local gates |
|---|---|---|---|
| **W1** box/PWA, invisible | A3 box; C2 validation; C5; C6 (+ `/summary` query function); C7; C8 (non-visual); C9; C10; C11; skew pins. Fixes A3 for every panel in the field. | — | backend, frontend, docs |
| **W2** identity | §11; retired threads hidden from the picker until P1 | W1 | backend, frontend, docs |
| **W3** contract | `/waiting` ids + `offered_at`; `/next?id=` + duration and delivery headers; `/played {heard_ms}`; `/returned` (+ `_ALLOWED` + tunnel test); C3 routes; `/summary`; additive columns `heard_ms`, `offered_at`, `returns`; `test_waiting_body_fits_0_3_39_buffer` | W2 | backend, docs |
| **F1** host foundations | §10, no behaviour change | — (parallel) | firmware, backend, supervisor, docs |
| **F2** engine | §7; defects only, no policy change; wrappers | F1, W3 | firmware, backend, supervisor, docs |
| **F3** state machine | §8; every B fix; hand-back on; owner decisions Q1–Q3 (placeholder `CUE_BUSY`); wrappers deleted | F2 | firmware, backend, supervisor, docs |
| **F4** panel visuals | Overlay, pips, pulse, glyphs, hush ring, `CUE_NOBODY` / `CUE_SENT` / `CUE_BUSY` contours. **GUI gate:** three interactive mocks (WebAudio) first; the chosen one goes into `docs/mocks/jpanel-panel-messaging/` | F3 | firmware, backend, supervisor, docs |
| **P1** PWA | C3 buttons; C4 copy; C5 status; composer; launcher badge; read dwell; retention line; retired threads and merge; "Displays" group; "Load earlier"; delete/revoke copy. **GUI gate:** three mocks first | W2, W3 | frontend, docs |

**Order:** W1 → W2 → W3 → F1 → F2 → F3 → F4. P1 runs any time after W3, and F1 runs in parallel
with the W waves. Each wave ships on its own and changes no intended behaviour before the wave
that owns that change.

## 13. Risks, losses, questions

**Risks**

- **F3 is a one-wave replacement.** Mitigation: §8.6–8.7.
- **Cross-core memory ordering** is reviewed, not host-tested.
- **The HTTPS fallback's uninterruptible waits** make a stop or skip there end late, though
  within §7.5's bounds.

**What the owner loses**

- **No web push.**
- **No resend** of a failed send.
- **"Played", not "heard".** The panel cannot tell who was in the room.
- **Renames rewrite past names.**
- **A stop under 2 s returns the message**, so the notice comes back.
- **The notice cannot be played during her own upload**; she hears "busy".
- **A fetch cancelled before its head can cost one delivery attempt.** It is bounded and
  retryable.
- **Each retired pre-0211 subject needs one explicit merge.**

**Questions** (the plan runs with these defaults unless overruled):

1. `HEARD_MIN_MS = 2 s`.
2. The typed-message cap stays at 600 characters.
3. Web push stays deferred.

## 14. Review log

**Rounds 1–2** (32 blocking items, 59 non-blocking). Every claim was verified and accepted.

- **Still standing from those rounds:**
  - the consumer-owned discard;
  - the seqlock fences;
  - host-built `stream.c`;
  - network cancel;
  - the single ack slot;
  - one step per frame;
  - the standing pair;
  - per-mode targets;
  - "new" by id;
  - the level snapshot;
  - the serial engine;
  - the audio task as sole speaker owner;
  - re-flash re-address without RLS changes;
  - owner decisions restored.
- **Removed as superseded:**
  - the event queue;
  - the subject/RLS rewrite;
  - its revert revision;
  - the busy token;
  - `PEND_REPLY`;
  - the consumer-stall discard.

**Round 3** (18 blocking items). Every claim was checked against the code, and all were
accepted:

- `panel_ws._ALLOWED` lists nine routes;
- `link_request` waits `LINK_UP_WAIT_MS` (8 s);
- `http_request` takes `s_http_lock` with `timeout_ms`;
- `ui.c` ignores the transport while a play is pending;
- `speech_mute_commands` covers LISTEN;
- the longest cue is 749 ms;
- `update-inner.sh` runs `git reset --hard @{u}`;
- the backend tests listed in §10 do read the stated firmware files.

| Item | Resolution |
|---|---|
| FW-1 bounds false vs `link.c`; send told "didn't go" then delivered | Engine-authoritative expiry checked at dequeue and before each attempt; terminal outcome per seq; bounds derived from `link.c`; UI backstops strictly larger; late outcomes logged, never sounded (§7.5, §8.4) |
| FW-2 consumer-stall discard cuts on slow Wi-Fi | Removed. Only a wedged codec is detected; starvation is bounded by the producer stall |
| FW-3 host tests drove a model, not `audio_task` | `stream_tick` is a pure per-chunk arbiter, and `audio.c` does I/O only, with a grep gate (F1 extracts it as-is) |
| FW-4 lease timing contradictory | Render acquires before the post; the engine releases; `test_lease_taken_before_post` |
| UX-1 transport live before the sound starts | Claimed and ignored until ON; `pause_wanted` dropped; the 0.3.36 test unchanged |
| UX-2 face vs reply recipient | One `sm_face` for drawing and addressing; per-phase test |
| UX-3 microphone gate refused PLAY→REPLY | Gate admits PLAY-ON-REPLY after `STOP` |
| UX-4 PAIR stranded a handed-back message | Re-derived under the one-press exit: EXIT never leads to PAIR; PAIR only after a natural end with `count == 0`; any `count > 0` → PET |
| UX-5 `sm_busy` kept the screen awake on polls | busy = non-rest modes, sending, FETCHING / SOUNDING only |
| UX-6 dark wake cleared the pair | Dropped; the pair survives sleep (0.3.25) |
| UX-7 voice "stop" mis-scoped | Exactly today's semantics in every unmuted state; test ported unchanged |
| UX-8 send deadline false | As FW-1 |
| UX-9 no progress field | `progress {out_ms, last_progress_ms, paused}` in the snapshot; the ON backstop uses it |
| UX-10 rewrite list incomplete | Full table with new assertions; the unchanged tests named (§8.6) |
| BE-1 `/returned` not tunnelled | Added to `_ALLOWED`; tunnel round-trip test; 204 / 409 / 404 told apart |
| BE-2 census of firmware-reading tests | Full table (§10); backend job and gates in F1–F4 |
| BE-3 rollback removes revisions | Rollbacks never remove revision files; W3 is additive; insert-shape test |
| BE-4 old box + F3 hands back without a refund | Hand-back only with `X-Jpanel-Delivery`; otherwise today's ack-on-stop |

**Round-3 non-blocking items.** All accepted, and placed as follows:

- **§7.3:**
  - run gen check;
  - `out_ms == 0` never acked;
  - 4xx terminal and age cap;
  - mid-run arrivals join the run;
  - the `heard_unacked` outcome kind.
- **§7.4:**
  - pause tagged by `stream_id`;
  - no ring reset;
  - begin waits for `seen_w`;
  - reply played from talk's buffer, `reply_done` on every disposal, 749 ms;
  - press cues dropped while a reply is pending;
  - queued cues only behind reply/capture, with expiry and overflow policy;
  - the follow-up opener in the wrappers.
- **§7.6:** the cJSON gap is stated.
- **§10:** the full retarget census.
- **§7.1:** `seq_ge`.
- **§8.3:**
  - FACE liveness;
  - `CUE_REPLY` only on open;
  - a refused PAIR→REPLY stays in PAIR;
  - standby disarms the follow-up, and the gate refuses in standby;
  - held-LISTEN targets.
- **§8.1:** calibration outcome handling.
- **Elsewhere:**
  - pre-fetch-cancel burn stated (§13, E2.4);
  - `rotate` re-address, deleted principals, and "Displays" kept (§11.1);
  - retired threads only with rows;
  - merge guards, audit and tombstones hidden;
  - `/summary` equals what renders;
  - `RETURNING id`;
  - fallback subject;
  - W3 placement of the buffer test;
  - `reset --hard` wording;
  - cap pins split;
  - merge via the debug API before P1.
