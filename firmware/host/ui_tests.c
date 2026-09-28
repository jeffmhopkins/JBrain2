/* THE PANEL'S INTERFACE, DRIVEN ON A HOST — one frame at a time, with no panel.
 *
 * `main/display.c` held the whole interaction state machine inside an infinite `while(true)`
 * wrapped around ESP-IDF, so the only integration test this firmware ever had was a
 * four-year-old in a bedroom. Six user-facing faults shipped in one evening. `main/ui.h` is that
 * logic extracted into a struct-in, struct-out module; this file is the harness that drives it.
 *
 * WHAT A "FRAME" IS HERE. `step()` runs exactly one pass of the render loop in the order
 * `display.c` runs it: sample the speaker, `ui_tap`, perform what came back, derive the mute,
 * `ui_frame`, perform what came back, then — only if the screen is not dark — `ui_overlay` and a
 * draw. Time advances by `dt_ms` at the end, where the real loop's delay is. Nothing sleeps.
 *
 * WHAT THE WORLD MODELS. The fakes on the other side of the boundary are deliberately thin but
 * not trivial, because the bugs live in the timing: the speaker stays busy for the length of a
 * cue (which is what makes a deferred press a deferred press), a fetch does not land on the
 * frame it was asked for, and a stream is a separate fact from the queue. Everything a caller
 * would have done to a peripheral is recorded instead.
 *
 * SOME TESTS BELOW ASSERT BEHAVIOUR THAT IS WRONG. They are named `..._today` and each one
 * carries the reason. The extraction was a move, not a fix — a refactor that also changed
 * behaviour would have been a refactor nobody could check — so the faults came across intact and
 * are pinned here so that fixing them is a visible, deliberate diff. `README.md` lists them.
 */

#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "confirm.h"
#include "cue.h"
#include "face.h"
#include "gesture.h"
#include "ui.h"

static int checks;
#define CHECK(c, msg)                                                       \
    do {                                                                    \
        checks++;                                                           \
        if (!(c)) {                                                         \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, (msg)); \
            exit(1);                                                        \
        }                                                                   \
    } while (0)

/* ── THE WORLD ────────────────────────────────────────────────────────────────────────────
 *
 * Everything `display.c` owns: the peripherals, and the panel state that did not move.
 */

/* How long the fake speaker is busy for a cue. The real ones run 55-440 ms (`cue.c`); the
   deferred-press machinery only cares that it is non-zero and outlasts one frame. */
#define FAKE_CUE_MS 120

typedef struct {
    ui_state_t st;
    ui_in_t in;
    ui_out_t tap_out;   /* what `ui_tap` asked for */
    ui_out_t frame_out; /* what `ui_frame` asked for */
    ui_overlay_t ov;
    uint16_t *fb;

    /* the fake speaker */
    uint32_t busy_until; /* a cue is sounding until here */
    bool stream;         /* a message is coming out of the speaker */
    bool paused;
    bool fetching; /* asked for, not yet arrived */

    /* the fake microphone */
    bool capture_open;

    /* every call the module asked for */
    int cue[128];
    int cues;
    int play_next, replay, stop, drop, opened, talk_clear, jpanel_clear, poll_soon;
    int send_talk, send_jpanel;
    ui_to_t send_jpanel_to;
    int standby_n, wake_n, blank_n, power_off_n, pause_on, pause_off;
    int blits;

    /* the panel state that stayed in `display.c` */
    bool standby;
    bool dark;
    bool muted;
    bool talk_send_ok; /* what `talk_send()` will answer */
} world_t;

static world_t w;

static bool audio_playing(void)
{
    return w.in.now < w.busy_until || w.stream;
}

static int last_cue(void)
{
    return w.cues > 0 ? w.cue[w.cues - 1] : -1;
}

static bool saw_cue(int c)
{
    for (int i = 0; i < w.cues; i++) {
        if (w.cue[i] == c) return true;
    }
    return false;
}

static void world_reset(void)
{
    uint16_t *fb = w.fb;
    memset(&w, 0, sizeof(w));
    w.fb = fb;
    memset(w.fb, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
    ui_reset(&w.st);
    /* Portrait, upright: the band is the whole frame. The quarter-turn geometry is
       `confirm.c`'s to test and `tests.c` already does. */
    w.in.over_y0 = 0;
    w.in.over_h = FACE_H;
    /* MEASURED, and deliberately not the nominal 40: a real render pass is 80-100 ms. Every
       threshold this harness crosses is crossed at the rate the panel actually runs at. */
    w.in.dt_ms = 80;
    w.in.now = 1000; /* not 0 — `now == 0` is the never-been-active value in several places */
    w.in.capture_cap_ms = 10000;
    w.in.net = UI_NET_IDLE;
    w.in.jstate = UI_JP_IDLE;
    w.in.in_from = UI_TO_PANEL;
    w.talk_send_ok = true;
}

static void perform(ui_out_t *out)
{
    for (int i = 0; i < out->n; i++) {
        const int arg = out->act[i].arg;
        switch (out->act[i].kind) {
        case UI_ACT_CUE:
            if (w.cues < (int)(sizeof(w.cue) / sizeof(w.cue[0]))) w.cue[w.cues++] = arg;
            /* The speaker is busy for as long as the noise lasts, which is the whole reason a
               press has to be able to outlive its own acknowledgement. */
            w.busy_until = w.in.now + FAKE_CUE_MS;
            break;
        case UI_ACT_PLAY_NEXT:
            w.play_next++;
            /* A FETCH, NOT A SOUND. It does not land on this frame — `jpanel.c` is an HTTPS
               round trip away — and the gap is exactly where the run controls have to appear
               from the press rather than from the stream. */
            w.fetching = true;
            break;
        case UI_ACT_REPLAY:
            w.replay++;
            w.fetching = true;
            break;
        case UI_ACT_STOP:
            w.stop++;
            w.stream = false;
            w.fetching = false;
            w.paused = false;
            w.in.jrunning = false;
            break;
        case UI_ACT_PAUSE:
            if (arg) {
                w.pause_on++;
                w.paused = true;
            } else {
                w.pause_off++;
                w.paused = false;
            }
            break;
        case UI_ACT_CAPTURE_OPEN:
            w.opened++;
            w.capture_open = true;
            w.in.capture_ms = 0;
            break;
        case UI_ACT_CAPTURE_DROP:
            w.drop++;
            w.capture_open = false;
            break;
        case UI_ACT_SEND_TALK:
            w.send_talk++;
            w.capture_open = false;
            /* The one answer the module cannot work out for itself. */
            ui_talk_send_result(&w.st, w.talk_send_ok, w.in.now);
            if (w.talk_send_ok) w.in.net = UI_NET_BUSY;
            break;
        case UI_ACT_SEND_JPANEL:
            w.send_jpanel++;
            w.send_jpanel_to = (ui_to_t)arg;
            w.capture_open = false;
            w.in.jstate = UI_JP_BUSY;
            break;
        case UI_ACT_TALK_CLEAR:
            w.talk_clear++;
            w.in.net = UI_NET_IDLE;
            break;
        case UI_ACT_JPANEL_CLEAR:
            w.jpanel_clear++;
            w.in.jstate = UI_JP_IDLE;
            break;
        case UI_ACT_POLL_SOON:
            w.poll_soon++;
            break;
        case UI_ACT_ENTER_STANDBY:
            w.standby_n++;
            w.standby = true;
            w.dark = true;
            break;
        case UI_ACT_WAKE:
            w.wake_n++;
            w.standby = false;
            w.dark = false;
            break;
        case UI_ACT_BLANK:
            /* DARK HAS TO BE PAINTED, NOT SWITCHED OFF: `0x51` is inert on these panels and an
               AMOLED holds its last frame, so "dark" without this is a frozen robot. */
            w.blank_n++;
            memset(w.fb, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
            w.blits++;
            break;
        case UI_ACT_POWER_OFF:
            w.power_off_n++;
            break;
        }
    }
}

/* Only what the pure tier can actually draw. The pop-up, the badge, the run bar and the
   sender's face are `display.c`'s pixels and stay there; what is asserted for those is the
   overlay the module chose and the hit rectangle it armed. */
static void draw_overlay(void)
{
    memset(w.fb, 0, (size_t)FACE_W * FACE_H * sizeof(uint16_t));
    if (w.ov.confirm) confirm_draw(w.fb, FACE_W, FACE_H, w.in.over_h);
    if (w.ov.pair) confirm_draw_transport(w.fb, FACE_W, FACE_H, w.in.over_h, w.ov.pair_playing);
    if (w.ov.run) confirm_draw_transport(w.fb, FACE_W, FACE_H, w.in.over_h, w.ov.run_playing);
    if (w.ov.exit_corner) confirm_draw_exit(w.fb, FACE_W, FACE_H, w.in.over_y0, w.in.over_h);
    if (w.ov.grid) sendto_draw(w.fb, FACE_W, FACE_H, w.in.over_h);
    w.blits++;
}

static void step(void)
{
    /* One clock read per frame, shared by every branch — and `speaking` sampled here, at the
       top, because a tap LATER in the same frame is what starts a message. */
    w.in.speaking = audio_playing();
    w.in.stream_active = w.stream;
    w.in.stream_paused = w.paused;
    w.in.jfetching = w.fetching;
    w.in.standby = w.standby;

    memset(&w.tap_out, 0, sizeof(w.tap_out));
    ui_tap(&w.st, &w.in, &w.tap_out);
    perform(&w.tap_out);

    /* `display.c`: deaf while recording (a message must not also be a command) and deaf in
       standby (the child asked it to stop listening). Derived per frame, never armed at the
       edges, so neither state can leave the microphone muted after it ends. */
    w.muted = ui_recording(&w.st) || w.standby;

    if (w.capture_open) w.in.capture_ms += w.in.dt_ms;

    w.in.audio_playing = audio_playing();
    memset(&w.frame_out, 0, sizeof(w.frame_out));
    ui_frame(&w.st, &w.in, &w.frame_out);
    perform(&w.frame_out);

    /* THE DRAW GATE. A dark screen composes nothing: brightness 0 already hides the picture and
       shipping 322 KB to a screen nobody can see is the part worth not doing all night. */
    if (!w.dark) {
        w.in.audio_playing = audio_playing();
        ui_overlay(&w.st, &w.in, &w.ov);
        draw_overlay();
    }

    /* AT THE END, NOT BETWEEN THE TWO CALLS. `tapped` is one local in `face_task` and it stays
       true for the whole pass: the hold block reads it to learn whether THIS press carried a
       point, and `gesture_poll` counts it. Clearing it early made a hold look like a finger that
       had appeared out of nowhere, with `down_x` at -1 and therefore never on the pet. */
    w.in.tapped = false;
    w.in.btn_short = false;
    w.in.btn_hold = false;

    w.in.now += (uint32_t)w.in.dt_ms;
}

static void run_ms(uint32_t ms)
{
    const uint32_t until = w.in.now + ms;
    while (w.in.now < until) step();
}

/* A press that has already ended: queued by `touch.c`, drained by the loop, finger gone. */
static void tap_at(int x, int y)
{
    w.in.tapped = true;
    w.in.ox = x;
    w.in.oy = y;
    w.in.panel_x = x;
    w.in.panel_y = y;
}

/* A finger still on the glass. The down EDGE carries the point, which is what makes the rim
   margin a fact about this press rather than about the last one. */
static void press_down(int x, int y)
{
    tap_at(x, y);
    w.in.down = true;
}

static uint16_t px(int x, int y)
{
    return w.fb[(size_t)y * FACE_W + x];
}

static bool frame_is_black(void)
{
    for (size_t i = 0; i < (size_t)FACE_W * FACE_H; i++) {
        if (w.fb[i] != 0) return false;
    }
    return true;
}

/* Is anything drawn within `r` of here — "is there a control under my finger", which is the
   question a hit test has to agree with. */
static bool drawn_near(int cx, int cy, int r)
{
    for (int y = cy - r; y <= cy + r; y++) {
        if (y < 0 || y >= FACE_H) continue;
        for (int x = cx - r; x <= cx + r; x++) {
            if (x < 0 || x >= FACE_W) continue;
            if (px(x, y) != 0) return true;
        }
    }
    return false;
}

/* The fetch arrives: the queue drops one, the stream opens, the run is live. */
static void land_fetch(void)
{
    w.fetching = false;
    w.stream = true;
    w.in.jrunning = true;
    w.in.jstate = UI_JP_PLAYING;
    if (w.in.waiting > 0) w.in.waiting--;
}

/* The message finishes: the speaker falls silent and the box is told it was played. */
static void finish_message(void)
{
    w.stream = false;
    w.in.jrunning = false;
    w.in.jstate = UI_JP_PLAYING; /* the acknowledgement the renderer has to answer */
}

/* Drive a message all the way through: notice, press, fetch, play. Leaves the run sounding. */
static void play_a_message(bool from_dad)
{
    w.in.waiting = 1;
    w.in.waiting_from_dad = from_dad;
    step();                     /* it arrives */
    run_ms(400);                /* the arrival cue finishes */
    tap_at(60, 100);            /* the notice, top-left quadrant */
    step();
    run_ms(400);                /* the press's own cue finishes; the fetch is asked for */
    land_fetch();
    /* WHO IT IS FROM IS ONLY KNOWN ONCE THE FETCH LANDS: `jpanel_in_from()` reads the fetch's own
       response header, which is exactly why the run overlay asks the QUEUE while it is still
       starting — until this moment the value belongs to the PREVIOUS message. */
    w.in.in_from = from_dad ? UI_TO_DAD : UI_TO_PANEL;
    step();
}

/* ── THE ARBITRATION ORDER ────────────────────────────────────────────────────────────────*/

static void test_the_order_is_a_list_and_ends_with_the_pet(void)
{
    /* The order used to be a chain of `if`s, which is how a target could sit too high in it
       without anyone being able to read that off the code. Asserted literally, so moving one
       is a test change and therefore a decision. */
    const ui_target_t want[UI_TAP_ORDER_LEN] = {
        UI_TARGET_GRID, UI_TARGET_EXIT, UI_TARGET_POPUP, UI_TARGET_PAIR, UI_TARGET_CONFIRM,
        UI_TARGET_PET,
    };
    for (int i = 0; i < UI_TAP_ORDER_LEN; i++) {
        CHECK(UI_TAP_ORDER[i] == want[i], "the tap order is what it says it is");
    }
    /* THE LAST ENTRY IS LOAD-BEARING: every press must reach something, or a tap would fall out
       of the table and be silently lost. */
    CHECK(UI_TAP_ORDER[UI_TAP_ORDER_LEN - 1] == UI_TARGET_PET, "the pet catches every press");
    CHECK(UI_TAP_ORDER_SPEAKING[UI_TAP_ORDER_SPEAKING_LEN - 1] == UI_TARGET_PET,
          "and it catches them while the speaker runs too");
}

static void test_the_grid_is_modal_and_outranks_everything(void)
{
    world_reset();
    /* Everything else on the glass at once: a notice waiting, a run sounding, a pair standing. */
    w.in.waiting = 2;
    w.in.jrunning = true;
    w.st.repeat_until = w.in.now + 1;
    w.st.popup_box[0] = 0;
    w.st.popup_box[1] = 0;
    w.st.popup_box[2] = FACE_W / 2;
    w.st.popup_box[3] = FACE_H / 2;
    w.st.sendto_until = w.in.now + SENDTO_MS;
    /* Every quadrant, and the dead middle: the menu takes all of them. */
    const int xs[5] = {10, FACE_W - 10, 10, FACE_W - 10, FACE_W / 2};
    const int ys[5] = {10, 10, FACE_H - 10, FACE_H - 10, FACE_H / 2};
    for (int i = 0; i < 5; i++) {
        tap_at(xs[i], ys[i]);
        CHECK(ui_tap_target(&w.st, &w.in) == UI_TARGET_GRID,
              "the grid consumes every press while it is up");
        w.in.tapped = false;
    }
}

static void test_the_notice_is_tappable_only_in_one_quadrant_today(void)
{
    /* THE PICTURE AND THE TARGET ARE DIFFERENT RECTANGLES, and that is a live fault.
       `draw_popup` paints a 296x214 box centred in the frame — x 36..332, y 117..331 — while the
       hit rectangle it arms is the top-LEFT quadrant, x 0..184, y 0..224. So more than half of
       what a child can see is not pressable, and the part of the visible box at x > 184
       reaches whatever is behind it instead: the exit corner if a pair is standing, otherwise
       the pet. A control you can see is a control you can press; these two disagree. */
    world_reset();
    w.in.waiting = 1;
    step();      /* the notice arrives and the rectangle is armed */
    run_ms(400); /* the arrival cue finishes, so presses are not swallowed */

    CHECK(w.ov.popup_big, "the big notice is up");
    CHECK(w.st.popup_box[0] == 0 && w.st.popup_box[1] == 0 && w.st.popup_box[2] == FACE_W / 2 &&
              w.st.popup_box[3] == FACE_H / 2,
          "and its target is the top-left quadrant alone");

    /* Inside the drawn box AND inside the target: plays, as it should. */
    tap_at(120, 150);
    CHECK(ui_tap_target(&w.st, &w.in) == UI_TARGET_POPUP, "the left of the notice plays it");
    w.in.tapped = false;

    /* Inside the drawn box, outside the target, nothing else live: the PET. A child presses the
       notification they are looking at and the pet farts. */
    tap_at(250, 150);
    CHECK(ui_tap_target(&w.st, &w.in) == UI_TARGET_PET,
          "TODAY: the right of the visible notice pokes the pet instead");

    /* AND IT IS NO LONGER EATEN BY THE EXIT. With a pair standing — which, since nothing expires
       it, is permanently true after the first message the panel ever plays — this press used to
       reach the exit corner, which plays no cue, leaves no mark, and clears the pair. First press
       dead, second press fine, once per message: *"playback doesn't seem to work most of the
       time"*. A message being OFFERED now outranks a message being ended. */
    w.st.repeat_until = w.in.now + 1;
    CHECK(ui_tap_target(&w.st, &w.in) == UI_TARGET_PET,
          "a standing pair no longer swallows a press aimed at a new notice");
    CHECK(w.st.repeat_until != 0, "and the pair it did not claim is still standing");

    /* The exit is only stood down while the notice is the thing being offered. Once a message is
       actually playing there IS something to exit from, and the corner comes back. */
    w.in.jrunning = true;
    tap_at(276, 112);
    CHECK(ui_tap_target(&w.st, &w.in) == UI_TARGET_EXIT,
          "and it returns the moment a message is actually running");
}

static void test_a_sounding_speaker_hides_every_target_but_two_today(void)
{
    /* THE WHOLE DISPATCH USED TO SIT INSIDE `tapped && !speaking`, and `audio_playing()` is true
       for the panel's OWN cues. So for the length of a notification beep — the exact moment a
       child looks up and reaches for the glass — a notice, the exit, the pair and the tick are
       all unreachable. The two tables make that visible instead of implicit. */
    world_reset();
    w.in.waiting = 1;
    w.st.popup_box[0] = 0;
    w.st.popup_box[1] = 0;
    w.st.popup_box[2] = FACE_W / 2;
    w.st.popup_box[3] = FACE_H / 2;
    w.st.repeat_until = w.in.now + 1;
    w.st.sendto_until = w.in.now + SENDTO_MS;

    w.in.speaking = false;
    tap_at(120, 150);
    CHECK(ui_tap_target(&w.st, &w.in) == UI_TARGET_GRID, "quiet: the table is the long one");

    w.in.speaking = true;
    CHECK(ui_tap_target(&w.st, &w.in) == UI_TARGET_PET,
          "TODAY: a sounding speaker puts the grid, the notice, the exit and the pair out of "
          "reach");
    CHECK(UI_TAP_ORDER_SPEAKING_LEN == 2, "only the transport and the pet remain");
}

static void test_the_draw_layers_are_a_list(void)
{
    const ui_layer_t want[UI_LAYER_COUNT] = {
        UI_LAYER_TALK, UI_LAYER_POPUP, UI_LAYER_TRANSPORT, UI_LAYER_GRID,
    };
    for (int i = 0; i < UI_LAYER_COUNT; i++) {
        CHECK(UI_DRAW_ORDER[i] == want[i], "the draw order is what it says it is");
    }
    /* The grid is last because it is the one overlay a child asked for by pressing; everything
       under it is something the panel offered. */
    CHECK(UI_DRAW_ORDER[UI_LAYER_COUNT - 1] == UI_LAYER_GRID, "the menu is never covered");
}

/* ── THE SCENARIOS ────────────────────────────────────────────────────────────────────────*/

static void test_a_notification_arrives_and_a_tap_plays_it(void)
{
    world_reset();
    w.in.waiting = 1;
    w.in.waiting_from_dad = true;
    step();

    CHECK(w.cues == 1 && w.cue[0] == CUE_MESSAGE, "one arrival makes one sound");
    CHECK(w.ov.popup_big, "the notice arrives big rather than as a badge");
    CHECK(w.ov.popup_from_dad, "and it carries the sender's own face");
    CHECK(w.st.popup_box[0] >= 0, "the target is armed from the queue, not from the painting");

    run_ms(400); /* the arrival cue finishes */
    CHECK(!audio_playing(), "the speaker is free again");

    tap_at(60, 100);
    step();
    CHECK(w.st.pending == UI_PEND_PLAY, "the press is taken at once");
    CHECK(last_cue() == CUE_HEARD, "and it answers the finger before the message");
    CHECK(w.play_next == 0, "the fetch waits for the cue to finish — see PENDING_MS");
    CHECK(w.st.popup_box[0] < 0 || w.ov.popup_big, "the notice does not invite a second press");

    run_ms(400);
    CHECK(w.play_next == 1, "then the message is asked for, exactly once");
}

static void test_a_press_does_not_evaporate_behind_its_own_cue(void)
{
    /* The owner's *"it doesn't really play every single time"*: `audio_play` refuses while
       anything else sounds, so the press's acknowledgement would eat the message. The press is
       held for up to PENDING_MS and fires on the first frame the speaker is free. */
    world_reset();
    w.in.waiting = 1;
    step();
    run_ms(400);

    tap_at(60, 100);
    step();
    CHECK(w.st.pending == UI_PEND_PLAY, "taken");
    /* Hold the speaker busy for most of the window and the press is still alive. */
    for (int i = 0; i < 60; i++) {
        w.busy_until = w.in.now + FAKE_CUE_MS;
        step();
    }
    CHECK(w.st.pending == UI_PEND_PLAY, "a busy speaker does not throw the press away");
    CHECK(w.play_next == 0, "nor play it into the noise");
    w.busy_until = 0;
    step();
    CHECK(w.play_next == 1, "it plays the moment the speaker is free");
    CHECK(w.st.pending == UI_PEND_NONE, "and once only");
}

static void test_a_press_during_a_cue_is_discarded_today(void)
{
    /* The other half of the same complaint, and still live: the press above survived because it
       was TAKEN before the cue started. A press that arrives WHILE the panel is making a noise
       is dropped on the floor — including the notification beep the child is reacting to. */
    world_reset();
    w.in.waiting = 1;
    step();
    CHECK(audio_playing(), "the arrival cue is still sounding");

    tap_at(60, 100);
    step();
    CHECK(w.st.pending == UI_PEND_NONE,
          "TODAY: a press landing during the notification beep is discarded");
    CHECK(w.tap_out.flinch, "the pet twitches, so it looks answered");
    CHECK(w.play_next == 0, "and nothing plays");
}

static void test_the_run_controls_appear_from_the_press_not_the_stream(void)
{
    /* The owner: *"there is a big delay from when I click the icon to when the next icons show
       up... as soon as I click it and it's registered it should show right away."* A pending tap
       IS the press being registered. */
    world_reset();
    w.in.waiting = 1;
    step();
    run_ms(400);
    tap_at(60, 100);
    step();
    CHECK(w.ov.run, "the controls are up on the frame of the press");
    CHECK(w.ov.run_playing, "showing pause, because sound is coming");
    CHECK(w.ov.exit_corner, "and the way out is drawn with them");
    CHECK(drawn_near(276, 112, 40), "the exit is really on the glass, top right");
    CHECK(!w.ov.pair, "and the ended state is not");
}

static void test_the_top_right_corner_ends_a_message_only_once_it_is_silent_today(void)
{
    /* The owner asked for this the other way round: *"when it does play and I want to exit it, I
       should be able to click on the top right where there's no icon and have it exit out."*
       It cannot, and the reason is the `!speaking` guard above: a message that is audible is a
       speaker that is running, so the press goes down the short table and reaches the transport
       pair instead — where the top right is neither half and does nothing but flinch. */
    world_reset();
    play_a_message(false);
    CHECK(w.ov.run && w.ov.exit_corner, "the exit is drawn while the message plays");
    CHECK(drawn_near(276, 112, 40), "and a finger has something to aim at");

    const int stops = w.stop;
    tap_at(276, 112); /* squarely on the drawn exit disc */
    step();
    CHECK(w.stop == stops, "TODAY: pressing the drawn exit mid-message does not end it");
    CHECK(w.tap_out.flinch, "it only flinches");
    CHECK(w.stream, "the message is still playing");

    /* Once it has finished, the pair comes up and the same corner works. */
    finish_message();
    step();
    CHECK(ui_pair_up(&w.st), "the message ended, so again-and-reply stands");
    run_ms(400);
    tap_at(276, 112);
    step();
    CHECK(w.stop > stops, "and now the corner ends it");
    CHECK(!ui_pair_up(&w.st), "the pair leaves with it");
    CHECK(w.st.pending == UI_PEND_NONE, "a deferred play must not resurrect what she ended");
}

static void test_the_pair_waits_for_a_finger_not_a_clock(void)
{
    /* The owner: *"no more waiting for it to time out and making it disappear. Have it only
       disappear if we click out into the top right."* A control that vanishes while you are
       deciding is the same fault as one you cannot press, wearing a different face. */
    world_reset();
    play_a_message(true);
    finish_message();
    step();
    CHECK(ui_pair_up(&w.st), "the pair is up when the message ends");
    CHECK(w.jpanel_clear >= 1, "and the box has been told it was played");

    run_ms(REPEAT_MS * 3);
    CHECK(ui_pair_up(&w.st), "thirty seconds later it is still there");
    CHECK(w.ov.pair, "and still drawn");
    CHECK(w.ov.pair_from_dad, "with the face of whoever just spoke");
    CHECK(drawn_near(CONFIRM_CX_CANCEL, confirm_cy(FACE_H), 40), "again is on the glass");
    CHECK(drawn_near(CONFIRM_CX_SEND, confirm_cy(FACE_H), 40), "and so is reply");

    run_ms(120000);
    CHECK(ui_pair_up(&w.st), "two minutes later, still there");
}

static void test_reply_records_to_whoever_just_spoke(void)
{
    /* THE REPLY NEEDS NO CHOICE MADE. Answering a message used to mean opening the menu and
       picking the person who had this second finished talking. */
    world_reset();
    w.in.in_from = UI_TO_DAD;
    play_a_message(true);
    finish_message();
    step();
    run_ms(400);
    CHECK(ui_pair_up(&w.st), "the pair is up");

    const int opened = w.opened;
    tap_at(CONFIRM_CX_SEND, confirm_cy(FACE_H) + 10);
    step();
    CHECK(w.st.talk == UI_TALK_RECORDING, "reply opens a recording");
    CHECK(w.st.rec_to == UI_TO_DAD, "addressed to the person who just spoke");
    CHECK(w.opened == opened + 1, "the microphone was opened once");
    CHECK(last_cue() == CUE_LISTEN, "and it says so in the one sound that means start talking");
    CHECK(!ui_pair_up(&w.st), "the pair steps aside for the tick and cross");
    CHECK(w.muted, "commands are deaf: a message must not also be a command");

    /* Speak, then go quiet, and it goes to dad rather than to the pet. */
    w.in.hearing = true;
    run_ms(400);
    w.in.hearing = false;
    run_ms(LISTEN_HUSH_MS + 400);
    CHECK(w.send_jpanel == 1, "the message was posted");
    CHECK(w.send_jpanel_to == UI_TO_DAD, "to dad");
    CHECK(w.send_talk == 0, "and never to the conversation");
    CHECK(!w.muted, "the mute lifts with the recording, derived rather than armed");
}

static void test_sending_does_not_raise_the_playback_controls(void)
{
    /* SHIPPED BROKEN. `JPANEL_BUSY` covers a SEND as well as a fetch, so reading "the panel is
       doing something" put a pause button and a sender's face over an OUTGOING message — audio
       that did not exist, with a tap handler that correctly refused to honour any of it. */
    world_reset();
    CHECK(ui_start_record(&w.st, &w.in, &w.tap_out, UI_TO_PANEL), "a recording starts");
    perform(&w.tap_out);
    w.in.hearing = true;
    run_ms(400);
    w.in.hearing = false;
    run_ms(LISTEN_HUSH_MS + 400);
    CHECK(w.send_jpanel == 1, "it was sent");
    CHECK(w.in.jstate == UI_JP_BUSY || w.jpanel_clear > 0, "the send is in flight");

    /* The send is in flight for as long as the box takes. Nothing about it is playback. */
    w.in.jstate = UI_JP_BUSY;
    w.fetching = false;
    w.in.jrunning = false;
    step();
    CHECK(!ui_layer_up(&w.ov, UI_LAYER_TRANSPORT), "an outgoing message raises no transport");
    CHECK(!w.ov.run, "no run bar");
    CHECK(!w.ov.pair, "no again-and-reply");
    CHECK(!drawn_near(CONFIRM_CX_CANCEL, confirm_cy(FACE_H), 40), "and no pixels either");

    /* The positive control, so this test cannot pass by the overlay simply never appearing: a
       real FETCH does raise them, on the frame it starts. */
    w.in.jstate = UI_JP_IDLE;
    w.fetching = true;
    step();
    CHECK(ui_layer_up(&w.ov, UI_LAYER_TRANSPORT), "a fetch does raise them");
    CHECK(w.ov.run && w.ov.run_playing, "as a pause, because sound is coming");
}

static void test_a_short_button_press_darkens_blanks_and_mutes(void)
{
    /* The owner: *"single press blinks the screen and stops listening. But it keeps looking for
       incoming messages and will light up if a new message comes in."* */
    world_reset();
    /* Something on the glass first, so the blank has something to clear. */
    w.st.repeat_until = w.in.now + 1;
    step();
    CHECK(!frame_is_black(), "there are pixels on the panel before the press");

    w.in.btn_short = true;
    step();
    CHECK(w.standby_n == 1, "the press enters standby");
    CHECK(w.standby && w.dark, "the screen is off");
    CHECK(w.blank_n == 1, "and a black frame was actually asked for");
    CHECK(frame_is_black(), "and actually blitted — an AMOLED holds its last frame otherwise");
    CHECK(saw_cue(CUE_STOP), "it blinks with a sound, because dark and crashed look the same");
    /* ONE FRAME LATER, and that is the real shape rather than a concession to the harness: the
       mute is DERIVED per frame, near the top, and the button is read below it — so a press
       mutes on the following pass, ~80 ms later. Derived rather than armed at the edges is what
       matters: recording ends five ways and a mute armed on entry would be left on forever by
       whichever exit someone forgot. */
    step();
    CHECK(w.muted, "commands are muted: screen off AND deaf");
    CHECK(w.stop >= 1, "nothing modal survives being told to be quiet");
    CHECK(!ui_pair_up(&w.st), "including the pair");

    /* Still reachable: the next press comes back rather than going deeper. */
    run_ms(400);
    w.in.btn_short = true;
    step();
    CHECK(w.wake_n == 1, "a second press wakes it");
    CHECK(!w.standby, "and it is awake again");
    step(); /* the mute is derived a frame later, in both directions */
    CHECK(!w.muted, "and listening again");
    CHECK(w.power_off_n == 0, "a short press never powers down");
}

static void test_a_five_second_hold_requests_deep_sleep_exactly_once(void)
{
    /* Five seconds, as the owner asked, and deliberately far past anything a child produces by
       leaning on the button: this is the one control whose outcome cannot be undone from the
       panel, because a unit in deep sleep answers nothing but its own button.
       The debounce and the edge machine are still `display.c`'s (`boot_button_poll`), so what is
       driven here is its output: one `btn_hold` frame at the threshold, and the finger still
       down for seconds afterwards. */
    world_reset();
    const uint32_t down_at = w.in.now;
    while (w.in.now - down_at < 5000) {
        CHECK(w.power_off_n == 0, "nothing happens before the five seconds are up");
        step();
    }
    w.in.btn_hold = true; /* what `boot_button_poll` raises, once, while still held */
    step();
    CHECK(w.power_off_n == 1, "the hold asks for deep sleep");

    /* The finger stays down for another three seconds. It must not ask again. */
    run_ms(3000);
    CHECK(w.power_off_n == 1, "exactly once, however long the finger stays");
    CHECK(w.standby_n == 0, "and a hold is not a short press half-fired");
}

static void test_a_hold_on_the_pet_opens_the_grid_at_every_free_tap_count(void)
{
    /* The owner: *"make sure long press will pull up the menu even if it's after multiple
       presses"*. Children do not hold from a standing start — they poke the pet, it does
       something, they poke it again, and then they hold. Every one of those attempts did
       nothing while the count had to be zero. */
    for (int taps = 0; taps <= GESTURE_TAPS_MAX; taps++) {
        world_reset();
        w.in.gest_taps = taps;
        press_down(FACE_W / 2, FACE_H / 2); /* squarely on the pet, well inside the rim */
        step();
        run_ms(HOLD_TALK_MS + 200);

        if (gesture_reserved(taps)) {
            CHECK(!ui_grid_up(&w.st), "a count the maintenance gestures own does not open it");
            CHECK(!w.ov.grid, "and draws no menu");
        } else {
            CHECK(ui_grid_up(&w.st), "every other count opens the grid on a hold");
            CHECK(w.ov.grid, "and the menu is drawn");
            CHECK(saw_cue(CUE_HEARD), "with the sound that means something has appeared");
            /* All four icons are really there to be pressed. */
            CHECK(drawn_near(FACE_W / 4, sendto_cy_top(FACE_H), 50), "sister is drawn");
            CHECK(drawn_near(FACE_W * 3 / 4, sendto_cy_top(FACE_H), 50), "dad is drawn");
            CHECK(drawn_near(FACE_W * 3 / 4, sendto_cy_bottom(FACE_H), 50), "the pet is drawn");
        }
    }
    CHECK(gesture_reserved(GESTURE_TAPS_REBOOT), "the reboot count is still reserved");
    CHECK(!gesture_reserved(1) && !gesture_reserved(2),
          "and the low counts are still free for the menu");
}

static void test_the_grid_reaches_all_four_people(void)
{
    /* Four quadrants, four answers, and the pet is one of them — so a hold can reach a
       conversation as well as a sister. */
    struct {
        int x, y;
        const char *what;
    } presses[3] = {
        {FACE_W / 4, 60, "sister"},
        {FACE_W * 3 / 4, 60, "dad"},
        {FACE_W * 3 / 4, FACE_H - 60, "the pet"},
    };
    for (int i = 0; i < 3; i++) {
        world_reset();
        press_down(FACE_W / 2, FACE_H / 2);
        step();
        run_ms(HOLD_TALK_MS + 200);
        CHECK(ui_grid_up(&w.st), "the grid is up");
        w.in.down = false;
        step();

        tap_at(presses[i].x, presses[i].y);
        step();
        CHECK(!ui_grid_up(&w.st), "the choice closes the menu");
        CHECK(last_cue() == CUE_LISTEN, "and opens a microphone");
        if (i == 2) {
            CHECK(w.st.talk == UI_TALK_LISTENING, "the pet's icon starts a conversation");
            CHECK(!w.muted, "which is a command, so the recogniser stays live");
        } else {
            CHECK(w.st.talk == UI_TALK_RECORDING, "a person's icon starts a message");
            CHECK(w.st.rec_to == (i == 0 ? UI_TO_PANEL : UI_TO_DAD), "addressed to them");
            CHECK(w.muted, "and a message must not also be a command");
        }
    }

    /* The cross closes it with nothing started — and the pet underneath is not poked. */
    world_reset();
    press_down(FACE_W / 2, FACE_H / 2);
    step();
    run_ms(HOLD_TALK_MS + 200);
    w.in.down = false;
    step();
    tap_at(60, FACE_H - 60);
    step();
    CHECK(!ui_grid_up(&w.st), "the cross closes the menu");
    CHECK(w.st.talk == UI_TALK_IDLE, "with nothing recording");
    CHECK(w.opened == 0, "and the microphone never opened");
    CHECK(!w.tap_out.tap_fell_through, "the pet cannot be poked through a menu");
}

static void test_the_grid_closes_itself_but_a_stray_finger_does_not(void)
{
    /* SIDE-MOUNTED, because that is the only orientation with anywhere to miss. Inside the band
       the menu is four quadrants and four answers — `tests.c` already proves it leaves nowhere
       to miss — so the press that tests "a miss keeps it up" has to land in the bars a quarter
       turn does not carry, above and below the 368 px square. */
    world_reset();
    w.in.over_y0 = (FACE_H - FACE_W) / 2; /* 40 */
    w.in.over_h = w.in.over_y0 + FACE_W;  /* 408 */
    press_down(FACE_W / 2, FACE_H / 2);
    step();
    run_ms(HOLD_TALK_MS + 200);
    CHECK(ui_grid_up(&w.st), "up");
    w.in.down = false;
    step();

    /* Below the square: outside every target, and the menu must survive it. Closing on a stray
       finger would make the grid something a child has to aim at twice. */
    tap_at(FACE_W / 2, w.in.over_h + 10);
    step();
    CHECK(ui_grid_up(&w.st), "a finger off the band does not close the menu");
    CHECK(w.st.talk == UI_TALK_IDLE, "and starts nothing");
    CHECK(w.tap_out.flinch, "it is consumed rather than ignored");
    CHECK(!w.tap_out.tap_fell_through, "and the pet underneath is not poked");

    run_ms(SENDTO_MS + 400);
    CHECK(!ui_grid_up(&w.st), "but its own clock does — the pet is what she came back to");
}

static void test_a_tick_pressed_into_silence_refuses_to_send(void)
{
    /* The one exit that skips the silence check must not become the way six seconds of a bedroom
       reaches dad. Said out loud, because a child who pressed the tick and heard nothing has
       been told it went. */
    world_reset();
    CHECK(ui_start_record(&w.st, &w.in, &w.tap_out, UI_TO_DAD), "recording");
    perform(&w.tap_out);
    step();
    CHECK(w.ov.confirm, "the tick and the cross are up");
    CHECK(drawn_near(CONFIRM_CX_SEND, confirm_cy(FACE_H), 40), "and drawn");

    tap_at(CONFIRM_CX_SEND, confirm_cy(FACE_H) + 10);
    step();
    CHECK(w.send_jpanel == 0, "nobody spoke, so nothing was sent");
    CHECK(last_cue() == CUE_OOPS, "and it says so");
    CHECK(w.st.talk == UI_TALK_IDLE, "the recording is over");
    CHECK(w.drop == 1, "the bytes were dropped rather than posted");
}

static void test_the_cross_discards_and_the_pet_is_not_poked(void)
{
    world_reset();
    CHECK(ui_start_listening(&w.st, &w.in, &w.tap_out), "listening, hands-free");
    perform(&w.tap_out);
    w.in.hearing = true;
    run_ms(400);
    w.in.hearing = false;

    tap_at(CONFIRM_CX_CANCEL, confirm_cy(FACE_H) + 10);
    step();
    CHECK(w.st.talk == UI_TALK_IDLE, "the cross ends the turn");
    CHECK(w.drop == 1, "and throws the capture away");
    CHECK(w.send_talk == 0, "nothing was uploaded");
    CHECK(saw_cue(CUE_STOP), "it answers instantly, unlike the box");

    /* A miss while a child is talking is consumed WITHOUT a flinch: a pet that twitches
       mid-sentence is the panel inviting the next poke. */
    world_reset();
    CHECK(ui_start_listening(&w.st, &w.in, &w.tap_out), "listening again");
    perform(&w.tap_out);
    tap_at(FACE_W / 2, 40); /* the top half is inert on purpose */
    step();
    CHECK(w.st.talk == UI_TALK_LISTENING, "the turn survives a stray finger");
    CHECK(!w.tap_out.flinch, "and the pet does not twitch at it");
    CHECK(w.tap_out.tap_missed, "but it is logged, or it cannot be diagnosed without a cable");
}

static void test_a_named_turn_with_nobody_speaking_costs_nothing(void)
{
    /* An accidental "hey fish" from the television must not become an upload. */
    world_reset();
    CHECK(ui_start_listening(&w.st, &w.in, &w.tap_out), "the name opened a turn");
    perform(&w.tap_out);
    run_ms(LISTEN_LEAD_MS + 400);
    CHECK(w.st.talk == UI_TALK_IDLE, "silence closes it");
    CHECK(w.send_talk == 0, "with nothing sent");
    CHECK(w.drop == 1, "and the bytes dropped");
    CHECK(w.cues == 0, "silently: an accidental wake costs nothing at all");
}

static void test_a_failed_turn_says_so_rather_than_going_quiet(void)
{
    /* On a panel whose owner has no terminal, "it did not hear you" and "it is broken" must not
       look identical — and to a four-year-old, silence IS the failure. */
    world_reset();
    CHECK(ui_start_listening(&w.st, &w.in, &w.tap_out), "listening");
    perform(&w.tap_out);
    w.in.hearing = true;
    run_ms(400);
    w.in.hearing = false;
    run_ms(LISTEN_HUSH_MS + 400);
    CHECK(w.send_talk == 1, "the turn went up");
    CHECK(w.st.talk == UI_TALK_THINKING, "and the bubble is up");
    step();
    CHECK(w.ov.thinking && !w.ov.thinking_failed, "thinking, not failed");

    w.in.net = UI_NET_FAILED;
    step();
    CHECK(w.st.talk == UI_TALK_FAILED, "a failure is a state, not a silent return to idle");
    CHECK(saw_cue(CUE_OOPS), "with a gentle sound — try again, not told off");
    step();
    CHECK(w.ov.thinking && w.ov.thinking_failed, "and a bewildered face");

    run_ms(TALK_FAILED_MS + 400);
    CHECK(w.st.talk == UI_TALK_IDLE, "then the pet goes back to being a pet");
}

static void test_a_reply_arms_a_follow_up_and_the_cap_ends_it(void)
{
    /* The exchange continues itself, fired on the EDGE where the speaker falls silent — and
       bounded, because this is a loop with a loudspeaker in it: a television in the room could
       otherwise hold a conversation with the panel all night. */
    world_reset();
    for (int turn = 1; turn <= FOLLOW_MAX_TURNS + 2; turn++) {
        /* A reply arrives and plays. */
        w.st.talk = UI_TALK_THINKING;
        w.st.talk_since = w.in.now;
        w.in.net = UI_NET_SPOKE;
        step();
        CHECK(w.st.talk == UI_TALK_IDLE, "the bubble goes when the reply arrives");
        w.stream = true; /* the reply is coming out of the speaker */
        step();
        w.stream = false;
        step(); /* the edge where it falls silent */

        if (turn <= FOLLOW_MAX_TURNS) {
            CHECK(w.st.talk == UI_TALK_LISTENING, "the microphone reopens by itself");
            CHECK(w.st.follow_turns == turn, "counting the turns since a deliberate start");
            /* No beep on a follow-up: a tone after every reply is the toy interrupting the
               conversation it just started. */
            run_ms(FOLLOW_LEAD_MS + 400); /* nobody spoke; it closes again */
            CHECK(w.st.talk == UI_TALK_IDLE, "and closes on silence");
        } else {
            CHECK(w.st.talk == UI_TALK_IDLE, "past the cap it stops following up");
        }
    }
    CHECK(w.st.follow_turns == FOLLOW_MAX_TURNS, "the counter parks at the cap");

    /* A deliberate start is a fresh exchange. */
    CHECK(ui_start_listening(&w.st, &w.in, &w.tap_out), "a name still opens a turn");
    CHECK(w.st.follow_turns == 0, "and resets the runaway counter");
}

static void test_stop_said_out_loud_leaves_every_state(void)
{
    world_reset();
    CHECK(ui_start_record(&w.st, &w.in, &w.tap_out, UI_TO_PANEL), "recording");
    perform(&w.tap_out);
    memset(&w.tap_out, 0, sizeof(w.tap_out));
    ui_voice_stop(&w.st, &w.tap_out);
    perform(&w.tap_out);
    CHECK(w.st.talk == UI_TALK_IDLE, "stop stops a recording");
    CHECK(w.drop == 1, "dropping it rather than sending it");
    CHECK(w.st.follow_turns == FOLLOW_MAX_TURNS, "and closes the follow-up window");

    world_reset();
    w.st.talk = UI_TALK_THINKING;
    ui_voice_stop(&w.st, &w.tap_out);
    perform(&w.tap_out);
    CHECK(w.st.talk == UI_TALK_IDLE, "stop abandons a reply in flight");
    CHECK(w.talk_clear >= 1, "telling the network half to let go");
}

static void test_a_held_stream_resumes_rather_than_being_thrown_away(void)
{
    /* A paused stream is an HTTP response held open on the box, and a four-year-old who put the
       pet down mid-message would otherwise hold it until the panel rebooted. */
    world_reset();
    play_a_message(false);
    tap_at(CONFIRM_CX_CANCEL, confirm_cy(FACE_H) + 10); /* pause: the run is sounding */
    step();
    CHECK(w.pause_on == 1, "a finger holds the message");
    CHECK(w.paused, "and the speaker waits");
    CHECK(w.st.paused_since != 0, "with a deadline on the hold");

    run_ms(70000); /* past AUDIO_PAUSE_MAX_MS */
    CHECK(w.pause_off >= 1, "a hold is not forever");
    CHECK(!w.paused, "it resumes rather than aborting");
    CHECK(w.st.paused_since == 0, "and the deadline is spent");
}

static void test_the_notice_shrinks_to_a_badge_and_a_second_one_does_not_restore_it(void)
{
    /* A box over the pet's face is right for the first fifteen seconds and wrong for the next
       hour. A second message arriving while the first is unheard must not restore it — the child
       has already been interrupted once and has chosen not to come. */
    world_reset();
    w.in.waiting = 1;
    step();
    CHECK(w.ov.popup_big, "big to start with");
    run_ms(POPUP_BIG_MS + 400);
    CHECK(w.ov.popup_badge && !w.ov.popup_big, "then a badge");
    CHECK(w.st.popup_box[0] >= 0, "and still pressable");

    w.in.waiting = 2;
    step();
    CHECK(w.ov.popup_badge, "a second message does not take the screen hostage again");
    CHECK(saw_cue(CUE_MESSAGE), "it still makes a sound");

    /* A night, though, is not that: waking to one left overnight shows the big box again. */
    run_ms(400); /* the arrival cue has to finish first — see the flicker test below */
    ui_popup_restart(&w.st, w.in.now);
    step();
    CHECK(w.ov.popup_big, "waking to a message left overnight interrupts properly");
}

static void test_the_notice_is_unpainted_while_any_cue_sounds_today(void)
{
    /* THE TARGET SURVIVES A NOISE AND THE PICTURE DOES NOT, which is half a fix.
     *
     * The rectangle was moved out from behind the idle guard because tying it to the painting is
     * what made a notice intermittently unpressable. The PAINTING is still behind that guard,
     * and the guard includes `!speaking` — so for the length of the arrival cue the notice is
     * armed but invisible, and it vanishes again for every burp and every button beep after
     * that. A control that blinks off while it still works is the same fault as one that works
     * only while it is drawn, wearing the other face. */
    world_reset();
    w.in.waiting = 1;
    step();
    /* The arrival frame still paints it: `speaking` is sampled at the TOP of the pass and the
       cue is asked for below, so the noise does not exist yet as far as this frame knows. */
    CHECK(w.ov.popup_big, "the notice is painted on the frame it arrives");
    CHECK(audio_playing(), "with the arrival cue now sounding");

    step();
    CHECK(w.st.popup_box[0] >= 0, "the target is still armed a frame later");
    CHECK(!w.ov.popup_big && !w.ov.popup_badge,
          "TODAY: but nothing at all is painted while the cue sounds");

    run_ms(400);
    CHECK(w.ov.popup_big, "the picture comes back once the panel is quiet");
    CHECK(w.st.popup_box[0] >= 0, "and the target never moved");
}

static void test_the_queue_count_only_sounds_on_the_way_up(void)
{
    /* The count falls when a message is played, and announcing that would be the panel telling a
       child about the thing they just did. */
    world_reset();
    w.in.waiting = 3;
    step();
    CHECK(w.cues == 1 && w.cue[0] == CUE_MESSAGE, "three arriving at once is one sound");
    w.in.waiting = 2;
    step();
    CHECK(w.cues == 1, "one being played is none");
    w.in.waiting = 0;
    step();
    CHECK(w.cues == 1, "and emptying the queue is none");
    w.in.waiting = 1;
    step();
    CHECK(w.cues == 2, "a new one is news again");
}

static void test_what_the_box_said_about_a_message_is_answered_in_sound(void)
{
    world_reset();
    w.in.jstate = UI_JP_SENT;
    step();
    CHECK(saw_cue(CUE_SENT), "it went");
    CHECK(w.poll_soon == 1, "and the twin may already have answered");
    CHECK(w.jpanel_clear == 1, "the outcome is consumed once");

    world_reset();
    w.in.jstate = UI_JP_NOBODY;
    step();
    CHECK(saw_cue(CUE_OOPS), "there is nobody to send it to");
    CHECK(w.frame_out.say != NULL, "and the caption says so for whoever can read");

    world_reset();
    w.in.jstate = UI_JP_FAILED;
    step();
    CHECK(saw_cue(CUE_OOPS), "it did not go — never fail silently");
    CHECK(w.frame_out.say == NULL, "and that is a different sentence from having nobody");
}

static void test_a_replay_still_reads_as_playing(void)
{
    /* The owner: *"on a replay of a message it doesn't go back to the pause button."* A replay
       deliberately sets none of the queue's flags, so playing has to be an AUDIO fact. */
    world_reset();
    play_a_message(false);
    finish_message();
    step();
    run_ms(400);
    CHECK(ui_pair_up(&w.st), "the pair is up");

    tap_at(CONFIRM_CX_CANCEL, confirm_cy(FACE_H) + 10); /* again */
    step();
    CHECK(w.st.pending == UI_PEND_REPLAY, "again asks the box");
    CHECK(ui_pair_up(&w.st), "and keeps the offer standing");
    CHECK(w.ov.run && w.ov.run_playing, "the controls show pause from the press");

    run_ms(400);
    CHECK(w.replay == 1, "the replay was asked for");
    /* AND IT SOUNDS WITHOUT THE QUEUE KNOWING. `do_replay` sets none of the run's flags on
       purpose — the message was acknowledged the first time — so the pair keeps the screen and
       only its GLYPH changes. That is the whole of the owner's *"on a replay of a message it
       doesn't go back to the pause button"*: playing had to become an audio fact. */
    w.fetching = false;
    w.stream = true; /* sounding, with jrunning still false */
    step();
    CHECK(!w.ov.run, "a replay never raises the run bar, because there is no run");
    CHECK(w.ov.pair, "the pair keeps the screen");
    CHECK(w.ov.pair_playing, "and reads as playing, not as waiting to be played");
    CHECK(w.ov.exit_corner, "with a way out either way");

    /* Held, and the glyph goes back to play — the same pair, the same place. */
    w.paused = true;
    step();
    CHECK(w.ov.pair && !w.ov.pair_playing, "a held replay reads as held");
}

static void test_the_action_list_never_overflows(void)
{
    /* The bound in `ui.h` is a claim about the machine. The standby press is the busiest frame
       there is; if a branch is added that beats it, this fails rather than silently dropping a
       peripheral call. */
    world_reset();
    CHECK(ui_start_record(&w.st, &w.in, &w.tap_out, UI_TO_DAD), "recording");
    perform(&w.tap_out);
    /* Everything at once, on purpose: a short press and a completed hold, a finger on the pet,
       a stream held past its deadline, a deferred reply settling, a send outcome to answer and a
       message arriving. Not a likely frame; every part of it reachable. */
    w.in.jrunning = true;
    w.st.repeat_until = w.in.now + 1;
    w.st.paused_since = 1; /* long past AUDIO_PAUSE_MAX_MS */
    w.st.pending = UI_PEND_REPLY;
    w.st.pend_reply_to = UI_TO_DAD;
    w.st.pending_until = w.in.now + PENDING_MS;
    w.st.down_since = w.in.now - HOLD_TALK_MS - 1;
    w.st.down_x = FACE_W / 2;
    w.st.down_y = FACE_H / 2;
    w.in.down = true;
    w.in.btn_short = true;
    w.in.btn_hold = true;
    w.in.jstate = UI_JP_NOBODY;
    w.in.waiting = 1;
    step();
    CHECK(w.frame_out.n <= UI_ACTS_MAX, "the busiest frame that can be built still fits");
    CHECK(w.frame_out.n < UI_ACTS_MAX, "with room left, so a new branch is not a silent loss");
    CHECK(w.frame_out.n >= 8, "and it really is a busy frame, not an empty assertion");
}

int main(void)
{
    w.fb = malloc((size_t)FACE_W * FACE_H * sizeof(uint16_t));
    if (w.fb == NULL) return 1;

    test_the_order_is_a_list_and_ends_with_the_pet();
    test_the_grid_is_modal_and_outranks_everything();
    test_the_notice_is_tappable_only_in_one_quadrant_today();
    test_a_sounding_speaker_hides_every_target_but_two_today();
    test_the_draw_layers_are_a_list();

    test_a_notification_arrives_and_a_tap_plays_it();
    test_a_press_does_not_evaporate_behind_its_own_cue();
    test_a_press_during_a_cue_is_discarded_today();
    test_the_run_controls_appear_from_the_press_not_the_stream();
    test_the_top_right_corner_ends_a_message_only_once_it_is_silent_today();
    test_the_pair_waits_for_a_finger_not_a_clock();
    test_reply_records_to_whoever_just_spoke();
    test_sending_does_not_raise_the_playback_controls();
    test_a_short_button_press_darkens_blanks_and_mutes();
    test_a_five_second_hold_requests_deep_sleep_exactly_once();
    test_a_hold_on_the_pet_opens_the_grid_at_every_free_tap_count();
    test_the_grid_reaches_all_four_people();
    test_the_grid_closes_itself_but_a_stray_finger_does_not();

    test_a_tick_pressed_into_silence_refuses_to_send();
    test_the_cross_discards_and_the_pet_is_not_poked();
    test_a_named_turn_with_nobody_speaking_costs_nothing();
    test_a_failed_turn_says_so_rather_than_going_quiet();
    test_a_reply_arms_a_follow_up_and_the_cap_ends_it();
    test_stop_said_out_loud_leaves_every_state();
    test_a_held_stream_resumes_rather_than_being_thrown_away();
    test_the_notice_shrinks_to_a_badge_and_a_second_one_does_not_restore_it();
    test_the_notice_is_unpainted_while_any_cue_sounds_today();
    test_the_queue_count_only_sounds_on_the_way_up();
    test_what_the_box_said_about_a_message_is_answered_in_sound();
    test_a_replay_still_reads_as_playing();
    test_the_action_list_never_overflows();

    free(w.fb);
    printf("ok — %d checks (ui)\n", checks);
    return 0;
}
