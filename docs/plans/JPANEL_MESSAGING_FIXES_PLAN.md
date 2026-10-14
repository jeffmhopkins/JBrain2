# jpanel messaging — the fix plan after the adversarial review

> **Status:** In progress · **Last verified:** 2026-10-14 · **Waves:** FW🟡 (firmware 0.4.0–0.4.14, §15) W1◻️ W2◻️ W3◻️ P1◻️
>
> **FW's code has landed** — firmware 0.4.0, agents A–E of §15 — and stays open rather than ticked
> until it has run on hardware: `SPEECH_MIN_INTERNAL` is provisional until the panels' boot logs
> measure `S`, `T` and `P` (§6a), and the first reports' `int_min` and `speech_ok` must clear it
> (§15 step 5). F4's visuals ship as placeholders. What remains is listed in §15 "Status".

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
| Cold review, round 6 | 5 blocking + 36 non-blocking (backend GREEN) |
| Cold review, round 7 | 6 blocking + 34 non-blocking |
| Cold review, round 8 | 7 blocking + 33 non-blocking |
| Cold review, round 9 | 5 blocking + 33 non-blocking |
| Cold review, round 10 | 2 blocking + 44 non-blocking (backend and firmware GREEN) |
| Cold review, round 11 | 4 blocking + 35 non-blocking |
| Cold review, round 12 | 6 blocking + 43 non-blocking; plus the 0.3.39 boot-loop incident (§6a) |
| Cold review, round 13 | 7 blocking (F0 and two UX) + 39 non-blocking |

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
placeholder-named **Quorra** and **Tamsin**. Names in this plan are as of firmware 0.3.39, which does not boot on real hardware; F0 releases it with its persistent WebSocket disabled, as 0.3.40 (§6a).

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
- **E1.6** A hold that began on the pet centre opens the "who?" menu when nothing is up, when a
  badge is up, or when the again/reply pair has stood for 10 s. A finger still down from a press
  on another control (the exit, the face, a notice press that was ignored) never becomes a hold.
  - Today's rim, maintenance-tap and swipe guards stay.
  - **In a pair younger than 10 s, a hold opens nothing**, as today (0.3.35). The press belongs
    to whatever it landed on:
    - on the reply quadrant (lower right) it opens a reply recording, which shows the
      recipient's face and can be cancelled with the cross;
    - on the again quadrant (lower left) it replays;
    - on the exit quadrant (upper right, `x ≥ 184, y < 224`) it dismisses the pair with
      `CUE_STOP`;
    - on the rest of the upper pet it does nothing.
  - **With a standing pair**, the exit quadrant stays live: about a quarter of the pet lies in
    it, so a hold that lands there dismisses the pair and a second hold opens the menu. Only a
    hold outside the exit quadrant and the disc reach opens the menu at once. The tests use
    coordinates on both sides of that line.

**J2 — a child receives, plays, stops, replies**

- **E2.1** Pressing the notice plays the message whose sender the notice shows.
- **E2.2** A stop — the exit (after its grace, E2.12), the button, a face press or a swipe — is
  honoured at any engine phase.
  - The speaker is silent within 40 ms plus about 90 ms of DMA: the message, and any cue or pet
    reply already sounding, as `audio_stop()` does today. The stop's own cue then sounds. A send
    outcome still pending is not lost (E1.2).
  - Nothing chains on.
  - The sound is cut by the gen within that bound whatever the network does. With the socket
    disabled from F0 (§6a) the HTTPS rule applies; where the socket runs, a WebSocket request
    is cancelled at its next 250 ms wait step, plus however long `ws_send` takes to deliver the
    cancel under its own clamp (HTTPS: §7.6).
  - Sends are exempt (E1.2).
- **E2.3** The top-left face comes, every frame, from the same function that addresses reply
  (I2).
  - Face press cycles the selection and a swipe moves it, clamped (JP 0.3.37).
  - Either one stops the sound only if the selection actually moved.
  - No message is skipped.
- **E2.4** From F3, and only when the fetch carried `X-Jpanel-Delivery`:
  - a message counts as heard after `HEARD_MIN_MS`, or 80% of a short one;
  - a shorter stop hands the message back, refunded idempotently and within a cap — except a
    REPLY, which counts as heard (§4).

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
  playback chimes on the first frame the child can act on it. That is the PET screen, with the
  notice drawn, the microphone closed and no pet reply. During her own send the notice is drawn
  and a press on it answers "busy"; that still counts (§8.4).
  - It never chimes for a message that was already played in the run it arrived during.
  - Its 15 s big box counts from first visibility. A second arrival does not restore it; a wake
    from dark does (JP).
- **E2.10** A press that cannot be served always ends audibly.
- **E2.11** A message already announced is never announced again.
- **E2.12** PAUSE and REPLY are claimed and ignored until ON (JP 0.3.36, extended: §4). The exit is dead for
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
  - couldn't be delivered yet, or ever — three cases:
    - **gave up**: the box hit its delivery cap and the row is still addressed to a live key;
    - **waiting for re-flash**: the recipient key is not live, but a future re-address can still
      deliver it — the row is inside `READDRESS_WINDOW_DAYS` and its subject still exists and is
      not a `merged:` tombstone (a revoked panel the owner means to re-flash). Rows on an
      older same-name subject that a re-flash did not pick stay in this status until they age
      out; they need an explicit merge (§13);
    - **keyless**: no future re-address can deliver it — the recipient key is not live **and**
      the row is older than the window, or its subject is NULL, hard-deleted or a `merged:`
      tombstone. A waiting-for-re-flash row becomes keyless when it ages out.

  Twin-to-twin messages get the same statuses.
- **E3.4** A *gave-up* message can be retried. **Any unplayed message can be discarded
  individually** by the owner — an explicit act (§4).
- **E3.5** The "kept" count after a clear names the right child.
- **E3.6** A twin-to-twin message appears in both threads. Clearing either thread deletes it
  unless it is a panel-addressed unheard row that is **not keyless**. Gave-up and waiting-for-re-flash rows are kept by a
  clear (`test_clear_keeps_in_window_mail_of_revoked_panel`).
- **E3.7** A stale poll never removes a bubble that was just sent.
- **E3.8** Errors read as sentences. The composer shows its limit and a hint. A recording made at
  the cap is still sent.
- **E3.9** The launcher badge equals what the screen renders: both come from W1's
  `GET /api/jpanel/summary` → `{"unread": int, "threads": [{"device_id": str, "unread": int}]}`,
  computed by the same query function as the thread list (C6), with the same
  `?include_retired=1` flag: without it, retired, unknown and "Displays" groups are not counted,
  exactly as they are not drawn.
- **E3.10** iOS Safari can play the audio. A malformed id answers 404.

**J4 — re-flash**

- **E4.1** Unplayed mail created within `READDRESS_WINDOW_DAYS` follows a re-flash or a rotation
  to the new key, including mail inserted concurrently. History stays in one thread.
- **E4.2** From P1, retired panels are visible and clearable, and the owner can merge them
  explicitly; the debug API can merge them from W2. Until P1 they stay hidden, as today. Nothing
  merges automatically.

**J5 — network loss**

- **E5.1** A short stream is never acknowledged. From F3, with the delivery header, a stop
  acknowledges only after `HEARD_MIN_MS` or by REPLY (E2.4).
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
| A2 | ✓ | Every command ends by its published deadline (F2, which relies on that alone); the UI backstops on it (F3, §7.5c) | F2, F3 | `test_chain_fetch_failure_returns_to_idle` |
| A3 | ✓ | Firmware caps under the box's limit; box accepts the cap (+0.5 s); caps pinned | FW, W1, F2 | `test_a_full_panel_recording_is_one_the_box_accepts`, `test_send_accepts_exactly_the_capture_cap`, `test_capture_caps_pinned` |
| A4 | ✓ | Serial engine; ack before the next command; run by id | W3, F2 | `test_lost_ack_is_retried_and_never_replays` |
| A5 | ✓ | Render leases the capture before posting | F2 | `test_lease_taken_before_post` |
| A6 | ✓\* | Hand-back (F3) gated on the delivery header; selection over the ordered set `S` (§8.2) | W3, F3 | `test_stop_in_preroll_returns_not_acks`, `sm: test_face_cycles_without_skipping` |
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
| C3 | ✓ | Retry gave-up rows; discard any unplayed row (owner act) | W2, W3, P1 | `test_unheard_to_retired_panel_is_discardable`, `test_debug_discard_route` |
| C4 | ✓ | `heard_ms`, `offered_at` | W3, F3, P1 | `test_played_records_heard_ms` |
| C5 | ✓ | Between rows in both threads | W1, P1 | `test_between_message_in_both_threads` |
| C6 | ✓ | Per-thread limit + every unplayed owner row | W1, W2 | `test_every_unplayed_owner_row_is_returned` (live keys, W1; dead keys, W2) |
| C7 | ✓ (Range plausible) | 206; uuid → 404 | W1, W3 | `test_audio_range_206`, `test_non_uuid_is_404` |
| C8 | ✓ | `errorDetail`; `maxLength`; TTS timeout 60 s | W1, P1 | `client.test` |
| C9 | ✓ | `kept_for` | W1 | `test_clear_kept_names_recipient` |
| C10 | ✓ | PWA clamps; owner route trims at cap + slack | W1 | `voiceMessage.test: clamps_to_cap`, `test_owner_audio_at_30_02s_is_accepted`, `test_owner_audio_well_over_cap_is_trimmed` |
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
  - JP's "Capped at 20 s";
  - `display.c:4129-4132` ("AGAIN button owns the same corner for its ten seconds"), in F3;
  - `clear_history`'s docstring ("kept indefinitely", `jpanel.py:1250-1253`), in W2, with JP Part
    B and `sweep.py`.

## 4. Owner decisions

Appendix B lists every owner decision the machine touches. Each is kept unless this section
changes it.

**Decided by the owner on 2026-10-11**

| Question | Decision |
|---|---|
| Standing pair | **Yes.** After `REPEAT_MS` (10 s) AGAIN and REPLY are hit only by their drawn discs (`CONFIRM_HIT_R`); the exit keeps its quadrant; and a hold on the pet outside those opens the menu (E1.6). |
| Exit during playback | **One press.** It stops and leaves, and clears the offer. A message that ends by itself still produces the pair. |
| Refusal sound | **A new, distinct `CUE_BUSY`** for every refusal. `CUE_STOP` stays cancel/dropped. F4 designs it; F3 ships a placeholder (`test_cue_busy_distinct`). |

**Changed because the old behaviour is itself the finding**

- **Ack-on-stop.** From F3 only, and only when the fetch carried the delivery header, a stop
  below `HEARD_MIN_MS` hands the message back (C4/A6).
- **`/next?at=` → by id** (A4, A6).
- **The notice clock** counts from first visibility (B4).
- **A returned message reappears as a badge**, not a big box. The child has just dismissed it;
  a wait starts big only if one of its ids is still owed a chime (§8.4).
- **The selection goes home on an unseen id seen in PET, not on any count change** (on a box
  that sends ids; without ids, today's count rule stays). This narrows
  today's rule: a count drop from playing no longer moves a selection that still resolves (A6),
  and a message she picked with the face during playback stays picked (§8.2).
- **The transport is ignored until ON**, longer than JP 0.3.36's "while the press is pending":
  it now also covers the fetch and the preroll, where a pause lied (A1).
- **A pending press that expires sounds `CUE_OOPS`.** `ui.h`'s `PENDING_MS` comment made it
  silent by design; E2.10 requires every unserved press to end audibly.
- **A stop under `HEARD_MIN_MS` by REPLY still counts as heard** (F3): she is answering the
  message, so handing it back would raise it again as a badge after her reply.
- **Retention: keyless rows, and the owner's discard.** JP Part B *"### Data"* says *"unplayed
  messages are kept indefinitely"*. Two narrowings:
  - a *keyless* row (E3.3: no future re-address can deliver it) can never be heard, so a thread
    clear now deletes it. A *waiting-for-re-flash* row is kept by a clear, as today;
  - the owner (or the debug API on his behalf) can discard **any** unplayed row, one at a time.
    That is an explicit owner act, never automatic.

  *Gave-up* rows (cap reached, live key) are otherwise unchanged: kept by a clear, retryable. A
  gave-up row inside the window that is re-addressed by a re-flash is **offered again** with
  `deliveries = 0`: its attempts were spent on a key the panel no longer holds. W2 updates JP
  Part B "### Data" and the cross-reference in `sweep.py`'s docstring.
- **A press that changes the mode gives the new mode's targets a grace** of `EXIT_GRACE_MS`
  (§8.2): the tick and cross after REPLY or any grid cell, and FACE after the notice press. A
  bounce of the press can no longer send, drop or skip anything.
- **Two press cues now actually sound (F3).** On 0.3.39 each is followed in the same frame by
  `jpanel_stop()` → `audio_stop()`, which sets `s_play_pos = s_play_len` and cuts it:
  - the button's standby `CUE_STOP` (`display.c`'s standby path);
  - the reply press's `CUE_REPLY` (`display.c:2958`, then `jpanel_stop()` at `:2962`).

  The exit corner calls `jpanel_stop()` *before* its cue, so its `CUE_STOP` already sounds, after
  cutting whatever was sounding. F2 keeps all of this exactly (its `jpanel_stop()` wrapper is
  `silence` + `stream_stop()`, §7.7). In F3 every stop the child chooses — the button, EXIT, FACE
  and swipe — emits `SILENCE` first and its cue at a later seq, so the cut is kept and the cue
  always sounds (§8.3). A send outcome still pending survives a silence (§7.4).

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
  deletes a migration revision file**: W1 adds a step to the **backend job** (an already-required check, so branch protection enforces
  it) that runs on `pull_request` **and**
  `workflow_dispatch` with no `if:` gate — `ci.yml` notes that an App-opened PR starts no
  `pull_request` run, so a dispatch is how its checks run — changes the backend job's checkout
  to `fetch-depth: 0` (it is 1 today), and
  fails if `git diff --no-renames --diff-filter=D --name-only origin/main...HEAD --
  backend/migrations/versions/` is non-empty. A later re-land of a reverted wave therefore finds
  its revision file still present and must not add it again.
  - W2 is data-only.
  - W3's columns are nullable or defaulted (`returns integer NOT NULL DEFAULT 0`), checked by
    `test_w3_schema_accepts_pre_w3_box_insert_shapes`.
  - Under rolled-back code, `merged:` tombstones show in the device list. This is cosmetic.
  - If W3 is rolled back while W2 stays, re-address stops resetting `returns` and `offered_at`;
    a row re-addressed meanwhile keeps stale values when W3 re-lands. Bounded by the cap of 5 and
    cosmetic in the status.

Every box change is additive. Firmware degrades to today's path whenever a field, header or route
is missing.

| Change | New box + 0.3.39 / 0.3.40 (F0) | Old code + new firmware |
|---|---|---|
| `/send` ceiling = cap + 0.5 s | A3 fixed for every panel in the field | 413 → `FAILED("too-long")` |
| `/waiting` `queue[].id` | ignored. `test_waiting_body_fits_0_3_39_buffer` (W3) pins the worst case against 0.3.39's 1,023-byte cap: 8 entries with ids, `from_owner=false`, names at **31 bytes**, a 4-digit `count`, and a sibling — about 1,021 B. The bound is real because W2 gives the flash name the rename rule (`MAX_PANEL_NAME` = 14 and `_panel_name`'s charset, none of which grows under JSON escaping, §11.1 item 8) **and** W1's `/waiting` cuts every name so that even its `ensure_ascii=True` escaping is at most 31 bytes, on a character boundary. The body itself goes out as UTF-8 (`JSONResponse`, relayed unchanged by `panel_ws`), which is never longer, so the escaped length is a safe upper bound and a name stored before the rule, quotes, control or non-ASCII characters included, cannot overflow it. The worst case leaves 2 bytes of headroom, so the pin test is load-bearing. The sibling comparison uses the **uncut** label and cuts afterwards, so a legacy name over 31 bytes still finds its sibling. `/next`'s `X-Jpanel-From` gets the same cut and is made printable ASCII (anything else becomes `?`; Starlette refuses a header it cannot encode), and `test_next_head_fits_link_head_max` assumes both. | positional `?at=`; "new" means a count rise |
| `/next?id=` (still `recipient_device = :me AND played_at IS NULL AND deliveries < cap`) + `X-Jpanel-Duration-Ms` / `X-Jpanel-Delivery` (`UPDATE … RETURNING`) | not sent | the firmware also sends `at`, which a rolled-back box honours; with no delivery header a stop acks as today. The head frame stays ≤ 0.3.39's `LINK_HEAD_MAX` (1024 B), pinned by `test_next_head_fits_link_head_max`. |
| `/played {id, heard_ms}` | `heard_ms` NULL | extra field ignored |
| `/returned` (tunnelled through `_ALLOWED`) | never called | F3 with no delivery header never calls it. If it is called, the tunnel answers 404 for a path it does not allow: counted `no_refund`, slot cleared. |
| Re-address, owner grouping, `offered_at` | box-side | the data stays principal-keyed |

**PWA skew.** The box serves the PWA, but a service-worker-cached copy can lag one release.
- Every owner-route change is additive.
- Retired, unknown and "Displays" groups need `?include_retired=1`, which only P1's PWA sends.
- A cached PWA therefore sees today's shapes.

---

## 6a. Baseline (F0): 0.3.39 does not boot

*F0's content ships inside 0.4.0 (§15); it is no longer a release of its own.*

**The incident, confirmed by the panel's console over USB.** A panel updated 0.3.38 → 0.3.39
aborts 3–4 s into every boot; no panel has run 0.3.39 successfully. Every boot logs:

```
mem: wifi internal 119083 free / 45056 largest
link: socket up — internal free 70643, low-water 39720
mem: pre-speech internal 70535
speech: starting with 70535 B            (passes SPEECH_MIN_INTERNAL, 48 KB)
… AFE created …
ESP_ERROR_CHECK failed: ESP_ERR_NO_MEM … esp_phy/src/phy_common.c:118
  phy_track_pll_init: esp_timer_create(…)  → abort → reboot
```

0.3.39's persistent TLS WebSocket (#1565) holds ~48 KB of internal RAM (119 → 70.5 KB free).
Speech and its AFE then **start** in the 70.5 KB that is left — so speech costs less than that —
and the Wi-Fi PHY's own internal allocation is what fails. `MBEDTLS_DYNAMIC_BUFFER` alone (~20 KB)
would not close the gap, and `MBEDTLS_EXTERNAL_MEM_ALLOC` was rejected in `ROOM_ENDPOINT_PLAN.md`
on security grounds (TLS keys on the external bus without flash encryption).

**Owner decision:** the twins stay on their existing S3 panels.

**F0 = 0.3.39's code with the persistent WebSocket disabled**, released as 0.3.40 (a forward
version, so the panels install it: `main.c` installs any version that differs).

- `link.c` starts in its existing HTTPS fallback (`s_fallback`, `http_request`): one TLS session
  at a time under its one-lock invariant. Push falls back to the existing nudge-and-poll path.
- That removes the ~48 KB socket that caused the loop, and keeps `link.c`, `wsproto.c`, the
  backend tests that read them, and §7.5–§7.6 valid. F1's characterisation tests pin this build.
- It does **not** reintroduce 0.3.38's failure (`link.h`: "after about two talk turns every
  request failed with 'connect'"): that came from two sessions in flight, which the one lock
  forbids.
- **The persistent socket is not planned to return.** It costs ~48 KB of internal RAM, which is
  roughly the headroom the incident shows does not exist beside speech and the PHY. The
  fallback carries every request, and push latency is covered by the nudge. It would come back
  only as its own budgeted wave, and only if F0's measurements show room for it **plus** one
  fallback session **plus** the PHY margin.

**F0 measures before it sets any floor.** On hardware, through the panel console via the debug
API, it records:
- internal free before and after speech, giving the speech cost `S`;
- the cost of one TLS handshake, `T` (`link.h` estimates ~40 KB; the ~75 KB it also quotes is
  free heap at the moment of failure, not a cost);
- the PHY's margin, `P`, from the low-water mark across a send.

The report keeps `int_min` (0.3.39 already sends it) and gains a `speech_ok` flag, with
`TelemetryIn` and the field-scan test updated in F0's PR.

**0.4.0 ships a provisional floor of 96 KB, before the measurement (set at integration,
2026-10-12).** It sits above the 70,535 B free at which 0.3.39's PHY failed and below the
~119,000 B the `mem: wifi` line reads with the socket off, so a healthy panel starts speech and a
starved one boots deaf (`speech_ok: false`) instead of looping. The measurement below still decides
the floor; 0.4.x replaces 96 KB with `S + T + P` once the boot logs are in.

**Only then, `SPEECH_MIN_INTERNAL = S + T + P`.** The plan records the arithmetic. It must sit
**below** the measured pre-speech free with the socket off (~119 KB after Wi-Fi), so a healthy
panel always starts speech: `S < 70.5 KB` is known from the incident, and the check is
`S + T + P ≤` the measured pre-speech free. If it is not, F0 does not ship that floor; it goes
back to the owner.

**What the floor is for.** A deaf panel cannot send: RECORD and LISTEN learn that she spoke only
from `speech_hearing()`, so every recording drops after the lead silence and every tick answers
OOPS. The floor exists to turn a crash loop into a visible fault — `speech_ok = 0` through the
debug API — **never** as a normal state. Today's 48 KB floor is wrong for any firmware: it let
speech start and starve the PHY.

**Gates every firmware wave carries from F0 on** (§12):
1. **Static budget.** F0 adds an `idf.py size` step to `firmware.yml` and records the internal
   DRAM (`.data` + `.bss`) baseline. Each wave states its delta; the plan's new statics go to
   PSRAM where legal (§7.7), and lease and atomic words stay internal.
2. **Measured floor.** After deploy, the first report's `int_min` after every task has started
   must stay at or above F0's measured floor (`S + T + P` and the post-task low-water F0
   records). A panel below it is handled the F0 way: a fixed image at a higher version.
3. **Skew holds.** The box side of 0.3.39 is fine. F0's buffers (`/waiting` 1,024 B) and its
   `X-Jpanel-*` header parsing are 0.3.39's, so the §6 pins hold. `LINK_HEAD_MAX` bounds the
   WebSocket head frame; with the socket off, the HTTPS head is parsed by `esp_http_client`, and
   the pin stays for the day the socket returns.
4. **A broken image must always be replaceable over the air.** Today's order already allows it,
   in 0.3.38 and 0.3.39 alike: reach the box → `ota_confirm_health` → report → the manifest check
   and `ota_apply` → only then speech, talk, jpanel and nudge. The 0.3.39 crash comes after
   `ota_apply`, so looping panels will take 0.3.40. Every wave keeps that order
   (`test_update_check_precedes_task_starts`, a `main.c` order test). The image is marked valid
   before the tasks start, so the bootloader will **not** roll back a later crash; the
   update-first order is the recovery path. The watchdogs (§7.5d) do not cover a panic; this
   ordering does.

## 7. The engine (F2)

### 7.1 Two arrows

**render → engine: a command queue of depth 4.**

```
cmd { kind PLAY|REPLAY|SEND, seq (never 0), gen, expires_at, id[48], at, to,
      from[32], from_dad }      // from/from_dad: REPLAY only; gen is zero and ignored for SEND
```

- Only render posts. A failed post is reported back as `in.post_failed = seq`.
- "Poll soon" is an atomic flag, not a command. `jpanel_poll_soon()` keeps its name.
- **Expiry before claim:**
  - PLAY and REPLAY expire 35 s after posting, so they are claimable for 32 s (the claim
    margin below). That still exceeds the longest ordinary predecessor that cannot be cancelled:
    an HTTPS poll (20 s) plus an ack (10 s), with slop. Both are enforced
    deadlines (§7.5). On the HTTPS fallback a poll's body can stall a further `LINK_STALL_MS`
    after its head, since cancellation is checked only between steps; a PLAY behind it can expire
    before claim, audibly (§13). A PLAY never queues behind a SEND (lease 1, §7.7).
  - SEND expires 60 s after posting.
- **A SEND carries no gen and is never cancelled by STOP.** Only expiry before claim ends it.
- **Claim margin.** The engine never claims a command once `due(now, expires_at −
  UI_MARGIN_MS)`; it ends it as expired instead. So the UI's unclaimed backstop at `expires_at +
  UI_MARGIN_MS` cannot fire on a command the engine claimed in time (§7.5c).

**engine → render: one seqlock snapshot.** Only the engine writes it; render copies it once per
frame.

```
eng_view {
  phase IDLE|POLLING|FETCHING|SOUNDING|ACKING|SENDING
  claimed_seq, deadline_ms               // what it is doing NOW and when that must have ended
  done_seq                               // every command ends here exactly once
  cur  { seq, stream_id, id, from[32], from_dad, replay, run_continues, pos, in_waiting }
  last_begun { id, from[32], from_dad }  // set at a non-replay stream's first byte
  play_outcome { seq, id, kind heard|heard_unacked|stopped_heard|returned|short|empty
                                |cancelled|failed, reason }  // a replay uses heard|short|cancelled|failed
                                // heard_unacked: heard whole, but the head carried no
                                // X-Jpanel-Id, so there is nothing to acknowledge. A positional
                                // fetch (an old box) still acks the head's X-Jpanel-Id, as 0.3.39
                                // does (test_old_box_positional_fetch_still_acks_header_id)
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
- **`last_begun {id, from, from_dad}`** is set when a non-replay stream **begins** — 0.3.39's
  `s_in_id` — and is what F2's `jpanel_replay()` replays. `cur` is published at the claim, so a
  PLAY that fails before its first byte must not become the replay target.
- **`cur.pos`** is the index `cur.id` had in `waiting` when it became `cur` (−1 for a REPLAY), and
  **`cur.in_waiting`** says whether the published `waiting` still lists it. The machine's selection
  set is built from both (§8.2), on a new box and an old one alike.
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

`jpanel_core_step(core, now)` runs these steps in order.

**1. Finish the current stream.**
- Classify the stream, then publish `play_outcome` (carrying `{id, from, from_dad}`) and
  `run_continues` **before** acknowledging. Then arm the ack slot and attempt it.
- **A `returned` outcome re-inserts its id into the published `waiting`, at its old position,
  in the same snapshot write as the outcome.** That is safe before `/returned` answers: the box
  has not counted the message played, so it is still waiting there too, and the next poll agrees.
  The machine therefore never sees a frame in which the stopped message is in neither. The
  re-insert also bumps `waiting.count`; if `q[8]` is full, the last described entry drops out of
  `q` (the count still includes it), so `count − |q|` never goes negative.
- `stream_begin` refuses until classification is done.
- `run_continues` is true only after `heard` or `heard_unacked`, while ids remain in the run and
  the gen is fresh.
  - Every other kind ends the run. That matches today's `s_run = false`: a flaky link never
    refetches.
  - `stopped_heard` and `returned` only follow a STOP, which bumps the gen, so they could never
    continue a run anyway.
- Every id the run *attempts* leaves the run list.
- **A 204 for a run's next id** (the owner discarded it, or it was played elsewhere) publishes no
  outcome: the id leaves the run and the run continues with the next one; if none is left, the
  run ends on the previous message's outcome. A 204 for the **pressed** message publishes `empty`
  (`CUE_OOPS`), as there is nothing to play.
- A REPLAY never continues a run.

**2. Acknowledge.** One slot: `{id, kind PLAYED|RETURNED, heard_ms, delivery, first_at}`.
- The request has a 10 s deadline and is never cancelled.
- 204 → clear. 409 or 404 → clear; a 404 also counts `no_refund`. The tunnel answers 404 for a
  path it does not allow.
- Any other 4xx except 408 or 429 → terminal: clear and log.
- Anything else → end the run, set `ack_owed`, and back off 1, 2, 4 … 30 s for up to 10 min.
- While the ack is owed:
  - the poll drops the owed PLAYED id from `waiting`;
  - a PLAY retries the ack once first, then ends `failed("ack-pending")`.
- A REPLAY is never acknowledged.

**3. Continue a run** (PLAY only).
- The run list is the waiting list at the start, plus ids that arrive mid-run (JP), minus ids
  already attempted.
- **Between messages, if the poll flag is set, the engine polls first** (under the poll's own
  deadline), so an id that arrived mid-run joins it (`test_message_arriving_mid_run_joins_it`).
  This is the only poll a run makes. `run_continues` is decided before it, so an arrival during
  the run's **last** message does not join — as in 0.3.39; it is announced as a new message.
- The run continues only while `cur.gen` equals the stop word's gen.
- With no ids from the box, the engine fetches `?at=0`.
- A RETURNED message is already back in `waiting` (step 1); a poll is flagged.

**4. Dequeue.**
- A stale-gen PLAY or REPLAY, or any command at or past `expires_at − UI_MARGIN_MS` (§7.1), ends
  at once.
- A claimed SEND is never re-checked against its expiry.

**5. Poll** when due or flagged, and only while the arbiter reports `!speaker_busy` — today's
guard (`jpanel.c`: a poll is a round trip, a TLS handshake on the HTTPS fallback, and the audio
task feeds I2S from the same core's spare cycles). The between-message poll of step 3 keeps the
same guard. A queued command cancels the poll.

Every exit publishes `done_seq` and its outcome in one write; a run that ends on a failed ack
publishes it with `run_continues = false`.

**The published `waiting` keeps `cur.id`** from the claim until the stream begins, as 0.3.39's
`fetch_head` does, and `cur.in_waiting` says which side of that edge it is on. The machine never
assumes either; it works over the ordered selection set of §8.2.

**Leases.** Render acquires each lease by CAS **before** posting.

| Lease | Taken before | Released by | Also released when |
|---|---|---|---|
| 1 | posting a SEND | the engine, at every end of the SEND (including expiry at dequeue), before `send_outcome` | render, if the post fails |
| 2 | `talk_send` (in F2, inside the `talk_send` wrapper `display.c` already calls) | talk, when its upload request returns | render, if `talk_send` refuses |

- A SEND holds lease 1 through its upload and the box's transcription, because a 422 retry needs
  the buffer. On a 10 KB/s link a 35 s capture (1.1 MB) uploads for ~112 s before the 75 s head,
  so the pet can answer "busy" for **about three minutes**, and about six if the box answers 422
  and the retry uploads again (§13).
- **Talk and `s_reply`.** `talk.c` sets SPOKE as soon as it submits the reply slot, so its next
  turn waits for `reply_done == last id` before its `link_request`, and only then reads into
  `s_reply`.

**Outcomes, by wave**

| Stop when | F2 (today's policy) | F3, with delivery header | F3, without |
|---|---|---|---|
| `out_ms` ≥ the heard threshold | PLAYED | PLAYED | PLAYED |
| below it, or `out_ms == 0` | PLAYED | RETURNED (PLAYED if the stop was a REPLY) | PLAYED |
| short (unverified) | none; the run ends | none; the run ends | none; the run ends |

- The engine learns the stop's kind from `stream_stop(heard)`. The flag is packed into the same
  32-bit stop word as the gen (`{gen31, heard1}`), so the engine can never read a new gen with an
  old flag: REPLY passes `heard = true` (§4). The bit is **sticky** until the engine publishes
  that it has classified that stream: a later STOP inside the classification window (standby,
  calibration, recovery) ORs into it and never clears it. Render writes the stop word **before**
  `silence`, so the arbiter never sees the silence without the new gen.
- **Replay outcomes** (never acknowledged): `heard`, `short` (logged only, as today), `failed`
  (`CUE_OOPS`), `cancelled` (a STOP; consumed in PET).

### 7.4 The audio arbiter: `stream_tick`, pure and host-tested

Every per-chunk decision lives in **`stream.c`**: `stream_tick(&st, now) → { WRITE(ptr, len) |
IDLE, capture: COPY|SILENCE|NONE }`. It covers:

- the cue-before-ring rule;
- the preroll;
- pause;
- deafness;
- the capture copy;
- slot intake.

`audio_task` keeps codec I/O, ALC, levels and `speech_feed`. `check_no_logic_in_audio` (F1) is a
grep: `audio.c` may not reference `ring_*`, `s_play*`, `cue_render`, any `stream_t` field, or the
slot words.

**Cues are rendered whole, by render, as today.**
- Render calls `cue_render` with today's two passes and today's per-cue `uint8_t` variant
  counter, which moves from `audio.c` to the render side.
- It renders into a buffer **it owns, one per slot**: 800 ms, 25.6 KB of PSRAM each, about 128 KB
  in total.
- Each slot buffer starts with the rendered length `n`: lengths vary by variant (`CUE_FART`
  170–749 ms, the others ±7 %), so the arbiter copies exactly `n` samples, never a fixed
  `cue_samples(c)` that could splice an older, longer render's tail. `n` is covered by the same
  seq re-read as the samples.
- The arbiter **only copies samples**: on start it copies the whole cue into `s_play`.
- There is no peak table, no chunked rendering and no oscillator state on the audio task, and the
  render task does the CPU work it does today.

**Slots.** Every slot except `reply` is **one packed 32-bit word, `seq24 | cue8`**, and each slot
has a single writer: render.
- **One render-wide seq counter** feeds every slot, `silence` included. Each write takes the next
  two values: odd while render writes, even once published. Seqs compare with
  `seq24_ge(a, b) = ((a − b) & 0xFFFFFF) < 0x800000`, so "before this seq" survives the 24-bit
  wrap.
- The word works as a per-slot seqlock:
  1. render makes `seq` odd, renders into the buffer, then makes `seq` even, with release
     ordering;
  2. the arbiter reads an even word, copies, then re-reads the word;
  3. if the word was odd or changed, it **skips that slot until the next tick** — bounded, so the
     arbiter (priority 5) never spins against render (priority 4).

  The fences are §7.2's: a release fence after the odd store, an acquire fence before the
  re-read.
- A cue therefore never pairs one seq with another's samples.
- The arbiter writes only its own `*_done` words, and writes `*_done = seq` on **every**
  disposal of a slot's cue: played, dropped, cut or expired — including when it has no `s_play`
  to copy into (a PSRAM failure). If render's own slot buffers fail to allocate, render treats
  the panel as `sound == false` and writes no slots at all. Either way `speaker_busy` cannot
  latch.

**Processing order on every tick**

1. `silence`
2. stale-stream discard
3. `reply`
4. the cue slots, in this order: `press`, `send_result`, `fail`, `refusal`, `chime`
5. the stream

The arbiter **reads** the slot words (acquire) **before** it reads `silence`, then processes in
the order above. Render publishes `silence` before the press it pairs with, so a press the
arbiter sees always comes with its silence; it can never drop a press against a cue that silence
is about to cut.

| Slot | Writer | Rule |
|---|---|---|
| `silence` | render | Cuts whatever is **sounding** — any cue or reply, a SENT already sounding included — and every **pending** `press`, `fail`, `refusal` and `chime` cue whose seq is before this one, and every pending reply whose stamp is before it. Discards the stream once. **Only a pending `send_result` survives** and plays after it (E1.2): a failed pet turn's OOPS must not sound in the dark after a stop. Anything later survives. |
| `reply {ptr, len, stamp, id}` | talk | `stamp` is the `silence` seq talk read when it submitted; talk writes `ptr`, `len`, `stamp`, then `id` **last**, with release ordering, and the arbiter reads `id` first with acquire. So "a reply submitted after a silence plays, as today" is decidable within one tick. Played from talk's own `s_reply` without a copy, at the highest priority. Talk must not reuse `s_reply`, or rewrite the slot, until `reply_done == id`. Waits out a cue already sounding (≤ 749 ms). Never refused. `reply_done = id` on every disposal. |
| `send_result` | render | Latest wins. Eligible once no reply is pending or sounding; played before `fail`. Expires 15 s after it first becomes eligible. |
| `fail` (play/replay failure, failed pet turn) | render | Latest wins, same eligibility. A failure therefore never overwrites a pending SENT. |
| `refusal` | render | `CUE_BUSY`, coalesced. Eligible, like every cue slot, once no reply is pending or sounding — arbiter-local facts. Expires 2 s after it first becomes eligible. |
| `chime` | render | Written only when the chime is actionable (§8.4). Same eligibility. Expires 20 s after first eligible. |
| `press` | render | **Today's rule, applied by the arbiter after `silence`:** played if no cue or reply is sounding (`clip_busy`), otherwise dropped. The stream does not count — it yields chunk by chunk, as with today's `audio_cue` — so `CUE_PAUSE`/`RESUME`/`REPLY` sound during playback. Because the arbiter decides after `silence`, the standby `CUE_STOP` is not dropped against a cue that `silence` is about to cut. It never cuts another cue. An outcome cue that arrives while a press cue sounds waits it out (`test_heard_then_oops_both_sound`). Two press writes inside one arbiter tick now play the **second** (latest wins); today's `audio_cue` kept the first — a known difference, inaudible at a 40 ms tick. |
| stream (ring) | engine | Yields to everything above, chunk by chunk. |

**Stop and pause**
- The stop word (`uint32_t`, `{gen31, heard1}`) lives in `stream.c`; render writes it via
  `stream_stop(heard)`.
- The stream word packs `{gen16, stream_id8}`. Stale means the low 16 gen bits differ from
  `gen16`.
- The producer publishes that word, with release ordering, before its first write to `w`. The
  arbiter loads `w` with acquire ordering, then reads the word.
- Stale-stream discard runs on **every** tick: `r = w`, and pause is ignored. So `stream_begin`
  waits at most one tick, 40 ms, after a stop.
- `pause_req {stream_id8, on}` is one word, honoured only by its own stream.
- The sink checks gen on every chunk.

**The ring**
- Cursors are monotonic `uint32_t`, never reset.
- The ring holds **65,536 samples (4.096 s)**, so `w % cap` stays continuous across the u32 wrap.
  A `_Static_assert` checks the power of two. Today's ring is 64,000 samples; the change is
  **F2's** (A9), since F1 extracts `stream.c` as is.
- `w` belongs to the producer and `r` to the arbiter, with release/acquire ordering.
- `stream_begin` waits for `seen_w == w && r == w`.
- `{stream_id8, out_ms24}` is one atomic word, written only by the arbiter, along with `arb_hb`
  and the busy facts. `stream_id 0` means "not begun".
- **Busy facts**, each one atomic, arbiter-written. A stream whose gen is stale counts in none of
  them: the arbiter knows the gen, and a producer stalled in an HTTPS body may not notice the stop
  for up to `LINK_STALL_MS`.
  - `speaker_busy` = a cue, a reply or a current-gen stream is active — today's
    `audio_playing()`, which includes the stream;
  - `clip_busy` = a cue or a reply is sounding — the press-drop rule and the mic gate use it;
  - `reply_busy` = a reply is pending or sounding;

  Render's copies, which the machine reads, also count what the arbiter has **not yet picked
  up**: `speaker_busy` and `clip_busy` include any cue-slot seq render wrote that its `*_done`
  word has not reached (`silence` has no done word and is excluded: the arbiter consumes it every
  tick), and `reply_busy` includes `reply.id != reply_done`. So in the frame after a `CUE_PLAY`
  is written, before the arbiter picks it up, a PLAY is still not posted; and a reply talk wrote
  in the last 40 ms counts at once. (In the press frame itself, frame-boundary rule 1 holds the
  PLAY back, §8.2.)
  - `stream_live` = the stream is open or the ring is undrained — today's
    `audio_stream_active()`.
- `PLAY_BUF_MS` and `CAPTURE_MAX_MS` move from `audio.c` to `stream.h` in F1, so
  `panel_sm.c`'s `_Static_assert` and the backend pins can read them.

**Capture.** `capture_open()` returns a bool and refuses while either lease is held. Every opener
handles the refusal.

**No codec.** When `audio_start()` failed there is no arbiter — or the codec answered but the
ring or `s_play` PSRAM allocation failed — so `stream_begin` refuses at once and the PLAY ends
`failed("no-audio")` instead of waiting for `r == w` until the backstop. The "arbiter present"
flag is set only after `xTaskCreate` succeeds (`audio.c` allocates the ring before the task).

### 7.5 Deadlines are true by construction

**(a) `link.c` enforces the deadline and reports progress.** From F0 the persistent socket is
disabled (§6a), so the HTTPS path is what runs; the WebSocket clamps below stay, because the code
stays. `link_types.h` gains:

```
deadline_ms; upload_mode LINK_DEADLINE_ABSOLUTE|LINK_DEADLINE_PROGRESS; head_budget_ms;
void (*on_progress)(void *ctx, link_phase_t phase, uint32_t deadline_ms);
bool (*cancelled)(void *ctx);
```

- Every wait is clamped to `left`:
  - the socket wait;
  - `ws_send`'s lock and send;
  - `s_http_lock`;
  - `esp_http_client_open`, at `left/3` per phase;
  - each `write`;
  - `fetch_headers`.
- **PROGRESS mode** (jpanel's SEND only):
  - after each upload frame, the deadline becomes `now + LINK_STALL_MS`, then
    `on_progress(UPLOAD)`;
  - when the upload ends, it becomes `now + head_budget_ms`, then `on_progress(HEAD)`.
- **Every** `link.c` wait loop (`LINK_UP_WAIT`, WebSocket head and body, `ws_send`'s lock wait,
  the HTTPS read loop) also calls `on_progress(TICK, unchanged)` every `LINK_STEP_MS`. So does the
  sink's ring-full wait. The engine bumps `hb` on each call and on every step.

| Phase | Bound |
|---|---|
| SEND, before the first frame | claim + `LINK_UP_WAIT_MS` + `LINK_STALL_MS`; **restarted at a 422 retry's start** |
| SEND upload | `LINK_STALL_MS` per frame, refreshed |
| SEND head | 60 s + 15 s. Real because W1 puts `asyncio.wait_for(client.transcribe(…), 60)` **inside** `_transcribe`'s `try`: httpx's `timeout` applies per operation, not in total, and a timeout then returns `""` like any other STT failure, so the send still succeeds (`test_transcribe_timeout_returns_empty_and_sends`). |
| Other heads | the request's own budget |
| Bodies | `LINK_STALL_MS` per network gap, as today. Sink-blocked time does not count. On HTTPS the client timeout is reset to `LINK_STALL_MS` once headers arrive. |

- Slop is at most one `LINK_STEP_MS`, **except** DNS and the TLS handshake's internal reads inside
  `esp_http_client_open`, which cannot be clamped (§13).
- Every caller migrates from `timeout_ms` in ABSOLUTE mode. On WebSocket the semantics are
  unchanged; on HTTPS a per-operation timeout becomes a total for the request, which aligns the
  two transports:
  - jpanel's other requests;
  - `talk.c` — head at `TALK_HTTP_TIMEOUT_MS`, body on the stall rule;
  - `ota.c`, three sites.

**(b) Published `deadline_ms`**

| Phase | `deadline_ms` |
|---|---|
| IDLE | none. The loop keeps today's bounded 250 ms queue wait (`jpanel.c`) and bumps `hb` on every pass. In IDLE the watchdog's deadline term does not apply: a stalled `hb` alone decides (§7.5d) |
| POLLING, FETCHING, ACKING | the step's deadline |
| SENDING | as moved by `on_progress` |
| SOUNDING, and any body | `now + LINK_STALL_MS + 2 s`, refreshed while bytes arrive, `out_ms` advances, or the stream is paused and `arb_hb` advances |

**(c) UI backstops — two, never a sum of durations**

`UI_MARGIN_MS = 3000` ≥ the largest snapshot delay the machine tolerates (2 s) + `LINK_STEP_MS`
(the most a step may overrun, §7.5a) + one frame, with slop.

- **claimed:** `due(now, deadline_ms + UI_MARGIN_MS)`;
- **unclaimed:** `due(now, expires_at + UI_MARGIN_MS)`; the engine stops claiming at
  `expires_at − UI_MARGIN_MS` (§7.1).

On firing:
- **PLAY** → STOP + `CUE_OOPS`, then PET. A replay keeps its offer.
- **SEND** → clear the send state and play `CUE_OOPS`; the mode is unchanged.
- A real outcome that arrives later is counted in `late_outcome` and never sounded.

**(d) Watchdogs.** Both use the existing safe reboot, logged with a reason.
- **The engine is wedged** only when **both** hold: `hb` has not moved for 60 s, **and**
  `due(now, deadline_ms + UI_MARGIN_MS + 60000)` — the second term only outside IDLE, which has no
  deadline. So it can never pre-empt a published deadline, including
  the 75 s head wait inside `esp_http_client_fetch_headers`, which does not tick.
- **The arbiter is wedged** when `arb_hb` has not moved for 10 s. The audio task is also the
  capture and recogniser clock, so a dead arbiter means a deaf, mute panel.
  - **Armed only if the audio task exists.** `audio_start()` returns false before
    `xTaskCreate` when the ES8311 does not answer, and `display.c` carries on as a silent pet
    (`sound = audio_start()`). Arming on that path would turn one I2C glitch at boot into a
    reboot loop every 10 s that the owner cannot reach. So the watchdog arms only when `sound` is
    true, and its 10 s counts from the **first observed** `arb_hb` change, not from boot.
    Test: `test_no_codec_never_arms_arbiter_watchdog`.
- **Both watchdogs are suspended while an OTA runs**: the partition erase in `esp_https_ota` can
  starve the audio task, and the OTA has its own rollback gate. `ota.c` gains an `ota_running`
  flag, set before `link_suspend` and cleared on every failure path; when it clears, both
  watchdogs restart their last-change clocks, so a failed OTA after a long erase does not trip on
  its first frame back.
- **A cap on watchdog reboots.** RTC-retained words, validated by a magic as `display.c`'s
  already are, count watchdog reboots. They survive `esp_restart`, not a power cycle or a
  brownout, which simply resets the count. After 3 the watchdogs disarm and the report says so
  (`wd_disarmed`). The count decays by one per hour of uptime without a watchdog reboot (uptime,
  because `esp_timer` restarts at each boot), and a **version change re-arms** them, so an OTA fix
  restores them without a power cycle the owner cannot perform. A deterministic wedge therefore
  cannot become a boot loop. (A panic is a different class; §6a.)
- Reboot releases any lease.

**Tests**
- `test_engine_meets_published_deadlines`. The fake link models:
  - the 8 s socket wait;
  - 20 s for `ws_send`;
  - the HTTPS lock;
  - a 10 KB/s upload driven through `on_progress`;
  - 60 s of transcription;
  - body gaps.

  It proves the engine's bookkeeping. `link.c` itself is covered only through the `wsproto.c`
  helpers (`wsp_wait_step`, `link_left`, the `open` split).
- `test_35s_send_on_slow_link_survives_transcription`: no UI backstop fires, **the watchdog does
  not fire**, and exactly one SENT sounds.
- `test_60s_pause_does_not_trip_watchdog`.
- `test_idle_engine_bumps_hb_every_pass`.
- `test_422_retry_restarts_first_frame_bound`.
- `test_storm` keeps snapshot delays at or under 2 s, below `UI_MARGIN_MS − LINK_STEP_MS − one
  frame`, and runs the engine up to `LINK_STEP_MS` past its deadlines.
- `test_claim_near_expiry_never_trips_unclaimed_backstop`.

### 7.6 Network cancel

- **WebSocket** (only if the socket ever returns, §6a): waits call `wsp_wait_step` every 250 ms
  and send `wsp_cancel`.
- **HTTPS:** cancellation is checked between steps.
- **SEND** passes no `cancelled`.
- **Buffers:** `rel[96]`, `path[112]`, `/waiting` 1536 B.
- **Gap:** cJSON stays on the device. Host tests feed parsed structs; backend tests pin the field
  names.

### 7.7 Writers, telemetry, F2

| State | Writer |
|---|---|
| the stop word (`{gen31, heard1}`); slot words and buffers (`silence`, `send_result`, `fail`, `refusal`, `chime`, `press`); `pause_req`; commands; lease acquire | render |
| poll flag | any task (atomic) |
| snapshot; run list; ack slot; `hb`; ring `w`; stream word; lease 1 release | engine (`on_progress` runs on the engine task) |
| ring `r`; `seen_w`; `{stream_id, out_ms}`; `arb_hb`; the busy facts; `*_done`; eligibility stamps; `s_play` | arbiter |
| `reply` slot; `s_reply`; lease 2 release | talk |

- `jpanel_types.h` is split from `jpanel.h`.
- **Internal RAM** (§6a). One TLS session costs ~40 KB of internal RAM (`link.h`; F0 measures it).
  The new statics
  that no DMA or ISR touches — both copies of the snapshot, the 1,536-byte `/waiting` buffer,
  `sm_t`, the slot buffers — are `EXT_RAM_BSS_ATTR`. The lease words and every word used with an
  atomic compare-and-swap stay in internal RAM (S32C1I does not work on PSRAM on the S3). The
  attribute is a macro that is empty on the host build, so the shared sources still compile there.
- **Build lists:** F1 adds `stream.c` and `jpanel_core.c`, and F3 swaps `ui.c` for `panel_sm.c`,
  in **both** the host Makefile's `SRC` and `firmware/main/CMakeLists.txt`'s explicit `SRCS` —
  without the second, `firmware.yml`'s ESP-IDF build fails.

**Telemetry**, which reaches the owner only through `main.c`'s report and the debug API (CLAUDE.md
#10):

- **Kept:** today's `msg_bytes`, `msg_ms`, `msg_ok`, `msg_bad` and `msg_waited_ms`, now read from
  the engine's counters, and `msg_err`, which is a string (the last error), from the engine's
  last outcome reason.
- **New**, in one compact `jp` object of **flat integer keys only** (about 140 B): `ack_owed`,
  `ack_retries`, `no_refund`, `returned`, `lease_refusals`, `snapshot_retries`, `post_failed`,
  `late_outcome`, `cue_expired`, `jpanel_stack_free`, `sm_violations`, `wd_engine`,
  `wd_arbiter`, `wd_disarmed` (0/1). Anything nested or non-integer would fail `TelemetryIn`
  with a 422, and a 422 is a failed report: every F2 panel would lose its only no-terminal
  diagnostic.
- **Placement.** `jp` is emitted **after** `pmu_history` and before the room-guarded variable
  tails, outside the span that `test_endpoint_report_now.py` ::
  `TestEveryFieldThePanelSendsIsDeclaredHere` :: `test_the_model_declares_all_of_them` scans.
- **Byte budget.** The report's body is a fixed 2,048 bytes and already near full, so F2 measures
  the worst case and shrinks the heard ring to make room. The existing
  `test_endpoint_api.py` :: `test_the_panel_s_whole_payload_fits_the_buffer_it_is_built_in` is
  extended to include a worst-case `jp`.
- **Backend (in F2's PR):** `TelemetryIn` gains `jp: dict[str, int] | None`, stored with the rest
  and shown by the debug API. `test_jp_keys_from_main_c_validate_against_telemetry_in` reads the
  `jp` keys from `main.c`'s format string and validates a worst-case `jp` built from them against
  `TelemetryIn`, so the shape is pinned on both sides.

*Superseded by §15: 0.4.0 has no wrapper layer. The table is kept as the map of every old call and what replaces it.*

**F2 changes no intended behaviour.** The old `display.c` and `talk.c` calls become wrappers:

| Old call | F2 wrapper |
|---|---|
| `jpanel_state()` / `jpanel_clear()` | derived from the phase and unseen outcome seqs / marks an outcome seen. With a play and a send outcome both unseen, the **send** outcome is returned first (the child's own act) and `jpanel_clear()` marks that one; the next call returns the play outcome (A8) |
| `jpanel_running`, `jpanel_in_from` | read `cur` |
| `jpanel_fetching` | true from the **post** — render's posted seq not yet reached by `done_seq` — through the claim, as 0.3.39 raised `s_fetching` on the render task at the press; so the controls still show from the press (Appendix B, 0.3.24) |
| `jpanel_waiting`, `jpanel_waiting_at`, `jpanel_waiting_from_dad`, `jpanel_sibling` | read `snap.waiting` (`count`, `q[i]`, `q[i].from_dad`, `sibling`) |
| `jpanel_message_stats` (`main.c`'s report) | the telemetry counters of this section |
| `jpanel_play_at(at)` | posts PLAY (with id, `at`, gen); refused while lease 1 is held |
| `jpanel_replay()` | posts REPLAY with `{id, from, from_dad}` from **`last_begun`** (§7.1): the last non-replay message whose stream began, which is 0.3.39's `s_in_id`. A stopped or short message is replayable, as today. Refused, as today, with no id or while `speaker_busy`; and — **new in F2** — while lease 1 is held, so it never queues behind a send (today's `display.c` already logs a refused replay). Tests: `test_f2_stop_then_again_replays_stopped_id`, `test_f2_short_then_again_replays_short_id` |
| `jpanel_stop()` | `silence` + `stream_stop()` — today's `audio_stop()`, so a sounding cue or pet reply is still cut, as on 0.3.39 |
| `jpanel_send()` | lease 1 CAS, then post SEND |
| `audio_cue(c)` | renders into `press`, or into `send_result`/`fail` for SENT/OOPS; the arbiter applies today's drop-while-a-clip-sounds rule |
| `audio_play()` (talk) | writes the `reply` slot |
| `audio_playing()`, `audio_stream_active()` | `speaker_busy` (clip, reply or stream), `stream_live` |
| `audio_stream_pause(on)` | `pause_req` scoped to the current `stream_id` |
| `audio_stream_paused()` | `pause_req.on && pause_req.stream_id == audio.stream_id && stream_live` — so a pause pressed before the first byte never draws a play glyph over the message that then plays (closes A1's "pause lies" in F2) |
| `talk_send()` | lease 2 CAS, then today's call |
| `audio_stop()` | `silence` + `stream_stop()` |
| `audio_capture_open()` | now returns a bool (refused while leased); every caller handles it |

- Outcome mapping stays as today: EMPTY sounds as FAILED (OOPS).
- **Rollback:** a source revert, plus a version bump and a rebuilt dist — the F0 pattern (§6a).

---

## 8. The panel state machine (F3)

### 8.1 Shape

`panel_sm.{c,h}` replaces `ui.{c,h}`. `sm_step(sm, &in, &out)` runs once per frame, both on the
device and in the tests.

**Inputs (`in`):**

- `now`, `snap`, `audio {stream_id, out_ms, speaker_busy, clip_busy, reply_busy, stream_live}`,
  `post_failed`;
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

- the mode goes to PET, with `CAPTURE_DROP`, `PAUSE(0)` and `STOP`. An open recording is
  dropped silently: calibration is the owner's maintenance act, not the child's;
- **the offer is kept**, and any send continues;
- the pet turn is left as it is: talk runs on, and the machine reads its edge on the first frame
  back — a SPOKE or FAILED edge first, then `TALK_TIMEOUT_MS`, so a reply that landed during
  calibration is not reported as a timeout;
- **standby** is written by the machine (its button effects); `display.c`'s touch-wake clears it
  only through the machine's wake input, so there is one writer;
- the machine is not stepped during calibration;
- on the first frame back, outcomes are processed before backstops.

**`make check_no_logic_in_display`** lands in **F3**, the wave that empties `display.c`. Before
that it could not pass. Run by `make test`, it fails if either is true:

- `display.c` has any static, file-scope or function-local, whose name with `s_` stripped matches
  an `sm_t` field (extracted by awk);
- `display.c` calls a `jpanel_*` or `stream_*` function outside the executor's whitelist:
  `jpanel_snapshot`, `jpanel_post`, `jpanel_poll_soon`, `stream_stop`, `stream_pause_req`,
  `stream_cue_*`, `stream_silence`, `stream_audio_facts`.

A renamed duplicate would pass this check; the wave review covers that case.

### 8.2 State and invariants

```
sm_t {
  mode PET|GRID|LISTEN|RECORD|PLAY, since           // THINK is derived, not a mode
  play { seq, target{id, from, from_dad, replay}, since /* the press */, run_pos,
         pause_req{stream_id, on, since} }
  offer { id, from, from_dad, since, hidden }         // the again/reply pair; not a mode
  rec  { to, heard, hush, deliberate, opened_at }
  talk { voice, heard, hush, lead, follow_armed, follow_turns, dash... }
  turn { state NONE|THINKING|FAILED, since }         // the pet turn in flight; NOT a mode
  grid_until; send { seq, posted_at, expires_at }; notice { first_visible, big }
  chime_ids[8]; sel { id, index }; seen_ids[16]; down { at, x, y, on_pet, target, swiped }
  mode_press_at, hold_at{x, y}                       // the press (or hold) that last changed the mode
  stopped_seq                                        // a stopped, posted PLAY whose outcome is pending
}
```

**The pet turn is a sub-state, processed on every frame whatever the mode.** Today the turn and a
pending replay coexist (`ui.c` sets `pending` during THINKING and `ui_frame` still reads `net`).
The machine keeps that behaviour:

- **SPOKE** → `TALK_CLEAR`, nod, arm the follow-up.
- **FAILED, or `TALK_TIMEOUT_MS`** → dash (`dash_why`, counted) and `CUE_OOPS` to the `fail` slot.
- **IDLE** → clear.

**THINK is derived: `mode == PET && turn.state != NONE`** (the FAILED state includes the 2.5 s
dash). LISTEN ends into PET with `turn.state = THINKING` when the question goes up. THINK decides
only the drawing and the PET targets of §8.3, where its row outranks the other PET rows. AGAIN
pressed mid-turn moves to PLAY while the turn resolves in the background; every PLAY exit returns
to PET, which is THINK again if the turn is still in flight, so the bubble comes back and a later
FAILED draws its dash (`test_again_during_think_then_failed_turn_still_dashes`).

**`play.seq` is NONE from the press until the command is posted.** Every engine-facing rule —
Claimed, Finished, both backstops, `post_failed` — applies only when `play.seq != NONE`; before
that the pending PLAY is bounded by `PENDING_MS` alone. So an old outcome can never finish or
cancel a new press (`test_old_stop_outcome_does_not_finish_new_pending_play`).

**Claimed** means `play.seq != NONE && snap.claimed_seq == play.seq &&
!seq_ge(snap.done_seq, play.seq)`: the engine has taken this PLAY and not yet ended it. A finished
`cur` is therefore never "claimed".

**ON** means claimed, `audio.stream_id == snap.cur.stream_id`,
`audio.out_ms > 0` **and `audio.stream_live`**: the message is actually audible. The last term
matters in a run's gap: `{stream_id, out_ms}` keeps its last value after the ring drains, through
the ack and the next fetch, so without it a PAUSE in the gap would arm `pause_req` for a stream
that has ended and break I5 when the next one begins (`test_pause_in_run_gap_is_ignored`).

**Two general rules about the frame boundary**

1. **Nothing entered this frame is judged by this frame's inputs.** `in` is sampled before
   `sm_step` and the executor runs its effects after it, so `net`, `hearing`, `capture_ms` and the
   audio facts describe the world **before** anything the machine did this frame. Every timer and
   edge rule therefore skips state whose `since`/`opened_at == now`:
   - the pet turn's IDLE → clear does not fire on the frame `turn.state` became THINKING (the
     executor's `talk_send` sets `net = BUSY` only after the step);
   - a PLAY entered this frame is not posted on it (`play.since == now`), whatever the speaker
     facts say;
   - the LISTEN/RECORD hush, cap and lead timers read `capture_ms` and `hearing` only for a
     capture opened in an earlier frame, and the executor's `capture_open` resets the count, so
     a LISTEN opened right after a recording ended at the cap is never sent at once;
   - I1 is checked against the capture the machine has **committed** to (the `CAPTURE_OPEN` and
     `CAPTURE_DROP` it emitted), not the frame-start input, so an entry frame cannot trip recovery.
     If the executor's `capture_open` is refused (a lease taken by this frame's SEND — practically
     unreachable, since the mic gate checks the lease), it reports `capture_refused` on the next
     frame and the machine returns to PET with the refusal cue.

   Tests: `test_turn_entered_this_frame_survives_stale_net`,
   `test_cap_not_read_from_previous_capture`.
2. **A press that changes the mode gives the new mode's targets a grace.** Touch reports every
   down edge without debounce, so a bounce of the press lands on whatever the new mode drew under
   it. For `EXIT_GRACE_MS` after `mode_press_at`, the new mode's **EXIT, FACE, NOTICE, TICK,
   CROSS, the pair's AGAIN and REPLY, and grid cells** are *ignored*. That covers:
   - the tick and cross after any touch that opened LISTEN or RECORD — a grid cell (the pet cell
     shares the tick's lower-right quadrant, `confirm.c`) or REPLY;
   - the notice, AGAIN and REPLY after a press that returned to PET: the grid's cancel cell, the
     cross and AGAIN all sit lower left (`sendto_hit`, `confirm_hit`, and `sendto_cy_bottom` is
     `confirm_cy`), so a bounce of the cancel or the cross would otherwise replay an old message,
     and a bounce of the tick would hit REPLY and sound a spurious "busy";
   - FACE after the notice press that entered PLAY;
   - grid cells after the **hold** that opened GRID — but only for a down edge within 40 px of the
     hold's own point (the four-cell junction, where a lightening finger lands), so a deliberate
     tap on a face elsewhere acts at once.

   PLAY's EXIT grace (`play.since` is that press) is the same rule. `mode_press_at` is cleared
   whenever the mode changes **without** a press — a PLAY that fails inside 700 ms must not leave
   AGAIN or the notice silently ignored. The hold's point is the finger's **last** position when
   the hold fired, since a finger may drift up to 59 px without becoming a swipe.
   - **Exempt: PLAY's PAUSE and REPLY, and swipes.** PLAY's PAUSE and REPLY are already
     *ignored* until ON, which is the exact hazard; a clock on them is the shape `ui.c:482-485`
     and JP 0.3.36 record as tried and rejected ("700 ms of dead pause button is a real cost"),
     and it would break #45. (The pair's REPLY in PET acts at once, so it is graced.) Swipes are
     gestures, not targets: the swipe-from-the-big-box unwind (A1) and #18 need them at once. A
     swipe that starts on the big box has already pressed the notice on its down edge, so §8.3's
     PLAY rule unwinds it to PET whatever the selection does.
   - A mode entered without a press or a hold (voice, a timer, an outcome) has no grace.
   - A press ignored by the grace is logged (`tap_missed`) and silent. It is treated as part of
     the press that opened the mode, which was already answered (E2.10), so a bounce never makes
     a second sound.

   Tests: `test_pet_cell_bounce_does_not_drop_listen`, `test_notice_bounce_does_not_hit_face`,
   `test_grid_cancel_bounce_does_not_replay`, `test_cross_bounce_with_offer_does_not_replay`,
   `test_hold_opened_grid_junction_bounce_graced`, `test_deliberate_cell_tap_after_hold_acts`,
   `test_pause_live_at_on_inside_grace`.

**The selection set `S`** is what the face, the swipe and the counter range over:

- `waiting.q` in the box's order, plus `cur` — while claimed and not a replay — at `cur.pos` if
  `cur.in_waiting` is false. De-duplicated by id. So `cur` keeps one position whether or not the
  engine has dropped it from `waiting` yet, and a swipe's direction and its clamped ends do not
  flip at the stream's first byte.
- A RETURNED message is re-inserted at its old position (§7.3), which is the same place.
- In a non-replay PLAY, `sel.id` follows `cur.id`. An arrival joins `S` at its box position and
  leaves `sel` on `cur`.
- **In a replay, FACE and swipe are *ignored*.** The replayed message is not in `S` (the pair was
  only pressable with nothing waiting), so there is nothing for the selection to move from.
- **Re-resolve, every frame:** `sel` is looked up by `sel.id` in `S`; if the id has left `S`, it
  keeps `sel.index`, clamped to `|S| − 1`, and takes that entry's id. **Except while a stop's
  outcome is pending:** a child-chosen stop of a **posted** PLAY records `stopped_seq`, and until
  `done_seq` reaches it `sel.id` is held. A PLAY abandoned before it was posted holds nothing.
  - During the hold the notice and `sm_face` show the **held id's** sender, taken from the
    stopped PLAY's `play_outcome`/`cur`, even while that id is in neither `waiting` nor `S`.
  - A notice press during the hold is taken (`CUE_PLAY`, PLAY pending) but **not posted** until
    `done_seq ≥ stopped_seq`. Its target is **resolved at the post**, from `S` as it then stands:
    the held id if it came back RETURNED, otherwise the entry the selection re-resolves to. A
    message acked by the stop is never fetched again
    (`test_notice_press_during_stop_hold_resolves_at_post`). The engine puts a RETURNED id back into `waiting` in the same snapshot
  write as its outcome (§7.3), so the stopped message keeps the selection with no gap
  (`test_selection_held_while_stop_outcome_pending`, run end to end against the real
  `jpanel_core`).
- **Home** (`sel` to index 0) happens only on an unseen-id edge observed **in PET** (§8.4). Every
  mode marks ids seen, so a message that arrived during playback and that she then picked with
  the face stays picked when PLAY ends
  (`test_face_select_of_mid_play_arrival_survives_return_to_pet`).
- The box describes at most 8 ids. `S` covers only those (plus `cur`); the swipe clamps at the
  last described entry, and the counter adds the undescribed rest, `waiting.count − |waiting.q|`.
  An id that scrolls into the described eight as older ones leave is not an arrival (§8.4).
- With no ids from the box (an old box after rollback), in a non-replay PLAY the selection still
  follows `cur`; the count rule below applies in PET only. `|S| = waiting.count − (cur.in_waiting ?
  1 : 0) + (claimed ? 1 : 0)`, and the counter adds nothing more — the count already includes the
  undescribed rest. The engine knows which side of `fetch_head`'s removal it is on, so a single
  message is never counted twice. The selection is an index only, so today's rule stays: any
  count change sends it home.

**Order within a frame**

1. input edges — the button, then touch, then voice — resolved against the targets drawn on
   the previous frame, what the finger saw (I3). A release that ends a swipe is not a tap
   (today's `s_swiped`), so a tap and a swipe never both act in one frame. **If the button takes
   the machine into standby, that frame's touch and voice edges are dropped**, so a queued notice
   press cannot start a PLAY in the dark (`test_button_and_touch_in_one_frame_drops_touch`);
2. the hold timer;
3. snapshot outcomes (play, then send), then `post_failed`;
4. backstops and mode timers: the PLAY/SEND backstops, pending expiry, the pause limit, GRID's
   `SENDTO_MS`, and the recording's hush, cap and lead-silence;
5. posting a pending PLAY once `!speaker_busy`;
6. the pet turn, then the selection's re-resolution (§8.2) — after the press of step 1, which
   carries the id drawn on the previous frame (E2.1);
7. the follow-up decision on the reply-end edge (`reply_busy` falling), which reads `chime_ids`
   **before** step 8. A cut also makes `reply_busy` fall; what stops an EXIT that cut the reply
   from opening a follow-up is that **every child-chosen stop disarms the follow-up explicitly**,
   and the mic gate refuses while its `CUE_STOP` sounds;
8. new ids (§8.4), the chime, and the notice clock.

So a natural finish and an EXIT press in the same frame are the one-press exit: the EXIT leaves
PLAY with its stop pattern (`SILENCE`, `STOP`, `CUE_STOP`, §8.3) and no offer, and the finish arrives in PET, where it is only
consumed (`test_finish_and_exit_in_one_frame`). A notice press in the frame an unseen id arrives
plays the selection she saw. A chime owed on the reply-end edge disarms the follow-up
(`test_chime_and_follow_up_in_one_frame`).

**The offer**

- *Set* when a non-replay PLAY finishes with `heard` or `heard_unacked`. It takes
  `{id, from, from_dad}` from `play_outcome` itself, so a delayed snapshot cannot misaddress a
  reply. It is hidden if anything is still waiting.
- *Re-stamped* (`since = now`, fresh again) when a replay ends by itself. JP 0.3.36: a replay
  re-arms the pair.
- *Cleared* by:
  - EXIT on the pair;
  - REPLY on the pair;
  - button standby;
  - **every exit from PLAY that the child chose** — EXIT, FACE, swipe, button. A failure, an
    expiry or a backstop keeps it: a replay's offer survives (the Finished table, §7.5c), and a
    non-replay PLAY already cleared it at the press. Calibration keeps it too;
  - **pressing to play a non-replay message** (cleared at the press).
- *Hidden, not cleared,* while `waiting.count > 0`.
- *Standing* once `due(now, offer.since + REPEAT_MS)`.

It survives a conversation, the grid, a recording started by voice, and calibration, as today.
It stays drawn and live in THINK, as today: a replay pressed mid-turn is pre-empted by the pet's
reply and then resumes (§13 Q4).

**Invariants.** They are asserted after every host step and counted on the device
(`sm_violations`). Only a violation of I1, I5 or I6 — a state that would leave the mic or the
speaker wrong — recovers, to PET with `CAPTURE_DROP`, `PAUSE(0)` and `STOP`, plus `CUE_OOPS` if a
recording was dropped. A violation of I2, I3, I4 or I7 is **counted only**: recovery never drops
a child's recording over a drawing or a missing cue.

- **I1:** exactly one mode; the committed capture is open if and only if the mode is LISTEN or
  RECORD (frame-boundary rule 1).
- **I2:** `sm_face(sm, snap)` both draws the top-left face and addresses reply. It returns:
  - in PLAY, claimed: `snap.cur` (for a replay, seeded from the offer);
  - in PLAY, not yet claimed: `play.target`;
  - in RECORD: `rec.to`; in LISTEN: the pet (`draw_who`, as today);
  - in GRID: none — the cells are the faces;
  - in PET (THINK included) with a visible offer: the offer;
  - in PET with the notice drawn: the selected entry of `S`;
  - otherwise (THINK with no offer, or nothing waiting): none.

  The drawn face is asserted every frame.
- **I3:** the drawn controls are exactly the mode's targets.
- **I4:** every press gets a cue or a view change (a flinch counts), except the presses §8.3
  lists as *ignored*, which are logged (`tap_missed`).
- **I5:** `pause_req.on` implies PLAY with `pause_req.stream_id == snap.cur.stream_id`.
  - Every exit from PLAY emits `PAUSE(0)`.
  - Every exit except a natural finish also emits `STOP`.
  - An ack never carries a cancel, so a STOP cannot cut it.
- **I6:** every mode except PET has a deadline:

  | Mode | Deadline |
  |---|---|
  | PLAY, SEND | §7.5c |
  | GRID | `SENDTO_MS` |
  | LISTEN, the pet turn (THINK) | today's (`TALK_TIMEOUT_MS`) |
  | RECORD | `CAPTURE_MAX_MS + 5 s` |
  | pause | `AUDIO_PAUSE_MAX_MS` |
  | pending | `PENDING_MS` |

- **I7:** times compare with `due()`, seqs with `seq_ge()`.

### 8.3 Targets and transitions

**Global (any mode)**

- **Button short:**
  - in standby → wake, with `CUE_HEARD` (today's);
  - otherwise, these effects in this order:
    1. `SILENCE` (seq *n*);
    2. `CUE_STOP` to the `press` slot (a later seq);
    3. `CAPTURE_DROP`, `PAUSE(0)`, `STOP`;
    4. end the pet turn: `turn.state = NONE` **without** `TALK_CLEAR`, `follow_turns = MAX`, and
       disarm the follow-up;
    5. clear the offer;
    6. standby, `BLANK`, PET.

    **A send continues.**

  The arbiter processes `silence` first. It cuts what is sounding and what is pending from before
  *n* (only a pending `send_result` survives, §7.4), so the newer `CUE_STOP` sounds before the dark, as
  the owner asked: *"IT BLINKS FIRST"*.

  - **This is a fix, too (F3; F2 keeps today's cut, §4).** On 0.3.39, `display.c` calls
    `audio_cue(CUE_STOP)` and then `jpanel_stop()` → `audio_stop()`, which sets
    `s_play_pos = s_play_len` in the same frame. So on the device the stop cue was cut before it
    sounded. The harness only records effects, which is why test #30 passed.
  - **A pet reply already sounding is cut**, as `audio_stop()` does today. **A pet reply still in
    flight plays when it arrives**, as today: `talk.c` submits it whatever the UI does
    (`talk_clear` is a no-op while the request is on the network). Today's standby resets
    `s_talk` to idle, so the SPOKE edge never runs; step 4 is the machine's form of that. Without
    it the reply's SPOKE would re-arm the follow-up, and a microphone would open in the dark
    (`test_standby_during_think_reply_plays_without_follow_up`).
  - Test: `stream: test_standby_silences_then_sounds_its_own_stop`.
- **Button hold** → deep sleep.
- **Voice "stop"** runs today's `VOCAB_STOP` handler wherever it is delivered:
  - RECORD or LISTEN → drop;
  - `turn.state != NONE` → `talk_clear` and `turn.state = NONE`, whatever the mode. As today,
    `talk_clear` is a no-op while the request is on the network, so an answer in flight still
    plays — with no SPOKE edge, so no follow-up;
  - always → disarm the follow-up and set turns to MAX.

  Delivery is gated by today's mute.
- **The wake phrase, "tell sister/dad", and the follow-up** go through the mic gate.

**The mic gate (today's rule).** The mic opens only if all of these hold:

- the mode is PET, GRID or PLAY (for a REPLY);
- `turn.state == NONE` — today's `talk != IDLE` refusal, which also covers the 2.5 s FAILED dash;
- not standby;
- not leased;
- `net != BUSY`;
- the speaker is quiet — `!reply_busy`, `!clip_busy`, and no stream other than one this same
  transition is stopping.

The cue for the opening press itself sounds after the mic opens, inside the deaf window. A
refusal of a press or a phrase writes the `refusal` slot. A refused **follow-up** is disarmed
without a cue — nobody asked for it (`test_follow_up_open_handles_refusal`). This keeps *"NOT
WHILE WE ARE SPEAKING"*.

**Targets, in priority order** (*ignored* = the press is claimed with no effect):

| Mode | Targets | Hold |
|---|---|---|
| PET (offer hidden or none) | NOTICE (big box, or the badge quadrant; *ignored* in the press grace of §8.2 — e.g. after a press that left PLAY, so a bounce cannot play a message) · PET | `down.target == PET` (the down edge itself landed on the pet, so a finger left over from EXIT, FACE or an ignored notice press never counts), `on_pet`, free taps, not swiped, and `net != BUSY` (today's guard) → GRID |
| PET, fresh offer | EXIT · AGAIN quadrant · REPLY quadrant · PET | **none**: the press belongs to the target it landed on, as today (0.3.35, `ui_menu_up`) |
| PET, standing offer | EXIT (the upper-right quadrant, as with a fresh offer) · AGAIN/REPLY disc reach (r 82 at (92 \| 276, `confirm_cy`)) · PET | `down.target == PET` → GRID; a hold that lands in the exit quadrant is an EXIT (E1.6) |
| GRID | four cells · swallow (flinches; the grid stays up, as today). A cell whose recording or listen is refused closes the grid with `CUE_BUSY`, as today's `s_sendto_until = 0` does | — |
| LISTEN (voice), RECORD | CROSS · TICK quadrants (both *ignored* in the press grace when a touch opened the mode — REPLY, or any grid cell, whose lower-right pet cell shares the tick's quadrant) · a miss is *ignored*: no flinch, logged `tap_missed`, as today (a pet that twitches mid-sentence invites the next poke) | — |
| PET while THINK | the offer's targets, if it is visible (as today) · PET (today's poke; `display.c`'s guard turns it into a flinch only while a reply sounds) | — |
| PLAY | EXIT (*ignored* until `play.since + EXIT_GRACE_MS`, every phase) · FACE (live while \|`S`\| > 1 in a non-replay PLAY, after the press grace; *ignored* in a replay) · PAUSE/REPLY quadrants (*ignored* until ON, with today's flinch while the press is pending) · swallow (flinches, as today's off-disc transport press) | — |

`sm_target()` reports an *ignored* target as `TARGET_IGNORED`, distinct from the target itself.
So "a press in the grace is not the exit" stays literally true, and Appendix A #9 holds.

**The counter** (`n/m`) uses `S`: in PLAY, `n = run_pos` and `m = run_pos − 1 + |S| +
(waiting.count − |waiting.q|)`. Whether or not the engine has dropped `cur.id` from `waiting` yet,
a single message is "1/1" and its face is dead (`test_single_message_face_dead_between_claim_and_stream`,
with a snapshot that still lists `cur.id`; `test_old_box_single_message_face_dead`).

- `run_pos` starts at 1 at the press and advances by one each time a new `cur.id` is **seeded**
  and then begins; an id that answers 204 (§7.3) is skipped without advancing it. It
  counts "1/3" → "2/3" with no gap between seeding and the first byte.
- At the press, a child who picked "2/3" therefore sees "1/3" once it plays: the
  numeral in PLAY counts the run this press started, which begins at the chosen message. This is
  intended and pinned by `test_counter_counts_the_run_from_the_press`; in PET the numeral is still
  the selection's position.

**The PLAY view** is today's:

- a PLAY draws the run bar;
- a replay draws the run bar until ON, then the pair layout with the playing glyph
  (`test_a_replay_still_reads_as_playing`).

**PET**

- **Notice:**
  - while sending → refusal;
  - otherwise → `CUE_PLAY`, clear the offer, PLAY{target = the selection, `since = now`}. The
    target is re-resolved when the command is posted (§8.2), so a press made during a stop's hold
    plays what `S` holds at the post.
- **AGAIN:**
  - while sending → refusal;
  - otherwise → `CUE_PLAY`, PLAY{replay; target = the offer's `{id, from, from_dad}`, carried in
    the REPLAY command}. The offer stays.
- **REPLY** → mic gate:
  - opens → clear the offer, RECORD{to = `sm_face`}, `CUE_REPLY`;
  - refused → refusal; the offer stays.
- **EXIT** → the stop pattern of PLAY (`SILENCE`, then `CUE_STOP` at a later seq), clear the
  offer, disarm the follow-up. So a pet reply sounding with the pair up (the reply itself plays
  after SPOKE, when the turn is already NONE) is cut, as `jpanel_stop()` →
  `audio_stop()` does today.
- **Swipe** → select, clamped; `CUE_HEARD` only if the selection moved.
- **An unseen id arrives in PET** → the selection goes home (§8.2). In every mode it joins
  `seen_ids` and `chime_ids` (§8.4).

**RECORD**

- **Tick, after she was heard** → effect `SEND` (the executor CASes the lease and posts),
  `CUE_HEARD`, PET.
- **Hush or cap, after she was heard** → `SEND`, with no cue, as today, PET.
- **Tick, nobody heard** → drop, outcome `CUE_OOPS` (today's).
- **Cross** → drop, `CUE_STOP`.
- **Lead-silence** → drop, plus `CUE_STOP` (to the `send_result` slot) if the recording was
  deliberate.
- **A swipe in the press that opened the recording** → drop it silently, then apply the swipe.

**LISTEN / the pet turn** — today's behaviour, with one change. The follow-up opens on the
reply-end edge (`reply_busy` falling) only in PET, and only through the mic gate. If the mode is anything else (PLAY,
GRID, RECORD), a chime is owed, or the gate refuses, it is **disarmed**, not deferred: the
child's attention has moved.

All listens are hands-free; `start_listening` always sets `listen_voice`. The held-listen branch
is dead code in 0.3.39 and is not ported.

**PLAY**

- **Pending.** The command is posted once `!speaker_busy` (today's: the press cue finishes
  first).
  - `PENDING_MS` is derived from the worst case of what can hold `speaker_busy` ahead of it: a
    reply (`PLAY_BUF_MS`) plus one cue in each of the five cue slots (`5 × CUE_MAX_MS`) plus 1 s
    — about 15 s. `CUE_MAX_MS` does not exist today: F3 defines it in `cue.h` from
    `CUE_MAX_SAMPLES` (12,800 samples, 800 ms). `_Static_assert` against `stream.h` and
    `cue.h`.
  - When pending expires → outcome `CUE_OOPS`, PET.
  - On `post_failed` for `play.seq` → `CUE_OOPS`, PET.
- **Finished** (posted PLAYs only), meaning `play_outcome.seq == play.seq && !run_continues`, or
  `done_seq` reached `play.seq`:

  | Kind | Result |
  |---|---|
  | `heard` / `heard_unacked`, not a replay | set the offer from `play_outcome` (hidden if anything waits), PET |
  | replay `heard` | PET; offer kept and re-stamped |
  | replay `short` | logged only, as today; PET, offer kept |
  | replay `failed`, or a replay backstop | `CUE_OOPS`, PET; offer kept |
  | play `failed` / `short` / `empty` | `CUE_OOPS`, PET |
  | `cancelled` (expired before claim, or stale) while still in PLAY | `CUE_OOPS`, PET (E2.10); a replay keeps its offer |

  A STOP-caused outcome (`stopped_heard`, `returned`, `cancelled`) is silent because the machine
  left PLAY when it emitted the STOP; it arrives in another mode and is only consumed.

  A natural finish emits `PAUSE(0)` only.
- **Every stop the child chooses in PLAY uses the button's pattern:** `SILENCE` (seq *n*),
  `PAUSE(0)`, `STOP`, then its cue at a later seq, and it disarms the follow-up. So whatever is sounding — a pet reply that
  pre-empted a replay, a `CUE_PAUSE` — is cut, as `audio_stop()` does today, and the stop's own cue
  is never dropped behind it (`test_exit_during_pet_reply_silences_it_then_sounds_stop`). A pending
  `send_result` survives (§7.4).
- **EXIT** → that pattern with `CUE_STOP`, clear the offer, PET (owner).
- **FACE / swipe:**
  - if the selection over `S` moves → that pattern with `CUE_HEARD`, clear the offer, PET
    (JP 0.3.37);
  - FACE **wraps** (today's `select_step(…, true)`); a swipe that **began after** the press that
    entered PLAY, at a clamped end → nothing.
- **A swipe whose down edge is the press that entered PLAY** (`down.at == play.since`) → the stop
  pattern with `CUE_HEARD`, PET — whatever the selection does. Touch queues the press on the down
  edge and a swipe is recognised only after 60 px of travel, so that press has already become
  the notice press; this is RECORD's "swipe in the opening press" rule, and it keeps *"a lone
  message must not be losable to a smear"* (`ui.h`) true: a smear across a lone message, or
  toward an end, never plays it (`test_smear_from_notice_never_plays`).
- **PAUSE** (when ON) → toggle `pause_req` for `cur.stream_id`, with `CUE_PAUSE` or
  `CUE_RESUME`.
- **REPLY** (when ON) → mic gate, checked **before** stopping:
  - opens → `PAUSE(0)`, `STOP` (with `heard`, §7.3), clear the offer, RECORD{to = `sm_face`},
    `CUE_REPLY`. No `SILENCE` is needed: the gate has just checked that no cue or reply sounds;
  - refused → refusal, and the message keeps playing.

**SEND**

- On `post_failed` → `CUE_OOPS` at once, clear `send`. Render has already released the lease.
- On the outcome, written to the `send_result` slot:

  | Outcome | Cue |
  |---|---|
  | SENT | `CUE_SENT`, and poll soon |
  | NOBODY | `CUE_OOPS` (until F4's `CUE_NOBODY`) |
  | EMPTY | `CUE_STOP` |
  | FAILED | `CUE_OOPS` |

- A late outcome after the backstop is logged and not sounded.

### 8.4 New messages, the chime, sleep

- **"New"** is an id not in `seen_ids`. With no ids from the box, a rise in the count stands in,
  and `chime_ids` becomes a `chime_owed` flag. The flag is dropped when the count falls to 0; it
  covers #22, #46 and #48.
  - **In every mode**, an id not in `seen_ids` joins `seen_ids` and `chime_ids` on the frame it
    first appears — unless it **scrolled in**: the previous snapshot had undescribed entries
    (`count > |q|`) and the count did not rise. Then it joins `seen_ids` silently, with no chime,
    no selection home and no `arrived` wake (`test_more_than_eight_waiting`).
  - `seen_ids[16]` evicts its oldest entry that is **not** in `waiting` and is not `cur.id`.
  - `waiting` describes at most 8 ids, so there is always one to evict, and an id still waiting is
    never forgotten.
- **The chime.** `chime_ids` holds the ids still owed a chime. The machine writes the `chime` slot
  on the first frame that meets **all** of these:
  - the mode is PET and not THINK, where the notice is pressable. In GRID the chime waits until
    the grid closes, and `first_visible` starts only when the notice is drawn in PET;
  - the notice is drawable: waiting > 0. During a send it **is drawn** — a press on it is refused
    with "busy" — so a send does not block the chime;
  - the capture is closed and `!reply_busy`;
  - at least one of `chime_ids` is still in `waiting`.

  `chime_ids` is then cleared. Ids that have left `waiting` (played in the run they arrived
  during, or played elsewhere) are dropped from it **every frame**, so they never chime.
- **`notice.first_visible`** is stamped on the first frame the notice is drawn, once per wait:
  - a new wait starts when the count goes from 0 to above 0. It starts **big** only if one of its
    ids is in `chime_ids`, checked before the chime clears them; so a returned message, which is
    already seen and owed nothing, starts as a badge;
  - a second arrival does not restart it;
  - a dark wake does (JP).
- **`sm_busy()`** = mode ≠ PET, or `turn.state != NONE`, or sending, or
  `snap.phase ∈ {FETCHING, SOUNDING}`.
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
  - `test_later_swipe_at_clamped_end_stops_nothing` (a swipe that began after the entering
    press), `test_smear_from_notice_never_plays`;
  - `test_hold_on_upper_pet_during_fresh_offer_opens_nothing`;
  - `test_again_during_think_then_failed_turn_still_dashes`;
  - `test_single_message_face_dead_between_claim_and_stream` (the snapshot still lists `cur.id`);
  - `test_face_bounce_after_select_does_not_play`;
  - `test_hold_refused_while_turn_in_flight` (today's `net != BUSY` guard);
  - `test_chime_owed_flag_without_ids`;
  - `test_standby_silences_then_sounds_its_own_stop`;
  - `test_short_stream_ends_run_without_refetch`;
  - `test_replay_end_restamps_offer`;
  - `test_exit_bounce_does_not_play_next_message`;
  - `test_chime_waits_for_grid_to_close`;
  - `test_offer_uses_play_outcome_sender`;
  - `test_seen_ids_never_evicts_a_waiting_or_current_id`;
  - `test_send_backstop_then_wedged_engine_reboots`;
  - `test_standby_during_think_reply_plays_without_follow_up`;
  - `test_held_exit_does_not_open_grid`, `test_held_face_does_not_open_grid`,
    `test_held_ignored_notice_press_does_not_open_grid`;
  - `test_pause_in_run_gap_is_ignored`;
  - `test_expiry_in_play_sounds_oops`;
  - `test_replay_backstop_keeps_offer`;
  - `test_finish_and_exit_in_one_frame`, `test_chime_and_follow_up_in_one_frame`;
  - `test_wake_from_standby_sounds_heard`;
  - `test_selection_order_stable_when_cur_leaves_waiting`, `test_more_than_eight_waiting`;
  - `test_old_box_single_message_face_dead`, `test_counter_counts_the_run_from_the_press`;
  - `test_voice_stop_during_replay_abandons_turn_without_follow_up`;
  - `test_face_select_of_mid_play_arrival_survives_return_to_pet`;
  - `test_selection_reresolves_by_id_then_index`;
  - `test_exit_during_pet_reply_silences_it_then_sounds_stop`;
  - `test_stray_touch_in_record_is_ignored_and_never_drops`;
  - `test_wake_phrase_refused_during_failed_dash`;
  - `test_reply_under_heard_min_counts_as_heard`;
  - `test_face_ignored_during_replay`;
  - `test_turn_entered_this_frame_survives_stale_net`, `test_cap_not_read_from_previous_capture`;
  - `test_pet_cell_bounce_does_not_drop_listen`, `test_notice_bounce_does_not_hit_face`;
  - `test_hold_in_exit_quadrant_of_standing_offer_dismisses`;
  - `test_selection_held_while_stop_outcome_pending`;
  - `test_exit_cutting_reply_opens_no_follow_up`;
  - `test_play_not_posted_in_frame_of_its_press_cue`;
  - `test_notice_press_during_stop_hold_resolves_at_post`;
  - `test_failed_play_clears_mode_press_grace`.
- **`test_storm`** — 300k seeded frames:
  - snapshots dropped, delayed (at most 2 s, below the `UI_MARGIN_MS` budget of §7.5c) or
    duplicated;
  - touches dropped or repeated;
  - clock jumps, including the u32 wrap — bounded below `UI_MARGIN_MS − LINK_STEP_MS`; a frame
    after a larger jump is exempt from the no-backstop assertion;
  - random calibration;
  - an engine running up to `LINK_STEP_MS` past its published deadlines, and claiming up to the
    claim margin;
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

- **FW (0.4.0):** the firmware caps the recording UNDER today's box's `/send` limit —
  `CAPTURE_MAX_MS` is 29,500 against `jpanel.MAX_MESSAGE_MS` = 30,000, which the box checks on
  the raw length — so a full recording is never a 413 on the box in the field.
  `test_a_full_panel_recording_is_one_the_box_accepts` pins panel cap ≤ box cap.
- **W1:** the box accepts `PANEL_AUDIO_MAX` + 0.5 s; W1 may then raise both caps together.
- **F2:** 400 → `EMPTY`, 409 → `NOBODY`, 413 → `FAILED("too-long")`.
- **Pins:**
  - `test_capture_caps_pinned`: `PANEL_AUDIO_MAX >= CAPTURE_MAX_MS × 32`, and
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
- backend `test_next_by_id_serves_that_row_or_204` (including an own-sent id → 204);
- backend isolation test `test_next_by_sibling_id_is_204` (`/next?id=` with the sibling's id).

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
  - A retry after a lost 204 can refund twice, but only if a newer `/next` has since brought
    `deliveries` back to the same value. `returns < 5` bounds this.
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
| A swipe during playback stops rather than moving to the next message | Kept: the swipe stops and selects (JP 0.3.37) | — |

**C findings**

- **C1** — §11.
- **C2** — `send_text` validates before TTS (404). The PWA refetches and keeps the draft. W2's
  grouping removes ghost chips.
- **C3** — two routes (W3), over one shared function.
  - `POST /messages/{id}/retry`, for **gave-up** rows on a live panel. It resets `deliveries`,
    `returns` and `offered_at`, then nudges. Until P1 no PWA calls it, so W3 also adds the debug
    twin `POST /api/debug/jpanel/messages/{id}/retry` (`jpanel-retry` verb, `whoami` scope,
    `DEBUG_ACCESS.md` row, a `jpanel.retry` scope in `whoami`, `test_debug_retry_route`).
  - `DELETE /messages/{id}`, for **any unplayed row** — the explicit owner act of §4, whatever
    the row's state (live, gave-up, keyless, re-addressed). A played row answers 409: clears and
    the sweep own those. It calls `discard_message`, which **lands in W2** with the debug route
    (§11.1) and releases the blob via `blob_referenced`.
  - Both answer 404 for a non-uuid id.
  - Tests: `test_unheard_to_retired_panel_is_discardable`,
    `test_unheard_to_unknown_panel_is_discardable`, `test_unheard_live_row_is_discardable`,
    `test_retry_refused_without_live_key`, `test_gave_up_row_kept_by_clear_and_retryable`.
- **C4** — columns `heard_ms` and `offered_at`. The copy says "Played".
  - `/waiting` reads the panel names (`_panel_names`, its own pooled connection) **before** it
    opens its session, then stamps `offered_at` in that session. W3 moves `/next`'s
    `_panel_names` call out of its open session the same way. No row-exclusive lock is ever held
    while waiting for a second connection.
- **C5** — a between row is listed in both threads.
  - Clearing either thread deletes the rows it shows, except panel-addressed unheard rows that
    are not keyless (owner-addressed rows are deleted, as today).
    Gave-up rows are kept, as today.
  - Keyless rows (E3.3) are deleted from W2; waiting-for-re-flash rows are kept
    (`test_clear_keeps_in_window_mail_of_revoked_panel`). The clear's SQL uses one predicate,
    `_KEYLESS_SQL` (a fragment in `jpanel.py`, not a database function, so W2 stays
    data-only, and restricted to `recipient_kind = 'panel'` — owner-inbox rows have no
    `recipient_device`), shared with the status in `messages()`.
  - Until P1, a clear reports what it deleted and kept only through its counts (`kept_for`); the
    confirm copy that names keyless rows is P1's.
  - **A clear releases the deleted rows' blobs** through `blob_referenced` (W1). Today
    `clear_history` deletes rows and orphans their audio (`test_clear_releases_blobs`).
- **C6** — a per-thread `row_number()` limit, a UNION with every unplayed owner row, and
  `?before=`. **W1 owns the query function and `/summary`** (E3.9). W1's
  `test_every_unplayed_owner_row_is_returned` covers rows on live keys; before W2's grouping a
  dead-key row has no thread to land in (today's `messages()` drops non-roster ids), so that case
  is W2's.
- **C7** — 206/416 with `Accept-Ranges`. uuid guards on: the audio route, owner read, the C3
  routes, `/next?id=`, `/returned`, and panel `/played`. On the firmware side, a 404 from
  `/played` means "row gone", so it clears.
- **C8** — `errorDetail` joins the list `msg`s; `maxLength={600}`; TTS timeout 60 s; the counter
  and hint arrive in P1.
- **C9** — `kept_for` per recipient.
- **C10** — the PWA clamps PCM to `MAX_MESSAGE_MS × PANEL_RATE`. The owner route keeps today's
  **truncation**, moved from the cap to cap + 0.5 s: audio up to that is kept whole, anything
  longer is trimmed to it, never refused, and each trim is logged (`jpanel.owner_audio_trimmed`). A cached PWA's 30 s stop is a `setTimeout`
  (`JpanelScreen.tsx`), which a background or locked tab throttles past 0.5 s; refusing would
  lose Dad's message for a release (`test_owner_audio_at_30_02s_is_accepted`,
  `test_owner_audio_well_over_cap_is_trimmed`). The panel route (E1.1) still refuses, since a
  panel cannot overrun its own buffer.
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

*In 0.4.0 (§15) these foundations are built as part of the rewrite, not as a behaviour-preserving wave; the retargets below land in 0.4.0.*

1. `link_types.h`, `jpanel_types.h`, and `wsp_wait_step`/`link_left` in `wsproto.c`.
2. `stream.c` with `stream_tick`, extracted **as is** (including today's 64,000-sample ring). `audio.c` keeps only I/O, and
   `check_no_logic_in_audio` is added.
3. `jpanel_core.c` behind `jpanel_io_t`. `jpanel.c` keeps the task, queue, cJSON, mbedtls and
   the real io.
4. `run_jpanel_tests`, `run_stream_tests` and **`check_no_logic_in_audio`** go into `make test`
   (`check_no_logic_in_display` follows in F3) (CI runs
   only `make -C firmware/host test`). The fakes are:
   - a scripted box, with a mode for F0's build (0.3.40);
   - the real ring and stream;
   - a fake clock;
   - a test-only SHA-256, so `dev-setup.sh` is unchanged;
   - yield hooks.
5. Characterisation tests assert the F0 baseline's behaviour (§6a) — bugs included, each named by finding. F2
   inverts them.
6. The hooks reach engine-side code and the render-side slot writer in `stream.c` (needed by
   `test_slot_seqlock_never_pairs_seq_with_other_samples`); cross-core ordering is otherwise
   reviewed, not tested.
7. `PLAY_BUF_MS` and `CAPTURE_MAX_MS` move to `stream.h` (§7.4).

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
| `test_endpoint_report_now` :: `TestEveryFieldThePanelSendsIsDeclaredHere` :: `test_the_model_declares_all_of_them` | `main.c` | **F2** (the scan span stops before `jp`) |
| `test_endpoint_api` :: `test_the_panel_s_whole_payload_fits_the_buffer_it_is_built_in` | `main.c` | **F2** (worst-case `jp` added) |
| `test_endpoint_report_now` :: the other `main.c`/`ota.c` tests | `main.c`, `ota.c` | none |
| `test_endpoint_api` :: `test_the_ceiling_is_the_panel_s_buffer` | `talk.c` | F2 |
| `test_endpoint_api` :: `test_the_panel_waits_longer_than_the_question_it_allows`, `test_the_recording_window_outlasts_the_silence_it_waits_for` | `audio.c`, `display.c`, `talk.c`, `ui.h` | F1 (`CAPTURE_MAX_MS` moves to `stream.h`), F2 (`talk.c` deadline), F3 |
| `test_endpoint_api` :: `main.c`/`vocab.c` tests (4) | — | none |
| `test_panel_ws` :: `test_the_firmware_spells_the_same_route_and_frame` | `link.c`, `wsproto.h` | F1 |
| `test_nudge` (2), `test_g2p` | `nudge.c/h`, `vocab.c` | none |

## 11. Identity (W2): no RLS change

**Problem (C1).** A re-flash replaces a panel's principal, so any unplayed mail still addressed to
the old key is stranded.

**What stays unchanged:** RLS, nudge keys, `_waiting_count`, `/converse`, rename and revoke.
Panel routes change in one way only in W2: after an insert commits, `send` runs
`readdress_orphans` in its own internal owner-context transaction. That is a context escalation inside a panel route, so it is named in W2's
red-team scope. `panel_ws` gets only W3's `_ALLOWED` row.

### 11.1 Design

**1. `readdress_orphans(session, *, subject_id: str | None = None) → moved rows`.** Idempotent;
runs in owner context. `subject_id` scopes it to one subject (the send path); `None` means every
subject (the sweep, the migration). It lives in the jpanel module; `SqlDeviceRepo.reflash` and
`rotate` take an `after_revoke(session)` callback, which the endpoint route supplies, so the
repo carries no jpanel SQL and the re-address still runs inside the same locked transaction.

- It moves unplayed rows addressed to a revoked key onto the same subject's single live jpet
  key. It never crosses subjects.
- **It moves only rows created within `READDRESS_WINDOW_DAYS = 14`**, one constant for every
  caller. Anything older stays on its revoked key: keyless, cleared by a thread clear and
  discardable, so nothing months old pops up on a child's panel at bedtime — not at the migration,
  and not seconds later when the retention loop or a send calls it.
- **"The single live key" is enforced in SQL**: the join is on a subquery of subjects with
  exactly one live jpet key (`GROUP BY subject_id HAVING count(*) = 1`), so a subject with two
  moves nothing (`test_readdress_skips_subject_with_two_live_keys`).
- **In the send path it is scoped** to the just-inserted row's recipient subject; reflash, rotate,
  merge and the sweep cover the rest. That keeps the owner-context escalation inside a panel
  route as narrow as it can be.
- **It is one SQL statement** (`UPDATE … FROM` the subject's live key `… RETURNING`), whose rows
  are taken by a `SELECT … ORDER BY id FOR UPDATE` subquery, so two concurrent calls lock rows in
  one order and cannot deadlock on them. Postgres
  locks the table while parsing, before the statement's snapshot, so a call that overlaps a
  re-flash waits for its `SHARE ROW EXCLUSIVE` lock and then sees the revoke; a read of the
  mapping cannot fall before the lock and the write after it. It does **not** take the table lock
  itself: two sends each holding `ROW EXCLUSIVE` from their INSERT would deadlock upgrading.
- It resets `deliveries = 0` on each moved row: the attempts were spent on a key the panel no
  longer holds. **W3** extends it to also reset `returns = 0` and `offered_at = NULL`, the columns
  W3 creates.
- Callers nudge each moved row's **new** recipient, not the old key.
- **Called from:**
  - `reflash` and `rotate`, and `merge_subject` (same window);
  - the retention loop (`sweep.py`), whose first pass runs at start-up. It runs in its own
    `try`, independent of `sweep_played_messages` and its early `(0, 0)` return;
  - after each insert in `send`, `send_text` and `send_audio` — **in its own owner transaction,
    after the insert has committed** (today's `send` already commits before it nudges). Running it
    inside the panel's transaction would deadlock without Postgres seeing it: the INSERT holds
    `ROW EXCLUSIVE`, a re-flash queues `SHARE ROW EXCLUSIVE` behind it, and a second session's
    `ROW EXCLUSIVE` would queue behind the re-flash while the first waits on it in Python.
    **It is best-effort:** any exception is caught and logged (`jpanel.readdress.failed`) and
    never changes the send's response — the message is already stored, and a 500 would make the
    child re-record a duplicate (E1.2). The sweep's next pass repairs it — up to the sweep
    interval (6 h) later.
  - A subject with two live jpet keys moves nothing (the `HAVING count(*) = 1` rule below); its
    revoked-key rows show "waiting for re-flash" until they age out, and each pass logs
    `jpanel.readdress.ambiguous_subject`, visible through the debug API.

**2. Serialisation and lock order.**

- `reflash` and `rotate` take `LOCK TABLE app.jpanel_message IN SHARE ROW EXCLUSIVE MODE`
  **first**, then lock the subject's `principals` rows `FOR UPDATE`. They live in the generic
  `SqlDeviceRepo`, so a phone's key rotation takes the lock too; that is harmless (it is brief,
  and no phone row is touched) and `repo.py` says why in a comment.
- A concurrent send's INSERT therefore waits for the re-flash to commit. Its post-insert
  readdress then sees the revoke.
- **Rule:** any transaction that writes `jpanel_message` and locks `principals` rows takes the
  table lock — or writes `jpanel_message` — **before** touching `principals`. That covers
  `merge_subject`, C3's retry, and the W2 migration. One lock order means no deadlock.
- Test `test_send_racing_reflash_is_carried` uses real connections with forced interleaves,
  including a re-flash that queues between the send's commit and its re-address.
  `test_merge_and_reflash_do_not_deadlock` checks the lock order, and
  `test_concurrent_sends_readdress_without_deadlock` checks that the send path takes no table
  lock.

**Why this lock order holds:**

- `jpanel_message` has no foreign key to `principals`, so an INSERT takes no key-share lock there.
- `SHARE ROW EXCLUSIVE` conflicts with itself, so reflash, rotate and merge serialise.

**3. `reflash` picks a subject** in this order (W2 rewrites the "oldest wins" comment and
docstring in `repo.py` to match):

1. the one owning the most recent key (live or revoked) among subjects whose
   **`subjects.display_name`** matches this name, with this role — compared **case-insensitively
   on the normalised name**, as the rename duplicate check already is; the subject's stored name is
   reused. Not `principals.label`: jpanel's `rename_panel` relabels only live keys (the generic
   `SqlDeviceRepo.rename` relabels revoked ones too), so a revoked key can keep an old label and
   would steer a flash under an old name onto a renamed subject;
2. otherwise the oldest;
3. otherwise a new one.

**4. Owner views group by subject** (read-only). The `device_id` is:

- a live panel → its live jpet key's id;
- no live key → `retired`, with the subject id (listed only when it has rows);
- a NULL subject or a hard-deleted principal → `unknown`.

Retired and unknown groups are returned **only with `?include_retired=1`**, which P1's PWA sends.
A cached old PWA therefore never draws ghost chips (§6).

`DELETE /messages`, **`DELETE /chats`**, the badge and `/summary` all resolve to the subject's
principals. Display-role turns get their own group, "Displays", returned **only with
`?include_retired=1`** too, so a cached PWA never draws it as a messaging chip.

**5. Keyless and waiting-for-re-flash rows** (E3.3). A row whose recipient key is not live is:

- **waiting for re-flash** if a future re-address can still deliver it: inside
  `READDRESS_WINDOW_DAYS`, on a subject that exists and is not a `merged:` tombstone. A revoke
  from Settings followed by a re-flash that evening is exactly this case (E4.1). A thread clear
  **keeps** it; the owner can still discard it individually;
- **keyless** if no future re-address can deliver it: older than the window, or on a NULL,
  hard-deleted or tombstoned subject. A thread clear deletes it.

A waiting-for-re-flash row becomes keyless as it ages out. One SQL fragment, `_KEYLESS_SQL` in
`jpanel.py` (panel-addressed rows only), defines it for the clear and for the status.

- This, and the owner's per-message discard of any unplayed row (C3), narrow JP Part B
  "### Data" (*"unplayed messages are kept indefinitely"*). Both are recorded in §4 and
  Appendix B, and W2 writes both into JP Part B "### Data".
- 0209's note that `DELETE /messages` *"deliberately preserves exactly that row"* is about
  **gave-up** rows. They stay kept by a clear.

**6. Explicit merge: `merge_subject(source, into)`.**

- It requires the same `device_role`, a live key on the target, and no live key on the source.
- In this order: take the table lock; **re-point the source's revoked keys to the target
  subject**; run `readdress_orphans` — which never crosses subjects, so it can move the mail only
  once the keys belong to the target — with **the same 14-day window** (older mail stays keyless
  and discardable); tombstone the source as `merged:<into>:<source>`; log the merge.
- `SqlDeviceRepo.list` hides `merged:` tombstones from W2.
- **Exposed** as owner `POST /jpanel/panels/{id}/merge`, where `{id}` is the source **subject**
  id (a retired group's id, §11.1 item 4), with body `{"into": <live-key device id>}` — the id the
  PWA already holds — resolved to its subject on the server; and in W2 as:
  - **debug `POST /api/debug/jpanel/panels/{id}/merge`** under `_OWNER_CTX`;
  - `GET /api/debug/jpanel/panels` (subjects, retired groups, ids, row counts);
  - the `debug-connect.sh` verbs `jpanel-panels` and `jpanel-merge`;
  - a `jpanel.merge` entry in `whoami`'s scopes;
  - a `DEBUG_ACCESS.md` row;
  - `test_debug_merge_route` (CLAUDE.md #10).

**7. The one-time migration** is `readdress_orphans` written as **inline SQL in the alembic
revision**, not a call into app code. There is no schema change.

- Same window (14 days) and same reset (`deliveries = 0` only — `returns` does not exist until
  W3). `test_migration_window_matches_constant` reads the revision file and pins its literal to
  `READDRESS_WINDOW_DAYS`.
- Before merging, the W2 PR runs a read-only count through the debug `/sql` route and states it.
  If the author holds no debug token, the PR says so and the merge waits for the owner to hand
  one over or to accept the bound: at most 14 days of mail moves, and all of it is discardable.
- **The safety valve, in W2:** `DELETE /api/debug/jpanel/messages/{id}`, under `_OWNER_CTX`,
  calling `discard_message` — the function C3's owner route uses from W3. It accepts **any
  unplayed row** (§4). It comes with:
  - a `jpanel.discard` scope in `whoami`;
  - a `debug-connect.sh jpanel-discard` verb;
  - a `DEBUG_ACCESS.md` row;
  - `test_debug_discard_route`, which discards a re-addressed row on a live key.

  So any message that surfaces can be removed without a terminal before P1 ships (CLAUDE.md #10).

**8. The flash-name rule (W2, with merge).** `build_flash` applies the rename rule **before**
`provision_or_reflash`, which revokes every live key for the name, so a refusal never leaves a
dead key behind. The debug `PanelFlashIn` gets it too (today `/flash` accepts any string). It
lands in W2 because its copy points at merge, which W2 builds:

- **Empty** stays valid: an unnamed unit.
- **Whitespace and case** are normalised as `_panel_name` does, and an existing subject of this
  role is matched on `lower(regexp_replace(trim(name), '\s+', ' ', 'g'))` (item 3), reusing its
  stored name — so a pre-rule label with a doubled space or other capitals still matches and the
  identity does not split. **Re-flash's revoke uses the same predicate** (today it compares
  `display_name = :label` exactly), or a case-variant subject would keep a live key and
  `_panel_names` would see two siblings. `build_flash` writes the reused stored name into NVS.
  Test: `test_flash_case_variant_revokes_old_key`.
- **`_panel_name`'s 409** ("that is what an unnamed panel is called") passes through, with the
  sentence "Leave the name empty to flash an unnamed panel."
- **Length over 14, or a character the panel's font lacks**, answers 422: *"Rename this panel in
  Settings to a name of 14 characters or fewer (letters, digits, spaces, hyphens, full stops),
  then flash it under that name. If the old panel was already revoked, flash it under the new
  name, then merge the old panel into it."* Rename works on a live key and moves
  `subjects.display_name`, so the re-flash matches the same subject: no new identity, no merge,
  history kept. A flash under a corrected name **without** renaming would mint a new subject
  while the old live key stays, leaving three live jpet keys and both twins' sends refused with
  409. The merge path needs no live key on the source, which a revoked panel satisfies; merge is
  the debug API from W2 and the PWA from P1.
- Tests: `test_flash_after_rename_keeps_subject`, `test_flash_name_refusals_say_rename_first`
  (which also asserts that no key was revoked), `test_flash_keeps_existing_unnormalised_label`,
  `test_flash_matches_name_case_insensitively`, `test_debug_flash_applies_name_rule`.

**Rollback.** None is needed: rows stay principal-keyed, and revision files are never removed. Under
rolled-back code the `merged:` names appear in the device list; that is cosmetic only.

**W2 touches principal scope.** It goes to the owner for escalation, and its wave gate includes a
red-team review (PROCESS.md).

### 11.2 Tests

No tables or policies change, so the existing RLS tests stand. New tests:

| Wave | Tests |
|---|---|
| **W2** | `test_reflash_carries_unplayed_mail_to_new_key` (sibling and played rows untouched) |
| | `test_rotate_carries_unplayed_mail` |
| | `test_send_racing_reflash_is_carried` |
| | `test_merge_and_reflash_do_not_deadlock` |
| | `test_readdress_orphans_idempotent` and `…_nudges_new_recipient` |
| | `test_sweep_and_send_readdress_respect_cutoff` (a 15-day-old row on a revoked key stays put after a sweep pass and after a send) |
| | `test_concurrent_sends_readdress_without_deadlock` |
| | `test_reflash_after_revoke_lands_on_last_used_subject`, `test_reflash_matches_display_name_not_revoked_label` |
| | `test_owner_threads_group_by_subject_across_reflash` |
| | `test_unknown_panel_group_clearable` |
| | `test_retired_listed_only_with_rows` |
| | `test_aged_out_unheard_to_retired_panel_is_clearable` (by thread clear; an in-window one is kept) |
| | `test_clear_pet_chat_covers_old_keys` |
| | `test_merge_rules_and_tombstone`, `test_reflash_never_picks_tombstoned_subject` |
| | `test_debug_discard_route` (a re-addressed live row), `test_migration_readdresses_only_last_14_days`, `test_migration_window_matches_constant`, `test_readdress_resets_deliveries` |
| | `test_merge_readdress_respects_cutoff`, `test_merge_moves_mail_after_repointing_keys`, `test_device_list_hides_merged_tombstones` |
| | `test_clear_keeps_in_window_mail_of_revoked_panel`, `test_clear_deletes_aged_out_and_tombstoned_rows` |
| | `test_readdress_skips_subject_with_two_live_keys`, `test_send_readdress_scoped_to_recipient_subject` |
| | `test_displays_group_only_with_include_retired` |
| | `test_debug_merge_route` |
| | `test_identity_backfill_pg` |
| | `test_send_after_reflash_delivers` |
| **W3** | `test_unheard_to_retired_panel_is_discardable` |
| | `test_unheard_to_unknown_panel_is_discardable`, `test_unheard_live_row_is_discardable` (`DELETE /messages/{id}`) |
| | `test_gave_up_row_kept_by_clear_and_retryable` |
| | `test_readdress_resets_returns_and_offered_at` |
| | `test_next_by_sibling_id_is_204` |
| | `test_waiting_offered_at_stamps_only_own_rows` (isolation: the W3 panel-reachable write) |

**Coverage.** `/returned`, `merge_subject`, `discard_message`, `readdress_orphans` and every debug
route are security paths — as is **every new jpanel debug route** — so 100% line coverage, as
CLAUDE.md #5 requires. `DEBUG_ACCESS.md`'s `whoami` row is rewritten with the full scope list
(`jpanel.panels` for `GET /api/debug/jpanel/panels`, `jpanel.merge`, `jpanel.discard`,
`jpanel.retry`). The W2 and W3 PRs attach
the per-file coverage lines for each as evidence.

## 12. Waves

*The firmware rows (F0–F4) and their order are superseded by §15: they ship as one release, 0.4.0, before W1–W3 and P1. The gates below still apply to it.*

Each wave is one PR. Each package runs its **whole** gate set, from its own directory (CLAUDE.md
#11):

- **backend:** `ruff check .`, `ruff format --check .`, `pyright`, `pytest` — including the testcontainers integration suite and the combined 80% coverage gate, which W2 and W3 especially depend on. It also runs on
  every firmware wave, because its path filter covers `firmware/main/**`.
- **frontend:** `npm run lint`, `npm run typecheck`, `npm run test`, `npm run build`.
- **W1 edits `ci.yml`**, which trips every package's path filter, so W1 also runs the supervisor,
  `jcode` and `jlaunch` gates. W1 also corrects `plans/README`'s JP row ("Proposed W1◻ …") to
  JP's own header.
- **supervisor:** its full set, triggered by `firmware/dist/**`.
- **firmware:** `make -C firmware/host test`, plus the job's ESP-IDF build and its `dist/`
  byte-compare. F3 replaces `run_ui_tests` with `run_sm_tests` in the host target.

**Every firmware wave also:**

- bumps `version.txt` and commits a rebuilt `dist/`;
- keeps the bootloader byte-identical;
- **passes the internal-RAM gates (§6a)**: the `idf.py size` step F0 adds to `firmware.yml`
  checks internal DRAM (`.data` + `.bss`) against F0's recorded baseline plus the delta the
  wave's PR states; and after deploy the first report's `int_min` (read through the debug API,
  after every task has started) must stay at or above F0's measured floor. A panel below it is
  handled the F0 way;
- keeps the update check **before** any task starts (§6a);
- applies the §10 retargets it owns.

**Every wave also:**

- runs `docs-freshness`;
- updates this plan, JP §3b, the firmware READMEs, and the ROADMAP / `plans/README` rows (the
  ROADMAP entry is under Phase 7, "Panel voice-post fixes");
- adds no dependency without updating `dev-setup.sh` (#8);
- leaves no shell-only operator step (#10).

F3 also updates JP's text on exit, the pair, and refusals.

| Wave | Contents | Depends on | Gates |
|---|---|---|---|
| **W1** box/PWA, invisible | A3 box (+ `asyncio.wait_for` inside `_transcribe`'s `try`); C2; C5 (+ a clear releases blobs); C6 (query function + backend `/summary` only — **no badge UI**, which is P1's); C7 audio route; C8 (non-visual); C9; C10; C11; names are cut to 31 escaped bytes on the wire; `ci.yml` edits (the revision-delete step in the backend job); skew pins; revision-delete CI check | — | backend, frontend, supervisor, `jcode`, `jlaunch` (the `ci.yml` edit trips every filter), docs |
| **W2** identity | §11 (one-statement `readdress_orphans` with its 14-day window and lock order, grouping, keyless rows cleared by a thread clear (waiting-for-re-flash rows kept), `discard_message` + debug discard, owner and debug merge, listing, tombstones hidden in `SqlDeviceRepo.list`, `whoami` scopes, inline-SQL migration with a pre-merge count, the flash-name rule of item 8); retired chips hidden; JP §3b, §5 and Part B "### Data" updated for the retention change. **Escalation + red-team review.** | W1 | backend, frontend, docs |
| **W3** contract | `/waiting` ids + `offered_at`; `/next?id=` + headers; `/played {heard_ms}`; `/returned` + `_ALLOWED` + tunnel test; C3 owner routes; uuid guards; columns `heard_ms`, `offered_at`, `returns NOT NULL DEFAULT 0`; `readdress_orphans` also resets `returns` and `offered_at`; head and buffer pins. **Red-team review**: it adds panel-reachable writes (`/returned`, the `offered_at` stamp, an `_ALLOWED` row). | W2 | backend, docs |
| **F0** booting baseline | §6a: 0.3.39's code with the persistent WebSocket disabled, as 0.3.40 — a minimal diff from HEAD, so the placeholder names stay; measurements on hardware; a `speech_ok` report flag with `TelemetryIn`; `SPEECH_MIN_INTERNAL = S + T + P`; the `idf.py size` step and DRAM baseline | — | firmware, backend, supervisor, docs |
| **F1** host foundations | §10, no behaviour change against F0's baseline | F0 | firmware, backend, supervisor, docs |
| **F2** engine | §7; defects only; wrappers | F1, W3 | firmware, backend, supervisor, docs |
| **F3** state machine | §8; Appendix A; Q1–Q3; hand-back | F2 | firmware, backend, supervisor, docs |
| **F4** panel visuals | Overlay, pips, pulse, glyphs, hush ring; `CUE_NOBODY` / `CUE_SENT` / `CUE_BUSY` contours. **GUI gate:** interactive mocks first, run on the bench (`docs/mocks/jpanel-s3/`, the firmware's own UI in WebAssembly, with its own cues). | F3 | firmware, backend, supervisor, docs |
| **P1** PWA | C3 buttons; C4 copy; C5 status; composer; launcher badge; read dwell; retention line; retired threads + merge button; "Displays"; "Load earlier"; delete/revoke copy. **GUI gate.** | W2, W3 | frontend, docs |

**Order:** W1 → W2 → W3, and F0 → F1 → F2 → F3 → F4. P1 can land any time after W3; F0 and F1
can run in parallel with the W waves, and F2 needs both W3 and F1.

## 13. Risks, losses, questions

**Risks**

- F3 is a one-wave replacement (§8.5–8.6).
- Cross-core ordering is reviewed, not tested. So is `link.c`'s clamping: on the host it is
  covered only through the `wsproto.c` helpers (`wsp_wait_step`, `link_left`, the `open` split),
  not the real socket and HTTP calls.
- **A PLAY behind a stalling HTTPS poll can expire.** On the HTTPS fallback a poll's body can
  stall a further `LINK_STALL_MS` after its head, and cancellation is checked only between steps,
  so a PLAY posted behind it can pass its 35 s expiry before claim and end with `CUE_OOPS`. That
  is audible and the child can press again, so it is accepted. (A PLAY cannot queue behind a long
  SEND: F2 refuses it while lease 1 is held, and F3's notice and AGAIN answer "busy" during a
  send.)
- HTTPS waits last until their enforced deadlines.
- **DNS and the TLS handshake's internal reads inside `esp_http_client_open` cannot be clamped**
  (lwIP's resolver timeout; the TLS reads each get the full timeout). On the HTTPS
  fallback, a slow resolve can push a step past its deadline. The claimed backstop then fires:
  - for a PLAY, "didn't go";
  - for a SEND, "didn't go" — and a SENT arriving later is logged but not sounded.

  This is visible as `late_outcome`.
- The name-based display gate needs review backing.

**What the owner loses**

- Web push.
- Resend after a failed send.
- "Played" instead of "heard".
- Renames rewrite past messages.
- A stop under 2 s returns the message (as a badge), unless the stop was a REPLY.
- During her own upload the notice is drawn, but pressing it or AGAIN answers "busy".
- The microphone stays leased through a send's upload and the box's transcription — up to about
  three minutes for a 35 s message on a 10 KB/s link, about six if the box asks for a retry — so the pet answers "busy" to its wake word
  for that long.
- A cancel before the head can cost one attempt.
- **Until F2, a long send can report "didn't go" although it was stored.** F0's
  `JPANEL_HTTP_TIMEOUT_MS` is 20 s, while W1 lets transcription take up to 60 s; a 35 s message
  whose transcription runs past 20 s times out on the panel, and the child may re-record a
  duplicate. F2's SEND head budget (60 + 15 s, §7.5) closes it.
- Each duplicate subject left by re-flashes before W2 needs one explicit merge.
- Between W2 and P1, display-role pet chats (today drawn as "panel xxxxxxxx" chips) disappear
  from the PWA: their "Displays" group needs `?include_retired=1`, which only P1 sends.
- An ack given up after 10 minutes may replay within the cap.
- An arrival during an upload is announced only after it.
- **The pair hides while any message waits.** So once Dad's message arrives, the child can no
  longer reply to her sister's earlier message from the pair. Playing Dad's message clears that
  offer, and replying to the sister then needs the grid. This is the B3 decision: one owner per
  quadrant.
- Unheard messages older than 14 days on a revoked key are not re-delivered — by the migration,
  the sweep, a send or a merge. They stay clearable and discardable instead.
- A gave-up message inside the window is offered again after a re-flash, up to its cap.

**Questions** (the plan runs with these defaults):

1. `HEARD_MIN_MS = 2 s`.
2. The 600-character text cap stays.
3. Web push stays deferred.
4. PLAY waits for a free speaker, not for a pet turn in flight (today's behaviour). A late pet
   reply preempts the stream, which then resumes.

## 14. Review log

Every item in thirteen rounds of cold review was checked against the code before it was accepted.
None was rejected. The per-round counts are in the intro.

**Mechanisms introduced and later removed**, because each kept producing defects:

- an event queue → replaced by the level snapshot;
- a subject/RLS identity rewrite and its revert revision → replaced by re-address on re-flash;
- a busy token;
- `PEND_REPLY`;
- stall and codec-wedge discards;
- summed time bounds → replaced by enforced deadlines;
- a cue queue → replaced by slots;
- a build-time cue peak table with chunked rendering → replaced by whole-cue rendering on the
  render task.

**Round 7** — 6 blocking items, with the evidence checked:

| Item | Evidence checked | Resolution |
|---|---|---|
| FW-1: the heartbeat watchdog would reboot during a normal long send | `ws_request`'s head wait and `esp_http_client_fetch_headers` block with no callback; the SEND head budget is 75 s | `hb` ticks in every `link.c` wait and in the sink's wait; "wedged" requires **both** a stalled `hb` **and** a passed deadline + 62 s. Tests: 35 s send + 60 s transcription; a 60 s pause (§7.5d) |
| FW-2: the peak table missed the rendered variants | `audio.c` passes a 0..255 counter; `cue.c` uses `v%8`, `(v/8)%4`, `v/32` and `v%5` | Table and chunking dropped. Render renders whole cues, as today, into per-slot buffers; slots are packed `seq24\|cue8` words used as per-slot seqlocks (§7.4) |
| UX-1: AGAIN during THINK dropped the turn's failure handling | `ui.c` keeps `pending` alongside THINKING, and `ui_frame` still reads `net` | The pet turn is a sub-state processed every frame; `test_again_during_think_then_failed_turn_still_dashes` |
| UX-2: FACE liveness and the counter assumed `cur` had already left `waiting` | `jpanel.c` drops the entry in `fetch_head`, after the speaker wait | Both use the de-duplicated set `waiting ∪ {cur.id}`; a test whose snapshot still lists `cur.id` |
| BE-1: which unheard rows does a clear delete? | 0209 is about cap-exhausted rows; `/retry` can deliver those | Split: **keyless** (deleted by clear, discardable — the recorded change) vs **gave up** (kept by clear, retryable, discardable individually) |
| BE-2: no way to remove a surfaced old message before P1 | The W2 debug surface had no delete; `/sql` is read-only | Debug `DELETE /api/debug/jpanel/messages/{id}` in W2, plus the migration re-addresses only rows from the last 14 days |

**Round 8** — 7 blocking items, each a cross-section inconsistency:

| Item | Evidence checked | Resolution |
|---|---|---|
| BE-1: the runtime re-address undid the migration's 14-day cutoff, and "keyless" was per panel | `jpanel_retention_loop` sweeps at once on start; `readdress_orphans` had no window | One `READDRESS_WINDOW_DAYS` for every caller and the migration (pinned); keyless defined per key; `test_sweep_and_send_readdress_respect_cutoff`; one-statement re-address (§11.1) |
| BE-2: W2 wrote `returns`, a W3 column | §6 "W2 is data-only"; the column is created in W3 | W2 resets `deliveries` only; W3 extends it to `returns` and `offered_at` |
| BE-3: the debug discard refused a re-addressed live row | the discard was limited to owner-sent, gave-up or keyless rows | `discard_message` (W2) accepts any unplayed row as an explicit owner act; recorded in §4, Appendix B and JP Part B |
| FW-1: F2's `jpanel_stop()` wrapper dropped today's clip cut | `jpanel_stop()` → `audio_stop()` sets `s_play_pos = s_play_len`; `CUE_REPLY` at `display.c:2958` is cut by `jpanel_stop()` at `:2962` | F2 maps it to `silence` + `stream_stop()`; the reorder that lets `CUE_STOP` and `CUE_REPLY` sound is F3's; §4 names both cues |
| FW-2: the arbiter watchdog would boot-loop a panel with no codec | `audio_start()` returns false before `xTaskCreate`; `display.c` carries on silent | Armed only when the task exists, timed from the first `arb_hb` change; `test_no_codec_never_arms_arbiter_watchdog` |
| UX-1: standby left the pet turn alive, so a follow-up could open the mic in the dark | today's standby resets `s_talk`; the machine's SPOKE edge would re-arm the follow-up | Standby sets `turn.state = NONE` without `TALK_CLEAR` and `follow_turns = MAX`; the follow-up opens through the mic gate |
| UX-2: Appendix A #44, #14 and #33 did not hold | #44 asserts `UI_TARGET_TRANSPORT`; #14 assumed `cur` left `waiting` at claim; #33's `!w.muted` is harness-only | #44, #33 → R; #14 rewritten over the ordered selection set `S`, now defined (§8.2) |

**Round 9** — 5 blocking items:

| Item | Evidence checked | Resolution |
|---|---|---|
| BE-1: "keyless" deleted mail that a re-flash would still deliver | `revoke` (`repo.py:170`) is unchanged and separate from re-flash; reflash, rotate and merge re-address in-window mail | Keyless = no future re-address can deliver it; a new **waiting-for-re-flash** status is kept by a clear; one `_KEYLESS_SQL` predicate; `test_clear_keeps_in_window_mail_of_revoked_panel` |
| FW-1: the UI backstop margins were smaller than snapshot delay plus step slop | the storm delays snapshots up to 2 s; a step may overrun by `LINK_STEP_MS` | `UI_MARGIN_MS = 3000` for both backstops and the watchdog; the engine stops claiming at `expires_at − UI_MARGIN_MS`; storm delays capped below the margin |
| FW-2: F3's EXIT, FACE and swipe no longer cut a sounding pet reply or cue | `audio_stop()` sets `s_play_pos = s_play_len` on every stop today | Every child-chosen stop emits `SILENCE` then its cue; a pending send outcome survives a silence (E1.2; narrowed to `send_result` alone in round 10) |
| UX-1: I4 contradicted LISTEN/RECORD's silent miss, and recovery could drop a recording | `ui.c`'s CONFIRM miss sets only `tap_missed`; #36 asserts no flinch | The miss is *ignored* (logged); only I1, I5 and I6 recover, and the rest are counted |
| UX-2: arrivals outside PET were never marked seen, so the selection jumped home after a face pick | the unseen-id rule was scoped to PET | Ids are marked seen in every mode; home only on an unseen edge in PET; re-resolve by id, then clamped index |

**Round 10** — backend and firmware GREEN; 2 blocking items (UX):

| Item | Evidence checked | Resolution |
|---|---|---|
| UX-1: a pet turn was cleared on the frame it started, and a new LISTEN read the last capture's length | `in` is sampled before `sm_step`; `talk_send` sets `BUSY` after it (`talk.c:166`); `audio_capture_ms()` is not reset on close; #38's harness sets `net` in `perform()` | Frame-boundary rule 1 (§8.2): nothing entered this frame is judged by this frame's inputs; `capture_open` resets the count; I1 checks the committed capture; #38 → R |
| UX-2: "grid cells do not share the tick's quadrant" was false | `sendto_hit` puts the pet cell lower right; `confirm_hit` makes that the tick; touch has no debounce | Frame-boundary rule 2: any press that changes the mode gives the new mode's targets `EXIT_GRACE_MS`; also covers the notice → FACE bounce; #42 → R |

**Round 11** — 4 blocking items:

| Item | Evidence checked | Resolution |
|---|---|---|
| BE-1: the flash-name 422 sent the owner down a path that splits identity and needs a merge the plan refuses | re-flash matches by `display_name` and revokes only same-name keys (`repo.py`); rename moves `display_name` on a live key; merge needs no live key on the source | The 422 says rename first, then flash; merge only for an already-revoked panel; 409 and whitespace handling stated (§11.1 item 8); `test_flash_after_rename_keeps_subject` |
| FW-1: F2's `jpanel_replay()` took its id from the last *heard* outcome | 0.3.39 leaves `s_in_id` set after a stop or a short stream and replays it | F2 takes `{id, from, from_dad}` from `cur`; two characterisation tests |
| UX-1: the press grace covered PAUSE and REPLY | `ui.c:482-485` and JP 0.3.36 record a clock on the pause as tried and rejected; it breaks #45 | Rule 2 covers EXIT, FACE, NOTICE, TICK, CROSS and grid cells; PAUSE, REPLY and swipes are exempt |
| UX-2: the selection hold had a gap during the `/returned` round trip | §7.3 re-inserted the id only at step 3, after the ack | The engine re-inserts a RETURNED id into `waiting` in the same snapshot write as the outcome |

**Round 12** — 6 blocking items, plus a field incident:

| Item | Evidence checked | Resolution |
|---|---|---|
| BE/FW: `jp` was nested, but `TelemetryIn` takes `dict[str, int]` | a 422 is a failed report (`main.c` keeps the crash ring); the field-scan test reads `main.c`'s keys up to `pmu_history` | Flat integer keys; `jp` placed after `pmu_history`; `test_jp_keys_from_main_c_validate_against_telemetry_in`; both existing tests retargeted in F2 and listed in the §10 census |
| FW: `heard_unacked` assumed a positional fetch has no id | today's box sends `X-Jpanel-Id` on every `/next`, and 0.3.39 acks it | A positional fetch still acks the head's id; `heard_unacked` only when the head has none; `test_old_box_positional_fetch_still_acks_header_id` |
| UX: rule 2 missed AGAIN and the pair's REPLY | the grid's cancel cell, the cross and AGAIN share the lower-left quadrant | Both graced; the exemption is PLAY's PAUSE/REPLY and swipes; two bounce tests |
| UX: Finished and the backstops matched an unposted PLAY | `play.seq` was unassigned before the post | `play.seq = NONE` until posted; every engine-facing rule gated on it; `test_old_stop_outcome_does_not_finish_new_pending_play` |
| UX: the hold grace silenced deliberate grid taps (#33) | #33 taps ~340 ms after the hold | The hold grace covers only down edges within 40 px of the hold point; `test_deliberate_cell_tap_after_hold_acts` |
| Incident: 0.3.39 boot-loops on real hardware | the USB console: the WebSocket takes ~48 KB internal (119 → 70 KB); speech starts above its 48 KB floor; the PHY's `esp_timer_create` then fails `ESP_ERR_NO_MEM` and aborts | §6a (as revised in round 13): F0 = 0.3.39 with the socket disabled; internal-RAM gates on every firmware wave; a measured `SPEECH_MIN_INTERNAL`; the update check stays first |

**Round 13** — 7 blocking items; the owner decided the twins stay on the S3 panels:

| Item | Evidence checked | Resolution |
|---|---|---|
| F0 as 0.3.38's code deletes what F1–F3 build on | 0.3.38 has no `link.c`/`wsproto.*`; the backend reads them (`test_panel_ws.py`, `test_jpanel_api.py`); `link.h:9-14` records 0.3.38's two-session "connect" failure | F0 = 0.3.39 with the persistent WebSocket disabled: `link.c`'s HTTPS fallback under its one lock; the socket is not planned to return (§6a) |
| RAM floors rested on misread figures | the ~100 KB was the AGC-on cost (`ROOM_ENDPOINT_PLAN.md` §10.4ba); `link.h`'s ~75 KB is free heap at failure; speech started at 70.5 KB in the incident | F0 measures `S`, `T`, `P` on hardware and sets `SPEECH_MIN_INTERNAL = S + T + P` below the measured pre-speech free; `speech_ok` added; a deaf panel cannot send, so the floor only prevents a crash loop |
| The DRAM gate had no CI step | `firmware.yml` has no size step | F0 adds `idf.py size` with a recorded baseline; each wave states its delta |
| A swipe from the notice played a message | touch queues the press on the down edge; a swipe is recognised after 60 px | A swipe whose down edge entered PLAY unwinds to PET with the stop pattern; `test_smear_from_notice_never_plays` |
| A notice press during a stop's hold targeted an acked id | the held id is in neither `waiting` nor `S` | Posting waits for `done_seq ≥ stopped_seq`; the target is resolved at the post; the held sender is drawn; a test |

**Round-13 non-blocking items, all taken** (39): backend — the revoke uses the flash match's
normalised, case-insensitive predicate, and the reused name goes to NVS; "panel-addressed"
unheard rows in E3.6 and C5; `readdress_orphans`' subject scope and its callback seam;
row-ordered locking; the revision step's wording, `--no-renames` and `fetch-depth`; the
F0-to-F2 send-timeout gap in §13; the merge body; the `whoami` scope list; the `offered_at`
isolation test; the coverage wording. Firmware — §6a's numbering; the F0 skew header and scripted
box; `LINK_HEAD_MAX` on the HTTPS path; F0 as a minimal diff that keeps the placeholder names;
SEND's zero gen; `msg_err` a string; RTC words and brownouts; `run_continues = false` after a
failed ack. UX — `mode_press_at` cleared without a press; graced presses silent and logged; the
button-before-touch change in Appendix B; the hold point taken at the hold; re-resolution placed
in the frame; `run_pos` skips 204s; #28, #30, #33 and #35 notes; the pet-reply wording; the grid
refusal closes the grid; a pending transport press flinches; calibration's SPOKE-before-timeout
order; one writer for standby.

**Round-12 non-blocking items, all taken** (43): backend — the name rule before
`provision_or_reflash`, with no key revoked on refusal; case-insensitive matching on the
normalised name; the rule moved to W2 with merge; the `ensure_ascii` wording and the 2-byte
headroom; the sibling compared before the cut; `/next?id=`'s full predicate; the revision check
folded into the backend job; trims logged; the sweep's re-address independent of its early
return and the 6 h repair; item 3's rename wording; older same-name subjects need a merge; the
`jpanel.retry` scope; the merge route takes a subject id; the swipe-stops B-UX row; the
`plans/README` JP row. Firmware — `last_begun` as the replay source; the replay guard stated as
new in F2; `jpanel_fetching` from the post; the RETURNED re-insert bumps the count; the sticky
heard bit; the watchdog cap's time base, magic and re-arm on a version change; the telemetry test
names; `CUE_MAX_MS` defined from `CUE_MAX_SAMPLES`; the slot seqlock's fences; the stop word
before `silence`; CAS words in internal RAM and a host shim for `EXT_RAM_BSS_ATTR`; the
arbiter-present flag after `xTaskCreate`; A2 split across F2 and F3; the impossible
SEND-behind-PLAY risk removed. UX — FACE wraps; the voice-stop test renamed; E2.2 "at any engine
phase"; `run_pos` advances at the seed; the old-box selection in PLAY; §4's standing-pair row;
`capture_refused`; a swipe from the big box stays a swipe; #43's note.

**Round-11 non-blocking items, all taken** (35): firmware — the report's fields, `jp` object,
byte budget and `TelemetryIn`; an RTC cap on watchdog reboots; `ota_running` and the clock
restart; `silence` excluded from the busy count; `*_done` written with no buffers; the stop flag
packed into the gen word; the HTTPS-body predecessor; the WebSocket-cancel wording; storm clock
jumps bounded; `PENDING_MS` derived from the slot worst case; the ring change assigned to F2;
`EXT_RAM_BSS_ATTR`; F2 `jpanel_state`/`jpanel_clear` precedence; F2 refusal guards kept; slot
eligibility for refusal and chime; two press writes per tick noted. Backend — the revision check
on `pull_request` and `workflow_dispatch`; W1's gate column; best-effort post-send re-address;
`/summary`'s flag; the owner route keeps truncation at cap + 0.5 s; retry resets `offered_at`,
with a debug twin; the W3-rollback note; the two-live-keys case logged; the `clear_history`
docstring; the `repo.py` lock comment. UX — swipes exempt from the grace; `play.seq` assigned at
post; a standby frame drops touch and voice; the pair's EXIT silences; every child-chosen stop
disarms the follow-up; #22's mechanism; hold-opened GRID graced; `run_pos` advances; E2.9 aligned
with §8.4; the stale `display.c` comment.

**Round-10 non-blocking items, all taken** (44): backend — the send-path re-address in its own
transaction after commit, with the race test extended; re-flash matches `subjects.display_name`;
the flash-name rule in `build_flash` (empty valid; charset and length copy; merge, not rename,
for a revoked unit); `ensure_ascii` length and an ASCII-safe `X-Jpanel-From`; `_KEYLESS_SQL`
panel rows only; the Displays loss; E4.1's window; the W1/W2 split of the owner-row test;
`/summary`'s path and shape; `ci.yml` trips every package; `/next` moves `_panel_names` too.
Firmware — slot buffers carry their length; only `send_result` survives a silence;
`heard_unacked` defined; the F2 wrapper rows completed; the hooks reach the slot writer; step 4
uses the claim margin; render-side busy facts count unconsumed seqs; talk waits on
`reply_done`; the no-poll-while-busy guard kept; both watchdogs suspended during OTA; HTTPS
ABSOLUTE semantics stated; the 422 retry doubles the busy window; the firmware gate names the
IDF build and the `dist/` compare; a PSRAM failure refuses at once; mid-run arrivals join only
while another message remains. UX — E1.6's exit quadrant on a standing pair; FACE grace;
`sm_face` none with no notice; today's poke kept in THINK; Appendix B relabels; `speaker_busy`
counts unconsumed seqs; the selection held while a stop's outcome is pending; scroll-in ids
beyond 8 are not arrivals; the old box keeps today's count rule; Appendix A #1, #7, #15, #40
relabelled; I1 on the committed capture; `mode_press_at`; calibration leaves the turn; the
follow-up edge is `reply_busy` falling.

**Round-9 non-blocking items, all taken:**
- **Firmware:**
  - bounded slot-seqlock retry;
  - slot words read before `silence`;
  - stale-gen streams excluded from the busy facts;
  - F2's `audio_stream_paused()` scoped to the live stream;
  - the IDLE watchdog rule;
  - an honest SEND-blocking risk, and a busy window of about three minutes;
  - the unreachable replay `stopped` row removed;
  - a 204 on a run's next id;
  - no double count on an old box;
  - `CMakeLists.txt` `SRCS`;
  - lease 2's F2 wrapper;
  - a no-codec PLAY refuses at once;
  - `display.c:2958`.
- **UX:**
  - a full in-frame order (hold, timers, posting; a swipe's release is not a tap);
  - `turn.state == NONE` in the mic gate;
  - `talk_clear` corrected — it is a no-op while BUSY;
  - `sm_face` for LISTEN, RECORD and GRID;
  - "claimed" defined;
  - FACE ignored in a replay;
  - REPLY counts as heard;
  - Appendix B relabels (transport until ON, pending expiry);
  - Appendix A #7 and #36 notes;
  - SENT polls soon;
  - `seen_ids` never evicts `cur`;
  - calibration's silent drop stated.
- **Backend:**
  - merge order (re-point keys, then re-address);
  - `HAVING count(*) = 1`;
  - the send-path re-address scoped to the recipient's subject;
  - names cut on escaped length, `X-Jpanel-From` included;
  - the `FlashIn` bound and its 422 copy;
  - "Displays" gated behind `?include_retired=1`;
  - until P1 a clear reports through its counts;
  - gave-up rows re-offered after a re-flash, stated;
  - coverage evidence in the PRs;
  - the revision-check mechanics.

**Round-8 non-blocking items, all taken:** ON requires `stream_live`; a run polls between
messages when flagged; `speaker_busy` / `clip_busy` / `reply_busy` / `stream_live` defined; the
press-drop rule moved to the arbiter after `silence`, excluding the stream; the reply slot's
`stamp` with `id` published last; the 422 retry restarts its first-frame bound; IDLE bumps `hb`;
SEND-behind-PLAY expiry and host-untested `link.c` clamping in §13; one render-wide 24-bit seq
counter; `CAPTURE_MAX_MS` and `PLAY_BUF_MS` move to `stream.h`; THINK derived and voice "stop"
keyed on `turn.state`; the hold requires `down.target == PET`; expiry in PLAY sounds `CUE_OOPS`;
a backstop no longer clears the offer; wake sounds `CUE_HEARD`; the PLAY swallow flinches;
the counter's numerator at the press is decided; `cur.in_waiting` for old boxes; same-frame order;
`asyncio.wait_for` inside the `try`; `FlashIn.name` bounded and names cut on the wire; W3's
red-team gate; 100% coverage for the new security paths and a sibling-id isolation test; the
ROADMAP entry; tombstones hidden in W2; W1 is backend `/summary` only; the no-token path for the
pre-merge count; a clear releases blobs; merge applies the window.

**Round-7 non-blocking items, all taken:**

- **Engine and audio:**
  - every non-reply slot is one packed word;
  - `s_reply` is reused only after `reply_done`;
  - `run_continues` is simplified;
  - the F2 wrapper table is complete;
  - today's press-cue drop rule is kept;
  - the TLS handshake is named as a risk;
  - `asyncio.wait_for` around `_transcribe` (W1);
  - an `arb_hb` watchdog;
  - a truncated gen compare;
  - `check_no_logic_in_audio` has a defined mechanism;
  - the Makefile `SRC` changes are stated.
- **Panel state machine:**
  - the §4 exit-cue claim is corrected (round 8 adds the reply cue);
  - `TARGET_IGNORED` keeps #9 unchanged;
  - the tick/cross grace applies only after REPLY;
  - a count-rise chime fallback for boxes without ids;
  - the replay rows in the finished table, and the replay backstop keeping the offer;
  - Appendix B's THINK row corrected;
  - losing the hidden pair is listed in §13;
  - `sm_face` defined in THINK;
  - the hold keeps `net != BUSY`;
  - the notice grace also covers FACE (swipes are exempt since round 11);
  - silence disposes of a pending reply;
  - the follow-up rule clarified;
  - the chime is PET-only everywhere.
- **Backend:**
  - re-address resets `deliveries` (and, from W3, `returns`);
  - the JP Part B "### Data" location and the `sweep.py` cross-reference;
  - the `repo.py` comment rewrite;
  - `?include_retired=1` plus the PWA-skew note;
  - why the lock order holds;
  - an explicit merge-tombstone test;
  - backend gates spell out the integration suite and coverage;
  - the 31-character buffer pin;
  - the tunnel's 404 wording.

---

## 15. Execution: the 0.4.0 rewrite

**Owner decision.** No stopgap: F0–F4 ship as one firmware release, **0.4.0**, an almost complete
rewrite of the panel's interaction and voice-post code. The twins stay on their S3 panels. This
section supersedes the firmware sequencing of §6a (F0 as a release), §10 (F1 as a
behaviour-preserving wave), §12 (the F rows and their order) and §7.7's F2 wrapper table: there is
no wrapper layer, because `panel_sm.c` replaces `ui.c` and `display.c`'s duplicated logic
directly. Every design rule in §6a–§10 still holds.

- **Order:** 0.4.0 first; the backend waves W1–W3 and the PWA's P1 follow it.
- **0.4.0 against today's box.** It must work fully against the box as it is today, degrading
  exactly as §6 says — for example, a stop acknowledges when the head has no `X-Jpanel-Delivery`,
  and "new" means a count rise when `/waiting` carries no ids — and pick up each new box feature
  when it appears.
- **F0's content is part of 0.4.0:** the persistent socket off (`LINK_PERSISTENT_SOCKET 0`), the
  `idf.py size` gate, `int_min`, `speech_ok`, and a measured `SPEECH_MIN_INTERNAL`.

**The interfaces are written** and compile with `-Werror` on the host (`make -C firmware/host
test`) and with the ESP32-S3 toolchain:

| Header | Contract |
|---|---|
| `link_types.h` | `link_req_t`/`link_res_t` moved out of `link.h`, plus `deadline_ms`, `upload_mode`, `head_budget_ms`, `on_progress`, `cancelled`, `timed_out`. `deadline_ms == 0` keeps the legacy `timeout_ms`, so callers migrate one by one. |
| `link.h` | Includes `link_types.h`; `LINK_PERSISTENT_SOCKET 0`. |
| `jpanel_types.h` | Commands (`jp_cmd_t`), the snapshot (`jp_view_t`), outcomes, waiting entries with ids, flat `jp_stats_t`, `jp_seq_ge`, `jp_due`, the expiry and margin constants. |
| `stream.h` | The shared audio state (`stream_t`) with a single writer per word; cue slots, silence, reply, stop word, pause, lease; render-side `stream_facts`; producer and arbiter calls; `stream_tick`. |
| `jpanel_core.h` | `jpanel_io_t` (request, queue, clock, `/waiting` parser, SHA-256, yield hooks); `jpanel_core_init`/`_step`/`_poll_soon`/`_snapshot`/`_stats`. |
| `panel_sm.h` | `sm_in_t`, effects (`sm_fx_t`), `sm_out_t`, `sm_view_t`, `sm_t`, `sm_step` and the pure queries `sm_target`, `sm_face`, `sm_busy`, `sm_check`. |

`firmware/host/iface_check.c` compiles them until the implementations land; it is then deleted.

**Modules and owners** — one agent each, working in parallel against the headers:

| Agent | Files | Tests to port or add |
|---|---|---|
| **A — link** | `link.c` (deadlines and `on_progress` in every wait; `cancelled`; the socket off, starting in the HTTPS fallback); `wsproto.c/h` (`wsp_wait_step`, `link_left`); the callers' migration in `ota.c` (3 sites), `talk.c`, `jpanel.c`'s io | `tests.c` wsproto group plus `wsp_wait_step` and `link_left`; backend `test_the_firmware_builds_exactly_that_base`, `test_the_firmware_spells_the_same_route_and_frame` kept green |
| **B — audio** | new `stream.c`; `audio.c` cut to I/O, ALC, levels, `speech_feed`, calling `stream_tick`; `cue.c/h` (`CUE_BUSY`, `CUE_NOBODY` placeholders); `check_no_logic_in_audio`; `CAPTURE_MAX_MS`/`PLAY_BUF_MS` deleted from `audio.c` | new `run_stream_tests`: `test_stop_while_paused_drains`, `test_cursor_writers`, `test_outcome_waits_out_reply`, `test_standby_silences_then_sounds_its_own_stop`, `test_exit_during_pet_reply_silences_it_then_sounds_stop`, `test_slot_seqlock_never_pairs_seq_with_other_samples`, `test_heard_then_oops_both_sound`, `test_press_never_cuts_outcome`, `test_refusal_coalesced_and_expires_from_eligibility`, `test_no_codec_never_arms_arbiter_watchdog`; `tests.c` `test_cue_busy_distinct`; backend retargets: `test_a_cue_does_not_shorten_a_recording` → `stream.c`, the two `CAPTURE_MAX_MS` readers → `stream.h` |
| **C — engine** | new `jpanel_core.c`; `jpanel.c` cut to the task, queue, cJSON, mbedTLS and the real io (`jpanel_post` and `jpanel_snapshot` for render); `jpanel.h` trimmed (its `JPANEL_QUEUE_MAX` copy deleted) | new `run_jpanel_tests` with a scripted box in two modes (**today's box** and the new contract): every engine test in §9 A1, A2, A3 (F2 half), A4, A5 (engine half), A6 (engine half), A7–A9; §7.5's tests; `test_old_box_positional_fetch_still_acks_header_id`; `test_send_outcome_survives_concurrent_chain`; `test_claim_near_expiry_never_trips_unclaimed_backstop`; backend retargets of every census row that reads `jpanel.c`/`jpanel.h` |
| **D — machine** | new `panel_sm.c`; `ui.c`, `ui.h`, `host/ui_tests.c` **deleted**; `ui.h`'s timing constants move, with their comments, to `panel_sm.h` | new `host/sm_tests.c` (`run_sm_tests`): all 51 Appendix A tests with the stated U/R dispositions; every test named in §8.2, §8.5 and §9 B1–B10; `test_storm` |
| **E — integration** | `display.c` cut to sampling, the executor and drawing `sm_view_t` (its `s_pending`, `s_repeat_until`, `s_talk`, `s_sendto_until`, `s_paused_since`, `s_pend_reply_to` and tap-order logic deleted); `main.c` (the `jp` object after `pmu_history`, `speech_ok`, the update-first order, watchdogs and the RTC cap, the internal-RAM floor); `talk.c` (lease 2, the reply slot, waiting on `reply_done`); `ota.c`'s `ota_running`; `check_no_logic_in_display`; `CMakeLists.txt` `SRCS` and the host Makefile; `firmware.yml`'s `idf.py size` step with the DRAM baseline; `version.txt` 0.4.0 and `dist/` | `jpanel_tests` end to end (`sm_step` against the real `jpanel_core` and `stream.c`, including `test_selection_held_while_stop_outcome_pending`); `test_update_check_precedes_task_starts`; backend: `TelemetryIn.jp` and `speech_ok`, `test_jp_keys_from_main_c_validate_against_telemetry_in`, `test_the_model_declares_all_of_them` and `test_the_panel_s_whole_payload_fits_the_buffer_it_is_built_in` retargeted, and the §10 census rows that read `display.c`, `ui.h` and `ui.c` |
| **F — visuals (F4)** | the overlay, pips, pulse, glyphs and hush ring; the `CUE_NOBODY`/`CUE_SENT`/`CUE_BUSY` contours | The GUI gate first: interactive mocks on the bench (`docs/mocks/jpanel-s3/`), approved by the owner. If they are not approved when E is done, 0.4.0 ships the placeholders and F follows in a later release (0.4.1 went to the bench's `overlay.c` split). |

**Integration order**

1. A, B, C and D in parallel, each against the headers with its own host suite green. A's work is
   device-only and is checked by the ESP-IDF build. A header change goes through E.
2. **Measure before setting the floor.** E derives `S`, `T` and `P` (§6a) from 0.3.38's recorded
   boots (the `mem: pre-speech`/`post-speech` console lines and the reported `int_free`/`int_min`
   history, through the debug API) and from the incident log. It sets
   `SPEECH_MIN_INTERNAL = S + T + P`, writing the arithmetic into §6a, below the pre-speech free.
3. E wires the executor, runs the end-to-end `jpanel_tests`, the ESP-IDF build, the size gate,
   and every package's gates (§12).
4. An adversarial review of the whole diff.
5. Hardware: after the owner merges, the first reports' `int_min` and `speech_ok`, read through
   the debug API, must clear the floor; a panel that does not is handled by a fixed image at a
   higher version.

**Deleted:**
- `ui.c`, `ui.h` and `host/ui_tests.c`;
- `display.c`'s duplicated interaction state;
- `jpanel.c`'s `s_state`, `s_run`, `s_stopped`, `s_fetching`, `s_in_*` and its own poll logic
  (into the core);
- `audio.c`'s `s_play`, ring and cue mixing (into `stream.c`);
- `host/iface_check.c`;
- the F2 characterisation tests (`test_f2_*`), which only guarded the wrapper layer.

### Status (2026-10-12, integration)

**Landed in 0.4.0 (FW 🟡, pending hardware):** A (link, socket off, deadlines), B (`stream.c`,
`audio.c` cut to I/O), C (`jpanel_core.c`), D (`panel_sm.c`; `ui.c`/`ui.h`/`ui_tests.c` deleted),
E (integration):

- `display.c` samples, executes the machine's effects in order (the stop word hoisted before its
  silence, the gen stamped on each PLAY, lease 1 before a SEND's post, lease 2 before
  `talk_send`, a refused capture or post reported back as the next frame's input) and draws
  `sm_view_t`; its duplicated state is deleted and `check_no_logic_in_display` gates it.
- `talk.c` plays through the reply slot with a changing id, waits for `reply_done` before reusing
  its buffer, and releases lease 2 when its upload returns.
- `main.c`: `jpanel_start(&cfg, stream)`; the report's `msg_*` from the engine's counters,
  `speech_ok`, and the flat `jp` object after `pmu_history`; both watchdogs (§7.5d) on a 1 s
  timer with the RTC cap; `ota.c`'s `ota_running`. `test_update_check_precedes_task_starts` pins
  the update-first order.
- `audio_compat.c`, the transitional block of `audio.h` and `host/iface_check.c` are deleted.
- `run_e2e_tests` drives the machine against the real engine and arbiter, including
  `test_selection_held_while_stop_outcome_pending`. Its first run found a seam bug the per-module
  suites could not: `stream_facts` read `stream_live` off the arbiter's last tick, so a short
  body landing whole between two ticks was classified heard before it sounded. It now reads the
  ring itself.
- `firmware.yml` gates static DIRAM (`.data` + `.bss`) at 0.3.39's 97,144 B + 1,024 B; 0.4.0
  measures 93,536 B.
- Backend: `TelemetryIn.jp` and `speech_ok`, `test_jp_keys_from_main_c_validate_against_telemetry_in`,
  the payload-fits test extended with a worst-case `jp`, and the census rows that read
  `display.c`/`ui.h` retargeted to `panel_sm.{c,h}`.

**Remaining:**

- **Hardware (FW → ✅):** read the boot log's `mem: pre-speech`/`post-speech` and a send's low-water
  through the debug API, record `S`, `T`, `P` in §6a and set the floor; confirm `speech_ok` and
  `int_min` on both panels.
- **F4:** the overlay, pips and contours replacing 0.4.0's placeholders (`SENDING`,
  `SENT`/`NOBODY`/`NOT SENT`/`DROPPED`; `CUE_NOBODY` is still sounded as `CUE_OOPS`), in the
  release after 0.4.1. The GUI gate's mocks are now the bench (below): each candidate is drawn by
  `overlay.c` and run in the browser, not drawn a second time in HTML.

**0.4.2, from the first 0.4.0 panel on hardware (2026-10-12).** It updated over the air and
reported `speech_ok`, every `jp` counter zero, and an internal low-water of 18,208 B with a request
in flight. Two faults the owner saw within the hour:

- *Messages paused when the room was noisy.* A word the recogniser thought it heard sounded its cue
  in the press slot, and a message stream yields the speaker to a cue. 0.3.39 had refused the cue.
  The machine now mutes commands in PLAY and while a stream is live
  (`test_commands_are_deaf_while_a_message_plays`).
- *REPLY only from a message that has stopped talking* (owner). In PLAY the envelope is drawn and
  pressable only while the message is paused; once it ends, the AGAIN/REPLY pair carries it as
  before. Mid-message the corner is empty and a press there does nothing (`view.reply`, checked
  under I3).
- *The animation was choppy.* With the socket off, every request was a fresh TLS handshake, and
  the 3 s settings poll made that one every 3 s. `link.c` now keeps one connection between
  requests, turned on only after speech has started (`firmware/README.md`).

**0.4.14, happy birthday and a game.** The song in beeps with a party hat and confetti, and a
tilt-to-dodge game with the pet shrunk to a fifth (`firmware/README.md`).

**0.4.13, tickled out.** The laughing fit the fifth tickle sets off leads into a fart, and the
fart into sitting dazed (`firmware/README.md`).

**0.4.12, a tickle that looks like a tickle.** Each tickle is its own squirm and "hehehe", not
the poke giggle, and he waits visibly braced while the game is on. And one fart in ten is the
long one: seven seconds of different rude noises back to back, with a cloud. And the poop is
one of eight, each its own shape, face, stink and flies (`firmware/README.md`).

**0.4.11, from the owner trying 0.4.10.** The meal is three bites of one of eight snacks, and the tickle's laugh is
cheap enough to render that the squirming no longer freezes at each one. Three meals in a
minute and he poops, and the poop slides with the panel's tilt for 20 s; and "I'm going to tickle
you" warns him, after which five pokes tickle him into the laughing fit (`firmware/README.md`).

**0.4.10, tickle tickle and a real meal.** A new laughing action from `tickle tickle` /
`tickle me`; eating becomes five bites of a cookie with a crunch on each; the dizzy and love
actions last twice as long; and a poke over the pet's own sounds reacts again rather than only
flinching (`firmware/README.md`).

**0.4.9, a ring round the play button.** It fills as the message plays, from the length the box
now sends as `X-Jpanel-Duration-Ms` on `/next` and on a replay (`firmware/README.md`).

**0.4.8, from the owner's photo of the head righting itself.** The eyes, their lids and the beak
now turn with the head instead of only moving to where it turned (`firmware/README.md`).

**0.4.7, two animations the owner asked for.** Shaking the panel makes the pet sit down dazed
with stars round his head, and `i love you` / `love you` brings hearts and a double coo
(`firmware/README.md`).

**0.4.6, from the owner holding the panel on its side.** The pet slides to the glass's edge
(the 368 square follows the lean into the 40 px bands either side) and its head rights itself
toward true up while the body leans, at 0.6 of the tilt (`firmware/README.md`). The bench has a
tilt slider.

**0.4.4, a cheaper frame.** On 0.4.3 Lydian's panel drew 13.5 frames a second at 68 ms of work each,
untouched. The idle redraw was every frame counting the state machine's per-frame
`MUTE_COMMANDS`, and the limbs were stamped a disc per pixel. Both are fixed (the limbs pixel for
pixel, checked on the host against the old loops), and the report's `note` splits draw from blit.

**0.4.3, the frame rate.** Lydian's panel ran 0.4.2 for three hours with 7 handshakes, the
kept connection working and the internal low-water unchanged at 18,208 B. A cold review of the
render path then found the loop pacing by a fixed sleep after the work (about 12 fps), the
talking mouth drawn at 5 fps by a dead `dirty`, and the build at -Og and 160 MHz. All three are
fixed, and the report's `note` now carries frame timings (`firmware/README.md`).

**Landed in 0.4.1 (no behaviour change): the bench.** The overlays moved from `display.c` into a
pure `overlay.c` (`overlay_draw(fb, view, ctx)`), so the host and the browser draw the panel's
own pixels. `firmware/mock/` compiles the machine, the engine, the arbiter, the cues, the pet and
the overlays to WebAssembly against the scripted box. `docs/mocks/jpanel-s3/` is the interactive
page, with the machine's hit test painted over the glass on request. `firmware.yml` rebuilds the
module, byte-compares the committed one, and runs `firmware/mock/smoke.mjs` (message, play,
AGAIN/REPLY, a sent reply, the who-menu).
- **W1–W3, P1** as §12 orders them; 0.4.0 already degrades to today's box as §6 says.

---

## Appendix A — every `ui_tests.c` test (51), re-verified assertion by assertion

**U** = ported to `sm_step` with the same assertions; only API names change, and every literal
assertion holds against the new machine. **R** = rewritten; the new assertion keeps the property
the test protects. No test is deleted.

| # | Test | Disp. | Note / new assertion (property protected) |
|---|---|---|---|
| 1 | `test_the_order_is_a_list_and_ends_with_the_pet` | R | Each mode's target table asserted literally; each ends in PET, a swallow, or LISTEN/RECORD's *ignored* miss (*every press reaches something or is logged*). |
| 2 | `test_the_grid_is_modal_and_outranks_everything` | R | In GRID, every point (quadrants and centre) resolves to a cell or swallow, with a notice waiting and an offer standing. A run cannot coexist with GRID, so that part becomes "a hold during PLAY is swallowed" (*the menu is modal*). |
| 3 | `test_the_notice_is_tappable_only_in_one_quadrant_today` | R | Geometry and the "standing offer does not swallow a notice press" checks as before (offer hidden, still kept). The last step drives a real press into PLAY and, after grace, the top right → EXIT (*the exit returns once a message actually runs*). |
| 4 | `test_a_cue_no_longer_decides_what_a_press_means` | R | With `speaker_busy` from a cue, PET/GRID targets equal the quiet ones, and PLAY's targets do not change while a cue sounds (*a cue does not take the table away*). The table-length literal is dropped. |
| 5 | `test_the_exit_ends_a_message_while_it_is_audible` | R | Press into PLAY, run past grace, top right → EXIT ends the run with STOP (*the corner is the exit while a message is audible*). The `UI_TAP_ORDER*` literals become PLAY-table literals. |
| 6 | `test_the_notice_is_painted_through_its_own_arrival_cue` | R | Notice drawn while its own chime sounds; then a **press** into PLAY hides it (*a playing message owns the screen*), instead of setting a bare `stream_active`. |
| 7 | `test_a_hold_after_a_poke_still_opens_the_menu` | R | Same assertions; the hold needs no quiet speaker. The port drives a real down edge on the pet (the harness set hold state directly), so `down.target == PET`. |
| 8 | `test_the_controls_are_pressable_for_as_long_as_they_are_drawn` | R | From the press: controls drawn, transport claimed (ignored) through pending/waiting/claimed-not-ON; a replay likewise (*drawn ⇔ pressable*). Bare `jfetching`/`stream_active` inputs are not used. |
| 9 | `test_a_second_press_cannot_cancel_what_the_first_one_started` | U | Exit grace from the press, every phase. |
| 10 | `test_the_face_follows_the_message_that_is_playing` | U | `cur` is seeded across the chain; plus the every-frame assertion. |
| 11 | `test_a_notice_does_not_show_through_the_playback_screen` | U | Press-driven; the gap stays in PLAY. |
| 12 | `test_a_swipe_changes_which_waiting_message_is_offered` | U | |
| 13 | `test_one_waiting_message_cannot_be_swiped_away` | U | Stops only if the selection moved. |
| 14 | `test_a_swipe_stops_the_message_it_moves_off` | R | `sel` now indexes the selection set `S` of §8.2 (`waiting.q` in box order with `cur` at `cur.pos`, de-duplicated), not a bare index into `waiting`. The swipe that "moves to the next one" asserts that the **selected id** is the entry of `S` after the playing one — the same id whether or not the snapshot still lists `cur.id`; `stop == 1` and `pause_off == 1` are kept, and "at the end stops nothing" is kept (*a swipe moves on and stops what it left*). |
| 15 | `test_a_swipe_is_not_also_a_hold` | R | The harness drove a tap and a swipe in one frame; the port drives one gesture, since a release that ends a swipe is not a tap (§8.2). Same assertions: no grid, the swipe acts (*a swipe is not also a hold*). |
| 16 | `test_a_long_press_on_a_menu_is_just_a_press` | U | Fresh offer: no grid. Standing case is a new test. |
| 17 | `test_the_selected_message_is_the_one_that_plays` | R | The posted command carries the selected **id** (index on an old box); `sm_face` before the claim = the sister (*the selected message plays, and its face shows from the press*). |
| 18 | `test_the_numeral_says_which_of_how_many` | U | The swipe during pending stops and returns to PET with sel moved; `sel_shown == 3` on the notice. |
| 19 | `test_a_new_message_sends_the_selection_home` | U | The harness sends no ids → the count-rise fallback; same results. |
| 20 | `test_every_playback_control_answers_the_finger` | R | Reply mid-message → `mode == RECORD` with `CUE_REPLY` (was `pending == UI_PEND_REPLY`); the other cue assertions stay (*every control answers with its own sound*). |
| 21 | `test_the_draw_layers_are_a_list` | R | Per-mode view composition; grid on top (*a menu is never covered*). |
| 22 | `test_a_notification_arrives_and_a_tap_plays_it` | U | Posted after the speaker is free. `play_next == 0` on the press frame holds by frame-boundary rule 1 (the PLAY entered this frame is not posted on it, §8.2); on the next frame, by render's `speaker_busy`, which counts the `CUE_PLAY` the arbiter has not yet picked up (§7.4). |
| 23 | `test_a_press_does_not_evaporate_behind_its_own_cue` | U | Larger `PENDING_MS`. |
| 24 | `test_a_press_during_the_arrival_cue_plays_the_message` | U | |
| 25 | `test_the_run_controls_appear_from_the_press_not_the_stream` | U | The pause glyph is drawn while ignored. |
| 26 | `test_the_top_right_corner_ends_a_message_while_it_plays` | R | Second half: after the exit, mode PET with **no offer**; one press (owner Q2). |
| 27 | `test_the_pair_waits_for_a_finger_not_a_clock` | R | `jpanel_clear >= 1` becomes "`play_outcome` heard consumed, offer set"; plus standing at 10 s, still drawn, and hidden while a message waits (*the pair waits for a finger*). |
| 28 | `test_reply_records_to_whoever_just_spoke` | U | The recipient comes from `sm_face` (the offer). U only because the port opens RECORD the way the harness does, without a touch edge; a touch-opened RECORD graces the tick (§8.2), which the new tests cover. |
| 29 | `test_sending_does_not_raise_the_playback_controls` | R | After SEND, `sending` with no transport or pair drawn; then a **press** on the notice is refused "busy", and a press after `SENT` raises the controls (*an outgoing message never shows playback controls*). |
| 30 | `test_a_short_button_press_darkens_blanks_and_mutes` | U | Effects: `SILENCE`, then `CUE_STOP`, `STOP`, standby, `BLANK`. Extended to every mode; no `CAPTURE_OPEN` after standby, including a follow-up; a send continues. Modes are entered without a touch edge, as in the harness. The audible order is proved in `stream_tests`. |
| 31 | `test_a_five_second_hold_requests_deep_sleep_exactly_once` | U | |
| 32 | `test_a_hold_on_the_pet_opens_the_grid_at_every_free_tap_count` | U | |
| 33 | `test_the_grid_reaches_all_four_people` | R | It taps each cell ~200 ms after the hold opened the grid; the cell centres lie well outside the 40 px of the hold point that the hold grace covers (§8.2), so every tap acts. Every cell still reaches its person, with `CUE_LISTEN` and the right `rec_to`. The pet cell's `!w.muted` pinned a harness-only rule (`w.muted = ui_recording \|\| standby`); the device mutes commands in LISTEN too (`display.c:3212`, Appendix B). Rewritten: `cmds_muted` is true in LISTEN and in RECORD (*a cell opens a conversation or a message; an open microphone is deaf to commands*). |
| 34 | `test_the_grid_closes_itself_but_a_stray_finger_does_not` | U | |
| 35 | `test_a_tick_pressed_into_silence_refuses_to_send` | U | `CUE_OOPS`. As #28: RECORD is opened without a touch edge, so the tick is not graced. |
| 36 | `test_the_cross_discards_and_the_pet_is_not_poked` | U | The top-half miss in LISTEN stays *ignored*: no flinch, `tap_missed` logged, and the turn survives (I4's exception; no recovery). |
| 37 | `test_a_named_turn_with_nobody_speaking_costs_nothing` | U | |
| 38 | `test_a_failed_turn_says_so_rather_than_going_quiet` | R | The harness sets `net = BUSY` in `perform()` after the step; the port keeps `CHECK(turn == THINKING)` on the entry frame because of frame-boundary rule 1 (§8.2), then the same failure, dash and OOPS assertions (*a failed turn says so*). |
| 39 | `test_every_dash_is_counted_and_its_two_causes_told_apart` | U | |
| 40 | `test_a_reply_arms_a_follow_up_and_the_cap_ends_it` | R | The harness modelled the reply as a stream; the port drives `reply_busy` and its falling edge. Same assertions on arming and the cap of 6 (*a reply arms a follow-up, and the cap ends it*). |
| 41 | `test_stop_said_out_loud_leaves_every_state` | U | Today's handler, including the RECORD drop. |
| 42 | `test_pressing_the_face_cycles_through_the_waiting_messages` | R | Face presses are spaced past the press grace of the notice press that started the playback (§8.2); then, as before, the face moves the selection → stop → PET (menu down) (*the face cycles*). |
| 43 | `test_the_face_is_not_a_control_with_one_message` | U | The assertion holds whichever way it is reached: the harness's fetch never lands, and its face press falls inside FACE's press grace, so it is ignored. On the counting side, the harness leaves `waiting = 1` before the claim (1 + 0) and 0 after (0 + 1). With one message playing **and** one waiting, the face is live — a deliberate rule change, covered by `test_face_cycles_without_skipping`. |
| 44 | `test_pressing_replay_twice_does_not_pause_what_never_started` | R | The literal `== UI_TARGET_TRANSPORT` becomes `== TARGET_IGNORED` while the replay is pending: the press is **claimed and not PET** (no poke, no hold). The second press still does not pause; once ON the same disc pauses (*a press cannot pause what never started*). |
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
- **cue**: the sweeps cover `CUE_BUSY`/`CUE_NOBODY`. New: `test_cue_busy_distinct`; and in
  `stream_tests`, `test_slot_seqlock_never_pairs_seq_with_other_samples`.

## Appendix B — owner decisions the machine touches

**K** = kept. **C** = changed, either by a §4 owner decision (★) or because the old behaviour is
the finding (†).

| Source | Decision | Disp. |
|---|---|---|
| JP Part B; `display.c` "pop-up box" | Notice when a message waits; no auto-play | K |
| `ui.h`/`display.c` "very large … 15 seconds … smaller one top left" | Big for 15 s, then a badge; a second arrival does not restore it | K (clock from first visible †; a returned message starts as a badge †) |
| `display.c` "capture everything in that top left quadrant" | The badge's hit area is the whole quadrant | K |
| JP sleep rules; dark wake | Big box after dark; waiting does not hold the screen awake | K |
| JP "multiple messages are one press, and a finger gets out" | One tap plays all; a finger stops it | K (a mid-run arrival joins while another waiting message remains) |
| JP 0.3.33 "the count became a position" | `n/m` and swipe | K (counts the playing message †) |
| `ui.h`/`display.c` "replay button … 10 seconds"; 0.3.25 "no more waiting for it to time out" | The pair never expires | K; **standing after 10 s ★** |
| `confirm.h` "the pair never moves between the two states" | Disc positions fixed | K (standing changes the hit area, not the drawing) |
| 0.3.25 "the face stays for the whole exchange" | Sender's face with the pair | K |
| 0.3.25 "the new message takes the corner" | A waiting message takes the corner | **C †** (B3): the pair now hides while anything waits, which today it did not (§13) |
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
| JP 0.3.36 "click the play button sometimes it just pauses" | Transport ignored while the press is pending | **C †** (A1): ignored until ON, which also covers the fetch and preroll (§4) — still bounded by the state, never by a clock (`ui.c:482-485`); PAUSE and REPLY are exempt from the press grace of §8.2 |
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
| `ui.h` `PENDING_MS` comment — a pending press that expires does so silently | Silent pending expiry | **C †** (E2.10): `CUE_OOPS` (§4) |
| `display.c` 0.3.26 "single press blinks the screen"; "hold … 5 seconds" | Button = standby / deep sleep, from any mode | K (a send continues) |
| `display.c` "frozen robot" | Dark is painted | K |
| JP "Dim is a number"; "voice does not hold the screen awake"; "the pet answering still holds it" | Sleep `used` terms | K |
| `display.c` "long press seems to just reset the device" | Release-based button | K |
| JP PWA decisions (male voice, PWA audio, one child one conversation, two clears, clear history, picker) | PWA/box | K |
| `jpanel.c` ack-on-stop | A stop acknowledges | C † (F3, with a delivery header only); a REPLY stop still acknowledges (§4) |
| `display.c` exit corner: `jpanel_stop()` → `audio_stop()`, then `CUE_STOP` | A stop cuts whatever is sounding, then sounds its own cue | K for EXIT, FACE and swipe: `SILENCE` then the cue (§8.3). The button's cue was cut today and now sounds (the "IT BLINKS FIRST" row, †). A pending `send_result` now survives (†, E1.2) |
| — | Refusal sound — including a refused wake phrase or "tell sister", which today keeps `CUE_HEARD` | **C ★** (`CUE_BUSY`) |
| `confirm.c` *"split the [screen] into quarters … the children's press accuracy is not great"* | Disc controls hit by quadrants | K for playback, recording and a fresh pair. **C ★** for a standing pair: AGAIN and REPLY by disc reach only, the exit keeps its quadrant (Q1) |
| `ui.h`/`display.c` `TALK_MARGIN_PX` rim guard (a hold that starts on the rim is not a hold) | Rim guard | K |
| `display.c` 0.3.26 *"IT BLINKS FIRST"* — the stop cue sounds before the dark | Standby cue | K. Made true in F3: `SILENCE` then `CUE_STOP`. On 0.3.39 (and in F2) `audio_stop()` cuts it in the same frame (†) |
| 0.3.32 "every playback control answers the finger" — the reply press | `CUE_REPLY` at the reply press | K. Made true in F3: on 0.3.39 (and in F2) `jpanel_stop()` cuts it in the same frame (†) |
| `display.c` wake from standby | A press in standby wakes with `CUE_HEARD` | K |
| `display.c` order: touch dispatch (~2480) runs before the button (~3240) | A touch and a button press in one frame both act | **C †**: the button is read first, and a frame that enters standby drops its touch and voice edges, so a queued press cannot start a PLAY in the dark (§8.2) |
| `display.c` standby path (resets `s_talk` to idle) | A pet reply still in flight when the button is pressed plays when it arrives, and nothing follows it | K: `turn.state = NONE` with no `TALK_CLEAR`, `follow_turns = MAX` (§8.3). A reply already sounding is cut, as `audio_stop()` does today |
| — | The new mode's targets right after the press that opened it (tick and cross after REPLY or a grid cell; the notice, AGAIN and REPLY after a press back to PET; FACE after the notice; grid cells near a hold's own point) | **C †**: ignored for `EXIT_GRACE_MS`, so a bounce cannot send, drop, replay or skip anything (§8.2). PLAY's PAUSE and REPLY and every swipe are exempt |
| `display.c` hush/cap auto-send | No cue on auto-send; `CUE_SENT` follows | K |
| `ui.c` tap order (pair live during THINK) | AGAIN works mid-turn, and the turn still resolves (dash, follow-up). REPLY mid-turn was refused (`start_record` needs talk idle) with OOPS. | K; REPLY's refusal now sounds `CUE_BUSY` (★). The pair is not live in LISTEN or RECORD (B1 †). **C †:** a follow-up whose reply ends while she is in PLAY is disarmed, not opened (§8.3). |
| JP *"unplayed messages are kept indefinitely"*; 0209 *"DELETE /messages deliberately preserves exactly that row"* | Unheard rows survive a clear | K, except: keyless rows (no future re-address can deliver them, E3.3) are deleted by a clear (**C †**) — waiting-for-re-flash rows are kept; and the owner can discard any unplayed row one at a time, an explicit act (**C †**). Both written into JP Part B "### Data" by W2 (§11.1) |
| JP 0.3.36 replay re-arms the pair | The pair after a replay | K (`offer.since` re-stamped) |
