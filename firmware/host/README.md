# The firmware's host suite

Two binaries, both run by `make -C firmware/host test`. Nothing here needs hardware, an
ESP-IDF toolchain, or a network.

| binary | source | what it is for |
|---|---|---|
| `run_tests` | `tests.c` | **Pure functions.** Given these numbers, what comes out: cue contours, face geometry, the rig's cooldowns, calibration fits, the tick-and-cross hit tests. ~4.1M checks, most of them swept over every input. |
| `run_ui_tests` | `ui_tests.c` | **The interaction state machine** (`main/ui.c`), driven one frame at a time: a press, a clock advanced, an overlay arbitrated, a peripheral call recorded. |

They are separate because they need different things. The first wants to be a tight loop over
inputs; the second needs a world — a fake speaker whose cues take time, a fake message queue
whose fetches do not land on the frame they were asked for, a framebuffer to look at. Putting the
harness in `tests.c` would slow four million checks down to buy nothing.

`make test` runs them in sequence, not in parallel: a suite whose failure output interleaves with
another's is a suite nobody reads.

## Why `ui.c` exists at all

`main/display.c` held the entire user interface inside `face_task()`, an infinite `while (true)`
wrapped around ESP-IDF, a QSPI bus and a 322 KB framebuffer. It could not be built off-device,
let alone stepped — so the only integration test this firmware ever had was a four-year-old in a
bedroom, and six user-facing faults shipped in one evening.

`main/ui.h` is the arbitration half of that loop extracted into a module that takes a struct and
returns a struct: **which overlay is up, and what a finger at (x, y) means given everything else
that is happening.** It touches no hardware, draws nothing and logs nothing. `display.c` still
owns every pixel, every peripheral and every `esp_` call.

Two shapes in it are deliberate and load-bearing:

- **The tap order is a table** (`UI_TAP_ORDER`), not a chain of `if`s. Every arbitration bug found
  so far was a target sitting in the wrong place in that chain, and a chain does not let you read
  its own order. `ui_tap_target()` answers "what does this press reach" with no side effects at
  all, so the order can be asserted without driving its consequences.
- **`dt_ms` is measured, never assumed.** A 40 ms delay followed by a frame's work is not a 40 ms
  frame; a real pass is 80–100 ms. `display.c` still hands `gesture_poll` the nominal constant,
  which stretches every gesture threshold by 2–2.5x — the "five second" reboot hold is really
  twelve. Nothing on this side of the boundary may inherit that.

## Adding a scenario

Everything is in `ui_tests.c`. A scenario is one `static void test_...(void)` plus one line in
`main()`.

```c
static void test_the_thing_that_matters(void)
{
    world_reset();          /* a fresh state, a fresh world, a black framebuffer */
    w.in.waiting = 1;       /* set the frame's facts directly */
    step();                 /* run exactly one pass of the render loop */

    tap_at(60, 100);        /* a press that has already ended, in OVERLAY coordinates */
    step();

    CHECK(w.st.pending == UI_PEND_PLAY, "the press was taken");
    CHECK(w.play_next == 0, "and deferred behind its own cue");
    run_ms(400);            /* advance the clock, a frame at a time */
    CHECK(w.play_next == 1, "then it plays");
}
```

The pieces:

- **`step()`** is one pass, in the order `display.c` runs it: sample the speaker → `ui_tap` →
  perform what came back → derive the mute → `ui_frame` → perform what came back → (unless the
  screen is dark) `ui_overlay` and a draw → advance the clock by `dt_ms`. Read it before writing a
  test; the ordering inside it is the thing most likely to make an assertion look wrong.
- **`run_ms(n)`** steps until the clock has moved `n` ms. Nothing sleeps.
- **`tap_at(x, y)`** is a press that has ended; **`press_down(x, y)`** leaves the finger on the
  glass, which is what a hold needs. Clear it with `w.in.down = false`.
- **`w.in`** is the frame's facts. Set anything on it directly — that is the whole point of the
  boundary. `w.st` is the module's own state; prefer driving it through presses, but setting a
  field to reach an awkward corner is fair.
- **`w.*` counters** are every call the module asked for: `play_next`, `stop`, `drop`, `opened`,
  `send_jpanel`, `blank_n`, `power_off_n`, and `cue[]` with `saw_cue()` / `last_cue()`.
- **`w.fb`** is a real framebuffer. `draw_overlay()` renders the layers the pure tier can actually
  draw (the tick and cross, the transport pair, the exit, the grid) using `confirm.c` itself, so
  `drawn_near(cx, cy, r)` answers *is there really a control under this finger* — which is the
  question a hit test has to agree with. The pop-up, the badge, the run bar and the sender's face
  are `display.c`'s pixels; for those, assert the overlay the module chose and the hit rectangle
  it armed.
- **Helpers for the long paths**: `play_a_message(from_dad)` takes a notice all the way through to
  a sounding message, `land_fetch()` makes a requested fetch arrive, `finish_message()` ends one.

Conventions worth keeping:

- **One behaviour per test, named as a sentence.** The name is what a failure prints first.
- **Every `CHECK` message says what is true, not what was compared.** `"reply records to whoever
  just spoke"`, not `"rec_to == 1"`.
- **Include the negative control.** `test_sending_does_not_raise_the_playback_controls` also
  asserts that a real *fetch* does raise them — otherwise the test would pass just as happily if
  the overlay never appeared at all.
- **A test that pins known-wrong behaviour is named `..._today`** and carries the reason. See
  below.

## Behaviour preserved on purpose, and believed wrong

The extraction was a move, not a fix: a refactor that also changes behaviour is a refactor nobody
can check. These came across intact and are pinned by tests so that fixing them is a visible,
deliberate diff rather than a silent drift.

1. **A press landing while *any* sound comes out of the panel is discarded.**
   `test_a_press_during_a_cue_is_discarded_today`. The whole dispatch is chosen by `speaking`,
   which is `audio_playing()` — true for the panel's own cues, including the ~440 ms notification
   beep. That is the exact half-second in which a child looks up and reaches for the notice.
   *Shape of the fix:* the short table should be reserved for "a message is audibly playing"
   (`stream_active`), not for "the panel is making a noise".

2. **The top-right exit cannot end a message while the message is audible** — which is the state
   the owner asked for it in.
   `test_the_top_right_corner_ends_a_message_only_once_it_is_silent_today`. Same root cause as 1:
   a playing message means `speaking`, so the press goes down the short table, where the top-right
   corner is neither transport half and does nothing but flinch. The exit is *drawn* the whole
   time. It only works once the message has ended and the pair is standing.

3. **The big notice's picture and its tap target are different rectangles.**
   `test_the_notice_is_tappable_only_in_one_quadrant_today`. `draw_popup` paints x[36,332)
   y[117,331); the armed rectangle is the top-left quadrant, x[0,184) y[0,224). Most of what a
   child can see is not pressable. 0.3.28 stopped the right half being *swallowed* by the exit
   corner; a press there now pokes the pet instead, which is better but still not the notice.
   *Shape of the fix:* arm the rectangle from the same geometry the painting uses, or make the
   whole band the target while the big notice is up.

4. **The notice is unpainted for the length of every cue, while its hit rectangle stays armed.**
   `test_the_notice_is_unpainted_while_any_cue_sounds_today`. Moving the rectangle out from behind
   the idle guard was right and is only half the job — the *painting* is still behind it, and the
   guard includes `!speaking`. A control that blinks off while it still works is the same fault as
   one that works only while it is drawn, wearing the other face.

5. **`draw_popup_badge()` writes `s_popup_box` itself** — a second owner of the hit rectangle,
   inside a drawing function. Harmless today because it writes the same quadrant `display.c` has
   already armed. **It must be deleted in the wiring step:** once `ui_overlay()` owns the
   rectangle, that write lands on a variable nothing reads, and the two owners can disagree
   without anything failing.

6. **`ui_busy()` excludes the again/reply pair, and the idle timer must keep it that way.** The
   pair stopped expiring, and while it was in the idle test the panel counted itself permanently
   in use and could never dim again after the first message. A pair waiting patiently for a child
   is precisely the case where the screen *should* be allowed to sleep around it.

## Still inside `display.c`

Named so the next slice is obvious, not as an apology:

- **`boot_button_poll()`** — the debounce and the press/hold edges. `ui_frame` takes `btn_short`
  and `btn_hold` as facts, so `test_a_five_second_hold_requests_deep_sleep_exactly_once` proves
  the *consequence* of a hold and not the five seconds. Extracting this makes the timing testable
  too, and it is the smallest remaining piece.
- **`gesture_poll`'s caller** — including the nominal-`dt` fault above.
- The sleep/idle timer, the calibration routine, orientation, the face animation and emotion
  tweening, and every blit. Those are either hardware or drawing, and they belong where they are.
