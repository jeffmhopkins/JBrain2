# The firmware's host suite

Two binaries, both run by `make -C firmware/host test`. Nothing here needs hardware, an
ESP-IDF toolchain, or a network.

| binary | source | what it is for |
|---|---|---|
| `run_tests` | `tests.c` | **Pure functions.** Given these numbers, what comes out: cue contours, face geometry, the rig's cooldowns, calibration fits, the tick-and-cross hit tests, and the panel socket's framing, credit window and fallback (`main/wsproto.c`). ~4.1M checks, most of them swept over every input. |
| `run_ui_tests` | `ui_tests.c` | **The interaction state machine** (`main/ui.c`), driven one frame at a time: a press, a clock advanced, an overlay arbitrated, a peripheral call recorded. ~440 checks. |

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
  all, so the order can be asserted without driving its consequences. **`display.c` calls it**
  (`tap_target_now`) rather than keeping a second opinion: its branches ask only whether they were
  chosen, so a fault found here is a fault fixed there, once.
- **`dt_ms` is measured, never assumed.** A 40 ms delay followed by a frame's work is not a 40 ms
  frame; a real pass is 80–100 ms. `display.c` used to hand `gesture_poll` the nominal constant,
  which stretched every gesture threshold by 2–2.5x — the "five second" reboot hold was really
  twelve. It measures now, from `esp_timer`; this side of the boundary never had the choice.

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

## What the extraction found

The extraction was a move, not a fix — a refactor that also changes behaviour is a refactor nobody
can check — so the faults it exposed came across intact, pinned by tests named `..._today` that
asserted the wrong behaviour on purpose. All six are fixed, and each `..._today` test was replaced
by one asserting the behaviour that was wanted. The list is kept because it is the argument for the
module: none of these was found by a child, and none of them was visible in a chain of `if`s.

1. **A press landing while *any* sound came out of the panel was discarded.** The dispatch forked
   on `speaking` — `audio_playing()`, true for the panel's own cues, including the ~440 ms
   notification beep, which is the exact half-second a child looks up and reaches for the notice.
   It forks on a *message* sounding now. Only the poke branch still defers to any sound.

2. **The top-right exit could not end a message while the message was audible** — the one state
   the owner asked for it in. It was in the idle table only, and an audible message routes the
   press to the playing table. It is in both, and first in the playing one, because
   `target_hit(TRANSPORT)` is unconditional.

3. **The big notice's picture and its tap target were different rectangles.** `draw_popup` painted
   `x[36,332) y[117,331)`; the armed rectangle was the top-left quadrant, `x[0,184) y[0,224)`.
   `ui_popup_target` is the one answer, used by both.

4. **The notice was unpainted for the length of every cue while its hit rectangle stayed armed.** A
   control that blinks off while it still works is the same fault as one that works only while it is
   drawn, wearing the other face. One `notice_up` now decides the picture and the target together.

5. **`draw_popup_badge()` wrote `s_popup_box` itself** — a second owner of the hit rectangle,
   inside a drawing function, and `draw_popup` a third with a different shape. Both writes are
   gone; the arming site is the only writer.

6. **`ui_busy()` excludes the again/reply pair, and the idle timer must keep it that way.** The
   pair stopped expiring, and while it was in the idle test the panel counted itself permanently
   in use and could never dim again after the first message. A pair waiting patiently for a child
   is precisely the case where the screen *should* be allowed to sleep around it.

Plus two the tests found that no release had reported: the transport was drawn through the fetch
window while its handler could not act on it (a press there poked the pet), and the modal menu was
missing from the playing table, so a message starting under an open menu took the menu's presses.

## The sender's face, three times

Worth its own section because it is the clearest lesson the module has taught: **two of the three
fixes corrected a reading of a value that was itself wrong, and both were provably right about the
case they were written for.**

`jpanel_in_from()` is read off a fetch's own response header, and `do_fetch` used to CLEAR it before
the request — so through the whole window before those headers landed it said "the sister", because
that is what cleared means. 0.3.28 made the press-to-play window ask the queue instead; 0.3.31
narrowed that to "the queue only while nothing is sounding yet", after the owner watched a little
girl for the length of a message from Dad. Both are correct with one message queued. With two, the
panel finishes the first, chains into the second with `jpanel_running()` still true, and the clear
lands in plain sight — and the owner reported the same symptom a third time.

The third fix seeds the value from the queue entry the fetch is about to take, so the header only
confirms it, and the overlay reads one fact. `test_the_face_follows_the_message_that_is_playing`
drives the chain rather than a single message, and the harness's fake box seeds `in_from` at the
press exactly as `jpanel_play_at` does — **which is the part that matters**: a test whose fake is
more forgiving than the firmware is a test that will pass through this bug a fourth time.

## Still inside `display.c`

Named so the next slice is obvious, not as an apology:

- **`boot_button_poll()`** — the debounce and the press/hold edges. `ui_frame` takes `btn_short`
  and `btn_hold` as facts, so `test_a_five_second_hold_requests_deep_sleep_exactly_once` proves
  the *consequence* of a hold and not the five seconds. Extracting this makes the timing testable
  too, and it is the smallest remaining piece.
- **`gesture_poll`'s caller** — the measured `dt` is `display.c`'s to get right, and the only thing
  that proves it does is reading the call.
- **The action bodies.** `ui_tap_target` decides *which* control a press reached; starting a
  microphone, holding a ring, posting a reply and ending a run are still inline in `face_task`.
  The cutover — `ui_frame` and `ui_overlay` driving a pass, with an executor over `ui_action_t` —
  is the next slice, and worth doing alone so a regression in it is not mistaken for one in a
  behaviour fix.
- The sleep/idle timer, the calibration routine, orientation, the face animation and emotion
  tweening, and every blit. Those are either hardware or drawing, and they belong where they are.
