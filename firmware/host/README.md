# The firmware's host suite

Two binaries, both run by `make -C firmware/host test`. Nothing here needs hardware, an
ESP-IDF toolchain, or a network.

| binary | source | what it is for |
|---|---|---|
| `run_tests` | `tests.c` | **Pure functions.** Given these numbers, what comes out: cue contours, face geometry, the rig's cooldowns, calibration fits, the tick-and-cross hit tests, and the panel socket's framing, credit window and fallback (`main/wsproto.c`). Most of it swept over every input. |
| `run_sm_tests` | `sm_tests.c` | **The panel's state machine** (`main/panel_sm.c`, plan §8 of `docs/plans/JPANEL_MESSAGING_FIXES_PLAN.md`), driven one frame at a time against a fake engine, speaker, microphone and talk: every `ui_tests.c` scenario it replaced (plan Appendix A), the journeys of §8.5 and §9, and `test_storm`. |

They are separate because they need different things. The first wants to be a tight loop over
inputs; the second needs a world — cues that hold the speaker for a while, commands that are
claimed and begin a frame or two after they are posted, a stream whose `{stream_id, out_ms}`
outlive it.

`make test` runs them in sequence, not in parallel: a suite whose failure output interleaves with
another's is a suite nobody reads.

## Why the machine is a module

`display.c` used to hold the whole interface inside `face_task()`, an infinite loop around
ESP-IDF, so the only integration test was a four-year-old in a bedroom. `ui.c` extracted the
arbitration but `display.c` kept a hand-kept duplicate of the rest, and the two drifted (review
finding B0). `panel_sm.c` is the one machine: `sm_step(sm, &in, &out)` takes a frame's facts and
returns effects in order plus a view to draw. `display.c` samples, executes and draws — nothing
else — and every rule about what a finger means lives where this suite can step it.

## Adding a scenario

A scenario is one `static void test_...(void)` in `sm_tests.c`; `main()` lists them in order.

```c
static void test_the_thing_that_matters(void)
{
    world_reset();              /* a fresh machine and world, the clock at 1000 */
    arrive("m1", true);         /* the box describes a message from Dad, and a frame runs */
    run_ms(400);                /* the chime finishes */
    tap_the_notice();           /* a press that is over within the frame */
    CHECK(w.sm.mode == SM_PLAY, "the press was taken");
    CHECK(plays() == 0, "and waits a frame and its own cue");
    run_ms(400);
    CHECK(plays() == 1, "then the command is posted");
}
```

- **`step()`** is one pass: sample `sm_in_t` from the world, `sm_step`, assert no invariant broke
  (`out.violations` and `sm_check`) and that the drawn face is `sm_face`, run the effects against
  the fakes, advance the clock by 80 ms, let the fake engine move. Read it before writing a test.
- **The finger:** `tap_at(x, y)` (down and up in one frame), `press_at(x, y)` / `lift()` for a
  hold, `swipe(dir)`. Coordinates are OVERLAY coordinates on an upright portrait panel.
- **The box and engine:** `queue_is`, `box_add`/`box_remove`, `old_box` (no ids); the engine claims
  and begins on its own (`claim_ms`, `fetch_ms`), `engine_end(kind)` ends what plays, `chain`
  continues a run, `defer_stop` + `release_stop(kind)` hold a stop's outcome back.
- **The rest of the world:** `w.reply`, `w.cue_until`, `w.hearing`, `w.net`, `w.leased`, `say()`
  for a voice command, `w.btn_short`. Every effect is counted (`stop`, `opened`, `dropped`,
  `posts[]`, `cue[]` with `saw_cue`/`last_cue`/`count_cue`).
- **`target_at(x, y)`** asks `sm_target` what a press there would reach, with no side effects.

Conventions:

- **One behaviour per test, named as a sentence.** The name is what a failure prints first.
- **Every `CHECK` message says what is true, not what was compared.**
- **Include the negative control** — that the thing does happen when it should — so a test
  cannot pass by the feature never appearing.
- **Drive state through presses and the fakes**, not by writing `w.sm` fields; the invariants are
  checked after every step, and a hand-set state is a state the machine might never reach.

## The storm

`test_storm` runs 300k seeded frames of random presses, holds, swipes, bounces, voice, button and
calibration against the fake engine, with snapshots dropped, delayed up to 2 s and duplicated,
clock jumps and the u32 wrap, an engine that overruns its published deadlines by a link step, and
long recordings with arrivals. Every frame asserts I1–I7, the drawn face, and that no UI backstop
fires. It also checks that it actually reached every mode and played, sent, stopped, refused,
chimed, paused and slept — a storm that never reached a state proves nothing about it.
