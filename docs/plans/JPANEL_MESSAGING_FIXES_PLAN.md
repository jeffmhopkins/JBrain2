# jpanel messaging — the fix plan after the adversarial review

> **Status:** Scheduled · **Last verified:** 2026-10-11 · **Waves:** W1◻️ W2◻️ W3◻️ F1◻️ F2◻️ F3◻️ F4◻️ P1◻️

Voice post between the twins' panels and the owner's PWA works on the happy path. It fails on
the paths a four-year-old actually takes: pressing again while it loads, walking away
mid-message, holding the pet while a message waits, talking over a TV, and a USB re-flash.

An adversarial review raised **32 findings**. Three are duplicates of others (B6 = A3, B7 = A1,
B9 = A6), and three are lists of UX gaps. Verifying them against the code turned up **two more
defects** (A8, A9). The first cold review of this plan then raised **15 blocking and 26
non-blocking items**; all are resolved in the text, and §14 records each one.

The owner has said he is *"totally down for restructuring the whole UI state machine to make
sure that everything is done correctly."* This plan therefore **replaces** the panel's
interaction logic with one explicit, host-tested state machine (§8), rather than patching
`display.c`/`ui.c` finding by finding. It also rebuilds the messaging engine's cross-task
contract as one small model (§7).

Binding inputs: `CLAUDE.md`, `docs/DOC_LIFECYCLE.md`, `docs/reference/PROCESS.md`,
`docs/reference/DEVELOPMENT.md` ("Verifying a change"), and `docs/plans/JPANEL_PLAN.md`
(**JP** below). The owner decisions in JP are kept unless §4 says otherwise and why. Names: the
panels are placeholder-named **Quorra** and **Tamsin**. Function names are as of firmware
0.3.39; line numbers are omitted because they drift.

---

## 1. Goal

A child's message, and a message to a child, is never lost silently, never replayed by a fault,
and never reported to the owner as heard when it was not. Every press a pre-reader makes gets an
answer she can interpret without reading.

The panel's behaviour is defined by one state machine whose every mode, transition and drawn
control is exercised on the host. The code that ships is the code that is tested: there is no
second copy in `display.c`. Every journey below is pinned by a test that runs in CI.

## 2. What the fixed system must do — owner-facing expectations

Each line is a testable behaviour, cited by ID in §3 and in the tests.

### J1 — A child sends (panel → sister / Dad)

- **E1.1** Every recording a panel can capture (`CAPTURE_MAX_MS`) is accepted by the box. One
  byte over that ceiling is refused, never truncated.
- **E1.2** After the tick, or the hush auto-send, she hears exactly one outcome sound, within
  the HTTP timeout:
  - **sent**;
  - **nobody to send to**;
  - **dropped** — the cross's sound, used for silence the box trimmed to nothing;
  - **didn't go**.
- **E1.3** While an upload is in flight, the glass shows that it is sending (F4 visual). A
  message arriving in that window still gets its sound and notice.
- **E1.4** Nothing during an upload can alter the bytes being uploaded. Anything that needs the
  microphone is refused with the refusal sound.
- **E1.5** A recording she deliberately started and then left silent ends with the "dropped"
  sound. Every refusal to open the microphone — touch or voice — makes the refusal sound.
- **E1.6** A hold on the pet's centre opens the "who?" menu:
  - when nothing is up;
  - when a waiting message shows as a badge;
  - when the again/reply pair has stood for 10 s or more.

  In the first 10 s after a message ends, the pair's quadrants are live (the owner's 0.3.35 and
  quadrant rules), so a press there is the pair's button. That reply opens a recording that
  shows the recipient's face and can be cancelled with the cross (§8.3).

### J2 — A child receives, plays, stops, replies

- **E2.1** Pressing the notice plays the message whose sender's face the notice shows. The
  message is chosen by id when the box sends ids.
- **E2.2** A stop (the exit corner, the button, or reply) is honoured at **any** moment after the
  press:
  - before the fetch is dequeued;
  - while the speaker is busy;
  - while waiting for the response head;
  - before the first byte;
  - during the preroll;
  - during the message;
  - during the ring's final drain.

  After a stop, at most one audio chunk (~20 ms) plays, and nothing chains on. On the WebSocket
  transport the network request is cancelled within one link step (250 ms).
- **E2.3** "Next" during playback — the face, or a swipe — stops the current message and plays
  the next one by id. The next one is the oldest not yet played in this run, and none is
  skipped. With two messages waiting, "next" works while the first plays.
- **E2.4** A message counts as heard only if it actually sounded for `HEARD_MIN_MS`, or for 80%
  of a short message. A shorter stop hands it back: it stays waiting, and the delivery attempt is
  refunded — with a bound (§9 A6).
- **E2.5** A heard message is acknowledged exactly once, even through network loss. A lost
  acknowledgement never makes a run replay or skip, and never leaves controls standing over
  silence.
- **E2.6** A run always ends. A fetch failure mid-run returns to the pet with the "didn't go"
  sound and the notice for whatever remains.
- **E2.7** Reply during playback opens a working recording to the sender immediately, with the
  tick and cross live. The pair, a replay, or the exit can never be live over a recording.
- **E2.8** The exit corner leaves playback in one press.
- **E2.9** A message arriving during a pet conversation or a recording gets its sound (queued
  until the speaker is free) and its 15 s big notice, counted from when the notice is first
  visible.
- **E2.10** A press that cannot be served — the notice while sending, a skip whose stopped fetch
  has not unwound, or a press behind a long pet reply — always ends audibly: the refusal sound,
  or "didn't go" with the notice restored. It is never dropped silently.
- **E2.11** A message the panel has already announced is never announced again as new: not after
  a hand-back, and not after an acknowledgement retry.

### J3 — Dad sends, reads, clears

- **E3.1** The owner can always message every enrolled panel. A message to an unknown panel is
  refused before TTS runs, with a readable error, and the draft is kept.
- **E3.2** Every unplayed message to the owner is renderable, so the badge can always be cleared
  by reading. Reading counts only after a dwell, while the PWA has focus.
- **E3.3** Each message's status tells the owner what happened:
  - not yet seen by her panel;
  - on her panel, not played;
  - started, not finished;
  - played (with "stopped after N s" when it was cut);
  - couldn't be delivered.

  Twin-to-twin messages carry the same status. The word is "Played", never "Heard".
- **E3.4** An undelivered message can be retried or discarded from the PWA.
- **E3.5** The count of messages kept back by "Clear messages" names the right child.
- **E3.6** A twin-to-twin message appears in both children's threads.
- **E3.7** A poll that started before a send can never remove that send's bubble.
- **E3.8** Errors are readable sentences. The composer shows its limit and says the message waits
  for a tap on her panel.
- **E3.9** The launcher's jpanel tile shows the unread count.
- **E3.10** Audio plays in iOS Safari. A malformed message id gets 404, never 500.

### J4 — Re-flash (USB recovery)

- **E4.1** A re-flash loses nothing. Mail waiting for the panel is delivered. Its history,
  badge, pet transcripts and settings stay under one identity, and no ghost "panel 1a2b3c4d"
  chip appears.
- **E4.2** Messages addressed to a since-revoked panel are visible under its name, marked
  retired, and clearable (P1). Until P1, retired threads are hidden from the picker rather than
  shown as live chips (W2).

### J5 — Network loss

- **E5.1** A message is acknowledged only if it streamed whole and sounded. A cut-off message
  stays waiting.
- **E5.2** An acknowledgement the box did not receive is retried with backoff until it gets an
  answer. While it is owed, no fetch starts, and a press says "didn't go" with the notice kept.
- **E5.3** A send that fails says so. The recording is not kept for resend (§5).
- **E5.4** A panel and a box on different releases keep every journey working, at worst with
  today's behaviour on the older side (§6).

## 3. Traceability — every finding

"Verified" records what reading 0.3.39 found: **✓** confirmed as written, **✓\*** confirmed with
a correction (§3.1). Waves are defined in §12.

| ID | Verified | Fix | Wave | Proving test(s) |
|---|---|---|---|---|
| A1 | ✓ | Stop generation, consumer-owned stream discard, network cancel, pause intent (§7.1–7.3, §8.4) | F2, F3 | `jpanel_tests: test_stop_at_every_interleave_point`, `stream_tests: test_stop_during_drain_frees_the_speaker`, `sm_tests: test_pause_before_first_byte_holds` |
| A2 | ✓ | Every non-started fetch ends the run with `PLAY_ENDED{run_continues=false}` (§9) | F2 | `test_chain_fetch_failure_ends_the_run` (×4) |
| A3 | ✓ | Box accepts the capture cap; caps pinned; `EMPTY` outcome (§9) | W1, F2 | `test_send_accepts_exactly_the_capture_cap`, `test_send_refuses_cap_plus_one`, `test_capture_cap_pinned_across_firmware_and_box` |
| A4 | ✓ | Single ack slot; chain only after 204; view by id; owed-PLAYED id filtered from the view (§7.6) | W3, F2 | `test_lost_ack_is_retried_and_never_replays`, `test_unreachable_ack_ends_run_and_notice_returns` |
| A5 | ✓ | Capture lease in host-built `stream.c`; mic entry refused while leased (§7.5, §8.3) | F2, F3 | `stream_tests: test_capture_open_refused_while_leased`, `jpanel_tests: test_upload_bytes_unchanged_under_listen_attempts` |
| A6 | ✓\* | `HEARD_MIN_MS` from an output counter; bounded `/returned`; id-based "next" (§9) | W3, F2, F3 | `test_stop_in_preroll_returns_not_acks`, `test_returns_are_bounded`, `sm_tests: test_next_during_playback_never_skips` |
| A7 | ✓ | Seqlock with correct fences (§7.4) | F2 | `test_snapshot_is_never_torn` |
| A8 *(new)* | — | `s_state` written by two tasks; a chain fetch could overwrite a send's BUSY → events only (§7.3) | F2 | `test_send_outcome_survives_concurrent_chain` |
| A9 *(new)* | — | `audio_stop()` reset the SPSC ring from a third task → the consumer discards (§7.2) | F2 | `stream_tests: test_only_producer_and_consumer_touch_cursors` |
| A-tests | ✓ | `jpanel_core.c`, `stream.c`, `link_types.h` on the host (§10) | F1 | new `run_jpanel_tests`, `run_stream_tests` |
| B0 | ✓ | One state machine replaces `ui.c` and `display.c`'s duplicate (§8) | F3 | `make check_no_logic_in_display`; `sm_tests` (ported plus new) |
| B1 | ✓ | Reply = stop + RECORD immediately; `PEND_REPLY` removed; mode tables make pair and record exclusive (§8.3) | F3 | `test_reply_during_playback_opens_a_working_recording` |
| B2 | ✓\* | Pair FRESH/STANDING; standing pair hit by its discs; hold → grid when the down point is PET in the current mode's table (§8.3) | F3 | `test_hold_at_pet_centre_opens_grid_with_badge`, `…_after_pair_stands`, `test_hold_in_fresh_pair_is_the_pair_press` |
| B3 | ✓ | PAIR only when nothing waits; one quadrant owner per mode (§8.3) | F3 | `test_quadrant_face_is_what_a_press_plays` + invariant I2 |
| B4 | ✓ | Cue classes: press cues preempt cues, outcome/message cues queue; notice clock from first visible (§8.6) | F3 | `cue_tests: test_cue_classes`, `sm_tests: test_message_during_pet_reply_gets_chime_and_big_box` |
| B5 | ✓ | `sending` is a background flag; the notice stays; a press on it while sending is refused audibly; visual in F4 (§8.3) | F3, F4 | `test_notice_shown_and_refused_during_send` |
| B6 | = A3 | — | — | — |
| B7 | = A1 | — | — | — |
| B8 | ✓ | Silent deliberate recording → `CUE_STOP`; every mic refusal → `CUE_STOP` (§8.3) | F3 | `test_silent_recording_says_dropped`, `test_every_mic_refusal_sounds` |
| B9 | = A6 | — | — | — |
| B10 | ✓\* | Pending bounded and audible; follow-up yields; `due()` everywhere; "stop stop" kept muted (§8.5, §3.1) | F2, F3 | `test_press_behind_pet_reply_is_served`, `test_deadlines_survive_u32_wrap` |
| B-UX | ✓ | §9 B-UX table | F3, F4 | per row |
| C1 | ✓ | Messages and pet turns keyed to the panel's subject; RLS on `app.subject_id`; every call site enumerated (§11) | W2 | §11.4 tests |
| C2 | ✓ | `send_text` validates; picker from the subject roster; PWA refetch (§9) | W1, W2 | `test_send_text_unknown_panel_404_before_tts`, vitest |
| C3 | ✓ | Owner retry/discard (§9) | W3, P1 | `test_retry_resets_deliveries_and_returns`, `test_discard_only_owner_or_undelivered` |
| C4 | ✓ | `heard_ms`, `offered_at`; honest copy (§9) | W3, F2, P1 | `test_played_records_heard_ms`, vitest status matrix |
| C5 | ✓ | Between rows in both threads; status on them (§9) | W1, P1 | `test_between_message_in_both_threads` |
| C6 | ✓ | Per-thread limit plus every unplayed-to-owner row (§9) | W1 | `test_every_unplayed_owner_row_is_returned` |
| C7 | ✓ (Range plausible) | 206 Range; uuid → 404 (§9) | W1 | `test_audio_range_206`, `test_non_uuid_is_404` |
| C8 | ✓ | `errorDetail` lists; `maxLength`; TTS 60 s (§9) | W1, P1 | `client.test`, `test_send_text_tts_timeout_60s` |
| C9 | ✓ | `Cleared.kept_for` by recipient (§9) | W1 | `test_clear_kept_names_recipient` |
| C10 | ✓ | Owner audio over cap → 413 (§9) | W1 | `test_owner_audio_over_cap_is_413` |
| C11 | ✓ | Request sequence plus mutation epoch (§9) | W1 | vitest stale-poll test |
| C-UX | ✓ | §9 C-UX table | W3, P1 | per row |

### 3.1 Where the original review needed correcting

- **A6.** The face target needs `waiting > 1` (`ui.c: target_live(UI_TARGET_FACE)`) after
  `fetch_head` has already removed the playing message from the count. So with exactly **two**
  waiting, the face is dead during the first message, and the A→C skip needs three or more. One
  root cause, one fix.
- **B10, "stop stop".** The recording branch of `VOCAB_STOP` is dead because commands are muted
  while recording (`speech_mute_commands`). The mute is deliberate (JP 0.3.33: a word inside a
  message must not act). **Not changed.** The cross is the way out, and F4 makes it legible.
- **B2/B-UX, "the pair never expires".** This was the owner's explicit 0.3.25 call. It is
  **kept**; §8.3 removes the harm instead.
- **Doc and code that disagree** (fixed by the waves that touch them):
  - `s_repeat_until`'s comment claims a new message displaces the pair; nothing does, and that is
    the root of B3.
  - `ui.c`'s exit case says "No cue" after emitting `CUE_STOP`.
  - `jpanel.h`'s `jpanel_replay` says "without asking the box again", though it has asked since
    0.2.96.
  - JP still says "Capped at 20 s".
- **C7.** The iOS Range failure is plausible rather than proven; the fix is cheap and correct. The
  500 on a non-uuid id is confirmed.

## 4. Decisions in JPANEL_PLAN: kept, revisited, overturned

| JP decision | Here |
|---|---|
| UI logic lives in `ui.c`, with `display.c` "still owning the bodies" (0.3.29), and the cutover is "the next step". | **Revisited, with the owner's go-ahead.** `display.c` keeps sampling, effect execution and drawing. All interaction state and policy lives in one explicit machine, `panel_sm.c` (§8). Why a replacement rather than finishing the `ui.c` cutover: `ui.c` encodes modes implicitly, through about 30 independent fields and two global tap tables gated by `target_live` predicates. Every B finding is two of those fields disagreeing — the pair live during a recording, the badge and the pair owning one quadrant, a hold gate built from four flags. An explicit mode, with one target table per mode that is used for both drawing and hit-testing, makes those states unrepresentable instead of guarded. That is less mechanism than the executor plus a patch per finding. |
| **Ack-on-stop** (a stop does not burn a delivery attempt). | **Purpose kept, effect narrowed.** A stop never burns an attempt. A stop after `HEARD_MIN_MS` of real output acknowledges, as today. A shorter stop hands the message back via `POST /returned`, which refunds the attempt — bounded, so a misclassifying firmware cannot defeat the box's cap. |
| **Every press fires on the down edge** (measured for jabbing). | **Kept.** |
| **A long press on a menu is just a press** (0.3.35). | **Kept**, inside each mode's table. |
| **Quadrant hit areas for the two discs** (*"split the [screen] into quarters"*). | **Kept** while the controls are fresh: during playback, recording, and the pair's first `REPEAT_MS` (10 s, the owner's figure). A pair that has stood longer than that is hit by its disc reach (`CONFIRM_HIT_R` around each disc). Otherwise a pair that never expires would permanently turn the pet's centre into a reply button, which is exactly what made E1.6 impossible. |
| **The pair never expires** (0.3.25). | **Kept** (still drawn until a finger, a new message or a dark wake). It no longer blocks the hold (it becomes STANDING), it yields to a waiting message, and it clears on wake from dark — not dim — by the same reasoning as JP's dark-wake big-box rule. |
| **Screen-sleep rules.** | **Kept unchanged**; the machine reports `busy()` to the sleep stage exactly as `ui_busy` does. |
| **"A message that cannot be delivered is still a message"**; bulk clear keeps unheard rows. | **Kept.** The owner may act on one undelivered message (retry, or discard what he sent or the box gave up on). |
| **Index-addressed `/next?at=`.** | **Overturned.** `/waiting` carries ids and `/next` takes `id`. Positional addressing caused A4 and A6. A panel can already read every row addressed to it, so the ids are no new exposure. |
| **Messages addressed by principal id** (JP's "dead letter" residual). | **Overturned** in favour of the subject, which `DevicesRepo.reflash` has kept stable since 0211. |
| **"The other panel" refusal; sibling RLS; panels never read pet transcripts.** | Kept; re-proved against the subject (§11). |
| **Two clears for one thread.** | Kept; only the confirm copy changes. |
| **30-day retention after `played_at`.** | Kept; the PWA states it. |
| **A refused reply waits (`PEND_REPLY`) for the fetch to unwind.** | **Removed.** The capture lease (§7.5) is the only microphone exclusivity, and a stopped stream is silent within one chunk (§7.2), so reply opens the microphone immediately. The cue's own deaf window already writes silence (JP 0.3.33). |

## 5. Scope boundaries

- **Push notifications to the owner's phone: deferred.** There is no web-push infrastructure
  (no `PushManager`, no VAPID), so it would be a new runtime dependency and a privacy decision JP
  §5 left open. The in-app launcher badge is in scope. Cost: §13.
- **No resend of a failed send** (E5.3). Keeping the capture buffer would hold the lease, and
  with it the pet's microphone, indefinitely. A second 1.1 MB PSRAM buffer was rejected while
  `free_psram` after F3 is unmeasured.
- **Child-speech STT**: out of scope (JP §5).
- `endpoint_status` (telemetry, keyed by principal) is not messaging and is unchanged by W2.

## 6. Version skew

**The box ships the firmware.** Panels OTA from the box's `firmware/dist/`
(`ota_fetch_manifest`). `main.c` installs whatever version **differs** from the running one
(`strcmp`), so the box decides the version in both directions. Two cases follow:

- **New box + old firmware is routine.** It is the state between a box update and each panel's
  next manifest poll (about 15 min), and it lasts indefinitely for a panel whose OTA keeps
  refusing (`ota_apply_faults`).
- **Old box + new firmware happens only after a forward revert of the box.** Ops → Update is
  forward-only: `deploy/update-inner.sh` fast-forwards `main`, then `alembic upgrade head`. So a
  rollback is a revert PR merged to `main` and applied by the owner with Ops → Update — there is
  no downgrade button. If that revert reaches back past W3, the panels are not reverted
  automatically; reverting them is a separate dist change (§12).

Rules:

1. Every box change is **additive** for panels. cJSON ignores unknown fields, `on_header` matches
   by name, and no status that 0.3.39 acts on changes meaning.
2. Every firmware change **degrades to today's path** when a field, header or route is absent
   (404/405), and never shows the child an error for it.
3. Owner routes and the PWA ship together. A service-worker-cached PWA can lag one release, so
   owner routes accept the old id space for one release (§11.2).

| Contract change | New box + 0.3.39 | After a forward revert of the box, with new firmware |
|---|---|---|
| `/send` raw ceiling = capture cap (A3) | 35 s recordings stop being 413: **fixed for every panel in the field by the box alone** | 413 as today; the new firmware maps it to `FAILED("too-long")` |
| `/waiting` `queue[].id` | ignored | absent → positional `?at=` and positional local removal; "new" detected by count rise (today's rule) |
| `/next?id=` + `X-Jpanel-Duration-Ms` | not sent | old box ignores `id` and serves `at` (the panel sends both); no duration → absolute `HEARD_MIN_MS` |
| `/played {id, heard_ms}` | `{id}` → `heard_ms` NULL → PWA shows "Played" | extra field ignored by pydantic |
| `/returned {id}` | never called; stops ack as today | 404 → ack slot cleared, `reach_fail(REACH_ACK,"no-returned")`, one attempt burned (today's worst case) |
| `offered_at` stamping | box-side, applies to old panels too | n/a |
| Subject identity + RLS | invisible to panels (they only see message uuids) | the forward-revert revision restores principal keying (§11.3) |
| `kept_for`, per-thread `limit`, `?before=`, `retired` | owner-only | a cached old PWA ignores new fields |

Pinned by `test_old_firmware_contract_*` (backend: the exact 0.3.39 bodies and headers, with
today's answers asserted) and `test_against_0_3_39_box_*` (firmware host: a scripted box that
omits every new field and 404s every new route).

---

## 7. The messaging engine's cross-task model (F2)

Four tasks touch voice post:

- **render** — `face_task`, created **unpinned** (`xTaskCreate` in `display.c`);
- **jpanel** — core 0;
- **audio** — the consumer of `s_play` and the ring;
- **link** — owns the socket.

Today render and jpanel both write most of `jpanel.c`'s `volatile` fields, which is the root of
A1, A7 and A8, and render resets the ring's cursors (A9). The model below is the **whole** cross-task
contract. It is deliberately small: **one counter, two queues, one snapshot, one lease**, and
every field has one writer task.

Ordering uses GCC `__atomic_*` (`uint32_t` operands) with the fences stated. `volatile` is
removed from these fields.

### 7.1 Stop is one counter

- `uint32_t s_stop_gen`. **Only render writes it**: `jpanel_stop()` does
  `__atomic_add_fetch(&s_stop_gen, 1, __ATOMIC_RELEASE)` and nothing else. It does not touch the
  ring, `s_run`, or `s_stopped` (deleted). A stop when nothing is playing changes one number and
  can wedge nothing.
- Every fetch or replay command carries `gen = load(s_stop_gen, ACQUIRE)` taken by render **at
  the post**, plus a render-assigned `seq`. A run (the chain) inherits the starting command's
  `gen`.
- "This work is cancelled" means `cmd.gen != load(s_stop_gen, ACQUIRE)`, and it is checked at
  every point that blocks or commits:
  - on dequeue (a stale command is dropped, emitting `PLAY_ENDED{seq, cancelled}`);
  - each 20 ms step of `stream_begin_waiting`;
  - after the stream is claimed;
  - by the link (§7.3);
  - by the consumer (§7.2).

### 7.2 The stream belongs to its producer and its consumer only (`stream.c`)

The ring, the stream state and the capture buffer's bookkeeping move from `audio.c` into
**`stream.c`**: pure and host-built, next to `ring.c`, and used by both `audio.c` and the host
fakes. Its API:

- **producer** (jpanel): `stream_begin(gen)`, `stream_write(gen, …)`, `stream_end()`;
- **consumer** (audio task): `stream_step(out_cap) → n`;
- **readers**: `stream_live(gen)`, `stream_active()`, `stream_out_ms()`.

- `stream_begin(gen)` stamps the stream with `gen`. It succeeds only when the previous stream
  is closed and the ring is empty.
- **The consumer owns discard.** Each step, if `stream.gen != load(s_stop_gen)`, it advances
  `r = w` — a legal consumer write — outputs nothing, and marks the stream `stale`.
  - `stream_active()` and `audio_playing()` are false for a stale stream, so a stop during the
    **final drain** frees the speaker within one chunk, and the ack check sees "finished" on its
    next pass.
  - `stream_write` refuses a stale `gen`, so `pump` returns 1, and the producer calls
    `stream_end()`.
  - Nobody else ever touches `r` or `w`. A9 is gone, and so is `audio_stream_abort()` from any
    task but the producer.
- **`stream_out_ms()`** counts samples actually handed to the codec. Only the consumer writes it;
  it is reset by `stream_begin`, and a paused stream is not read, so pause adds nothing. A6's
  "heard" test reads it; it is not derived from `r`, so a discard cannot inflate it.
- **Pause** stays render → consumer: one `bool` that only render writes, honoured per chunk.
  `stream_begin` no longer clears it. The machine (§8.4) decides when pause applies.

### 7.3 Commands in, events out, and the network can be cancelled

- **Commands** (render → jpanel): a FreeRTOS queue, depth 4, of `{kind, seq, gen, id[48], at,
  to}`. `jpanel_replay` carries the id it is replaying. There is **no busy token**: commands
  simply queue, and stale ones drop on dequeue. A send's exclusivity is the capture lease
  (§7.5).
- **Events** (jpanel → render): a FreeRTOS queue, depth 8. Render **drains all of it every
  frame** into `in.ev[]`, and the machine consumes them in order. Every command yields exactly one
  terminal event carrying its `seq`; a run emits two events per message. The kinds:
  - `SEND_DONE{outcome ∈ SENT|NOBODY|EMPTY|FAILED, reason}`;
  - `PLAY_STARTED{seq, id, from[32], from_dad, duration_ms, run}`;
  - `PLAY_ENDED{seq, id, outcome ∈ heard|stopped_heard|returned|short|empty|cancelled|failed, reason, run_continues}`.

  `from`, `from_dad` and `id` travel **by value**. `jpanel_in_from()`, `jpanel_last_from()`,
  `jpanel_state()`, `jpanel_clear()`, `jpanel_running()` and `jpanel_fetching()` are deleted;
  render-side facts come only from events and the snapshot. That removes A8: there is no shared
  state for a chained fetch to overwrite. Overflow is counted (`ev_dropped`), and with a full
  drain each frame it needs more than 8 events in about 100 ms.
- **Network cancel.** `link_types.h`'s `link_req_t` gains `bool (*cancelled)(void *ctx)`.
  - `ws_request` polls it in the head wait and in both body loops, every `LINK_STEP_MS` (250
    ms), and on true calls `cancel(id)` (`wsp_cancel`) and returns `stopped`.
  - On the HTTPS fallback it is checked in `http_event` on each data event, and returns
    `ESP_FAIL` to abort. The head phase of `esp_http_client_perform` cannot be interrupted, so
    there a cancelled request still holds the jpanel task until `timeout_ms`. That is bounded
    and stated: the machine's pending press then ends audibly (E2.10).
  - jpanel passes "is my `gen` stale".
- Buffers sized for `/next?id=<36>&at=N`: `rel[96]`, `path[112]`.

### 7.4 The waiting view is a seqlock snapshot

`jpanel_view_t { uint32_t count, known; struct { char id[48]; char from[32]; bool from_dad; }
q[JPANEL_QUEUE_MAX]; char sibling[32]; }`. **Only jpanel writes it**, from `do_poll` and from
`fetch_head`'s removal by id.

- **Writer:** `seq = load(relaxed); store(seq+1, relaxed); __atomic_thread_fence(RELEASE);`
  write the fields; `store(seq+2, RELEASE)`.
- **Reader** (`jpanel_snapshot`): `s1 = load(ACQUIRE)`, retry if odd; copy;
  `__atomic_thread_fence(ACQUIRE)`; `s2 = load(relaxed)`; accept if `s1 == s2`. Up to 4 tries;
  on failure it returns false and render keeps the previous frame's view.
- Render reads it once per frame into `in.view`.
- The `/waiting` body buffer grows to 1536 bytes (static): with ids the worst case is ~1019 bytes.

### 7.5 The capture lease (`stream.c`)

`uint32_t s_cap_lease` (0 free, 1 jpanel, 2 talk). Acquire is CAS 0→k; release is
`store(0, RELEASE)`. `capture_open()` returns `bool` and refuses while leased.

- `jpanel_send()` acquires lease 1 **before** posting and releases it itself if the post fails.
  The jpanel task releases it when `do_send` returns, whatever the outcome.
- `talk_send()` acquires lease 2 and releases it when the upload request returns — before the
  reply plays, so the follow-up listen can open.
- The machine's single microphone gate (`can_open_mic`, §8.3) refuses while `in.leased` is set,
  with `CUE_STOP`. The executor treats a `capture_open()` false (a race) as `ev MIC_REFUSED`,
  which takes the machine back to PET with `CUE_STOP`.

### 7.6 One acknowledgement slot

`ack_t { char id[48]; enum {PLAYED, RETURNED} kind; int heard_ms; uint32_t tries, next_at; }`.
**One slot.** Only jpanel touches it.

- **Armed when a message ends:**
  - drained, verified and sounded → `PLAYED`;
  - stale with `stream_out_ms() ≥ threshold` → `PLAYED`;
  - stale below the threshold, or cancelled after `/next` → `RETURNED`;
  - short or unverified → nothing (E5.1).
- **Attempted at once.**
  - **204** → clear the slot, and chain if `run_continues`.
  - **404/410** → clear (the row is gone, or `/returned` is absent on an old box).
  - **Anything else** → the run ends (`PLAY_ENDED{run_continues=false}`) and the slot retries
    with `due()` backoff of 1, 2, 4 … 30 s.
- **While the slot is owed:**
  - `do_poll` removes the owed id from the view if `kind == PLAYED` (it was heard; E2.11);
  - a fetch command first retries the ack, and if it still fails, answers
    `PLAY_ENDED{failed, "ack-pending"}` → "didn't go", with the notice kept (E5.2).

  So a second ack can never be produced while one is owed, and depth 1 is exact.

### 7.7 One writer per field

| State | Writer | Readers |
|---|---|---|
| `s_stop_gen` | render | jpanel, audio, link callback |
| commands queue | render (send) | jpanel |
| events queue, view seqlock, run, ack slot, `s_in_*` | jpanel | render (events/snapshot only) |
| ring `w`, stream open/gen | jpanel (producer) | audio |
| ring `r`, `stale`, `out_ms` | audio (consumer) | jpanel, render |
| pause flag | render | audio |
| capture lease | CAS acquire by render/talk; release by the holder's task | all |
| press-cue mailbox | render | audio |
| cue queue | render (push) / audio (pop) — FreeRTOS queue | — |
| `s_play` | audio (cues), **talk** (`audio_play` of a reply) | audio |

The **`s_play` exception is stated, not hidden.** A reply (`talk.c` → `audio_play`) and a cue
both write `s_play` today with check-then-write. After F2, render no longer writes `s_play` at
all: cues go through the mailbox or the queue, and the audio task writes them. That leaves
talk vs audio as the one two-writer pair. Its worst case is one dropped cue at the instant a
reply begins. This is pre-existing and out of scope here.

### 7.8 Shape of the code

`jpanel_core.c` exposes `jpanel_core_step(core, now, const cmd_t *cmd_or_null)`. One call
covers: dequeue handling, the ack slot, the chain, and the poll schedule (`due(now,
next_poll)`). `jpanel_task` is a five-line loop around it. The host tests drive the same step.

Telemetry gains `ev_dropped`, `ack_owed`, `ack_retries`, `returned`, `lease_refusals`,
`snapshot_retries` and `jpanel_stack_free` (high-water, like `display_stack_free`), so the
owner can see faults from the debug API and the Ops fleet card (CLAUDE.md #10).

---

## 8. The panel state machine (F3) — the restructure

### 8.1 Shape

`firmware/main/panel_sm.{c,h}` replaces `ui.{c,h}`. It is pure C, host-built, with no clock and
no peripherals.

- **One entry point:** `void sm_step(sm_t *sm, const sm_in_t *in, sm_out_t *out)`.
  - `in` holds the frame's sampled facts — `now`, `view`, `ev[]`, touch edges with their points,
    the voice command, button edges, `reply_sounding`, `hearing`, `capture_ms`, `leased`,
    `net`, `sleep` stage — **all sampled before the step**.
  - `out` holds an ordered effect list (as `ui_action_t` today) plus a **view model**
    (`sm_view_t`): what to draw, already resolved.
- **One pass, used twice.** `face_task` and the host harness both call `sm_step` exactly once
  per frame: sample → step → execute effects → draw `out.view`. There is no mid-frame
  re-sampling, because the machine never infers a message's end from `audio_playing()`; it uses
  `PLAY_ENDED`. That removes the "speaking sampled at the top, the tap later in the frame"
  hazard rather than ordering around it, and the tests run the very same pass (round-1 UX #1).
- `display.c` keeps: sampling (touch queue, gesture, speech, button, audio facts, jpanel
  snapshot and event drain), the **executor** (a switch over effects, keeping the `sound =
  audio_start()` gate on every cue), drawing from `sm_view_t`, the sleep and screen stages, and
  calibration. During calibration `sm_step` is **not called** (calibration owns the touches);
  leaving calibration sends one `RESYNC` input.
- **`make check_no_logic_in_display`** (part of `make test`) fails if `display.c` declares a
  file-scope static whose name matches any `sm_t` field. The list is derived mechanically by a
  small awk over `panel_sm.h`'s struct, not hand-listed. It also fails if `display.c` calls a
  jpanel getter other than `jpanel_snapshot`/`jpanel_drain`.

### 8.2 State

```
sm_t {
  mode: PET | GRID | LISTEN | THINK | RECORD | PLAY | PAIR     // one foreground mode
  play: { phase: PENDING | FETCHING | SOUNDING, paused, pause_wanted, seq, id, from, from_dad,
          run_pos, run_total, since }                         // valid in PLAY
  pair: { since, id, from, from_dad }                         // valid in PAIR; FRESH while
                                                              //   !due(now, since+REPEAT_MS)
  rec:  { to, heard, hush, since, deliberate }                // valid in RECORD
  talk: { voice, heard, hush, lead, since, follow_armed, follow_turns, failed_at }
  grid: { until }
  sending: bool, sending_to                                   // background, set by the effect,
                                                              //   cleared by SEND_DONE
  notice: { first_visible, big_shown }                        // background
  sel_id, seen_ids[16]                                        // selection and "is it new"
  down: { at, x, y, target, swiped, hold_fired }
  cues owed are not state here: they are effects (§8.6)
}
```

Invariants are asserted by `sm_check(sm, view)` after **every** host step, and at runtime they
are counted (`sm_violations` in telemetry) with recovery to PET:

- **I1** Exactly one foreground mode. The microphone is open ⇔ mode ∈ {LISTEN, RECORD}.
- **I2** The face drawn in the top-left quadrant is the sender of whatever a press there does.
- **I3** Every drawn control is in the current mode's target table, and every live target is
  drawn — one table feeds both (§8.3).
- **I4** Every press produces a cue or a view change, except the listed claimed-and-ignored
  cases.
- **I5** No mode other than PET and PAIR(STANDING) lets a hold open the grid.
- **I6** PLAY's `PENDING` and `FETCHING` phases each have a deadline (`PENDING_MS`,
  `FETCH_MAX_MS = JPANEL_HTTP_TIMEOUT_MS + 2000`) after which the machine leaves with an
  audible outcome.
- **I7** All time comparisons go through `due(now, t)`, which is `(int32_t)(now - t) >= 0`.

### 8.3 Modes, targets and transitions

**One target table per mode**, in priority order. The view model draws exactly these.

| Mode | Targets (priority order) | Notes |
|---|---|---|
| PET | NOTICE (big box, or badge in the top-left quadrant) · PET | Notice shown iff `view.count > 0 && !sending_hides`; a hold whose down point is PET → GRID |
| GRID | cells (sister, Dad, pet, cancel) · swallow | modal; `SENDTO_MS` |
| LISTEN (voice) / RECORD | CROSS quadrant · TICK quadrant · swallow | the held listen ends on release |
| THINK | PET (flinch only) | |
| PLAY | EXIT (top right, after `EXIT_GRACE_MS`) · FACE (top left, if anything remains) · PAUSE quadrant · REPLY quadrant · swallow | |
| PAIR (fresh) | EXIT · AGAIN quadrant · REPLY quadrant · PET | the pair's face top-left (drawn; not a target) |
| PAIR (standing) | EXIT · AGAIN disc reach · REPLY disc reach · PET | the pet centre is PET → hold → GRID |

Transitions (all others are no-ops). "Mic gate" means `can_open_mic`: `!leased && !reply_sounding
&& net != BUSY`. On refusal: `CUE_STOP`, and the mode is unchanged.

- **PET**
  - NOTICE press:
    - if `sending` → `CUE_STOP` (refusal; the notice stays);
    - else → `CUE_PLAY`, PLAY{PENDING, id = sel_id}.
  - Hold on PET → GRID (`CUE_HEARD`).
  - Wake phrase → mic gate → LISTEN.
  - "tell sister/Dad" → mic gate → RECORD{deliberate}.
  - Button short → standby effects (stop, drop mic), PET.
- **GRID**
  - sister/Dad → mic gate → RECORD{deliberate};
  - pet → mic gate → LISTEN;
  - cancel or timeout → PET with `CUE_STOP`.
- **RECORD**
  - tick:
    - heard → effect `SEND_JPANEL` (the executor acquires the lease; on success `sending =
      true`), `CUE_HEARD`, PET;
    - not heard → drop, `CUE_STOP`, PET.
  - cross → drop, `CUE_STOP`, PET.
  - Hush or cap → as tick (heard).
  - No speech within `RECORD_LEAD_MS` → drop and PET: `CUE_STOP` if deliberate (every RECORD is;
    B8).
- **LISTEN/THINK** — today's conversation behaviour, ported unchanged:
  - the follow-up opens on the reply-end edge, **unless** a press is pending or an unseen message
    arrived during the turn;
  - the dash on failure/timeout, with `dash_why`.
- **PLAY**
  - `PENDING` → when `!reply_sounding` (the cue has its own mailbox and never blocks this), emit
    `PLAY_ID{seq, id}` → FETCHING. If `due(PENDING_MS)` → `CUE_OOPS`, PET (the notice returns).
  - FETCHING → `PLAY_STARTED{seq}` → SOUNDING (apply `pause_wanted`).
    - `PLAY_ENDED{seq}` → see below.
    - `due(FETCH_MAX_MS)` → effect `STOP`, `CUE_OOPS`, PET.
  - **EXIT** → effect `STOP`, `CUE_STOP`, PET. **No pair** (E2.8).
  - **FACE or swipe** — "next": the oldest remaining id after the current one in run order →
    effect `STOP`, `CUE_HEARD`, PLAY{PENDING, id = next}. With nothing remaining the face is
    not a target.
  - **PAUSE** (SOUNDING) → toggle pause, `CUE_PAUSE`/`CUE_RESUME`. In PENDING/FETCHING it sets
    `pause_wanted` and draws paused (the picture is true).
  - **REPLY** → effect `STOP`, `CUE_REPLY`, mic gate → RECORD{to = play.from_dad}. The STOP has
    made the stream stale, so the speaker is free within one chunk, and there is no pending reply
    (B1). If the gate refuses (leased by a send) → `CUE_STOP`, PET.
  - **`PLAY_ENDED`:**
    - if `run_continues` → FETCHING for the engine's next message, with `run_pos++`;
    - else if outcome ∈ {heard, stopped_heard} **and** `view.count == 0` → PAIR{since = now};
    - else → PET. `failed`/`short` → `CUE_OOPS`; `returned`/`cancelled` → no cue (the press
      that caused them already sounded).
- **PAIR**
  - AGAIN → `CUE_PLAY`, PLAY{PENDING, replay id};
  - REPLY → `CUE_REPLY`, mic gate → RECORD{to = pair.from};
  - EXIT → `CUE_STOP`, PET;
  - hold on PET (only reachable while standing) → GRID;
  - an **unseen** id arrives in the view → PET (the notice takes the quadrant: B3, I2);
  - wake from DARK → PET;
  - the wake phrase or "tell…" → mic gate → LISTEN/RECORD (pair gone).

Because each mode has its own table, the pair cannot be live during a recording (B1, round-1
UX #6), the exit cannot fire in RECORD, and the badge and the pair never share a mode (B3).

### 8.4 Selection and "new"

- `sel_id` is the selected waiting id. Arrival or emptying puts it back to the oldest. It is
  cleared to the oldest only when the selected id leaves the view **other than** by being the
  one playing. Without ids (an old box) the machine falls back to an index, as today.
- **"New"** means an id not in `seen_ids` (FIFO of the last 16). `CUE_MESSAGE`, the
  pair-displacement and the notice clock key on it. With an old box, a rise in count is the
  fallback. A returned message, or one whose ack is pending, therefore never chimes twice
  (E2.11, round-1 UX #5).
- **Run position:** `run_pos` counts the messages played in this run (1-based);
  `run_total = run_pos + remaining`. A skipped message (`returned`) is not replayed in the same
  run. It is still waiting, so when the run ends the notice offers it.

### 8.5 Time

- `PENDING_MS = PLAY_BUF_MS + 2000` (12 s) is `_Static_assert`ed against a mirrored
  `PLAY_BUF_MS`, so a press behind the longest reply is served. While PENDING, the run controls
  are drawn **and EXIT is live** (it cancels the pending press with `CUE_STOP`), so the 12 s
  window can always be left.
- All deadlines use `due()`. "Off" is a mode or a `bool`, never a zero timestamp.
- `test_deadlines_survive_u32_wrap` runs every journey with `now` starting at `0xFFFFF000`.

### 8.6 Cues have classes

Cue policy is pure code in `cue.c` (already host-built); `audio.c` executes it.

| Class | Cues | Policy |
|---|---|---|
| **press** | BLIP, PLAY, HEARD, PAUSE, RESUME, REPLY, LISTEN, STOP-as-press | Immediate, through the mailbox. **Preempts** another cue. Sounds **over** a stream, as today (`s_play` is served ahead of the ring). Dropped only while a pet reply sounds (JP: a reply outranks an acknowledgement). |
| **outcome** | SENT, OOPS, NOBODY, STOP-as-dropped | FIFO queue (depth 4) behind whatever cue or reply holds `s_play`. **Not** behind a stream: it sounds over it, like a press cue. Expires at `PLAY_BUF_MS + 2000`. |
| **message** | MESSAGE | Queued as an outcome. Expires after 20 s, or as soon as the notice is pressed. |

HEARD followed by OOPS in one frame now gives both, in order. Every cue the machine emits names
its class, and `cue_tests: test_every_cue_has_a_class` fails on an unclassified one.

### 8.7 Tests (`firmware/host/sm_tests.c`, binary `run_sm_tests`)

1. **Port every scenario in `ui_tests.c`** (~440 checks) to `sm_step`. Each port keeps its
   owner-quote comment. Checks whose expected behaviour this plan deliberately changes are
   rewritten and listed in the F3 PR, and only these:
   - the second half of `test_the_top_right_corner_ends_a_message_while_it_plays` (one-press
     exit);
   - every `PEND_REPLY` check (removed);
   - `ui_menu_up`-based hold checks (replaced by per-mode tables);
   - pair-live-with-notice cases (B3).
2. **Journey tests** named in §3 and §9, including at the pet centre (184, 224):
   - with a badge → GRID;
   - in a pair younger than 10 s → REPLY;
   - in a pair older than 10 s → GRID;
   - during PLAY → swallowed.
3. **Property test** `test_random_event_storm`: seeded, 200k steps of random touches, holds,
   swipes, voice commands, button edges, events in legal orders, clock jumps (including wrap)
   and speaker facts. `sm_check` runs after every step, plus a liveness check that PLAY never
   outlives I6's deadlines.
4. **Engine + machine together:** `jpanel_tests` can run `sm_step` against the real
   `jpanel_core` and `stream` on fakes, so A1, A6 and B1 are tested end to end on the host.

### 8.8 Why one replacement wave, and no shadow mode

A shadow run (the new machine computing effects beside the old one, divergences logged for a
release) was considered and rejected:

- The old path has known bugs, so most divergences are intended, and the parity signal is noise.
- It doubles the state and code on a board whose render task has panicked on stack before.
- The host gate is stronger:
  - every old scenario ported;
  - the property test;
  - end-to-end host runs with the real engine.

Field protection is the existing OTA rollback gate for crashes, `sm_violations` and the new
telemetry for misbehaviour, and the owner's on-glass checklist in the F3 PR. If F3 regresses in
a bedroom, the remedy is a **dist revert PR**: the F2 image re-committed under a new version
string, applied with Ops → Update, after which the panels OTA to it because `main.c` installs
any differing version. No terminal is needed (CLAUDE.md #10).

---

## 9. Per-finding detail

Cause, as verified → design (pointing at §7/§8) → test. Tests not otherwise named live in
`jpanel_tests.c`, `stream_tests.c`, `cue_tests` (in `tests.c`), `sm_tests.c`, the backend's
`tests/unit` and `tests/integration`, or the frontend's vitest files.

**A1.**
- Cause: `fetch_head` resets `s_stopped`/`s_run` after `stream_begin_waiting`, so a stop before
  the head is erased; the exit's own `CUE_STOP` makes the wait succeed late;
  `audio_stream_begin` clears the pause.
- Design: §7.1–7.3 and `pause_wanted` (§8.3).
- Tests:
  - `test_stop_at_every_interleave_point` (points: dequeue, wait step, head, after claim, per
    chunk, drain, pre-ack, chain gap). Asserts ≤ 1 chunk after the stop, no further `/next`, the
    run ended, `RETURNED` vs `PLAYED` by threshold, and a following press plays normally.
  - `test_stop_with_no_stream_then_play_starts`.
  - `test_stop_during_drain_frees_the_speaker` (the ack fires on the next step).
  - `test_stop_during_5s_head_delay_cancels_request` (WS fake).
  - `test_pause_before_first_byte_holds`.
  - `test_swipe_from_big_notice_cancels_the_play`.

**A2.**
- Cause: every `!p.began` exit of a chained `do_fetch` left `s_run` true.
- Design: `jpanel_core_step` has one exit per fetch; if not started → `PLAY_ENDED{failed|empty,
  run_continues=false}`.
- Test: `test_chain_fetch_failure_ends_the_run` over {link error, 500, 204, speaker busy}. The
  machine goes to PET with OOPS (204: no cue, the notice refreshes).

**A3.**
- Cause: capture cap 35 s (`audio.c CAPTURE_MAX_MS`), box raw check 30 s before
  `_trim_to_speech` → 413 → OOPS; 400 maps to FAILED.
- Design:
  - W1: the `send` raw ceiling = `PANEL_AUDIO_MAX` (`endpoint.py`, 35 s, already the `/converse`
    cap). Policy: the box accepts every recording a panel can capture.
  - F2: the panel maps 400 → `EMPTY` (`CUE_STOP`), 409 → `NOBODY`, 413 → `FAILED("too-long")`.
  - Stale comments fixed.
- Tests:
  - `test_send_accepts_exactly_the_capture_cap`: posts `CAPTURE_MAX_MS × 32` bytes, read from
    `audio.c` → 200.
  - `test_send_refuses_cap_plus_one` → 413.
  - `test_capture_cap_pinned_across_firmware_and_box`: `PANEL_AUDIO_MAX == CAPTURE_MAX_MS × 32`;
    `panel_ws.MAX_BODY > PANEL_AUDIO_MAX`; `voiceMessage.MAX_MESSAGE_MS == MAX_MESSAGE_MS`.
  - `test_old_firmware_contract_send_35s`.

**A4.**
- Cause: `do_played` fires once; the chain re-fetches the unacked head; the local list is shifted
  positionally.
- Design: §7.6. `/waiting` ids and `/next?id=` come from W3. `fetch_head` removes by id
  (positionally only with no ids). The `/waiting` test is updated for ids:
  `test_the_panel_can_hold_the_longest_poll_the_box_will_send`.
- Tests:
  - `test_lost_ack_is_retried_and_never_replays`;
  - `test_unreachable_ack_ends_run_and_notice_returns`;
  - `test_owed_played_id_filtered_from_view`;
  - backend `test_waiting_queue_carries_ids`;
  - backend `test_next_by_id_serves_that_row_or_204`;
  - backend `test_old_firmware_contract_next_at`.

**A5.**
- Cause: `start_listening` and the follow-up open the capture buffer while `jpanel_send` is
  uploading it.
- Design: §7.5 and the mic gate (§8.3).
- Tests:
  - `stream_tests: test_capture_open_refused_while_leased`;
  - `test_lease_released_on_post_failure`;
  - `jpanel_tests: test_upload_bytes_unchanged_under_listen_attempts` (the digest of the upload
    equals the recorded digest, with a listen attempted at every yield point);
  - `sm_tests: test_every_mic_entry_refused_while_leased`.

**A6.**
- Cause: a stop acks regardless of time sounded; "next" ran against a count missing the
  playing message; `sel` reset on every count change.
- Design:
  - F2: `HEARD_MIN_MS = 2000`; heard iff `stream_out_ms() ≥ min(HEARD_MIN_MS, 0.8 ×
    duration_ms)` (duration from `X-Jpanel-Duration-Ms`, else the absolute threshold).
  - W3: `POST /returned {id}` → `UPDATE … SET deliveries = deliveries - 1, returns = returns + 1
    WHERE id=… AND played_at IS NULL AND deliveries > 0 AND returns < 5`, under the panel's
    UPDATE policy. The cap on `returns` keeps the box's bound (0209) intact even against a
    misclassifying firmware. `/played` accepts optional `heard_ms`.
  - F3: "next" by id (§8.3–8.4).
- Tests:
  - `test_stop_in_preroll_returns_not_acks`;
  - `test_stop_after_threshold_acks_with_heard_ms`;
  - `test_returned_404_on_old_box`;
  - `sm_tests: test_next_during_playback_never_skips` (2, 3 and 8 messages);
  - backend integration `test_returned_refunds_only_own_unplayed`, which includes the sibling
    case;
  - backend `test_returns_are_bounded`.

**A7.**
- Cause: `do_poll` wrote entries before lowering `s_wait_known`.
- Design: §7.4.
- Tests: `test_snapshot_is_never_torn`. In the single-threaded host, with the hook mid-write, the
  reader returns false and render keeps the previous view; at quiet points the reader succeeds
  and equals the last published poll.

**A8, A9.** §7.3 and §7.2.
- `test_send_outcome_survives_concurrent_chain`: a send posted during a chain yields its own
  `SEND_DONE`.
- `test_only_producer_and_consumer_touch_cursors`: `stream.c` asserts the writer id on every
  cursor write in its host build.

**B0.** §8 in full.

**B1.**
- Cause: `PEND_REPLY` opened the recording in the same pass that `case UI_JP_PLAYING` re-armed
  the pair; `PAIR` precedes `CONFIRM`.
- Design: per-mode tables; reply opens RECORD immediately; `PEND_REPLY` removed (§8.3).
- Test: `test_reply_during_playback_opens_a_working_recording` (the reported sequence: the tick
  sends, no OOPS, no pair drawn, the exit not live).

**B2.**
- Cause: the hold gate `!menu_up` includes the badge and the pair. With a pair, the bottom half
  and the top right are live, so a hold at the pet centre fired REPLY (round-1 UX #2, verified:
  (184, 224) is in `confirm_hit`'s send quadrant because `fy ≥ over_h/2`).
- Design: pair FRESH/STANDING; hold → GRID iff the down point resolves to PET in the current
  mode's table (§8.3). STANDING pair discs: `CONFIRM_HIT_R` (82) around (92, `confirm_cy`) and
  (276, `confirm_cy`). (184, 224) is ≈171 px from either centre, so it is PET.
- Tests:
  - `test_hold_at_pet_centre_opens_grid_with_badge`;
  - `test_hold_at_pet_centre_opens_grid_after_pair_stands`;
  - `test_hold_in_fresh_pair_is_the_pair_press` (0.3.35 kept);
  - `test_a_long_press_on_a_menu_is_just_a_press` (ported).

**B3.**
- Cause: the badge and the pair share the top-left quadrant with different senders, and POPUP
  precedes PAIR.
- Design: PAIR only when `view.count == 0`; an unseen id ends PAIR (§8.3). This settles JP's open
  "face collision" question.
- Test: `test_quadrant_face_is_what_a_press_plays`, plus invariant I2 in the storm test.

**B4.**
- Cause: `audio_cue` dropped silently while `s_play` was busy; `CUE_MESSAGE` fired once;
  `popup_since` started while hidden.
- Design: §8.6 cue classes; the notice clock starts at `notice.first_visible`.
- Tests:
  - `cue_tests: test_cue_classes_order_expiry_preempt`;
  - `sm_tests: test_message_during_pet_reply_gets_chime_and_big_box`;
  - `test_heard_then_oops_both_sound`;
  - `test_pause_cue_sounds_over_stream`.

**B5.**
- Cause: nothing is drawn while BUSY, and the notice is hidden by a *send*.
- Design: `sending` is background (§8.2). The notice stays, and a press on it is refused with
  `CUE_STOP` (the alternative — a fetch queued behind a 20 s upload with controls over silence —
  was worse). F4 draws the sending overlay.
- Test: `test_notice_shown_and_refused_during_send`.

**B8.**
- Cause: the silent drop of a deliberate recording; a refused `VOCAB_SEND` kept `CUE_HEARD`.
- Design: §8.3 (RECORD lead → `CUE_STOP`; the mic gate's refusal is `CUE_STOP` for every
  path). The wake-phrase listen keeps its free silent drop: an accidental wake costs nothing
  (JP).
- Tests: `test_silent_recording_says_dropped`, `test_every_mic_refusal_sounds`.

**B10.**
- `PENDING_MS` and the audible expiry: §8.5, `test_press_behind_pet_reply_is_served`.
- The follow-up yields: §8.3, `test_follow_up_yields_to_pending_press`.
- Wrap: §8.5.
- "stop stop": §3.1.

**B-UX.**

| Gap | Decision | Wave / test |
|---|---|---|
| X takes two presses | One press: EXIT → PET, no pair. | F3 `test_exit_leaves_in_one_press` |
| Every failure is OOPS | SENT / NOBODY (new `CUE_NOBODY`) / EMPTY (`CUE_STOP`) / FAILED (`CUE_OOPS`). The contour is chosen in F4's mock set (WebAudio). | F2 mapping, F4 cue |
| No sending sound or picture | F4 overlay; `CUE_SENT` contour picked from the mock. | F4 |
| Text instructions for pre-readers | Icons and pips per the chosen mock; names stay for the adult. | F4 (GUI gate) |
| Badge has no count, animation or reminder | Pips and a slow pulse. **No reminder sound** (JP: a message that chirps when someone walks past is nagging). | F4 |
| "1/2" counts the remaining queue | `run_pos/run_total` (§8.4). | F3 `test_run_position_counts_the_playing_message` |
| Swipe during playback stops | "Next" (§8.3). "During playback" = mode PLAY. | F3 |
| Arrivals mid-turn miss the big box | Notice clock (B4). | F3 |
| The pair never expires, survives sleep, blocks | §4: kept; STANDING; cleared on dark wake. | F3 `test_pair_cleared_on_dark_wake_not_dim` |
| No hint how to stop recording | Familiar tick/cross glyphs and a shrinking hush ring (F4 mock). | F4 |

**C1.** §11.

**C2.**
- Cause: chips from pet chats include dead ids; `send_text` never validates and answers 201;
  the PWA's map finds no thread.
- Design:
  - W1: validate before TTS (404); the PWA refetches when no thread matches and keeps the draft
    on 404 ("That panel isn't enrolled any more").
  - W2: one id space; the picker excludes `retired` threads until P1 renders them (E4.2).
- Tests: `test_send_text_unknown_panel_404_before_tts` (the TTS fake is never called); vitest
  `send to a vanished panel keeps the draft and says so`.

**C3.**
- Design:
  - `POST /messages/{id}/retry` (owner; unplayed and `deliveries ≥ cap`) → `deliveries = 0,
    returns = 0`, then a nudge.
  - `DELETE /messages/{id}` (owner; only owner-sent or undelivered rows).
  - uuid guard on both. P1 adds the buttons.
- Tests: `test_retry_resets_deliveries_and_returns`, `test_retry_refused_when_deliverable`,
  `test_discard_only_owner_or_undelivered`.

**C4.**
- Design: W3 columns `heard_ms`, `offered_at`.
  - `offered_at` is stamped by `GET /waiting` (`… WHERE id = ANY(:ids) AND offered_at IS NULL`):
    one write per message lifetime.
  - The P1 copy follows E3.3. "Played to an empty room" cannot be detected, so the word is
    "Played".
- Tests: `test_played_records_heard_ms`, `test_old_firmware_played_body_leaves_heard_ms_null`,
  `test_waiting_stamps_offered_once`, `test_offered_at_rls_own_rows_only` (sibling case); a
  vitest status matrix.

**C5.**
- Design: W1 — `messages()` appends `between` rows to both threads, and the unplayed count is
  unchanged. P1 — the status shows on out and between rows.
- Test: `test_between_message_in_both_threads`.

**C6.**
- Design: `limit` per thread (`row_number() OVER (PARTITION BY thread_key …)`, where a between
  row counts in both) `UNION` every unplayed panel→owner row; `?before=&device=` pages one thread.
- Tests: `test_every_unplayed_owner_row_is_returned`,
  `test_per_thread_limit_keeps_quiet_panel_visible`.

**C7.**
- Design: single-range 206, `Content-Range`, `Accept-Ranges`, 416; a uuid guard on
  `message_audio`, `mark_read` and the C3 routes.
- Tests: `test_audio_range_206`, `test_audio_range_416`, `test_non_uuid_is_404` (×4).

**C8.**
- Design:
  - `errorDetail` joins `detail[].msg` lists (W1);
  - `maxLength={600}` (W1);
  - counter and hint (P1);
  - TTS timeout 60 s (600 characters ≈ 50 s of speech). The 600-character cap is kept (§13 Q2).
- Tests: `client.test: errorDetail flattens a pydantic list`, `test_send_text_tts_timeout_60s`.

**C9.**
- Design: `Cleared.kept_for: [{name, count}]` by recipient.
- Test: `test_clear_kept_names_recipient`.

**C10.**
- Design: `send_audio` returns 413 over `MAX_MESSAGE_BYTES`.
- Test: `test_owner_audio_over_cap_is_413`.

**C11.**
- Design: `seq` and `epoch` refs. A refresh applies only if it is the latest and no mutation
  happened since it started. Mutations bump `epoch` and then refresh.
- Test: a vitest deferred-promise test.

**C-UX.**

| Gap | Decision | Wave |
|---|---|---|
| No notification to Dad | Launcher badge via `GET /jpanel/summary {unplayed}`. Push deferred (§5). | W3 route, P1 |
| "Not heard yet" is ambiguous | `offered_at`/`deliveries` statuses. | P1 |
| Composer hint and limit | "Waits on her panel until she taps it" + a counter from 500. | P1 |
| Read on 60% visibility | Dwell ≥ 1.5 s **and** `document.hasFocus()`. | P1 `test_read_needs_dwell_and_focus` |
| Read starts the 30-day sweep | Policy kept; the footer states it. | P1 |
| Renames rewrite past names | **Not fixed.** A rename is usually a correction, and snapshots would freeze typos. Loss stated in §13. | — |
| Two clears | Kept; the confirm copy names what remains. | P1 |

---

## 10. Host foundations (F1, behaviour-preserving)

1. **`link_types.h`.** `link_req_t`, `link_res_t` and the callbacks move out of `link.h`, which
   cannot build on the host (`cfg.h` → `esp_err.h`).
2. **`stream.c`.** The ring and stream state, plus capture bookkeeping, extracted from `audio.c`
   **as they are today** (no `gen`, no lease yet). `audio.c` calls it.
3. **`jpanel_core.c`.** The decisions of `jpanel.c` behind `jpanel_io_t`: request, stream ops
   (backed by `stream.c` on the host), SHA-256, clock, sleep, a `yield(point)` hook, and event
   post — written as `jpanel_core_step`. `jpanel.c` keeps the task, the queues, cJSON parsing
   into `jpanel_waiting_t` (cJSON is an ESP-IDF component; the field names stay pinned by the
   backend contract tests), mbedtls, and the real io table.
4. **Host suites** `run_jpanel_tests` and `run_stream_tests` join `make test`, with:
   - a scripted box, including a `v0_3_39` mode;
   - the real `ring.c`/`stream.c`;
   - a fake clock;
   - a test-only public-domain SHA-256 in `firmware/host/` (no tool or package, so
     `dev-setup.sh` is unchanged);
   - yield hooks.
5. **Characterisation tests.** F1's tests assert 0.3.39's behaviour, **including its bugs**
   (A1, A2, A4, A7), each named with its finding ID. F2 inverts them, so each bug is visibly
   reproduced before it is fixed.
6. The backend tests that grep `firmware/main/jpanel.c` (in `test_jpanel_api.py`, e.g.
   `test_the_panel_facing_routes_are_where_the_firmware_looks`,
   `test_every_way_of_starting_a_message_waits_for_the_speaker`) are retargeted to
   `jpanel_core.c` in the same PR. `firmware/main/**` is in the backend job's path filter.

**Limit, stated.** The host proves logic under every interleaving the hooks name. It does not
prove the dual-core memory ordering of §7. That is reviewed against the stated fences and
watched on-device through the §7.8 telemetry.

---

## 11. Data migrations

### 11.1 Identity (W2) — messages belong to the panel, not its key

**Cause (C1).** `DevicesRepo.reflash` keeps the subject and replaces principals, but
`jpanel_message.sender_device/recipient_device` and `pet_turn.device_id` hold principal ids, and
RLS matches `app.principal_id`. A re-flash therefore strands unplayed mail on a revoked key:
never served, never swept, invisible to the owner (`messages()` drops non-roster ids), and never
clearable.

**Canonical subject.** Flashes before 0211 each made their own subject, so one physical panel
may span several. The rule, in one transaction:

1. **Group** `jpet` subjects by `(display_name, device_role)` at migration time. `rename_panel`
   already renames every subject sharing the old name, so a renamed panel's subjects are still
   one group.
2. **Canonical** = the subject holding the group's **newest live key**. If no key is live, the
   oldest subject. This is the subject the panel uses, which is safer than "oldest" when an old
   subject carries a since-renamed history. `DevicesRepo.reflash` changes the same way ("the
   subject with a live key for this name and role, else the oldest"), so a later flash cannot
   jump to another subject.
3. **Re-point** every `device_key` principal in the group to the canonical subject. The newest
   `endpoint_panel` row moves to it if it has none, and the others are deleted. Both steps are
   **declared irreversible** (§11.3).

   This changes `DevicesRepo.delete`/revoke reach: acting on the canonical subject now touches
   every key of that panel, which is the intended meaning of "this panel". The Panels-tab copy
   says so (P1).
4. **Backfill with the old value kept on the row.** New nullable columns
   `jpanel_message.sender_principal`, `jpanel_message.recipient_principal` and
   `pet_turn.device_principal` receive the current principal-id values. Then
   `sender_device/recipient_device/device_id` are set to the principal's (now canonical)
   subject. Values matching no principal are left as-is and counted.

   **No mapping table is created**, so there is no new RLS surface: the old values live on rows
   already covered by `jpanel_message`/`pet_turn`'s policies.
5. **RLS.** Recreate `jpanel_message_panel_read/_send/_played` and `pet_turn_panel_write` with
   `current_setting('app.subject_id', true)` in place of `app.principal_id`, still requiring
   `principal_kind = 'device_key'`. Owner policies are untouched.
6. **Rows written after W2.** Owner inserts set the `*_principal` columns to the recipient
   subject's newest live key. Panel inserts set them to `principal.id`. This keeps a forward
   revert exact for anything a panel sent.

### 11.2 Every call site in W2 (round-1 backend #1, verified)

Every panel-authenticated query on these tables switches from `ctx_for(principal)` to
`panel_context(principal.id, principal.subject_id)`. A `device_key` without a subject is refused
(403, logged). The sites:

| Site | Change |
|---|---|
| `api/jpanel.py`: `send` insert, `waiting`, `next_message`, `message_pcm`, `played`, `/returned` (W3) | `panel_context`; `:me` = subject |
| `api/endpoint.py`: settings route `_waiting_count(request, ctx, principal.id)` (the 3 s count) | `panel_context` + subject |
| `api/endpoint.py`: `/converse` `pet_turn` insert (`"dev": principal.id` inside `except Exception`) | `panel_context` + subject + `device_principal`. **Without this, every transcript is silently dropped.** |
| Nudge key space: `endpoint.py` settings `nudge.remember(principal.id)`, `jpanel.py` `/events` (`nudge.remember/attach(principal.id)`), `jpanel.py` `waiting` `nudge.remember`, `panel_ws.py` `self.device = principal.id` (`_live`, `nudge.attach`) | all keyed by subject; `nudge.fire(subject)` from `send`, `send_text`, `send_audio` and C3's retry |
| Owner routes `messages`, `send_text`, `send_audio`, `clear_history`, `pet_chats`, `clear_pet_chats`, `rename_panel`, C3 routes | the subject id; **for one release** also accept a principal id, mapped to its canonical subject |
| `_panel_names` | the subject roster: canonical `jpet` subjects with ≥ 1 live key; `DISTINCT ON (label)` retired |
| `messages()` | a thread for every subject referenced by a row, `retired: true` without a live key; unmatched ids go under one retired "Unknown panel" thread |
| `pet_chats` | lists `jpet` subjects. Turns from `display`-role devices (the owner's own display also uses `/converse`) are **not** shown in a child's thread. They remain removable by "clear all". |

Not changed: `endpoint_status` (telemetry, principal-keyed, not messaging).

**Guard.** The unit test `test_no_panel_handler_uses_ctx_for_on_messaging_tables` parses
`api/jpanel.py` and `api/endpoint.py` (AST). It fails if a function that takes a `PanelDep`
parameter and mentions `jpanel_message` or `pet_turn` calls `ctx_for`.

### 11.3 Rollback is a forward revert

There is no owner path that runs `alembic downgrade` (§6), and deleting a revision file breaks
later updates ("Can't locate revision"). So:

- The W2 revision's `downgrade()` is implemented for developers only. The **owner path** is a
  **forward revert revision**, prepared and tested in W2 itself as
  `test_identity_forward_revert_pg`, and merged only if needed. It:
  - restores the four columns from the `*_principal` columns, where those are not NULL;
  - restores the principal-keyed policies.
- After the revert, the old code runs against principal-keyed rows. A panel on its newest key
  receives mail sent before **and** after W2.
- Irreversible and harmless to old code: the re-pointed `principals.subject_id` (the old roster
  reads labels, not subjects) and the deleted duplicate `endpoint_panel` rows (already
  unreachable, because `panel_context` pins one subject).
- The W3 columns are additive and need no revert. A forward revert of W3 code leaves them unused.

### 11.4 RLS isolation tests (CLAUDE.md #3)

No new table is created. Each changed policy is re-proved under the exact context a panel runs
as, against real Postgres:

- **`test_jpanel_message_rls.py`**, extended to the subject GUC:
  - a panel reads its own inbox and sent rows;
  - the **sibling cannot read, update, `/returned` or stamp `offered_at`** on them;
  - no panel can insert as its sibling;
  - two live keys on one subject read the same inbox;
  - the `*_principal` columns are readable only where the row is.
- **`test_pet_turn_rls.py`**, extended: inserts are pinned to the panel's own subject; there is
  no panel SELECT; a sibling insert is refused.
- **`test_jpanel_identity_migration_pg.py`**:
  - seeds three "Quorra" subjects and 13 principals (one renamed group, one with a live key on a
    non-oldest subject), mail to a revoked key, a between row, pet turns from a jpet and from a
    display, and an orphan id;
  - runs the upgrade and asserts the canonical choice, every row mapped, and the orphan listed;
  - asserts the panel on its newest key gets old mail through `/waiting` and `/next`;
  - asserts `endpoint_panel` is preserved and the thread count equals the physical panel count.
- **Route-level tests under a real panel key** (`tests/integration`, testcontainers):
  - `test_converse_records_pet_turn_after_w2` (the row exists);
  - `test_settings_waiting_count_after_w2`;
  - `test_events_nudge_reaches_subject_after_reflash`;
  - `test_send_after_reflash_delivers`.
- **`test_identity_forward_revert_pg`** (§11.3).
- **`test_jpanel_contract_columns_pg`**: `heard_ms`, `offered_at` and `returns` are written only
  through the recipient's policy.

### 11.5 Contract columns (W3)

```sql
ALTER TABLE app.jpanel_message
    ADD heard_ms integer,
    ADD offered_at timestamptz,
    ADD returns integer NOT NULL DEFAULT 0;
```

No backfill: NULL is the honest "unknown" for history.

---

## 12. Waves

One PR per wave (PROCESS.md). Local gates are the package's **whole** set, run from its own
directory (CLAUDE.md #11):

- **`backend`:** `ruff check .`, `ruff format --check .`, `pyright`, `pytest` (100 columns; the
  testcontainers suite runs in CI only).
- **`frontend`:** `npm run lint`, `npm run typecheck`, `npm run test`, `npm run build`.
- **`firmware`:** `make -C firmware/host test` locally. CI also rebuilds the image with ESP-IDF
  and byte-compares it against `firmware/dist/`. Every firmware wave therefore:
  - bumps `firmware/version.txt`;
  - commits a rebuilt `dist/` (`scripts/firmware-setup.sh` once, then `idf.py build`, then
    `scripts/firmware-dist.sh`);
  - keeps the bootloader byte-identical (`firmware/README.md`).

Every wave runs `scripts/docs-freshness.sh` and reconciles docs (CLAUDE.md #9):

- this plan's markers;
- JP §3b's contract table and its open "face collision" item;
- `firmware/README.md` and `firmware/host/README.md`;
- the `ROADMAP.md` and `plans/README.md` rows.

No wave adds a dependency or a setup step; if one does, `scripts/dev-setup.sh` changes in that
PR (CLAUDE.md #8). No operator step is shell-only (CLAUDE.md #10):

- migrations and dist reverts reach the box through Ops → Update;
- panels update by OTA;
- verification uses the PWA, the Ops fleet card and the debug-API telemetry.

| Wave | Contents | Depends on | CI jobs |
|---|---|---|---|
| **W1** — box + PWA, nothing visible | A3 (box); C2 (validation + refetch); C5 (both threads); C6; C7; C8 (`errorDetail`, `maxLength`, timeout); C9; C10; C11; `test_old_firmware_contract_*`. **Fixes A3 for every panel in the field.** | — | backend, frontend, docs |
| **W2** — identity | §11.1–11.4, including the prepared forward-revert revision; the picker hides `retired`. RLS → owner escalation and a red-team review. | W1 | backend, frontend, docs |
| **W3** — contract for the new firmware | `/waiting` ids + `offered_at`; `/next?id=` + `X-Jpanel-Duration-Ms`; `/played {heard_ms}`; bounded `/returned`; C3 routes; `/summary`; §11.5. All additive (§6). | W2 | backend, docs |
| **F1** — host foundations | §10. No behaviour change: a version bump and a rebuilt dist. | — (parallel to W1–W3) | firmware, backend (path filter), docs |
| **F2** — engine model | §7 entirely: A1/A2/A4/A5/A6/A7/A8/A9 engine side, the A3 mapping, cue classes in `cue.c`/`audio.c`, telemetry. `display.c`'s current consumers are switched **mechanically** to `jpanel_snapshot` + event drain (its `switch (jpanel_state())` becomes a switch on drained events; `jpanel_running`/`fetching`/`in_from` become render-side values set from those events). The old UI logic is otherwise untouched, so this wave ships on today's interaction model. | F1, W3 | firmware, backend (contract pins), docs |
| **F3** — the panel state machine | §8: `panel_sm.c` replaces `ui.c` and `display.c`'s logic; the executor; per-mode tables; ported plus new tests; the property test; `check_no_logic_in_display`; every B fix. Replaces the old logic in one wave (§8.8). Owner on-glass checklist in the PR. | F2 | firmware, docs |
| **F4** — panel visuals for pre-readers | Sending overlay, pips, badge pulse, tick/cross glyphs, hush ring, `CUE_NOBODY`, the `CUE_SENT` contour. **GUI gate:** three interactive mocks (WebAudio cues included) before building; the chosen one lands in `docs/mocks/jpanel-panel-messaging/`. | F3 | firmware, docs |
| **P1** — PWA honesty and controls | C3 buttons; C4 copy; C5 status; composer counter and hint; launcher badge; read dwell; retention line; retired threads; "Load earlier"; Panels-tab delete/revoke copy. **GUI gate:** three mocks first. | W3 | frontend, docs |

Default order: W1 → W2 → W3 → F1 → F2 → F3 → F4, with P1 any time after W3. F1 and P1 may run
as parallel tracks; each remains its own PR and its own wave gate.

Every wave is shippable on its own. In particular, F2 keeps today's UI on the new engine, and F3
lands only when its whole host gate is green.

## 13. Risks, losses, open questions

**Risks.**
- **F3 replaces the panel's interaction logic in one wave.** Mitigations:
  - every old scenario is ported;
  - the property test and the end-to-end host runs;
  - runtime invariant counting with recovery to PET;
  - OTA rollback for crashes, and a dist revert through Ops → Update for misbehaviour (§8.8).
- **Memory ordering is reviewed, not host-tested** (§10).
- **W2 rewrites RLS on children's recordings.** Mitigations: the policy shape is unchanged (one
  GUC); the sibling cases are re-asserted; route-level tests run under real keys; a tested
  forward revert exists.
- **HTTPS-fallback cancel cannot interrupt the head phase.** A stop there holds the engine for up
  to the request timeout. The child hears "didn't go" for a skip in that window (E2.10), and
  nothing plays.

**What the owner loses.**
- **No push notification.** Messages wait until he opens the PWA; the launcher badge narrows
  that.
- **A failed send is not kept for resend.**
- **"Played" ≠ "Heard."** The panel cannot tell an empty room from a listening child.
- **Renames rewrite past names.**
- **A stop within 2 s hands the message back.** The notice returns for it; that is intended, but
  she will notice it.
- **A reply pressed in the pair's first 10 s from the pet centre is a reply.** This is the 0.3.35
  and quadrant rule; the recording shows the recipient's face, and the cross cancels.
- **The notice cannot be played while her own message is uploading.** She hears the refusal and
  can press again after "sent".

**Open questions for the owner.** Each has a default that the plan executes unless he overrules
it.

1. **`HEARD_MIN_MS = 2 s`** — one constant.
2. **The typed-message cap stays 600 characters** (~50 s spoken). Whether a message to a
   four-year-old should be shorter is his call (JP §5).
3. **Push notifications** stay deferred unless he wants a web-push plan.
4. **The pair turns STANDING after 10 s** (`REPEAT_MS`, his own "about 10 seconds" figure).

## 14. Review log — round 1 (cold adversarial: firmware, UX, backend)

All three verdicts were RED. Every item was checked against the code before it was accepted. All
were accepted, some with a correction noted here; none was rejected.

| Item | Resolution |
|---|---|
| FW-B1 cancel flag has no owner when no producer runs (stop with no stream, stop during drain) | Verified (stop sites: swipe, face, standby, pair exit; `audio_playing()` true while `stream_filled() > 0`). The flag is removed: the consumer discards by generation (§7.1–7.2). Tests in §9 A1. |
| FW-B2 seqlock fences | Accepted as specified (§7.4). |
| FW-B3 A5/A9 tests would exercise a fake | `stream.c` extracted and host-built; its tests run the real code (§7.2, §10). |
| FW-B4 a stop cannot interrupt a network wait | Verified (`ws_request` head loop polls only `timeout_ms`). `link_req_t.cancelled`, polled every `LINK_STEP_MS`, plus `wsp_cancel`; HTTPS head limits stated; stale commands drop on dequeue; no busy token (§7.3). |
| FW-B5 ack outbox self-contradictory; reintroduces A2 | One slot. A failed attempt ends the run; fetches are refused while it is owed; the owed PLAYED id is filtered from the view (§7.6). |
| FW non-blocking (face_task unpinned; render reads `jpanel_in_from`/`last_from`/`s_in_id`; event count per run; read `consumed` before abort and "excluding silence"; buffer sizes; `next_poll` wrap; `uint32_t` CAS and release on post failure; core step function; seqlock host test; jpanel stack telemetry; `s_play` writers) | All taken: §7 intro, §7.3 (by-value events, drain all, buffers), §7.2 (output counter), §7.5, §7.8, §7.4 test semantics, §7.7 (`s_play` stated as the one exception). |
| UX-B1 F2 frame order differs from the tests | Removed at the root: one `sm_step` per frame shared by device and tests, with no mid-frame re-sampling (§8.1). Calibration skips the step. |
| UX-B2 a hold cannot work while the pair stands; a hold at the centre = reply | Verified ((184, 224) is in the send quadrant). Pair FRESH/STANDING, with the standing pair hit by its discs; tests at the pet centre (§8.3, §9 B2). The owner's never-expire rule is kept; default in §13 Q4. |
| UX-B3 the press is refused silently while the token is held | The token is gone. PLAY PENDING/FETCHING have audible deadlines; the notice during a send is refused with a cue (§8.3, §8.5, E2.10). |
| UX-B4 the cue queue silenced transport cues | Cue classes: press cues preempt and sound over the stream; only outcome and message cues queue (§8.6). |
| UX-B5 re-chimes for returned and unacked messages | "New" = an unseen id; the owed PLAYED id is filtered (§8.4, §7.6). |
| UX-B6 exit live during a voice-started recording | Per-mode tables: RECORD has no EXIT (§8.3). |
| UX-B7 F2/F3 sequencing and `jpanel_state` replacement | F2 switches `display.c` mechanically to events and the snapshot; F3 replaces the logic. Every `jpanel_state()` consumer — the pending-reply settle (removed), the start-record refusal (lease), the idle `used` test (`sm_busy()`) — is named in §7.3/§8. |
| UX non-blocking (swipe definition; skip with returned head; cue per failed reason; follow-up after arrival; `check_no_dup` list; `sound` gate; 12 s window; talk lease release) | §8.3 (definitions and cue map), §8.4, §8.1 (mechanical derivation; `sound` gate), §8.5 (EXIT live in PENDING), §7.5. |
| BE-B1 W2 misses call sites outside `jpanel.py` | Verified (`/converse` pet_turn insert inside `except Exception`; settings `_waiting_count`; nudge keys in `endpoint.py`, `/events`, `panel_ws.py`). Enumerated in §11.2, with an AST guard and route-level tests. |
| BE-B2 no box rollback path | Verified (`update-inner.sh` is fast-forward + `upgrade head`). A forward revert revision, prepared and tested; irreversible steps declared (§11.3); §6 column relabelled. |
| BE-B3 `jpanel_identity_map` was a new table without RLS | Replaced by per-row `*_principal` columns on tables that already have policies (§11.1). |
| BE non-blocking (A3 cap + 1 test; unbounded refund; canonical by display name; retired chips before P1; delete reach; display-role turns; "0212"→0211; intro count) | `test_send_refuses_cap_plus_one`; a `returns` cap; canonical = the subject holding the newest live key, with `reflash` aligned; W2 hides retired; delete reach documented; display turns excluded from child threads; date corrected; intro recounted. |
