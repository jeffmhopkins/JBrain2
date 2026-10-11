# jpanel messaging — the fix plan after the adversarial review

> **Status:** Scheduled · **Last verified:** 2026-10-11 · **Waves:** W1◻️ W2◻️ W3◻️ F1◻️ F2◻️ F3◻️ F4◻️ F5◻️ P1◻️

Voice post between the twins' panels and the owner's PWA works on the happy path and fails on
the paths a four-year-old actually takes. Those paths are: pressing again while it is still
loading, walking away mid-message, holding the pet while a message waits, talking over the TV,
and a panel re-flashed over USB. An adversarial review found 32 defects across the firmware
messaging engine (`jpanel.c`), the on-panel UI (`display.c`/`ui.c`) and the box/PWA
(`api/jpanel.py`, `JpanelScreen.tsx`). This plan fixes every one of them. Where it does not
fix one, it says why and what the owner loses. It also does the structural work that makes the
fixes testable, and it schedules that work **before** the fixes that depend on it.

Binding inputs: `CLAUDE.md`, `docs/DOC_LIFECYCLE.md`, `docs/reference/PROCESS.md`,
`docs/reference/DEVELOPMENT.md` ("Verifying a change"), and the design and owner intent in
`docs/plans/JPANEL_PLAN.md` (abbreviated **JP** below). Where this plan overturns a decision in
JP, §4 says so and gives the reason.

Names: the panels are placeholder-named **Quorra** and **Tamsin** in the repo. Function names
are as of firmware 0.3.39. Line numbers are deliberately omitted because they drift.

---

## 1. Goal

A child's message, and a message to a child, is never lost silently, never played twice
because of a fault, and never reported to the owner as "heard" when it was not. Every press a
pre-reader makes gets an answer they can interpret without reading. Every journey below is
pinned by a test that runs in CI: firmware host suites, backend unit tests plus testcontainers
integration tests, and frontend vitest.

## 2. What the fixed system must do — owner-facing expectations

Each line is a testable behaviour. The ID (E…) is what the traceability table (§3) and the
tests cite.

### J1 — A child sends (panel → sister / Dad)

- **E1.1** Every recording a panel can capture (up to `CAPTURE_MAX_MS`) is accepted by the box,
  whatever the room noise. No length a panel can produce is answered with 413.
- **E1.2** After the tick, or after the hush auto-send, the child hears exactly one of three
  sounds within the HTTP timeout: **sent**, **nobody to send to**, or **didn't go**. Silence that
  the box trimmed to nothing is answered with the same "dropped" sound the cross makes, not
  "didn't go".
- **E1.3** While a send is uploading, the glass shows that it is sending. A message arriving in
  that window is still announced (sound and notice).
- **E1.4** Nothing the child does during an upload — the wake phrase, the pet icon, an automatic
  follow-up listen — can alter the bytes being uploaded. Anything that would need the
  microphone is refused with the refusal sound.
- **E1.5** A recording the child deliberately started and then left silent ends with the
  "dropped" sound, never in silence. A spoken "tell sister" that is refused makes the refusal
  sound, not the "heard" sound.
- **E1.6** Holding a finger on the pet opens the "who?" menu whenever the finger is on the pet
  and not on a control. That includes while a message waits as a badge and while the
  again/reply pair stands.

### J2 — A child receives, plays, stops, replies

- **E2.1** Pressing the notice plays the message the notice's face shows (by message id, not by
  queue position). The face in the top-left quadrant is always the sender of what a press there
  would play.
- **E2.2** A stop — the exit corner, the face, a swipe, the button, or reply — issued at **any**
  moment after the press is honoured: before the fetch starts, during the speaker wait, between
  the response head and the first byte, during the preroll, or mid-message. After a stop no
  further audio from that message plays (beyond at most one audio chunk), and nothing chains on
  to the next message.
- **E2.3** A message counts as heard on the box only if it actually sounded for at least
  `HEARD_MIN_MS`, or for most of a short message. A stop before that hands the message back: it
  stays waiting, and the delivery attempt it cost is refunded.
- **E2.4** A message that has been heard is acknowledged exactly once, even if the first
  acknowledgement is lost on the network. A lost acknowledgement never makes a run replay the
  same message or skip the next one.
- **E2.5** A run (one press plays everything waiting) always ends. A fetch failure mid-run
  leaves no playback controls standing over silence, and the notice for whatever is still
  waiting comes back.
- **E2.6** Pressing reply during playback opens a recording to the sender with the tick and
  cross live. Neither the again/reply pair nor a replay can appear over a recording.
- **E2.7** The face control during playback means "next message", and next is the oldest
  message not yet played in this run. No message is skipped. With two messages waiting, the
  face works while the first plays.
- **E2.8** The exit corner leaves playback in one press.
- **E2.9** A message that arrives while the pet is talking, or during a recording, still gets
  its sound and its fifteen-second big notice, counted from when the notice first becomes
  visible.
- **E2.10** A press on the notice made while the pet is speaking is served when the pet
  finishes. If the press can never be served, it ends with the "didn't go" sound and the notice
  returns. It is never dropped silently.

### J3 — Dad sends, reads, clears

- **E3.1** The owner can always message every enrolled panel. A message to a panel that does
  not exist is refused immediately with a readable error, before any speech is rendered, and the
  typed draft is kept.
- **E3.2** Every unplayed message to the owner is renderable on screen, so the badge can always
  be cleared by reading. A message is marked read only after it has been on screen for a dwell
  time while the PWA has focus.
- **E3.3** The status on each message tells the owner what actually happened: not yet seen by
  her panel, on her panel and not played, started but not finished, played (with "stopped after
  N s" when it was cut short), or couldn't be delivered. Twin-to-twin messages carry the same
  status. The PWA says "Played", never "Heard", because the panel cannot know who was in the
  room.
- **E3.4** A message the box gave up delivering can be retried or discarded from the PWA.
- **E3.5** "Clear messages" names the right child in its kept-back count.
- **E3.6** A twin-to-twin message appears in both children's threads.
- **E3.7** A poll that started before a send can never wipe that send's bubble from the screen.
- **E3.8** Error messages are readable sentences, never `Request failed: 422`. The composer
  shows its character limit and says that the message waits for a tap on her panel.
- **E3.9** The launcher's jpanel tile shows how many messages from the children are unread.
- **E3.10** Audio plays in iOS Safari. A malformed message id gets 404, never 500.

### J4 — Re-flash (USB recovery of a panel)

- **E4.1** Re-flashing a panel loses nothing. Messages waiting for it are still delivered to it.
  Its history, unplayed badge, pet transcripts and settings stay under one identity in the PWA.
  No "panel 1a2b3c4d" ghost chip appears.
- **E4.2** Messages addressed to a panel that has since been **revoked** are visible in the PWA
  under that panel's name, marked retired, and can be cleared.

### J5 — Network loss

- **E5.1** A message is never acknowledged unless it streamed whole and sounded. One that was
  cut off stays waiting, as it does today.
- **E5.2** An acknowledgement that cannot reach the box is retried until the box answers. While
  it is outstanding, no automatic fetch runs.
- **E5.3** A send that fails says so (E1.2). The recording is not kept for retry: §12 explains
  that decision and what it costs.
- **E5.4** A panel and a box on different releases (§6) keep every journey above working, at
  worst with today's behaviour on the older side.

## 3. Traceability

"Verified" records what reading the code at 0.3.39 found. **✓** means confirmed as written.
**✓\*** means confirmed with a correction, given in §3.1.

| ID | Verified | Fix (section) | Wave | Proving test(s) |
|---|---|---|---|---|
| A1 | ✓ | Stop generation + claim-then-check; pause intent survives `audio_stream_begin` (§7.1, §8.A1) | F3 | `jpanel_tests.c: test_stop_at_every_interleave_point`, `ui_tests.c: test_pause_pressed_before_first_byte_holds` |
| A2 | ✓ | Every non-`began` exit of a fetch ends the run (§8.A2) | F3 | `test_chain_fetch_failure_ends_the_run` (×4 statuses) |
| A3 | ✓ | Box accepts the full capture cap; caps pinned across four files; distinct `EMPTY` outcome (§8.A3) | W1 (box), F3 (panel) | `test_send_accepts_every_length_the_panel_can_capture`, `test_capture_cap_pinned_across_firmware_and_box`, `test_nothing_said_is_dropped_not_failed` |
| A4 | ✓ | Ack outbox with retry; chain gated on 204; local list keyed by id (§8.A4) | W3, F3 | `test_lost_ack_is_retried_and_never_replays`, `test_waiting_queue_carries_ids` |
| A5 | ✓ | Capture-buffer lease; every capture opener refuses while leased (§8.A5) | F3 (+F4 ui) | `test_capture_cannot_open_while_send_holds_buffer`, `ui_tests: test_wake_phrase_refused_during_upload` |
| A6 | ✓\* | `HEARD_MIN_MS` + `/returned` refund; "next" = oldest unplayed of the run (§8.A6) | W3, F3, F4 | `test_stop_in_preroll_returns_not_acks`, `ui_tests: test_face_during_playback_never_skips` |
| A7 | ✓ | Seqlock snapshot; one writer per field (§7.2) | F3 | `test_snapshot_never_torn_at_any_interleave` |
| A8 *(new)* | — | `s_state` written by both tasks; a chain fetch overwrote a send's `BUSY` → replaced by an event queue (§7.3) | F3 | `test_send_outcome_survives_concurrent_chain` |
| A9 *(new)* | — | `audio_stop()` resets the SPSC ring from a third task → cancel flag (§7.4) | F3 | `test_cancel_never_touches_ring_cursors_off_task` |
| A-tests | ✓ | Extract `jpanel_core.c` into the host build (§9.1) | F1 | new `run_jpanel_tests` suite |
| B0 | ✓ | `display.c` drives `ui_tap`/`ui_frame`/`ui_overlay` + action executor; duplicate statics deleted (§9.2) | F2 | `make check_no_dup`; the existing ~440 ui checks become the shipping behaviour |
| B1 | ✓ | Pair only armed/live when talk idle and no pending reply; CONFIRM before PAIR (§8.B1) | F4 | `test_reply_during_playback_opens_a_working_recording` |
| B2 | ✓ | Hold opens the grid iff the down point resolves to `UI_TARGET_PET` (§8.B2) | F4 | `test_hold_on_pet_opens_grid_while_badge_or_pair_up`, existing 0.3.35 tests kept |
| B3 | ✓ | Pair is live only while nothing waits; a waiting message owns the quadrant (§8.B3) | F4 | `test_quadrant_face_is_what_a_press_plays` |
| B4 | ✓ | Cue queue with per-class expiry; notice clock starts when first visible (§8.B4) | F4 | `tests.c: test_cue_queue_*`, `ui_tests: test_message_during_pet_reply_gets_chime_and_big_box` |
| B5 | ✓ | Sending indicator (visual, F5); notice not hidden by a *send* (F4) (§8.B5) | F4, F5 | `test_notice_shown_during_send`, mock-bound draw test |
| B6 | = A3 | — | — | — |
| B7 | = A1 | — | — | — |
| B8 | ✓ | Deliberate recording dropped for silence → `CUE_STOP`; refused phrase → `CUE_STOP` (§8.B8) | F4 | `test_silent_recording_says_dropped`, `test_refused_tell_sister_sounds_refusal` |
| B9 | = A6 | — | — | — |
| B10 | ✓\* | `PENDING_MS` > reply cap and expiry is audible; follow-up yields to a pending press; wrap-safe deadlines; "stop stop" kept muted (§8.B10) | F4 | `test_press_during_pet_reply_is_served`, `test_deadlines_survive_u32_wrap` |
| B-UX | ✓ | §8.BUX, item by item | F4, F5 | per item |
| C1 | ✓ | Messages and pet turns keyed to the canonical **subject**; RLS on `app.subject_id`; backfill (§10) | W2 | `test_jpanel_identity_migration_pg.py`, `test_jpanel_message_rls.py` (extended) |
| C2 | ✓ | `send_text` validates `to_device`; chips come from the subject roster; PWA refreshes on an unknown thread (§8.C2) | W1, W2 | `test_send_text_unknown_panel_404_before_tts`, `JpanelScreen.test: send to vanished panel keeps draft` |
| C3 | ✓ | Owner retry/discard routes + PWA controls (§8.C3) | W3, P1 | `test_retry_resets_deliveries_and_nudges`, `test_discard_only_owner_or_undelivered` |
| C4 | ✓ | `heard_ms` + `offered_at`; honest status copy (§8.C4) | W3, F3, P1 | `test_played_records_heard_ms_old_firmware_null`, vitest status matrix |
| C5 | ✓ | Between rows in both threads; status on between rows (§8.C5) | W1, P1 | `test_between_message_in_both_threads` |
| C6 | ✓ | Per-thread limit + always include unplayed-to-owner rows (§8.C6) | W1 | `test_every_unplayed_owner_row_is_returned` |
| C7 | ✓ (Range plausible) | Single-range 206 + `Accept-Ranges`; uuid guard → 404 (§8.C7) | W1 | `test_audio_range_206`, `test_non_uuid_is_404` |
| C8 | ✓ | `errorDetail` flattens pydantic lists; `maxLength`; TTS timeout 60 s (§8.C8) | W1, P1 | `client.test: 422 list detail`, `test_send_text_600_chars_within_timeout` |
| C9 | ✓ | `Cleared.kept_for` per recipient (§8.C9) | W1 | `test_clear_kept_names_recipient` |
| C10 | ✓ | `send_audio` 413 instead of truncation (§8.C10) | W1 | `test_owner_audio_over_cap_is_413` |
| C11 | ✓ | Request sequencing + mutation epoch (§8.C11) | W1 | `JpanelScreen.test: stale poll cannot drop sent bubble` |
| C-UX | ✓ | §8.CUX, item by item | W3, P1 | per item |

### 3.1 Where the review needs correcting

- **A6.** The face target is live only while `waiting > 1` (`ui.c: target_live(UI_TARGET_FACE)`),
  and `fetch_head` has already decremented the count. So with exactly **two** messages waiting,
  the face is *dead* during the first message. The skip the review describes (A → C, missing B)
  needs three or more. Both share one root cause — the local count stops including the message
  that is playing — and the fix in §8.A6 handles both.
- **B10, "stop stop".** The `VOCAB_STOP` recording branch in `display.c` is genuinely dead,
  because `speech_mute_commands(...TALK_RECORDING...)` is on. The mute is deliberate, though: it
  stops a word inside a message from being acted on (JP 0.3.33). Unmuting "stop" would let a
  child saying "stop" to her sister cancel her own message. **Not fixed**; the cross is the way
  out, and F5 makes it legible (§8.BUX).
- **B-UX, "pair never expires".** That was the owner's explicit call in 0.3.25 (*"no more
  waiting for it to time out and making it disappear"*, ROOM_ENDPOINT_PLAN "Touch stops being
  sampled…"). This plan keeps it. It removes the harm instead (B2, B3, and clearing on wake from
  dark). See §4.
- **Doc and code that disagree (fixed in the waves that touch them).**
  - `display.c`'s `s_repeat_until` comment says the pair leaves "when a new message displaces
    it". No code does that, and the gap is the root of B3.
  - `ui.c`'s `UI_TARGET_EXIT` case comments "No cue" right after emitting `CUE_STOP`.
  - `jpanel.h`'s `jpanel_replay` says "without asking the box again". It has asked the box since
    0.2.96.
  - JP's panel section still says "Capped at 20 s".
- **C7.** Range support is "plausible" as the cause of iOS failures. It is fixed anyway because
  it is cheap and correct; the 500 on a non-uuid id is confirmed.

## 4. Decisions in JPANEL_PLAN this plan keeps, and the ones it overturns

| JP decision | Here |
|---|---|
| **Ack-on-stop** — a finger stop acknowledges, so the stop does not burn one of the five delivery attempts (`jpanel_stop` comment). | **Kept for its purpose, narrowed in its effect.** A stop still never burns an attempt. A stop after `HEARD_MIN_MS` of actual sound acknowledges, as today. A stop before that hands the message back with `POST /returned`, which refunds the attempt, so it is neither marked heard (C4) nor burned. This also overturns "a finger stop = she heard it" for stops in the preroll: a child who pressed exit 0.8 s in heard a cue and nothing else. |
| **Screen-sleep rules** — a waiting message does not hold the screen awake; waking from dark restores the big box; the touch that wakes from dark only wakes. | **Kept unchanged.** One addition, in the same spirit as the big-box rule: the again/reply pair is cleared when waking from **dark** (not dim). Whoever looks at the panel in the morning was not the one who finished last night's message. |
| **"A message that cannot be delivered is still a message"**; unplayed rows survive a bulk clear. | **Kept.** A bulk clear still keeps everything a child has not heard. What is new is that the owner can act on **one** undelivered message: retry it, or discard it (owner-sent or given-up only). §5's rule is about bulk tidying, not about a parent's specific decision. |
| **The pair never expires** (0.3.25). | **Kept.** Its harms are removed instead: it no longer blocks the hold (B2), it yields the quadrant to a waiting message (B3), and it clears on wake from dark. |
| **Every press fires on the DOWN edge** (measured for jabbing). | **Kept**, including for a swipe that starts on the big notice. That swipe plays and then immediately cancels, which is clean once A1 is fixed. Moving the notice to fire on release was considered and rejected: it would re-open the 0.3.33 "long press works better" report. |
| **Index-addressed `/next?at=`** — "the panel never learns ids until it plays". | **Overturned.** `/waiting` now carries each queued message's id (additive), and `/next` accepts `id`. Positional addressing caused A4's local-list corruption and A6's skip. Ids are not a new exposure: a panel can already read every row addressed to it. |
| **Messages addressed by principal (device key) id** (JP §5 "dead letter" residual). | **Overturned** — by the subject, which `/flash` has kept stable since 0211 (`DevicesRepo.reflash`). JP named this residual: *"not fixable without knowing which key is LIVE"*. The subject removes the need to know. |
| **"The other panel" refusal; RLS sibling isolation; panels never read pet transcripts.** | Kept exactly. The RLS rewrite (§10) re-proves every one, against the subject. |
| **Two clears, one thread** (messages vs pet chat). | Kept (owner decision in "One child, one conversation"). Only the confirm copy changes: it says which half stays. |
| **"Played messages kept 30 days after `played_at`"**, and owner read-marking writes `played_at`. | Kept as policy. The PWA states it (P1) so the thread no longer thins silently. |

## 5. Scope boundaries

- **No push notifications to the owner's phone.** There is no web-push infrastructure in the
  repo: no `PushManager`, no VAPID keys, no service. Adding it is a new runtime dependency and a
  privacy decision about a parent's phone, which JP §5 left open on purpose. **Deferred**; §12
  says what the owner loses. The in-app launcher badge (E3.9) is in scope.
- **No retained outgoing recording for resend after a failed send** (E5.3). Holding the capture
  buffer across a failure would extend the A5 lease indefinitely and block the pet's
  microphone. A second 1.1 MB PSRAM buffer was rejected while `free_psram` margins are unmeasured
  after F2. **Deferred**, with the cost stated in §12.
- **Child-speech STT quality** — out of scope (JP §5).

## 6. Version skew

**The box ships the firmware.** Panels OTA from the box's own `firmware/dist/`
(`api/endpoint.py`, `ota_fetch_manifest`), so a new firmware never runs against an older box —
**except after the owner rolls the box back** through Ops → Update. A new box with old firmware,
on the other hand, is routine. It is the state between a box update and each panel's next
manifest poll (about 15 min), and it lasts indefinitely for a panel whose OTA keeps refusing
(`ota_apply_faults`). The rules:

1. **Every box change is additive for panels.** It accepts what 0.3.39 sends and answers in a
   shape 0.3.39 parses: unknown JSON fields and headers are ignored (cJSON; `on_header` matches
   by name). No panel route changes status codes that old firmware acts on.
2. **Every firmware change degrades to today's behaviour on an old box.** A missing field or
   header, or a 404/405 on a new route, means "the box is too old". The panel then uses today's
   path and never shows an error to the child.
3. **The owner routes and the PWA ship together** (the box serves the PWA). A service-worker
   cached PWA may run one release behind until reload, so owner routes accept the old id space
   for one release (§10).

| Contract change | New box + old firmware (0.3.39) | Old box + new firmware (box rollback) |
|---|---|---|
| `POST /send` raw ceiling raised to the capture cap (A3) | 35 s recordings stop being 413 — **fixed for old panels by the box alone** | 413 as today; new firmware's `EMPTY`/`FAILED` mapping still applies |
| `/waiting` `queue[].id` (A4/A6) | ignored | absent → positional `?at=` and positional local removal, exactly as today |
| `/next?id=` (A4/A6) | not sent; positional as today | old box ignores `id`, serves `at` — the panel sends both |
| `X-Jpanel-Duration-Ms` on `/next` (A6) | ignored | absent → `HEARD_MIN_MS` absolute threshold only |
| `POST /played {id, heard_ms}` (C4) | old body `{id}` → `heard_ms` NULL → PWA "Played" with no "stopped after" | pydantic ignores the extra field → acked as today |
| `POST /returned {id}` (A6) | never called — stops ack as today (owner sees "Played" for a 1 s stop until the panel updates) | 404 → the panel logs `reach_fail(REACH_ACK,"no-returned")` and leaves the row unplayed; the attempt is burned (today's worst case is one burned attempt; the cap is 5) |
| `offered_at` stamped by `/waiting` (C4) | stamped for old panels too — box-side | n/a |
| Subject identity + RLS on `app.subject_id` (C1) | invisible: panels only ever see message uuids | n/a — a box rollback below W2 is a **downgrade migration**; see §10.4 |
| `Cleared.kept_for`, `Threads` per-thread limit, `?before=` (C6/C9) | n/a (owner-only) | cached old PWA ignores new fields |

Each row is pinned. Backend tests post the exact 0.3.39 bodies and headers and assert today's
answers (`test_old_firmware_contract_*`). Firmware host tests run the core against a scripted
"old box" that sends no new fields and 404s new routes (`test_against_0_3_39_box_*`).

## 7. Firmware concurrency model (F3 introduces it; §8 fixes cite it)

Three tasks touch messaging state: **render** (`face_task` in `display.c`, core 1), **jpanel**
(`jpanel_task`, core 0) and **audio** (consumer of `s_play` and the ring). Today most of
`jpanel.c`'s fields are `volatile` and written by both render and jpanel. That is the root of
A1, A7 and A8. The rule after F3: **every shared field has exactly one writer task.** Cross-task
intent travels as (a) a monotonic counter, (b) a FreeRTOS queue, or (c) a seqlock-published
snapshot. The repo has precedent for this: the touch sampler uses "a real queue, not a
hand-rolled ring" (ROOM_ENDPOINT_PLAN 0.3.25).

Primitives are GCC `__atomic_*` builtins with explicit ordering. Xtensa LX7 under ESP-IDF
supports them, and host gcc compiles the same source. `volatile` alone does not order
cross-core accesses, and it is removed from these fields.

### 7.1 Stop: a generation counter, never a flag

- `static uint32_t s_stop_gen;` **Written only by render**, in `jpanel_stop()`:
  `__atomic_add_fetch(&s_stop_gen, 1, __ATOMIC_RELEASE)`, followed by `audio_stream_cancel()`
  (§7.4). `jpanel_stop` no longer writes `s_run` or `s_stopped`. `s_stopped` is deleted.
- Every command posted by render (`jpanel_play_at`, `jpanel_replay`) carries
  `gen = __atomic_load_n(&s_stop_gen, ACQUIRE)` taken **at the post**. A run started by that
  command stores `s_run_gen = cmd.gen`; a chained fetch reuses `s_run_gen`.
- `jpanel_running()` = `s_run_active && s_run_gen == load(s_stop_gen)`. `s_run_active` is
  written only by jpanel. A stop can therefore never be "undone" by the jpanel task: a stale run
  reads as not running the instant the counter moves, even before jpanel notices.
- **Claim-then-check** in `fetch_head`/`replay_head`. After `audio_stream_begin()` succeeds,
  jpanel sets `s_run_active`/`s_run_gen` and **then** re-loads `s_stop_gen`. If it moved, jpanel
  calls `audio_stream_abort()`, clears `s_run_active`, owes a `RETURNED` (§7.5) and returns 1.
  The same check runs in `stream_begin_waiting` at every 20 ms step, and in `pump` per chunk (it
  replaces `audio_stream_live()` as the "never" signal). This ordering closes the window between
  check and claim that a check-then-claim would leave: a stop landing after the check finds the
  stream already claimed, and the cancel tears it down.

### 7.2 The waiting list: a seqlock snapshot

- One struct `jpanel_view_t { int count, known; wait_one_t q[JPANEL_QUEUE_MAX] /* +id */;
  char sibling[32]; }`, published by jpanel through `s_view_seq` (`uint32_t`, odd while
  writing). Writer: `seq++` (RELEASE), write fields, `seq++` (RELEASE). Reader:
  `jpanel_snapshot(jpanel_view_t *out)` does load seq (ACQUIRE, retry if odd), memcpy, fence,
  re-load, and retries up to 4 times. On persistent contention it returns false, and render keeps
  last frame's view.
- Both writers that exist today, `do_poll` and `fetch_head`'s local removal, run on jpanel.
  `seed_sender` is **removed from the render side**: `jpanel_play_at` no longer writes `s_in_from`.
  Render already knows the selected entry's sender from its own snapshot (`sel_from_dad` in
  `display.c`), and the pending-play window is drawn from that.
- Render reads the view **once per frame**, into `ui_in_t` (F2). No other jpanel getter is read
  mid-frame, which removes the "a different answer on line 4084 than on line 3716" class.

### 7.3 Outcomes: an event queue, replacing `jpanel_state()`/`jpanel_clear()`

- `QueueHandle_t s_ev`, depth 8, jpanel → render. `jpanel_event_t { kind; reason; id[48]; from;
  from_dad; }` with kinds `SENT, NOBODY, EMPTY, FAILED, PLAY_STARTED, PLAY_ENDED{heard|stopped|
  returned|short|empty}`. Enqueue uses `xQueueSend(…, 0)`; overflow is counted (`ev_dropped`
  in telemetry), and at most two events are produced per command, so 8 is generous.
- **Busy is a token, not a state.** `s_inflight` (`uint8_t`): render takes it with
  `__atomic_compare_exchange_n(&s_inflight, 0→kind)` in `jpanel_send`/`jpanel_play_at`; jpanel
  releases it (store 0, RELEASE) at the end of the command. The handoff makes it single-writer
  per transition. This is the fix for **A8**: a chained fetch can no longer overwrite a send's
  `BUSY`. Chained fetches never take the token — they run inside jpanel's own loop — and
  `jpanel_send` still refuses while a fetch holds it.
- `ui_in_t` gains `jevent` (one event drained per frame; the rest are left for later frames, so
  no outcome is lost). `ui_frame`'s `switch (in->jstate)` becomes `switch (in->jevent.kind)`.
  `UI_ACT_JPANEL_CLEAR` is deleted.

### 7.4 Cancel: the ring keeps exactly two writers

`audio_stop()` today calls `audio_stream_abort()`, which runs `ring_reset()` from the **render**
task while jpanel is producing into the ring. That is a third party resetting an SPSC ring's
cursors (**A9**). After F3:

- `audio_stream_cancel()` (render) only stores `s_stream_cancel = true` (RELEASE) and moves
  `s_play_pos = s_play_len` for cues, as today.
- The audio task (consumer) sees the flag, stops output within one chunk (~20 ms), and stops
  advancing the read cursor.
- `audio_stream_live()` returns false while cancel is set, so `pump` returns 1. jpanel then
  calls `audio_stream_abort()`, which resets the ring and clears `s_stream_cancel` **on the
  producer's own task**.
- `audio_stream_begin()` refuses while the flag is set, so nothing new can start until the
  producer has finished cleaning up.

### 7.5 Acknowledgement outbox

jpanel holds one `ack_t { char id[48]; enum {PLAYED, RETURNED} kind; int heard_ms; uint8_t
tries; uint32_t next_at; }`. Only jpanel touches it.

- It is armed when a message ends: drained-and-verified → `PLAYED`; stopped with
  `consumed ≥ threshold` → `PLAYED`; stopped below the threshold, or cancelled before sound →
  `RETURNED`; short or unverified → nothing (today's rule, E5.1).
- Each loop pass, if `now ≥ next_at` (wrap-safe), the task sends the request. A 204 clears the
  outbox. A 404/410 also clears it (the row is gone). A 404 on `/returned` from an old box
  clears it with a `reach_fail`. Anything else backs off 1, 2, 4 … 30 s.
- **While an ack is owed, no automatic fetch starts and a run does not chain** (A4). A
  finger-asked fetch may still run; at worst the child hears a message twice, the bounded
  outcome JP accepts. A second ack arriving while one is owed is sent immediately after it. The
  depth is 2: one in flight plus one pending is the most a single-message pipeline can produce.

---

## 8. The fixes

Each entry: **cause** (verified) → **design** → **interactions** → **test**. File and function
names are as they will be after F1/F2 (`jpanel_core.c`, `ui.c`). The pre-refactor name is given
where it differs.

### A1 — a stop during the fetch window is undone (F3)

**Cause.** `fetch_head` sets `s_stopped = false; s_run = true` after `stream_begin_waiting`, so a
`jpanel_stop()` anywhere between the press and the head is erased. `CUE_STOP` (the exit's own
sound) occupies the speaker, so `stream_begin_waiting` waits it out and then starts anyway.
`audio_stream_begin` clears `s_stream_paused`, so a pause pressed before the first byte is
discarded while the disc shows "paused".

**Design.** §7.1 (generation, claim-then-check). For pause: `ui_state_t.pause_wanted` is set
when the transport's pause half is pressed while `in->jfetching && !in->stream_active`.
`ui_frame` emits `UI_ACT_PAUSE(1)` on the first frame `stream_active` rises with `pause_wanted`
set, and clears it on resume, on stop, or when `jfetching` falls without a stream. The disc
already draws paused from that state, so the picture becomes true.

**Interactions.** This is the basis for A6's refund (a cancel before sound → `RETURNED`) and for
B1 (a reply's stop is now real).

**Test.** `jpanel_tests.c: test_stop_at_every_interleave_point` scripts a 3-message queue, then
for each named yield point `P ∈ {pre_dequeue, wait_step, after_head, after_begin, per_chunk,
pre_ack, chain_gap}` injects `jpanel_stop()`. It asserts:
- samples written after the stop ≤ one chunk;
- `jpanel_running() == false` immediately, and stays false;
- no further `/next` request;
- an ack is `RETURNED` iff consumed < threshold, else `PLAYED`.

`ui_tests.c: test_pause_pressed_before_first_byte_holds` checks that the pause action arrives
on the stream-rise frame. `test_swipe_from_big_notice_cancels_the_play` checks that the
selection moves and no audio is emitted.

### A2 — a failed chained fetch leaves the run alive forever (F3)

**Cause.** `jpanel_task` chains `do_fetch(true, 0)` inside the `s_owed` block. Every
`!p.began` path (`status < 0`, non-200, 204, speaker busy) does `goto done` without clearing
`s_run`, and `run_controls_up()` then hides the notice for good.

**Design.** `jpanel_core_fetch()` has a single exit. If `!began`, it clears `s_run_active` and
emits `PLAY_ENDED{reason}`, which shows the notice again and asks for a poll soon. The 204 case
also refreshes the view.

**Test.** `test_chain_fetch_failure_ends_the_run` is parameterised over {link error, 500, 204,
speaker busy > `STREAM_WAIT_MAX_MS`}. Each case asserts the run ends, the notice reappears, and
a poll is queued.

### A3 — the box refuses what the panel records (W1 box, F3 panel)

**Cause.** `audio.c` `CAPTURE_MAX_MS` is 35000. `api/jpanel.py` checks `len(audio) >
MAX_MESSAGE_BYTES` (30 s) **before** `_trim_to_speech`, so a recording held open by a TV and
closed at the cap gets 413 → `JPANEL_FAILED` → OOPS, and is lost. The 400 "nothing said" also
maps to `FAILED`.

**Design.**
- Box (W1): `send` checks the raw body against `PANEL_AUDIO_MAX` (35 s; `endpoint.py`, already
  the `/converse` cap and the size `panel_ws.py` budgets) and not against `MAX_MESSAGE_BYTES`.
  Policy: **the box accepts every recording a panel can physically capture**. `MAX_MESSAGE_MS`
  remains the PWA recorder's self-stop (`voiceMessage.ts`) and the owner route's ceiling (C10).
- Panel (F3): `do_send_once` maps 400 → `EMPTY` (with `CUE_STOP`, "dropped"), 409 → `NOBODY`,
  413 → `FAILED("too-long")` (unreachable on a current box, but reported), other → `FAILED`.
- Stale comments: `audio.c` "Ten seconds", JP "Capped at 20 s".

**Test.**
- `test_send_accepts_every_length_the_panel_can_capture` reads `CAPTURE_MAX_MS` out of
  `firmware/main/audio.c` and posts that many noisy bytes plus one second: the result is 200.
- `test_capture_cap_pinned_across_firmware_and_box` asserts
  `PANEL_AUDIO_MAX == CAPTURE_MAX_MS * 32`, that `panel_ws.MAX_BODY > PANEL_AUDIO_MAX`, and that
  `voiceMessage.MAX_MESSAGE_MS == MAX_MESSAGE_MS`.
- `test_old_firmware_contract_send_35s` covers the skew row.
- Host: `test_nothing_said_is_dropped_not_failed`.

### A4 — a lost acknowledgement replays and corrupts the local list (W3, F3)

**Cause.** `do_played` sends once and ignores the outcome. The chain then fetches the head, the
box returns the same unplayed message again, and `fetch_head`'s positional removal deletes the
local `s_wait[0]`, which is now a different message.

**Design.** The ack outbox gates the chain (§7.5). `/waiting` carries `queue[].id`; `/next`
takes `?id=` (served only if it is still deliverable, else 204 — never a different message).
`fetch_head` removes the view entry whose id equals `X-Jpanel-Id`, or none. Positional removal
remains only when the box sent no ids. The poll body buffer grows from 1024 to 1536 bytes
(static, not stack): 8 × `"id":"<36>",` adds 360 bytes to the 667-byte worst case.
`test_the_panel_can_hold_the_longest_poll_the_box_will_send` is updated to include ids and
recomputes from both sides.

**Test.**
- `test_lost_ack_is_retried_and_never_replays`: `/played` fails 3×, then 204. Assert no `/next`
  while owed, exactly one PLAYED request succeeds, and the local view still holds B.
- Backend: `test_waiting_queue_carries_ids`, `test_next_by_id_serves_that_row_or_204`,
  `test_old_firmware_contract_next_at`.

### A5 — the capture buffer is overwritten mid-upload (F3, F4)

**Cause.** `jpanel_send` borrows `audio.c`'s capture buffer "until the next
`audio_capture_open()`" (`jpanel.h`). `start_send_recording` refuses while BUSY, but
`start_listening` (the wake phrase or the pet icon) and the follow-up listen in `ui_tap` do not.
Opening resets `s_cap_used` and overwrites the PCM while it is being hashed or uploaded.

**Design.**
- A **lease** in `audio.c`: `static uint8_t s_cap_lease` (0 free, 1 jpanel, 2 talk).
  `audio_capture_open()` returns `bool` and fails while leased.
- `jpanel_send()` takes lease 1 (CAS 0→1) before posting; the jpanel task releases it at the end
  of `do_send`, whatever the outcome. `talk_send` takes lease 2 the same way: the same hazard
  exists for a conversation upload.
- `ui_in_t.capture_leased`. In `ui.c`, `start_listening`/`start_record` refuse with `CUE_STOP`
  while leased, and the follow-up open is skipped.
- The executor checks `audio_capture_open()`'s result and calls `ui_capture_result(st, false)`
  to return to idle if it races.

**Interactions.** B5 shows "sending", so a refused pet request has a visible reason.

**Test.** `jpanel_tests: test_capture_cannot_open_while_send_holds_buffer` asserts the digest of
the uploaded bytes equals the digest of the recorded bytes when a listen is attempted at every
yield point. `ui_tests: test_wake_phrase_refused_during_upload`,
`test_follow_up_listen_skipped_during_upload`.

### A6 — a stop in the preroll acknowledges an unheard message; "next" skips (W3, F3, F4)

**Cause.** The stop path keeps `s_owed`, so a stop 0.2 s into the 1.5 s preroll retires the
message as heard. The face's `select_step(+1)` runs against `waiting`, which has already lost the
playing message. `ui_frame` also resets `sel = 0` on every count change, so from A the step lands
on C (§3.1: dead entirely with two).

**Design.**
- `audio_stream_consumed_ms()` reads the ring's monotonic read cursor (since `ring_reset`)
  divided by 16, **excluding** silence written while paused. It is a 32-bit single-writer value
  owned by the audio task, so an aligned load is atomic.
- `HEARD_MIN_MS = 2000`. A message is "heard" if `consumed ≥ min(HEARD_MIN_MS, 0.8 ×
  duration)`, where `duration` comes from the new `X-Jpanel-Duration-Ms` header (absent on an
  old box → `HEARD_MIN_MS` alone). Below that → `RETURNED`.
- Box (W3): `POST /returned {id}` runs `UPDATE … SET deliveries = GREATEST(deliveries-1, 0)
  WHERE id=… AND played_at IS NULL` under the panel RLS UPDATE policy, so only the recipient
  panel can call it. `/played` accepts optional `heard_ms`.
- UI (F4): during playback the selection domain is `[playing] + remaining`. The face or a swipe
  steps to `remaining[0]` and then onward. The step **skips to** that message (stop, then
  `PEND_PLAY` by id) rather than only stopping (B-UX). `waiting_shown` changes caused by the
  playing message leaving the queue (an id match in `PLAY_STARTED`) do not reset `sel`.
  `target_live(FACE)` uses `remaining ≥ 1` during playback.

**Interactions.** A stop below the threshold makes `PLAY_ENDED{returned}` show the notice again.
That is correct, because the message is genuinely unheard.

**Test.** `jpanel_tests: test_stop_in_preroll_returns_not_acks`,
`test_stop_after_threshold_acks_with_heard_ms`, `test_returned_404_on_old_box_is_logged_once`.
`ui_tests: test_face_during_playback_never_skips` (2, 3 and 8 messages),
`test_swipe_during_playback_skips_to_next`. Backend: `test_returned_refunds_only_own_unplayed`
(integration, including the sibling case).

### A7 — torn sender name or face (F3)

**Cause.** `do_poll` writes `s_wait[]` before lowering `s_wait_known`. That breaks the file's own
ordering rule, and the render task reads the entries live.

**Design.** §7.2.

**Test.** `test_snapshot_never_torn_at_any_interleave`: the fake io yields between every field
write, and the reader asserts each snapshot equals one fully published poll.

### B0 — `display.c` duplicates `ui.c` (F2, before every B fix)

**Cause.** Confirmed. `face_task` calls only `ui_tap_target()` (through `tap_target_now`). The
pending play, the jstate switch, the hold, the notice clock and the draw decisions all run from
`display.c` statics that mirror `ui_state_t` (`s_talk`, `s_pending`, `s_repeat_until`, `s_sel`,
`s_popup_box`, …). The ~440 host checks therefore test a machine the panel does not run.

**Design.** §9.2. Every B fix lands in `ui.c` afterwards, so the host suite tests what ships.

### B1 — reply during playback makes a recording the child cannot send (F4)

**Cause.** `PEND_REPLY` starts the recording. In the same `ui_frame` pass, `case UI_JP_PLAYING`
sees `!audio_playing` and sets `repeat_until`. `jpanel.c`'s stop path also reports `PLAYING`.
The pair draws with no talk check, and `UI_TAP_ORDER` lists `PAIR` before `CONFIRM`, so the tick
lands on "reply" → `ui_start_record` refuses → OOPS, while "again" replays into the open
microphone.

**Design.**
- The pair is armed by `PLAY_ENDED{heard|stopped}` only when `st->talk == UI_TALK_IDLE &&
  st->pending == UI_PEND_NONE`. `PLAY_ENDED` after a reply's stop never arms it.
- `target_live(PAIR)` and `ov->pair` require `talk == IDLE`.
- `UI_TAP_ORDER` becomes `GRID, EXIT, CONFIRM, POPUP, PAIR, PET`. CONFIRM consumes every press
  while recording, so it must outrank everything except the modal grid and the exit (the exit is
  not live during a recording).

**Test.** `test_reply_during_playback_opens_a_working_recording` replays the exact reported
sequence and asserts that the tick sends, there is no OOPS, and no pair is drawn while
recording. `test_tap_order_confirm_outranks_pair` walks the table.

### B2 — a waiting message or the pair blocks starting a message (F4)

**Cause.** The hold gate is `!menu_up`, and `menu_up` includes `popup_box[0] >= 0` (the badge
too) and `repeat_until`. Neither expires on its own, so with mail waiting a child cannot reach
"who?" by holding.

**Design.** The 0.3.35 rule restated precisely: **a hold is a menu press only if the press
landed on a menu target.** `ui_frame` opens the grid when `ui_tap_target(down point) ==
UI_TARGET_PET` (computed once at the down edge into `st->down_target`), instead of `!ui_menu_up`.
Opening the grid clears the pair. The badge stays, under the modal grid. `ui_menu_up` stays only
for its other readers.

**Test.** `test_hold_on_pet_opens_grid_while_badge_up`, `…while_pair_up`; the existing
`test_a_long_press_on_a_menu_is_just_a_press` and
`test_a_hold_on_the_pet_opens_the_grid_at_every_free_tap_count` stay green unchanged (the 0.3.35
guarantee).

### B3 — the corner shows one sender and plays another (F4)

**Cause.** The badge (`draw_popup_badge`, the selected waiting sender) and the pair
(`draw_sender_face(jpanel_in_from())`, the *previous* sender) share the top-left quadrant.
`POPUP` precedes `PAIR` in the tap order, so the visible face (painted last) is the pair's while
the press plays the badge's. The comment says the opposite order.

**Design.** **One owner for the quadrant: a waiting message.** The pair is live and drawn only
while `in->waiting == 0`. A rise in `waiting` clears `repeat_until` (the behaviour the
`s_repeat_until` comment already promises). This also settles JP's open question ("Two opposite
meanings on one pixel… the owner's to settle"): the face only ever changes the *waiting*
selection.

**Test.** `test_quadrant_face_is_what_a_press_plays` covers the arrival-during-pair and
stopped-run-with-remainder cases. It asserts that the drawn face's `from_dad` equals the sender
of whatever `ui_tap` would play, at every frame.

### B4 — cues dropped while the speaker is busy; a notice that never gets big (F4)

**Cause.** `audio_cue` returns silently while `s_play` is busy (pet reply, poke, another cue).
`CUE_MESSAGE` is emitted once on the count-rise frame. `SENT`/`FAILED` are cleared regardless.
HEARD followed by OOPS in one frame drops the OOPS. `popup_since` starts at arrival even while
the notice is hidden (talk not idle), so a pet turn longer than 15 s means the big box is never
seen.

**Design.**
- A **cue queue** as a pure module in `cue.c` (host-built): `cue_queue_push(q, cue, now)`,
  `cue_queue_next(q, now)`. Depth 4, FIFO, per-class expiry: `CUE_MESSAGE` 20 s, outcome cues
  (SENT/OOPS/NOBODY/STOP) 3 s, acknowledgements (HEARD/BLIP/PLAY) 400 ms (late feedback for a
  press is worse than none).
- `audio_cue()` becomes push. The audio task pops when `s_play` is free and no stream is
  primed. `talk.c`'s `audio_play` (a reply) still outranks queued cues.
- Notice clock: `popup_since` is stamped on the first frame `notice_up` is true for the current
  wait (`st->notice_seen` latch). The "second message does not restore the big box" rule is
  preserved.

**Test.** `tests.c: test_cue_queue_order_expiry_and_overflow`. `ui_tests:
test_message_during_pet_reply_gets_chime_and_big_box`, `test_heard_then_oops_both_sound`,
`test_sent_cue_survives_a_busy_speaker`.

### B5 — nothing on the glass while sending (F4 behaviour, F5 picture)

**Cause.** `JPANEL_BUSY` draws nothing (10–20 s). `notice_up` excludes BUSY, so a send also hides
arriving messages. Retry is refused with `CUE_STOP` from the menu and, misleadingly, `CUE_HEARD`
from voice (see B8).

**Design.** F4: `notice_up` excludes only an in-flight **fetch** (`in->jfetching`), not a send.
F5: `ov->sending` drives a sending indicator (blue dot plus the recipient face, travelling, per
the chosen mock) until `SENT/NOBODY/EMPTY/FAILED`. Retry while sending stays refused (lease, A5),
and the indicator is the reason the child can see.

**Test.** `test_notice_shown_during_send`, `test_sending_overlay_until_outcome`.

### B8 — silent outcomes for a deliberate recording (F4)

**Cause.** `RECORDING` with no speech for `RECORD_LEAD_MS` drops the recording without a sound
(`ui_frame`, `nothing`), while a tick into the same silence plays OOPS. A refused `VOCAB_SEND`
keeps `heard_cue = CUE_HEARD`. `ui.h` requires refusals to match the menu's `CUE_STOP`.

**Design.** The silent drop emits `CUE_STOP` (the cross's sound: "dropped, nothing went"). The
wake-by-name listen keeps its free silent drop: an accidental wake must cost nothing, per JP.
`ui_start_record` refusal → `CUE_STOP` on every path, voice included (`display.c` `VOCAB_SEND`
moves to `ui_voice_send`, which owns the cue).

**Test.** `test_silent_recording_says_dropped`, `test_refused_tell_sister_sounds_refusal`.

### B10 — the remaining low-severity items (F4)

- **A tap on the notice while the pet talks is dropped after 8 s.** `PENDING_MS` (8000) is
  shorter than the longest reply (`PLAY_BUF_MS` 10000). It becomes `PENDING_MS = PLAY_BUF_MS +
  2000`, pinned by `_Static_assert` in `ui.c` against a mirrored constant (and by a backend
  source test against `audio.c`). On expiry the press emits `CUE_OOPS`, and the notice is
  re-armed (`popup_box` restored). Test: `test_press_during_pet_reply_is_served`,
  `test_pending_expiry_is_audible`.
- **The follow-up listen and a pending play start in the same frame.** The follow-up in `ui_tap`
  is skipped when `st->pending != NONE` or a press was taken this frame: a finger outranks an
  auto-continue. Test: `test_follow_up_yields_to_pending_play`.
- **"stop stop" muted while recording.** Not fixed (§3.1).
- **uint32 wrap.** All deadline comparisons in `ui.c` go through `static inline bool
  due(uint32_t now, uint32_t t) { return (int32_t)(now - t) >= 0; }`. The "0 means off" sentinels
  get a separate `bool` (`sendto_up`, `pending`), so a deadline that computes to 0 is not
  mistaken for "off". Test: `test_deadlines_survive_u32_wrap` runs the whole arrival → play →
  reply scenario with `now` starting at `0xFFFFF000`.

### B-UX — the gaps, one by one

| Gap | Decision | Wave / test |
|---|---|---|
| X takes two presses during playback | **One press**: the exit stops and the pair is not armed (`PLAY_ENDED{stopped}` after an exit never arms it). The second half of `test_the_top_right_corner_ends_a_message_while_it_plays` ("Once it has finished, the pair comes up and the same corner works") is rewritten deliberately, and its comment explains why. | F4 `test_exit_leaves_in_one_press` |
| Every failure is the same OOPS | Four outcomes, four sounds: SENT; NOBODY (new `CUE_NOBODY`, a falling two-note "no one there"); EMPTY (`CUE_STOP`); FAILED (`CUE_OOPS`). The cue contour is chosen with the owner in F5's mock set (it plays in the browser via WebAudio). | F5 `tests.c: test_cue_nobody_contour_distinct` |
| No sending sound or picture; "sent" is 300 ms | Sending overlay (B5). `CUE_SENT` is lengthened to the "sent" contour the owner picks from the mock. | F5 |
| Text instructions for pre-readers ("TAP TO HEAR", "SENT YOU ONE", "- SWIPE -", "1/2") | Replaced by icons and pips (dots, one per message, the playing one filled), per the chosen mock. Names stay, for the adult. | F5 (GUI gate) |
| Badge has no count, animation or reminder | Pips on the badge plus a slow pulse while waiting. **No reminder sound**: JP's "a message that chirped whenever somebody walked past would be the panel nagging" stands. | F5 |
| "1/2" counts the remaining queue | The position is `index in [played-this-run + playing + remaining]`, derived from `PLAY_STARTED` ids (behaviour in F4, pips in F5). | F4 `test_run_position_counts_the_playing_message` |
| Swipe during playback stops rather than skips | Skips (A6). | F4 |
| Messages arriving mid-turn or mid-recording miss the big box | B4 notice clock. | F4 |
| The pair never expires, survives sleep, blocks actions | §4: kept non-expiring; cleared on wake from dark; no longer blocks (B2/B3). | F4 `test_pair_cleared_on_dark_wake_not_dim` |
| No hint how to stop recording | The tick and cross get glyphs a pre-reader knows (from the mock), and a hush ring shrinks during the 1.8 s auto-send countdown so "it will send when you stop talking" is visible. | F5 |

### C1 — re-flash orphans messages (W2)

§10 in full.

### C2 — a ghost chip; a send to nowhere answers 201 (W1, W2)

**Cause.** `threadPanels` adds a chip for every `device_id` in pet chats, including dead
principal ids. `send_text` never checks `to_device` (`send_audio` does), so it renders TTS,
inserts the row, and returns 201. The PWA's `setThreads(map…)` finds no thread: the draft
clears, no bubble appears, and no error is shown.

**Design.** W1: `send_text` resolves `to_device` against the roster **before** TTS and returns
404 otherwise. The PWA's `send`/`stopRecording` refetch when no thread matches and keep the draft
on 404, with "That panel isn't enrolled any more". W2 removes the cause (one id space;
`pet_chats` keyed by subject).

**Test.** `test_send_text_unknown_panel_404_before_tts` (asserts that the TTS fake was never
called); vitest `send to a vanished panel keeps the draft and says so`.

### C3 — "Couldn't be delivered" is a dead end (W3, P1)

**Design.** `POST /messages/{id}/retry` (owner): only if `played_at IS NULL AND deliveries ≥
cap`. It sets `deliveries = 0` and nudges the recipient. `DELETE /messages/{id}` (owner): only
for rows the owner sent, or rows that are undelivered. A child's still-deliverable message to her
sister cannot be deleted one at a time either (§4). Both guard against non-uuid ids (404). P1:
buttons on an undelivered bubble.

**Test.** `test_retry_resets_deliveries_and_nudges`, `test_retry_refused_when_deliverable`,
`test_discard_only_owner_or_undelivered`; vitest for the buttons.

### C4 — "Heard" is not what happened (W3, F3, P1)

**Design.** Two new nullable columns in the W3 migration (owner-written or recipient-written
under the existing policies):
- `heard_ms integer`, set by `/played` when present (A6);
- `offered_at timestamptz`, stamped by `GET /waiting` for rows it returns whose `offered_at IS
  NULL` (`UPDATE … WHERE id = ANY(:ids) AND offered_at IS NULL`). One cheap write the first time
  a panel is told about a message; it is a no-op afterwards.

`Message` gains `heard_ms`, `offered_at`. The PWA status copy (P1) follows E3.3. "Played to an
empty room" cannot be detected: no presence sensor exists, and inferring absence from the
accelerometer would be a guess presented as fact. The copy therefore says **Played**, not
**Heard**.

**Test.** `test_played_records_heard_ms`, `test_old_firmware_played_body_leaves_heard_ms_null`,
`test_waiting_stamps_offered_once`, `test_offered_at_rls_own_rows_only` (integration, sibling
case); vitest status matrix over (offered, deliveries, played, heard_ms, undelivered).

### C5 — a twin-to-twin message lives in one thread and has no status (W1, P1)

**Design.** W1: `messages()` appends a `between` row to **both** panels' threads (the sender's
and the recipient's). The unplayed count is unchanged: still only to-owner. P1: the status chip
shows for `direction != "in"` (out and between), worded with the recipient's name.

**Test.** `test_between_message_in_both_threads`; vitest.

### C6 — the badge can count rows the screen never shows (W1)

**Cause.** The query is `ORDER BY created_at DESC LIMIT 100` across all panels, while the badge
is uncapped. Read-marking only sees rendered bubbles.

**Design.** `limit` becomes **per thread**: `row_number() OVER (PARTITION BY thread_key ORDER BY
created_at DESC) ≤ :lim`, default 100. `thread_key` is the panel end, and a between row counts
in both partitions. It is `UNION`-ed with **every** unplayed panel→owner row. `?before=<iso>&
device=` pages older rows for one thread (P1 adds "Load earlier").

**Test.** `test_every_unplayed_owner_row_is_returned` (150 noisy rows on one panel, 3 unplayed
old ones on the other), `test_per_thread_limit_keeps_quiet_panel_visible`.

### C7 — audio on iOS; 500 on bad ids (W1)

**Design.** `message_audio` honours a single `Range: bytes=a-b|a-|-n` with 206 and
`Content-Range`, sends `Accept-Ranges: bytes` on 200, and answers 416 when unsatisfiable.
`uuid.UUID()` guards on `message_audio`, `mark_read`, and C3's routes.

**Test.** `test_audio_range_206`, `test_audio_range_416`, `test_non_uuid_is_404` (×4 routes).

### C8 — 600 characters, an unreadable 422, and the TTS timeout (W1, P1)

**Design.** `errorDetail` (`api/client.ts`) joins `detail[].msg` when `detail` is a list (W1).
The composer gets `maxLength={600}` (W1, invisible). Its counter and hint are P1 (visual).
`send_text`'s TTS client timeout becomes 60 s: 600 characters of Kokoro runs about 50 s of
speech. The 600-character cap is **kept**: E1.1's policy is about what a panel records, and
whether a typed message to a four-year-old should be shorter is not a fault (JP §5 leaves it
open; §12).

**Test.** `client.test: errorDetail flattens a pydantic list`; `test_send_text_uses_60s_tts_timeout`.

### C9 — "kept N Quorra hasn't heard" names the wrong child (W1)

**Design.** `Cleared` gains `kept_for: list[{name, count}]`, grouped by **recipient**. The PWA
renders it, and an old PWA ignores it.

**Test.** `test_clear_kept_names_recipient` (between row kept under the sister's name); vitest.

### C10 — owner audio silently truncated (W1)

**Design.** `send_audio` refuses a body over `MAX_MESSAGE_BYTES` with 413, matching the panel
route's refuse-don't-truncate policy. The PWA recorder already stops at `MAX_MESSAGE_MS`, so in
practice only a client bug reaches it.

**Test.** `test_owner_audio_over_cap_is_413`.

### C11 — a stale poll overwrites a fresh send (W1)

**Design.** In `ConversationTab`, `const seq = useRef(0)` and `const epoch = useRef(0)`.
`refresh` captures `mySeq = ++seq.current; myEpoch = epoch.current`, and applies its result only
if `mySeq === seq.current && myEpoch === epoch.current`. Every local mutation (send, voice send,
clear) bumps `epoch` and then calls `refresh()`, so the server's row is what remains.

**Test.** vitest with deferred promises: a poll starts → send resolves → the poll resolves with
the old list → the bubble is still present.

### C-UX — the gaps, one by one

| Gap | Decision | Wave / test |
|---|---|---|
| No notification to Dad | Launcher badge (`GET /jpanel/summary {unplayed}`, polled like the Review and Tasks tiles). Push is **deferred** (§5, §12). | W3 route; P1 `Launcher.test` |
| "Not heard yet" ambiguity | `offered_at`/`deliveries` statuses (C4). | P1 |
| Composer hint and limit | "Waits on her panel until she taps it", plus a counter from 500 characters. | P1 |
| Read-marking at 60% on screen = phone left open | Dwell: visible ≥ 1.5 s **and** `document.hasFocus()`, with the timer reset on scroll-out. | P1 `test_read_needs_dwell_and_focus` |
| Read-marking starts the 30-day sweep | Policy kept (§4). The thread footer states "Played messages are kept 30 days." | P1 |
| Renames rewrite past names | **Not fixed.** Names are the subject's current name, which is right when a rename is a correction (the common case: JP's rename exists to fix "the other one"). Snapshotting would freeze typos into history. Loss: a panel deliberately renamed shows its new name on old messages. | — |
| Two clears for one thread | Kept (§4); the confirm copy names what stays. | P1 |

---

## 9. Structural work (F1, F2) — testability before fixes

### 9.1 F1 — the messaging engine on the host

1. `link_types.h`: `link_req_t`, `link_res_t` and the callback typedefs, moved out of `link.h`.
   `link.h` includes `cfg.h` → `esp_err.h` and cannot build on the host. `link.h` includes the
   new header.
2. `jpanel_core.{c,h}`: everything in `jpanel.c` that decides — the send/poll/fetch/replay/ack
   flows, the pull/pump/verify logic, the view publication, the run — written against
   `jpanel_io_t`:
   - `request` (a `link_types` request);
   - `stream_begin/write/live/end/abort/playing/consumed_ms`;
   - `sha256_start/update/finish`;
   - `now_ms`, `sleep_ms`;
   - `post_event`;
   - `yield(jpanel_point_t)` — a no-op on device, the interleave hook on host.

   `jpanel.c` keeps the FreeRTOS task, the queue, cJSON parsing of `/waiting` into a
   `jpanel_waiting_t` (cJSON is an ESP-IDF component; the field names stay pinned by the backend
   contract tests), `mbedtls`, and the real io table.
3. `firmware/host/jpanel_tests.c` → `run_jpanel_tests`, added to `make test`. The fake world has:
   - a scripted box (per-request status, headers, body chunks, failure points, and a `v0_3_39`
     mode that 404s new routes and omits new fields);
   - a fake audio built on the real `ring.c`, consumed by a step function;
   - a fake clock;
   - a small public-domain SHA-256 in `firmware/host/` (test-only, ~150 lines; no tool or
     package, so `dev-setup.sh` is unchanged) so digests match the box's;
   - `yield` hooks that let a test inject render-side calls at any named point.
4. **F1 is behaviour-preserving.** Its tests characterise 0.3.39 as it is. Where 0.3.39 is
   wrong (A1, A2, A4, A7), the characterisation test asserts today's behaviour, names the
   finding, and is inverted in F3. A reviewer can see each bug reproduced before it is fixed.
5. The backend tests that grep `firmware/main/jpanel.c` (`test_jpanel_api.py`, e.g.
   `test_the_panel_facing_routes_are_where_the_firmware_looks`,
   `test_every_way_of_starting_a_message_waits_for_the_speaker`) are retargeted to
   `jpanel_core.c` in the same PR. `firmware/main/**` is already in the backend job's path
   filter, so the backend CI job runs.

**Limits, stated.** Host tests prove logic under every **interleaving the hooks name**. They do
not prove memory ordering on the dual-core S3. The `__atomic` orderings in §7 are justified in
comments and reviewed; on-device, telemetry carries `ev_dropped`, `ack_owed`, `ack_retries`,
`returned`, `cap_lease_refusals` and `snapshot_retries`, so a fault shows on the debug API and
the Ops fleet card without a cable (CLAUDE.md #10).

### 9.2 F2 — `display.c` drives `ui.c` (B0)

1. **Audit first** (it lands in the PR description and as `firmware/host/README.md` "Divergences
   resolved"): every behaviour in which the `display.c` shipping path differs from `ui.c`. Each
   one is resolved to **one** answer, with a test. Known so far:
   - the exit's `notice_offered` guard;
   - `menu_up`'s operands;
   - the pending-reply settle condition;
   - the silence-drop cue;
   - `VOCAB_*` paths that bypass `ui_start_*`.

   Default resolution: `ui.c`'s answer (it is the tested one). A `display.c` behaviour that is
   adopted instead is moved into `ui.c` with a test.
2. `face_task` builds one `ui_in_t` per frame from the jpanel view (§7.2), audio, the touch
   queue, speech, gesture and the button. It calls `ui_tap` → `gesture_poll` → `ui_frame`, then
   `exec_actions(&out)`, a switch over `UI_ACT_*` mapping each action to its peripheral call.
   `ui_overlay` runs inside the draw gate. Draw functions read only `ui_overlay_t` plus the
   view.
3. Executor feedback has one form: `ui_*_result(st, ok, now)`. `ui_talk_send_result` exists;
   `ui_capture_result` and `ui_jpanel_send_result` are added.
4. Delete every `display.c` static mirrored in `ui_state_t`. `make -C firmware/host
   check_no_dup` (in `make test`) fails if `display.c` declares any of `s_talk s_pending
   s_pending_until s_repeat_until s_sendto_until s_sel s_popup_box s_popup_since s_rec_*
   s_listen_* s_follow_*`.
5. **F2 is behaviour-preserving apart from the audited resolutions.** The owner verifies it on a
   panel through the normal OTA. The PR carries a ten-step on-glass checklist the owner runs from
   the PWA (send, receive, replay, reply, exit, standby).

---

## 10. Data migrations

### 10.1 Identity migration (W2) — messages belong to the panel, not its key

**Cause (C1).** `DevicesRepo.reflash` keeps the **subject** and revokes and mints **principals**.
`jpanel_message.sender_device/recipient_device` and `pet_turn.device_id` store the principal id,
and RLS matches `app.principal_id`. After a re-flash, unplayed mail sits addressed to a revoked
key: never served (`/waiting`/`/next` filter on the new id), never swept (unplayed), invisible
to the owner (`messages()` drops rows whose id is not in the live roster), never clearable.

**Canonical subject.** Before 0212, every flash made its own subject, and several subjects can
share one `(display_name, device_role)`. `reflash` already resolves this as "oldest subject for
that name and role". The migration makes it true in the data, in one transaction:

1. `canon(s)` = the oldest subject with the same `display_name` and `device_role = 'jpet'`.
2. `UPDATE app.principals SET subject_id = canon` for every `device_key` whose subject is
   non-canonical (live or revoked). The `endpoint_panel` row moves to `canon` if `canon` has
   none (newest `updated_at` wins); the others are deleted (their settings were already
   unreachable, because `panel_context` pins the subject).
3. Backfill, for each of `jpanel_message.sender_device`, `jpanel_message.recipient_device` and
   `pet_turn.device_id`: `SET col = p.subject_id::text FROM app.principals p WHERE col =
   p.id::text`. Values that match no principal are left as-is and counted in the migration log.
4. RLS: drop and recreate `jpanel_message_panel_read/_send/_played` and `pet_turn_panel_write`
   with `current_setting('app.subject_id', true)` in place of `app.principal_id`, still requiring
   `principal_kind = 'device_key'`. Owner policies are untouched. The data step runs with the
   same privileges as 0213's.
5. Index `jpanel_message_waiting` is unchanged (same column, new values).

**Code (same PR).**
- Every panel route uses `panel_context(principal.id, principal.subject_id)` (`db/session.py`)
  instead of `ctx_for`, and refuses (403, logged) a `device_key` with no subject.
- Inserts write `principal.subject_id`.
- `_panel_names` becomes the **subject roster**: canonical `jpet` subjects with ≥ 1 live key,
  `subject_id → display_name`. `DISTINCT ON (label)` and its "two panels called Quorra"
  collision are retired.
- `nudge.remember/fire` key on the subject.
- `rename_panel`, `send_text`, `send_audio`, `clear_history`, `clear_pet_chats` and C3's routes
  take the subject id. **For one release** they also accept a principal id and map it to its
  canonical subject, so a cached PWA does not 404.
- `messages()` lists a thread for every subject referenced by a row, flagged `retired: true`
  when it has no live key, so no row is silently dropped (E4.2). Rows whose ids matched no
  principal go under one "Unknown panel" thread, flagged retired and clearable.

**Security posture.** Any live key of a subject reads that subject's inbox, which is exactly
"the panel" and nothing wider. A revoked key reads nothing, because auth refuses it before RLS.
A sibling's subject differs, so the sibling case is unchanged. Per PROCESS.md this wave touches
RLS and principal scope, so it is an owner escalation, and its wave review includes a red-team
pass.

### 10.2 Contract columns (W3)

`ALTER TABLE app.jpanel_message ADD COLUMN heard_ms integer, ADD COLUMN offered_at
timestamptz`. No backfill: NULL is the honest "unknown" for history.

### 10.3 RLS isolation tests (CLAUDE.md #3)

There are no new tables. Every changed policy is re-proved under the exact context a panel runs
as, against real Postgres:

- `test_jpanel_message_rls.py` (extended to the subject GUC): self reads own inbox and sent
  rows; **sibling cannot read, update, `/returned` or stamp `offered_at`** on the other's rows; a
  panel cannot insert as its sibling; a second live key on the same subject reads the same inbox.
- `test_pet_turn_rls.py` (extended): insert pinned to own subject; no panel SELECT; sibling
  insert refused.
- `test_jpanel_identity_migration_pg.py`:
  - seeds the pre-W2 shape (3 subjects for "Quorra", 13 principals, messages addressed to a
    revoked key, a between row, pet turns, an orphan id);
  - runs the upgrade;
  - asserts every row maps to the canonical subject, the orphan is counted and listed, the panel
    on its newest key reads its old unplayed mail through `/waiting` and `/next`, `endpoint_panel`
    is preserved, and the owner thread count equals the physical panel count.
- `test_jpanel_contract_columns_pg.py`: `heard_ms`/`offered_at` written only through the
  recipient's policy.

### 10.4 Downgrade

The W2 downgrade restores principal-keyed values from a mapping table the upgrade writes
(`app.jpanel_identity_map(old_principal text, subject text)`, kept for one release and dropped
by a later migration), and restores the old policies. A box rolled back through Ops → Update
therefore keeps delivering. Messages created **after** W2 map to the subject's newest live key.
The W3 downgrade drops the two columns.

---

## 11. Waves

One PR per wave (PROCESS.md). Each lists its dependencies and the CI jobs it touches. "Local
gates" means the package's **whole** gate set, run from the package directory (CLAUDE.md #11):

- `backend`: `ruff check .`, `ruff format --check .`, `pyright`, `pytest` (100 columns;
  testcontainers integration runs in CI only);
- `frontend`: `npm run lint`, `npm run typecheck`, `npm run test`, `npm run build`;
- `firmware`: `make -C firmware/host test` locally. The CI firmware job also rebuilds with ESP-IDF
  and byte-compares `firmware/dist/`, so every firmware wave bumps `firmware/version.txt` and
  commits a rebuilt `dist/` (`scripts/firmware-setup.sh` once, `idf.py build`,
  `scripts/firmware-dist.sh`, with the bootloader byte-identical as `firmware/README.md`
  requires).

Every wave runs `scripts/docs-freshness.sh` and reconciles docs (CLAUDE.md #9):
- this plan's header and body markers, and `Last verified`;
- JP's §3b contract table and its open "face collision" item;
- `firmware/README.md` / `firmware/host/README.md` when firmware changes;
- the `ROADMAP.md` and `plans/README.md` rows.

No wave adds a dependency or a setup step. If one turns out to need one, `scripts/dev-setup.sh`
changes in that PR (CLAUDE.md #8). No operator step is shell-only (CLAUDE.md #10):
- migrations run in Ops → Update;
- firmware reaches panels by OTA from the box;
- verification uses the PWA, the Ops fleet card, and the debug API telemetry fields named in
  §9.1.

| Wave | Contents | Depends on | CI jobs |
|---|---|---|---|
| **W1** — box and PWA, invisible fixes | A3 (box), C2 (validation + PWA refetch), C5 (both threads), C6, C7, C8 (`errorDetail`, `maxLength`, timeout), C9, C10, C11; the `test_old_firmware_contract_*` suite. **This alone fixes A3 for every panel already in the field.** | — | backend, frontend, docs |
| **W2** — identity | §10.1, §10.3, §10.4; subject roster; nudge keying; one-release dual-id acceptance. RLS → owner escalation + red-team review. | W1 (C2's roster seam) | backend, frontend (id-opaque; tests only), docs |
| **W3** — contract for the new firmware | `/waiting` ids + `offered_at` stamping; `/next?id=` + `X-Jpanel-Duration-Ms`; `/played {heard_ms}`; `/returned`; C3 routes; `/summary`; §10.2 columns. All additive (§6). | W2 (subject-keyed RLS for `/returned`) | backend, docs |
| **F1** — engine on the host | §9.1; characterisation tests; backend source-grep tests retargeted. Firmware behaviour unchanged (version bump, rebuilt dist). | — (can run parallel to W1–W3) | firmware, backend (path filter), docs |
| **F2** — `display.c` drives `ui.c` | §9.2 audit and cutover; `check_no_dup`. Behaviour unchanged except audited resolutions. Owner on-glass checklist. | F1 (the view struct shape) | firmware, docs |
| **F3** — engine fixes | §7 entire; A1, A2, A3 (panel), A4, A5 (engine and audio), A6 (engine), A7, A8, A9; telemetry fields. | F1, F2, W3 (merged first, so new panels meet a box that has the routes; §6 covers a rollback) | firmware, backend (contract pins), docs |
| **F4** — UI behaviour fixes | B1, B2, B3, B4, B5 (behaviour), B8, B10, A5 (ui refusals), A6 (ui), B-UX behaviour rows (X one press, skip, position, dark-wake pair). No new drawn elements. | F2, F3 | firmware, docs |
| **F5** — panel visuals for pre-readers | Sending overlay, pips, badge pulse, tick/cross glyphs, hush ring, `CUE_NOBODY`, `CUE_SENT` contour. **GUI gate**: three interactive mocks (with WebAudio cues) shown to the owner before building; the chosen one lands in `docs/mocks/jpanel-panel-messaging/`. | F4 | firmware, docs |
| **P1** — PWA honesty and controls | C3 buttons, C4 status copy, C5 status on between, composer counter and hint, launcher badge, read dwell, retention line, retired-thread rendering, "Load earlier". **GUI gate**: three mocks first. | W3 | frontend, docs |

Default order: W1 → W2 → W3 → F1 → F2 → F3 → F4 → F5, with P1 after W3. F1 and P1 touch
disjoint packages from their neighbours and may run as parallel tracks; each is still its own PR
and its own wave gate.

## 12. Risks, losses, and open questions

**Risks.**
- **F2 is the largest single change to the panel's main loop since 0.3.29.** Mitigations: it is
  behaviour-preserving by construction, its divergences are listed, the ~440 existing checks
  plus `check_no_dup` guard it, and it ships alone so a regression cannot be confused with a
  fix. Rollback is the existing OTA rollback gate.
- **Memory ordering is reviewed, not tested** (§9.1). Telemetry is the on-device detector.
- **W2 rewrites RLS on children's recordings.** Mitigations: the policies keep their shape
  (one GUC changed), every sibling case is re-asserted, the migration test seeds the messy real
  shape, and a downgrade path exists.
- **`offered_at` turns a poll into an occasional write.** It is bounded at one UPDATE per
  message lifetime per recipient.

**What the owner loses by the decisions above.**
- **No push notification.** A message from a child waits until he opens the PWA. The launcher
  badge narrows that but does not close it.
- **A failed send is not kept for resend.** The child hears "didn't go" and must say it again.
- **Renames rewrite past names**, and **"Played" ≠ "Heard"**: the PWA cannot tell an empty room
  from a listening child.
- **Ack-on-stop narrowed.** A child who stops within the first 2 s finds the message still
  waiting, and the notice comes back for it. That is intended, but it is a change she will
  notice.

**Open questions for the owner** (everything else is decided above):
1. **`HEARD_MIN_MS = 2 s`.** Long enough to tell "I heard who it was" from a mis-press. A
   stopwatch on the twins would settle it, and it is one named constant.
2. **Typed-message length** stays 600 characters (~50 s spoken). Whether a message to a
   four-year-old should be capped nearer 30 s is a parenting call, not a fault (JP §5).
3. **Push notifications** — whether to take on web push (VAPID keys, a service-worker
   subscription, a new dependency) as its own plan.
