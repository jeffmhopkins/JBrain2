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
    int play_at; /* which queued message the last PLAY_NEXT asked for */
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
            w.play_at = arg;
            /* THE SENDER IS KNOWN FROM THE PRESS, because `jpanel_play_at` seeds it from the queue
               entry it is about to fetch — the box already described that exact message, and
               `/next?at=` resolves the same index off the same ordered list. Modelled here because
               the overlay's one-reader rule depends on it: the module trusts `in_from` about a
               message's own sender, which is only safe while this is true of the real thing. */
            if (arg >= 0 && arg < w.in.known) {
                w.in.in_from = w.in.from_dad[arg] ? UI_TO_DAD : UI_TO_PANEL;
            }
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

/* A PRESS ON THE NOTICE, WHEREVER THE NOTICE IS. These scenarios used to press (60,100), a
   point in the old top-left quadrant target — which the big bubble does not cover, so hardcoding
   it meant testing a rectangle rather than a control. Asking `ui_popup_target` for the middle of
   what is actually drawn is what a child does with their eyes. */
static void tap_the_notice(bool big)
{
    int box[4];
    ui_popup_target(0, FACE_H, big, box);
    tap_at((box[0] + box[2]) / 2, (box[1] + box[3]) / 2);
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

/* WHAT THE BOX SAYS IS WAITING, oldest first: `true` for a message from Dad, `false` for one from
   the sister. One helper because three fields have to agree — the count, how many of them the box
   described one by one, and each one's sender — and a test that set two of them by hand was how
   the face and the count could be asserted against different queues. */
static void queue_is(int n, const bool *from_dad)
{
    w.in.waiting = n;
    w.in.known = n < UI_QUEUE_MAX ? n : UI_QUEUE_MAX;
    for (int i = 0; i < UI_QUEUE_MAX; i++) {
        w.in.from_dad[i] = i < w.in.known ? from_dad[i] : false;
    }
    /* THE HEAD, SAID TWICE, exactly as the box says it twice: `from_owner` is `queue[0].from_owner`
       on any box new enough to send a list, and the only answer on one that is not. */
    w.in.waiting_from_dad = n > 0 ? from_dad[0] : false;
}

/* A finger that lands, travels and lifts. `dx` is the travel in panel pixels; `display.c` resolves
   it to a direction, so what reaches the module is the direction alone. */
static void swipe(int dx)
{
    w.in.swipe = dx > 0 ? 1 : (dx < 0 ? -1 : 0);
    w.in.down = true;
    step();
    w.in.swipe = 0;
    w.in.down = false;
}

/* The fetch arrives: the queue drops one, the stream opens, the run is live. */
static void land_fetch(void)
{
    w.fetching = false;
    w.stream = true;
    w.in.jrunning = true;
    w.in.jstate = UI_JP_PLAYING;
    /* THE ENTRY GOES, NOT JUST THE NUMBER — `do_fetch` shifts the local list for the same reason:
       a count and a list describing different queues is how a selection resolves against a stale
       name. The one that was taken is `play_at`. */
    if (w.in.waiting > 0) w.in.waiting--;
    if (w.in.known > 0) {
        const int taken = w.play_at >= 0 && w.play_at < w.in.known ? w.play_at : 0;
        w.in.known--;
        for (int i = taken; i < w.in.known; i++) w.in.from_dad[i] = w.in.from_dad[i + 1];
    }
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
    const bool q[1] = {from_dad};
    queue_is(1, q);
    step();                     /* it arrives */
    run_ms(400);                /* the arrival cue finishes */
    tap_the_notice(true);       /* the notice, wherever it is drawn */
    step();
    run_ms(400);                /* the press's own cue finishes; the fetch is asked for */
    land_fetch();
    /* NOTHING TO SET HERE ANY MORE, and that is the fix rather than a tidy-up. This used to assign
       `in_from` at exactly this line, with a comment explaining that the sender was unknowable
       until the response header landed — which was true of the firmware and was the bug: the value
       in the meantime was a CLEARED one, and cleared means the sister. `perform` seeds it from the
       queue at the press now, like `jpanel_play_at`, so the header confirms rather than reveals. */
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
    CHECK(UI_TAP_ORDER_PLAYING[UI_TAP_ORDER_PLAYING_LEN - 1] == UI_TARGET_PET,
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
       hit rectangle it armed was the top-LEFT quadrant, x 0..184, y 0..224 — and the two barely
       overlap. More than half of what a child could see was not pressable, and the part of the
       visible box at x > 184 reached whatever was behind it: the exit corner if a pair was
       standing, otherwise the pet. A control you can see is a control you can press.
     *
       `ui_popup_target` is now the only answer to "where is the notice", and the drawing takes
       its bounds from the same call. */
    world_reset();
    w.in.waiting = 1;
    step();      /* the notice arrives and the rectangle is armed */
    run_ms(400); /* the arrival cue finishes */

    CHECK(w.ov.popup_big, "the big notice is up");
    int want[4];
    ui_popup_target(0, FACE_H, true, want);
    CHECK(w.st.popup_box[0] == want[0] && w.st.popup_box[1] == want[1] &&
              w.st.popup_box[2] == want[2] && w.st.popup_box[3] == want[3],
          "and its target is the bubble a child is looking at, not a quadrant beside it");
    CHECK(want[0] == 36 && want[1] == 117 && want[2] == 332 && want[3] == 331,
          "the arithmetic, stated: 296x214 centred on the 368x448 frame");

    /* Inside the drawn box AND inside the target: plays, as it should. */
    tap_at(120, 150);
    CHECK(ui_tap_target(&w.st, &w.in) == UI_TARGET_POPUP, "the left of the notice plays it");
    w.in.tapped = false;

    /* The RIGHT of the drawn box, which used to reach the pet — or the exit corner — because the
       target stopped at the centre line. */
    w.in.tapped = false;
    tap_at(250, 150);
    CHECK(ui_tap_target(&w.st, &w.in) == UI_TARGET_POPUP,
          "and the right of the visible notice plays it too");

    /* And a corner of the old quadrant that the bubble never covered is NOT the notice: the
       target followed the picture in both directions, not just outwards. */
    w.in.tapped = false;
    tap_at(10, 20);
    CHECK(ui_tap_target(&w.st, &w.in) != UI_TARGET_POPUP,
          "while bare black above the box is not a notice press");

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

static void test_a_cue_no_longer_decides_what_a_press_means(void)
{
    /* THE WHOLE DISPATCH USED TO SIT INSIDE `tapped && !speaking`, and `audio_playing()` is true
       for the panel's OWN cues. So for the length of the 440 ms notification beep — the exact
       moment a child looks up and reaches for the glass — a notice, the exit, the pair and the
       tick were all unreachable. The two tables made it visible; the fix is that the fork reads
       `stream_active`, a MESSAGE sounding, and a cue is just the panel clearing its throat. */
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

    /* The arrival cue, sounding, with the notice on the glass. */
    w.in.speaking = true;
    CHECK(ui_tap_target(&w.st, &w.in) == UI_TARGET_GRID,
          "a cue does not take the long table away — the child pressing during the beep is "
          "pressing on what they can see");

    /* A message actually sounding does. The menu keeps its claim either way — it is modal and it
       is drawn on top of the transport — so this asks with the menu closed, where the notice and
       the pair were the things that had a claim on this point a moment ago. */
    w.in.stream_active = true;
    w.st.sendto_until = 0;
    const ui_target_t narrowed = ui_tap_target(&w.st, &w.in);
    CHECK(narrowed == UI_TARGET_TRANSPORT || narrowed == UI_TARGET_PET,
          "a MESSAGE sounding is what narrows the table");
    CHECK(UI_TAP_ORDER_PLAYING_LEN == 4,
          "only the menu, the exit, the transport and the pet remain");

    /* AND THE MENU STILL CONSUMES EVERYTHING WHILE IT IS UP, message or no message: it is drawn
       above the transport, so it has to be offered the press first. */
    w.st.sendto_until = w.in.now + SENDTO_MS;
    CHECK(ui_tap_target(&w.st, &w.in) == UI_TARGET_GRID,
          "and an open menu outranks a playing message, because it covers it");
}

static void test_the_exit_ends_a_message_while_it_is_audible(void)
{
    /* THE ONE STATE THE CORNER EXISTS FOR, and the old fork made it the one state it could not
       reach: EXIT lives in the long table, `speaking` is true while a message plays, so the
       press went to the short table and matched the transport or the pet. The owner asked for
       exactly this — *"when it does play and I want to exit it, I should be able to click on the
       top right"* — and reported that it *"doesn't happen either"*. */
    world_reset();
    w.in.jrunning = true;
    w.in.stream_active = true;
    w.in.speaking = true;
    tap_at(276, 112); /* top-right quadrant */
    CHECK(ui_tap_target(&w.st, &w.in) == UI_TARGET_EXIT,
          "the corner is the exit while a message plays, not the transport that swallows "
          "everything else");
    CHECK(UI_TAP_ORDER_PLAYING[1] == UI_TARGET_EXIT,
          "and it comes before the transport, which consumes every press it is offered");
    CHECK(UI_TAP_ORDER_PLAYING[0] == UI_TARGET_GRID, "behind only the modal menu, in both tables");
    CHECK(UI_TAP_ORDER[0] == UI_TARGET_GRID && UI_TAP_ORDER[1] == UI_TARGET_EXIT,
          "which is the same way the long table begins");

    /* The transport owns the LEFT half of that band (play/pause and reply); the exit owns the
       top-right corner. They must not both claim one point — see `confirm.h` for the geometry. */
    ui_out_t out = {0};
    ui_tap(&w.st, &w.in, &out);
    bool ended = false;
    for (int i = 0; i < out.n; i++) {
        if (out.act[i].kind == UI_ACT_STOP) ended = true;
    }
    CHECK(ended, "and it ends the run rather than poking the pet");
}

static void test_the_notice_is_painted_through_its_own_arrival_cue(void)
{
    /* WHAT YOU CAN SEE AND WHAT YOU CAN PRESS MUST NOT DISAGREE. The paint guard had the
       dispatcher's bug in the other half: `!speaking` blanked the notice for the first half
       second of its life while the hit rectangle stayed armed. */
    world_reset();
    w.in.waiting = 1;
    w.in.speaking = true; /* the 440 ms chirp that announced it */
    ui_overlay_t ov = {0};
    ui_overlay(&w.st, &w.in, &ov);
    CHECK(ov.popup_big || ov.popup_badge, "the notice is on the glass while its own cue sounds");
    CHECK(w.st.popup_box[0] >= 0, "and the target is armed to match the picture");

    /* A message sounding still holds it back: there is something happening now. */
    w.in.stream_active = true;
    ui_overlay_t ov2 = {0};
    ui_overlay(&w.st, &w.in, &ov2);
    CHECK(!ov2.popup_big && !ov2.popup_badge, "but a playing message still owns the screen");
}

static void test_a_hold_after_a_poke_still_opens_the_menu(void)
{
    /* CHILDREN DO NOT HOLD FROM A STANDING START — they poke the pet, it beeps, and then they
       hold. The hold tested `!speaking`, so the beep their own poke just made could stand in
       front of the menu. */
    world_reset();
    w.in.speaking = true; /* the poke's own CUE_TOGGLE, still sounding */
    w.in.down = true;
    w.st.down_x = FACE_W / 2;
    w.st.down_y = FACE_H / 2;
    w.st.down_since = w.in.now;
    w.in.now += HOLD_TALK_MS + 50;
    ui_out_t out = {0};
    ui_frame(&w.st, &w.in, &out);
    CHECK(w.st.sendto_until != 0, "the menu opens over the panel's own beep");
}

static void test_the_controls_are_pressable_for_as_long_as_they_are_drawn(void)
{
    /* THE DRAWING AND THE ARBITRATION USED TO ASK DIFFERENT QUESTIONS. The controls appear on the
       frame of the press, which is what the owner asked for — *"as soon as I click it and it's
       registered it should show right away"* — but liveness was `jrunning || stream_active`, and
       through the whole fetch window neither is true. So the pause button was painted and a press
       on it went down the IDLE table and poked the pet: the one response a child can be certain
       they did not ask for. `ui_run_controls_up` is now the single answer to both. */
    world_reset();
    w.in.waiting = 1;
    step();
    run_ms(400);
    tap_the_notice(true);
    step();
    CHECK(w.st.pending == UI_PEND_PLAY, "the press is taken");
    CHECK(w.ov.run, "and the controls are drawn at once");
    CHECK(!w.in.jrunning && !w.in.stream_active, "with nothing running and nothing audible yet");
    CHECK(ui_run_controls_up(&w.st, &w.in), "so they are live, because they are visible");

    w.in.tapped = false;
    tap_at(CONFIRM_CX_CANCEL, confirm_cy(FACE_H) + 10); /* the pause disc */
    CHECK(ui_tap_target(&w.st, &w.in) == UI_TARGET_TRANSPORT,
          "a press on the drawn pause button reaches the transport, not the pet");

    /* The fetch alone keeps them live once the pending flag has been served. */
    world_reset();
    w.in.jfetching = true;
    CHECK(ui_run_controls_up(&w.st, &w.in), "a fetch in flight is controls up");

    /* And a REPLAY, which sets no run at all: an audible message is a message however it started,
       which is what left the ended-state pair drawn over one that was playing. */
    world_reset();
    w.in.stream_active = true;
    CHECK(!w.in.jrunning, "a replay leaves no run behind it");
    CHECK(ui_run_controls_up(&w.st, &w.in), "and the speaker is asked instead");
}

static void test_a_second_press_cannot_cancel_what_the_first_one_started(void)
{
    /* THE OWNER, on 0.3.29: *"when I try and play an incoming message when I hit it, it just
       shows the screen with the pause icon for half a second and then goes back to the other
       indicator."* That is a stop, not a failure to start — `jpanel_stop()` acknowledges, which
       is why the box saw GET /next 200 followed by POST /played 204 half a second apart.
     *
       THE ARITHMETIC. The notice is x[36,332) y[117,331); the exit corner is x[184,368) y[0,224).
       They overlap over x[184,332) y[117,224) — 148x107 px, a QUARTER of the notice, and the part
       a right-handed adult presses. While the notice is offered it outranks the exit, so the
       first press plays. One frame later the controls are up, the table is the playing one, and
       that same point is the exit.
     *
       So any second edge there cancels the message the first one started. A deliberate double
       tap does it; so does a finger that lightens for one 15 ms sample, because `touch.c` reports
       edges with no inter-tap debounce — the button has 250 ms of it for exactly this reason and
       the glass has none. I introduced this in 0.3.29 by putting the exit in the playing table,
       which was right; what was missing is that a destructive control must not arm itself under
       a finger that is already on the glass. */
    world_reset();
    w.in.waiting = 1;
    step();
    run_ms(400);

    /* Press the notice inside the overlap — visibly the notice, geometrically the exit. */
    tap_at(250, 150);
    CHECK(ui_tap_target(&w.st, &w.in) == UI_TARGET_POPUP, "the notice wins the first press");
    step();
    CHECK(w.st.pending == UI_PEND_PLAY, "and the play is taken");
    CHECK(ui_run_controls_up(&w.st, &w.in), "so the controls come up");

    /* The same point, one frame later. */
    w.in.tapped = false;
    tap_at(250, 150);
    CHECK(ui_tap_target(&w.st, &w.in) != UI_TARGET_EXIT,
          "a press in the same place cannot cancel the message it just started");

    /* And it is a grace period, not a dead corner: with the message actually running, the same
       press works once the moment the finger was in has passed. */
    world_reset();
    play_a_message(false);
    run_ms(EXIT_GRACE_MS + 100);
    w.in.tapped = false;
    tap_at(276, 112);
    CHECK(ui_tap_target(&w.st, &w.in) == UI_TARGET_EXIT,
          "but it is a grace period, not a dead corner");

    /* And inside it, the same press is refused — the grace is measured from the controls, not
       from the notice. */
    world_reset();
    play_a_message(false);
    w.in.tapped = false;
    tap_at(276, 112);
    CHECK(ui_tap_target(&w.st, &w.in) != UI_TARGET_EXIT,
          "and a press in the first moments of a run is not the way out");
}

static void test_the_face_follows_the_message_that_is_playing(void)
{
    /* THE OWNER, THREE TIMES, ACROSS 0.3.30 AND 0.3.32: *"it still ended up having the little girl
       icon on the top left versus the dad icon."* Two fixes had already been shipped for this and
       the panel still did it, so the third one went after the SOURCE rather than the reading.
     *
       `in_from` was CLEARED at the top of every fetch and cleared means the sister. Every reader
       downstream then had to guess when to trust it: the overlay asked the queue while nothing was
       sounding and `in_from` once something was, which is correct for a single message and wrong
       the moment there are two. With two queued the panel finishes one, chains straight into the
       next with `jrunning` still true, and for the whole of that second fetch the corner shows a
       little girl — for a message from Dad, on a panel where both queued messages were from Dad.
     *
       So `jpanel.c` SEEDS the sender from the queue entry it is fetching, the overlay has one
       reader, and this drives the exact scenario that kept reproducing. */
    world_reset();
    const bool both_from_dad[2] = {true, true};
    queue_is(2, both_from_dad);
    step();
    run_ms(400);
    tap_the_notice(true);
    step();
    CHECK(w.ov.run, "the controls are up");
    CHECK(w.ov.run_from_dad, "and the press-to-play window shows Dad");

    /* The fetch lands. The header names Dad and so did the seed, which is the point: they agree.
       `jfetching` stays true — the download IS the playback. */
    land_fetch();
    w.fetching = true;
    step();
    CHECK(w.ov.run_from_dad, "and it is still Dad while the message actually plays");

    /* THE CHAIN, WHICH IS WHERE IT BROKE. The first message drains, the jpanel task acknowledges
       it and fetches the next one WITHOUT a finger — `jrunning` never falls, the stream does.
       Under the old code this frame read `in_from`, freshly cleared by the new fetch, and drew the
       sister over a second message from Dad. */
    w.stream = false;
    w.fetching = true;
    /* The chain always takes the head, and seeds from it exactly as a press would. */
    w.play_at = 0;
    w.in.in_from = w.in.from_dad[0] ? UI_TO_DAD : UI_TO_PANEL;
    step();
    CHECK(w.ov.run, "the controls stay up across the chain into the second message");
    CHECK(w.ov.run_from_dad, "and the second message from Dad shows Dad, not the sister");

    /* Ended: the pair carries the same face, which is the half that was always right. */
    w.fetching = false;
    finish_message();
    step();
    CHECK(w.ov.pair_from_dad, "and the ended state agrees with what just played");
}

static void test_a_notice_does_not_show_through_the_playback_screen(void)
{
    /* THE OWNER, with two messages waiting: *"When playing back the first message, the second
       message notification showed in the background of the menu."*
     *
       The top-left quadrant means one thing — who this is from — and it had two writers. The run
       is painted after the notice so it usually won; the exceptions are the windows where
       `stream_active` is false but a message is plainly in flight: between a press and the first
       byte, and in the gap where one message has drained and the panel is chaining into the next.
       The notice painted straight through the playback screen in both.
     *
       The tap table was already right about this — `UI_TAP_ORDER_PLAYING` has no notice in it — so
       the notice was also UNPRESSABLE for as long as it was visible, which is the fault class the
       pop-up's own arming was rewritten to kill. Drawing now agrees with arbitration. */
    world_reset();
    const bool two_from_dad[2] = {true, true};
    queue_is(2, two_from_dad);
    step();
    run_ms(400);
    tap_the_notice(true);
    step();
    CHECK(w.ov.run, "the press raised the playback controls");
    CHECK(!w.ov.popup_big && !w.ov.popup_badge,
          "and the notice for the one behind it is not painted over them");
    CHECK(ui_tap_target(&w.st, &w.in) != UI_TARGET_POPUP,
          "which is what the tap table already said");

    /* And in the chain gap, where nothing is sounding and a message is still plainly in flight. */
    land_fetch();
    step();
    w.stream = false;
    w.fetching = true;
    step();
    CHECK(w.ov.run, "the controls stay up through the gap between two messages");
    CHECK(!w.ov.popup_big && !w.ov.popup_badge, "and still nothing shows through them");

    /* It is not lost. The run ends and the frame after it draws what is still waiting. */
    w.in.jrunning = false;
    w.fetching = false;
    w.st.repeat_until = 0;
    step();
    CHECK(w.ov.popup_big || w.ov.popup_badge, "and the notice returns the moment the run is over");
}

/* ── SWIPING BETWEEN WHAT IS WAITING ──────────────────────────────────────────────────────── */

static void test_a_swipe_changes_which_waiting_message_is_offered(void)
{
    /* THE OWNER: *"maybe we can add a new gesture which is swipe left and swipe right to change
       between the messages. When changing between messages, we again need to make sure that the
       icon on the top left updates, as well as the number in the middle."* */
    world_reset();
    const bool dad_then_sister[2] = {true, false};
    queue_is(2, dad_then_sister);
    step();
    run_ms(400);
    CHECK(w.ov.popup_big, "the notice is up for two messages");
    CHECK(w.ov.popup_from_dad, "and it opens on the oldest, which is Dad's");
    CHECK(w.ov.sel == 0, "the selection starts at the oldest");

    swipe(+1);
    CHECK(w.st.sel == 1, "a swipe right moves on to the next one");
    CHECK(w.ov.sel == 1, "and the drawing is told which one");
    CHECK(!w.ov.popup_from_dad, "and the face follows it: the sister's");
    CHECK(last_cue() == CUE_HEARD, "and the panel acknowledges the finger");

    swipe(-1);
    CHECK(w.st.sel == 0, "a swipe left comes back");
    CHECK(w.ov.popup_from_dad, "and Dad's face with it");

    /* CLAMPED, NOT WRAPPED: with three messages a wrap means three swipes right land back where
       you started, which reads as the panel ignoring you. */
    w.cues = 0;
    swipe(-1);
    CHECK(w.st.sel == 0, "and the oldest is where it stops going back");
    CHECK(w.cues == 0, "silently, because an end is an end and a sound would claim otherwise");
    swipe(+1);
    swipe(+1);
    CHECK(w.st.sel == 1, "the newest is where it stops going forward");
}

static void test_one_waiting_message_cannot_be_swiped_away(void)
{
    /* A lone message must not be losable to a smear — and the 700 ms hold that opens the "who?"
       menu starts with a finger on the same glass, so the gesture is off entirely where there is
       nothing to choose between. */
    world_reset();
    const bool one[1] = {true};
    queue_is(1, one);
    step();
    run_ms(400);
    swipe(+1);
    CHECK(w.st.sel == 0, "one message: a swipe changes nothing");
    CHECK(w.ov.popup_from_dad, "and the face is still his");
    CHECK(w.stop == 0, "and nothing was stopped");

    world_reset();
    step();
    swipe(+1);
    CHECK(w.st.sel == 0, "an empty queue: likewise");
    CHECK(w.stop == 0, "and still nothing stopped");
}

static void test_a_swipe_stops_the_message_it_moves_off(void)
{
    /* THE OWNER: *"And that we handle stopping the current playing message if it's playing."*
     *
       It is also what makes the gesture safe to leave live during playback. Every press on this
       panel fires on the DOWN edge — the rule that came out of measuring how four-year-olds jab —
       so a swipe that STARTED on the notice or on the pause button has already triggered it by the
       time the travel is visible. Stopping is how that unwinds. */
    world_reset();
    const bool two[2] = {true, false};
    queue_is(2, two);
    step();
    run_ms(400);
    tap_the_notice(true);
    step();
    run_ms(400);
    land_fetch();
    step();
    CHECK(w.stream, "a message is playing");
    CHECK(w.stop == 0, "and nothing has stopped it");

    /* One is playing, so one is left — enough to swipe between the playing one and that one. */
    queue_is(2, two);
    step();
    swipe(+1);
    CHECK(w.st.sel == 1, "the swipe moves the selection");
    CHECK(w.stop == 1, "and stops what was playing");
    CHECK(w.pause_off == 1, "un-paused first, so the ring is never left held");
    CHECK(w.st.pending == UI_PEND_NONE, "and no deferred play survives for the one she left");

    /* A swipe that hits the end stops nothing: it did not move, so nothing was left. */
    w.stop = 0;
    swipe(+1);
    CHECK(w.stop == 0, "a swipe at the end of the queue stops nothing");
}

static void test_a_swipe_is_not_also_a_hold(void)
{
    /* A swipe is a finger down for as long as a hold and travelling. Without the latch, dragging
       across the pet for 700 ms would ALSO open the "who?" grid — a child would be handed a
       recipient menu for having changed message. */
    world_reset();
    const bool two[2] = {true, false};
    queue_is(2, two);
    step();
    run_ms(400);

    press_down(FACE_W / 2, FACE_H / 2); /* on the pet, where a hold opens the grid */
    w.in.swipe = 1;
    step();
    w.in.swipe = 0;
    CHECK(w.st.sel == 1, "the drag changed message");
    run_ms(HOLD_TALK_MS + 200); /* and stays down well past the hold */
    CHECK(w.st.sendto_until == 0, "and the same press does not also open the grid");

    /* The latch is per press: lift, press again, hold, and the menu is still there to be had.
       ON A BARE PET, because a hold no longer opens the grid while a menu is up and the queue
       above leaves the notice on the glass — see `test_a_long_press_on_a_menu_is_just_a_press`.
       The property under test here is the LATCH resetting, which needs a hold that is allowed at
       all; asserting it through a notice would be asserting two rules and naming one. */
    queue_is(0, NULL);
    w.in.down = false;
    step();
    press_down(FACE_W / 2, FACE_H / 2);
    run_ms(HOLD_TALK_MS + 200);
    CHECK(w.st.sendto_until != 0, "a fresh press with no travel still opens it");
}

static void test_a_long_press_on_a_menu_is_just_a_press(void)
{
    /* The owner: *"in the menu we need to disable the long press and have long press treated as a
       normal press of menu items. I think this is the cause of the girl icon showing up on the top
       left."*

       "On the pet" is everything more than `TALK_MARGIN_PX` from an edge — a 224x224 square in the
       middle of a 368x368 face — which is where the notice, the again/reply pair and the grid's
       own icons are all drawn. So a press on a menu item was BOTH: the target fired on the down
       edge, and the same unmoved finger opened the sendto grid over the top of it 700 ms later. A
       child holding the reply button armed a reply AND got a "who to send to?" menu, and both put
       a person's face on the glass. */
    world_reset();
    const bool one[1] = {true};
    queue_is(1, one);
    step();
    run_ms(400);

    /* A notice is up. Hold on it, dead centre, where the grid would otherwise open. */
    press_down(FACE_W / 2, FACE_H / 2);
    run_ms(HOLD_TALK_MS + 300);
    CHECK(w.st.sendto_until == 0, "a hold on the notice must not also open the grid");

    /* The again/reply pair, the other menu that can be up with nothing playing. */
    world_reset();
    step();
    w.st.repeat_until = w.in.now + 10000;
    press_down(FACE_W / 2, FACE_H / 2);
    run_ms(HOLD_TALK_MS + 300);
    CHECK(w.st.sendto_until == 0, "nor a hold on the again/reply pair");

    /* AND THE GESTURE STILL EXISTS. This is the half that matters — the hold is how a child
       reaches "I want to send something", and suppressing it everywhere would be a worse bug
       than the one being fixed. On a bare pet it opens exactly as it always did. */
    world_reset();
    step();
    press_down(FACE_W / 2, FACE_H / 2);
    run_ms(HOLD_TALK_MS + 300);
    CHECK(w.st.sendto_until != 0, "a hold on the bare pet still opens the grid");
}

static void test_the_selected_message_is_the_one_that_plays(void)
{
    /* The whole point of the gesture. The index rides the action, and it is read where the fetch
       goes out rather than captured at the press — a pending play can wait out a cue, and the
       child may have swiped in the meantime. */
    world_reset();
    const bool two[2] = {true, false};
    queue_is(2, two);
    step();
    run_ms(400);
    swipe(+1);
    tap_the_notice(w.ov.popup_big); /* whichever of the two is actually on the glass */
    step();
    CHECK(w.st.pending == UI_PEND_PLAY, "the press was taken");
    run_ms(400);
    CHECK(w.play_next == 1, "and the fetch went out");
    CHECK(w.play_at == 1, "for the message the finger was pointing at, not the oldest");
    CHECK(w.in.in_from == UI_TO_PANEL, "and the sender is the sister's from the press onward");
}

static void test_the_numeral_says_which_of_how_many(void)
{
    /* The numeral between the two discs used to count what was still to come. A finger can point
       at one of them now, so it says WHERE THAT FINGER IS — a position is only legible next to its
       total. */
    world_reset();
    const bool three[3] = {true, false, true};
    queue_is(3, three);
    step();
    run_ms(400);
    tap_the_notice(true);
    step();
    CHECK(w.ov.run_count == 3, "the total is what is waiting");
    CHECK(w.ov.sel_shown == 1, "and the position is one-based, for a reader who counts from one");

    queue_is(3, three);
    step();
    swipe(+1);
    swipe(+1);
    CHECK(w.ov.sel_shown == 3, "and it follows the swipe");
}

static void test_a_new_message_sends_the_selection_home(void)
{
    /* A finger pointing at "the second one" is pointing at a POSITION, and the thing at that
       position is different the moment anything is added or played. Holding the index would
       silently re-aim it at a message the child never chose. */
    world_reset();
    const bool two[2] = {true, false};
    queue_is(2, two);
    step();
    run_ms(400);
    swipe(+1);
    CHECK(w.st.sel == 1, "pointing at the second one");

    const bool three[3] = {true, false, true};
    queue_is(3, three);
    step();
    CHECK(w.st.sel == 0, "a third arrives and the selection goes back to the oldest");

    /* And it is clamped, not merely reset, because the queue also shrinks on another task. */
    swipe(+1);
    swipe(+1);
    CHECK(w.st.sel == 2, "pointing at the newest");
    w.in.waiting = 1;
    w.in.known = 1;
    step();
    CHECK(w.st.sel == 0, "and a queue that shrank under the finger cannot leave it past the end");
}

static void test_every_playback_control_answers_the_finger(void)
{
    /* THE SCRUB, at the owner's ask, and then his override of what it found. *"I think play,
       resume, stop and reply should have their own sound effects. You are good to overwrite
       the decision from before."*
     *
       Before this, play and reply both borrowed `CUE_HEARD` while pause, resume and stop made
       no sound at all — on the argument that what they do to the audio IS the answer. The
       argument is true and was not enough: reply stops the sound exactly as the exit does, so
       from a child's side answering her father and dismissing him were the same press.
     *
       Four events, four shapes, and `cue.h` carries why each is what it is. What this test
       pins is that they are DIFFERENT from each other and that every control makes one. */
    world_reset();
    play_a_message(false);
    run_ms(EXIT_GRACE_MS + 100);

    /* Pause, then resume: the same disc, and it must not sound the same both ways. */
    tap_at(CONFIRM_CX_CANCEL, confirm_cy(FACE_H) + 10);
    step();
    CHECK(w.paused, "pause takes effect");
    CHECK(last_cue() == CUE_PAUSE, "and says so");
    w.in.tapped = false;
    w.in.stream_paused = true;
    tap_at(CONFIRM_CX_CANCEL, confirm_cy(FACE_H) + 10);
    step();
    CHECK(!w.paused, "resume takes effect");
    CHECK(last_cue() == CUE_RESUME, "with its own sound, not the pause played backwards");

    /* Reply, mid-message. */
    world_reset();
    play_a_message(false);
    run_ms(EXIT_GRACE_MS + 100);
    tap_at(CONFIRM_CX_SEND, confirm_cy(FACE_H) + 10);
    step();
    CHECK(w.st.pending == UI_PEND_REPLY, "the reply is taken");
    CHECK(last_cue() == CUE_REPLY, "and answers the finger now, not when the microphone opens");

    /* The exit corner, which was the silent one that made reply ambiguous. */
    world_reset();
    play_a_message(false);
    run_ms(EXIT_GRACE_MS + 100);
    const int stops = w.stop;
    tap_at(276, 112);
    step();
    CHECK(w.stop > stops, "the corner ends the run");
    CHECK(last_cue() == CUE_STOP, "and no longer leaves the child guessing which she pressed");

    /* Play, from the notice. */
    world_reset();
    w.in.waiting = 1;
    step();
    run_ms(400);
    tap_the_notice(true);
    step();
    CHECK(w.st.pending == UI_PEND_PLAY, "the play is taken");
    CHECK(last_cue() == CUE_PLAY, "with the arriving shape rather than the coin");

    /* And the four are genuinely four. `cue.c` measures them apart as waveforms
       (`test_no_two_cues_are_the_same_sound`); this is the weaker, structural half — that the
       controls do not share a cue between them. */
    const cue_t used[4] = {CUE_PLAY, CUE_PAUSE, CUE_RESUME, CUE_REPLY};
    for (int i = 0; i < 4; i++) {
        for (int j = i + 1; j < 4; j++) {
            CHECK(used[i] != used[j], "no two playback controls share a cue");
        }
        CHECK(used[i] != CUE_STOP && used[i] != CUE_HEARD,
              "and none of them borrows the stop or the coin");
    }
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

    tap_the_notice(true);
    step();
    CHECK(w.st.pending == UI_PEND_PLAY, "the press is taken at once");
    CHECK(last_cue() == CUE_PLAY, "and it answers the finger before the message");
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

    tap_the_notice(true);
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

static void test_a_press_during_the_arrival_cue_plays_the_message(void)
{
    /* The other half of the same complaint: the press in the scenario above survived because it
       was TAKEN before the cue started. A press arriving WHILE the panel made a noise went down
       the speaking table, matched nothing and was dropped — including during the very beep the
       child was reacting to. The fork reads `stream_active` now, so a cue is just a cue. */
    world_reset();
    w.in.waiting = 1;
    step();
    CHECK(audio_playing(), "the arrival cue is still sounding");

    tap_the_notice(true);
    step();
    CHECK(w.st.pending != UI_PEND_NONE || w.play_next > 0,
          "a press landing during the notification beep is acted on");
    run_ms(400);
    step();
    CHECK(w.play_next == 1, "and the message plays once the speaker is free");
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
    tap_the_notice(true);
    step();
    CHECK(w.ov.run, "the controls are up on the frame of the press");
    CHECK(w.ov.run_playing, "showing pause, because sound is coming");
    CHECK(w.ov.exit_corner, "and the way out is drawn with them");
    CHECK(drawn_near(276, 112, 40), "the exit is really on the glass, top right");
    CHECK(!w.ov.pair, "and the ended state is not");
}

static void test_the_top_right_corner_ends_a_message_while_it_plays(void)
{
    /* The owner asked for exactly this and reported it did not work: *"when it does play and I
       want to exit it, I should be able to click on the top right where there's no icon and have
       it exit out."* Two things were wrong. The press went down the short table because a message
       is audible, and the exit was not IN the short table — and `target_hit(TRANSPORT)` is
       unconditional, so it swallowed the corner. The exit is now first in that table. */
    world_reset();
    play_a_message(false);
    CHECK(w.ov.run && w.ov.exit_corner, "the exit is drawn while the message plays");
    CHECK(drawn_near(276, 112, 40), "and a finger has something to aim at");
    /* Past the grace first: the corner is drawn from the start but refuses for `EXIT_GRACE_MS`,
       because the press that STARTS a message lands where cancelling it will be — see
       `test_a_second_press_cannot_cancel_what_the_first_one_started`. This test is about the
       deliberate press that follows. */
    run_ms(EXIT_GRACE_MS + 100);

    const int stops = w.stop;
    tap_at(276, 112); /* squarely on the drawn exit disc */
    step();
    CHECK(w.stop > stops, "pressing the drawn exit mid-message ends it");
    CHECK(!w.stream, "the message stops with it");

    /* Once it has finished, the pair comes up and the same corner works. The wait is not
       padding: since 0.3.32 the exit plays `CUE_STOP`, and the pair arms on a speaker that is
       free — so the corner's own acknowledgement holds it off for the length of that tone. */
    finish_message();
    run_ms(400);
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
    /* `CUE_REPLY` since 0.3.32, not `CUE_LISTEN`: the same disc must sound the same whether the
       message is still playing or has ended, and here the microphone opens at once so the press
       and the opening are one event. The deferred path keeps `CUE_LISTEN` for the later moment
       it really opens. */
    CHECK(last_cue() == CUE_REPLY, "and it says so in the sound that means your turn");
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

static void test_pressing_replay_twice_does_not_pause_what_never_started(void)
{
    /* The owner, on 0.3.35: *"it played through once and has stopped and has the play button
       again, but when we click the play button sometimes it just pauses ... usually just on the
       first time."*

       WHAT THE FINGER IS ON CHANGES UNDER IT. When a message ends the pair comes up and the
       arbitration uses the IDLE table, where that left disc is `UI_TARGET_PAIR` — replay.
       Pressing it arms `UI_PEND_REPLAY`, which makes `ui_run_controls_up` true, which swaps the
       table to `UI_TAP_ORDER_PLAYING` — where the same disc in the same place is now the
       TRANSPORT. A replay waits out its own cue before any sound, so for those few hundred
       milliseconds nothing has happened; a child presses again and the second press pauses a
       message that never started. "Usually just the first time" is the press that flips the
       table. */
    world_reset();
    play_a_message(true);
    /* The message ends and the pair comes up. Set directly, as every other test here does: the
       `JPANEL_PLAYING` -> ended transition that arms it lives in `display.c`, not in this half. */
    /* Through the world's own fields: `step()` re-derives `in` from them every frame, so setting
       `w.in.*` here would be undone before the arbitration ever sees it. */
    w.stream = false;
    w.fetching = false;
    w.in.jrunning = false;
    w.st.pending = UI_PEND_NONE;
    w.st.repeat_until = w.in.now + REPEAT_MS;
    step();
    CHECK(w.st.repeat_until != 0, "the again/reply pair is up");
    CHECK(!ui_run_controls_up(&w.st, &w.in), "with nothing running, so the IDLE table arbitrates");

    /* First press on the left disc: replay. */
    tap_at(CONFIRM_CX_CANCEL, confirm_cy(FACE_H) + 10);
    step();
    CHECK(w.st.pending == UI_PEND_REPLAY, "the first press asks for a replay");

    /* THE TABLE HAS NOW FLIPPED. The same disc is the transport, and an impatient second press
       lands on it before a single byte has been fetched. */
    CHECK(ui_tap_target(&w.st, &w.in) == UI_TARGET_TRANSPORT,
          "the same disc is the transport once a replay is pending — this is the trap");
    const int paused_before = w.pause_on;
    tap_at(CONFIRM_CX_CANCEL, confirm_cy(FACE_H) + 10);
    step();
    CHECK(w.pause_on == paused_before,
          "and the second press must not pause a replay that has not started");
    CHECK(w.st.pending == UI_PEND_REPLAY, "the replay is still on its way");

    /* AND THE BUTTON IS NOT DEAD — the refusal lasts exactly as long as the pending, not a
       clock. Once the audio is actually sounding, pause works on the very next press. */
    w.st.pending = UI_PEND_NONE;
    w.stream = true; /* through the world, for the reason above */
    step();
    tap_at(CONFIRM_CX_CANCEL, confirm_cy(FACE_H) + 10);
    step();
    CHECK(w.pause_on > paused_before, "a press once it IS sounding still pauses");
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

static void test_the_notice_stays_painted_while_a_cue_sounds(void)
{
    /* THE TARGET SURVIVED A NOISE AND THE PICTURE DID NOT, which was half a fix. The rectangle
       was moved out from behind the idle guard because tying it to the painting is what made a
       notice intermittently unpressable — but the PAINTING stayed behind a guard that included
       `!speaking`, so for the length of the arrival cue the notice was armed and invisible, and
       it vanished again for every burp and button beep after that. A control that blinks off
       while it still works is the same fault wearing the other face. Both halves read
       `stream_active` now. */
    world_reset();
    w.in.waiting = 1;
    step();
    CHECK(w.ov.popup_big, "the notice is painted on the frame it arrives");
    CHECK(audio_playing(), "with the arrival cue now sounding");

    step();
    CHECK(w.ov.popup_big, "and it is still painted a frame later, mid-cue");
    CHECK(w.st.popup_box[0] >= 0, "with its target armed to match");

    run_ms(400);
    CHECK(w.ov.popup_big, "the picture is there when the panel goes quiet");
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
    test_a_cue_no_longer_decides_what_a_press_means();
    test_the_exit_ends_a_message_while_it_is_audible();
    test_the_notice_is_painted_through_its_own_arrival_cue();
    test_a_hold_after_a_poke_still_opens_the_menu();
    test_the_controls_are_pressable_for_as_long_as_they_are_drawn();
    test_a_second_press_cannot_cancel_what_the_first_one_started();
    test_the_face_follows_the_message_that_is_playing();
    test_a_notice_does_not_show_through_the_playback_screen();
    test_a_swipe_changes_which_waiting_message_is_offered();
    test_one_waiting_message_cannot_be_swiped_away();
    test_a_swipe_stops_the_message_it_moves_off();
    test_a_swipe_is_not_also_a_hold();
    test_a_long_press_on_a_menu_is_just_a_press();
    test_the_selected_message_is_the_one_that_plays();
    test_the_numeral_says_which_of_how_many();
    test_a_new_message_sends_the_selection_home();
    test_every_playback_control_answers_the_finger();
    test_the_draw_layers_are_a_list();

    test_a_notification_arrives_and_a_tap_plays_it();
    test_a_press_does_not_evaporate_behind_its_own_cue();
    test_a_press_during_the_arrival_cue_plays_the_message();
    test_the_run_controls_appear_from_the_press_not_the_stream();
    test_the_top_right_corner_ends_a_message_while_it_plays();
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
    test_pressing_replay_twice_does_not_pause_what_never_started();
    test_a_held_stream_resumes_rather_than_being_thrown_away();
    test_the_notice_shrinks_to_a_badge_and_a_second_one_does_not_restore_it();
    test_the_notice_stays_painted_while_a_cue_sounds();
    test_the_queue_count_only_sounds_on_the_way_up();
    test_what_the_box_said_about_a_message_is_answered_in_sound();
    test_a_replay_still_reads_as_playing();
    test_the_action_list_never_overflows();

    free(w.fb);
    printf("ok — %d checks (ui)\n", checks);
    return 0;
}
