/* THE PANEL'S STATE MACHINE, DRIVEN ON A HOST — one frame at a time, with no panel (plan §8.5).
 *
 * `step()` is one render pass: sample the facts into `sm_in_t`, `sm_step`, assert the invariants
 * and the drawn face, run the effects against the fakes, advance the clock, let the fake engine
 * move. The fakes are thin but keep the timing the bugs live in: a cue holds the speaker for a
 * while, a command is claimed and begins a frame or two after it is posted, a stream keeps its
 * {stream_id, out_ms} after it drains.
 *
 * Part one ports every `ui_tests.c` test (plan Appendix A) under its old name, with the stated U/R
 * disposition. Part two is the machine's own: the journeys of §8.5 and §9, the frame-boundary
 * rules, and `test_storm`. */

#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "audio.h"
#include "confirm.h"
#include "cue.h"
#include "face.h"
#include "gesture.h"
#include "panel_sm.h"

static int checks;
#define CHECK(c, msg)                                                       \
    do {                                                                    \
        checks++;                                                           \
        if (!(c)) {                                                         \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, (msg)); \
            exit(1);                                                        \
        }                                                                   \
    } while (0)

/* ── THE WORLD ────────────────────────────────────────────────────────────────────────────── */

#define DT 80          /* a measured render pass, not the nominal 40 */
#define FAKE_CUE_MS 120

/* Where things are on an upright portrait panel (`confirm.c`, `sm_notice_box`). */
#define CENTRE_X (FACE_W / 2)
#define CENTRE_Y (FACE_H / 2)
#define EXIT_X 276
#define EXIT_Y 112
#define FACE_X (FACE_W / 4)
#define FACE_Y (FACE_H / 4)
#define DISC_Y (FACE_H - CONFIRM_MARGIN - CONFIRM_R + 10)
#define LEFT_X CONFIRM_CX_CANCEL
#define RIGHT_X CONFIRM_CX_SEND

typedef struct {
    sm_t sm;
    sm_in_t in;
    sm_out_t out;
    uint32_t now;
    int over_y0, over_h;

    /* the engine's published snapshot, and the fake engine behind it */
    jp_view_t snap;
    bool engine;          /* false: the test writes the snapshot by hand */
    jp_cmd_t cmd;
    bool cmd_waiting, claimed, begun, finished;
    uint32_t cmd_since, claimed_at;
    uint32_t claim_ms, fetch_ms;
    bool chain;           /* a non-replay run continues through what waits */
    bool send_waiting;
    jp_cmd_t send_cmd;
    uint32_t send_since, send_ms;
    jp_send_kind_t send_kind;
    bool post_fail;
    bool defer_stop;      /* a STOP cuts the sound now; its outcome waits for release_stop() */
    bool stop_pending, stop_pending_heard;

    /* the fake speaker */
    uint32_t cue_until;
    bool reply;
    uint8_t sid;
    bool sounding, paused;
    uint32_t out_ms;

    /* the fake microphone, talk and lease */
    bool capture_open, refuse_capture, refused_next;
    int capture_ms;
    bool hearing, leased, talk_ok;
    sm_net_t net;
    uint32_t post_failed_next;

    /* edges for the next frame */
    bool down, up, btn_short, btn_hold, wake, dark_wake, calibrating;
    int x, y, swipe, gest_taps;
    sm_voice_t voice;
    bool finger; /* still on the glass */

    /* everything the machine asked for */
    int cue[512], cue_slot[512], ncue;
    sm_fx_t log[4096];
    int nlog;
    int silence, stop, stop_heard, pause_on, pause_off, opened, dropped, send_talk, talk_clear,
        poll_soon, standby_on, standby_off, blank, deep_sleep, nod, flinch, pet_press, tap_missed;
    jp_cmd_t posts[256];
    int nposts;
    bool muted;
    bool standby;
    int violations;
    bool allow_violations;
    const jp_view_t *delivered; /* the storm's delayed copy; NULL = the engine's current one */
    uint32_t msg_len;           /* the storm's natural end */
} world_t;

static world_t w;

static int last_cue(void)
{
    return w.ncue > 0 ? w.cue[w.ncue - 1] : -1;
}

static bool saw_cue(int c)
{
    for (int i = 0; i < w.ncue; i++) {
        if (w.cue[i] == c) return true;
    }
    return false;
}

static int count_cue(int c)
{
    int n = 0;
    for (int i = 0; i < w.ncue; i++) n += w.cue[i] == c;
    return n;
}

static int sends(void)
{
    int n = 0;
    for (int i = 0; i < w.nposts; i++) n += w.posts[i].kind == JP_CMD_SEND;
    return n;
}

static int plays(void)
{
    int n = 0;
    for (int i = 0; i < w.nposts; i++) n += w.posts[i].kind != JP_CMD_SEND;
    return n;
}

static const jp_cmd_t *last_post(void)
{
    return w.nposts > 0 ? &w.posts[w.nposts - 1] : NULL;
}

static bool has_fx(sm_fx_kind_t k)
{
    for (int i = 0; i < w.out.n; i++) {
        if (w.out.fx[i].kind == k) return true;
    }
    return false;
}

static int fx_index(sm_fx_kind_t k, int arg)
{
    for (int i = 0; i < w.out.n; i++) {
        if (w.out.fx[i].kind == k && (arg < 0 || w.out.fx[i].arg == arg)) return i;
    }
    return -1;
}

static void world_reset(void)
{
    memset(&w, 0, sizeof(w));
    w.now = 1000; /* not 0: 0 is the never-been value in several places */
    w.over_y0 = 0;
    w.over_h = FACE_H;
    w.engine = true;
    w.claim_ms = 0;
    w.fetch_ms = 160;
    w.send_ms = 2000;
    w.send_kind = JP_SEND_SENT;
    w.talk_ok = true;
    w.snap.waiting.has_ids = true;
    strcpy(w.snap.waiting.sibling, "Tamsin");
    sm_init(&w.sm, w.now);
}

/* ── the box's queue ──────────────────────────────────────────────────────────────────────── */

static jp_who_t who(const char *id, bool dad)
{
    jp_who_t v;
    memset(&v, 0, sizeof(v));
    strcpy(v.id, id);
    strcpy(v.from, dad ? "Dad" : "Tamsin");
    v.from_dad = dad;
    return v;
}

static int box_find(const char *id)
{
    for (int i = 0; i < w.snap.waiting.n; i++) {
        if (strcmp(w.snap.waiting.q[i].id, id) == 0) return i;
    }
    return -1;
}

static void box_insert(int at, jp_who_t v)
{
    jp_waiting_t *q = &w.snap.waiting;
    if (q->n < JPANEL_QUEUE_MAX) {
        for (int i = q->n; i > at; i--) q->q[i] = q->q[i - 1];
        q->q[at] = v;
        q->n++;
    }
    q->count++;
}

static void box_add(const char *id, bool dad)
{
    box_insert(w.snap.waiting.n, who(id, dad));
}

static void box_remove(const char *id)
{
    jp_waiting_t *q = &w.snap.waiting;
    const int at = box_find(id);
    if (at < 0) return;
    for (int i = at; i < q->n - 1; i++) q->q[i] = q->q[i + 1];
    q->n--;
    q->count--;
    memset(&q->q[q->n], 0, sizeof(q->q[0]));
}

/* `true` for a message from Dad, oldest first, ids m1, m2, … */
static void queue_is(int n, const bool *dad)
{
    memset(w.snap.waiting.q, 0, sizeof(w.snap.waiting.q));
    w.snap.waiting.n = 0;
    w.snap.waiting.count = 0;
    char id[16];
    for (int i = 0; i < n; i++) {
        snprintf(id, sizeof(id), "m%d", i + 1);
        box_add(id, dad[i]);
    }
}

/* An old box: a count and the head's sender, no ids. */
static void old_box(int count, bool head_dad)
{
    memset(&w.snap.waiting, 0, sizeof(w.snap.waiting));
    w.snap.waiting.count = count;
    w.snap.waiting.n = count > 0 ? 1 : 0;
    if (count > 0) {
        w.snap.waiting.q[0].from_dad = head_dad;
        strcpy(w.snap.waiting.q[0].from, head_dad ? "Dad" : "Tamsin");
    }
}

/* ── the fake engine ──────────────────────────────────────────────────────────────────────── */

static void publish_done(uint32_t seq)
{
    if (!jp_seq_ge(w.snap.done_seq, seq)) w.snap.done_seq = seq;
}

static void engine_claim(void)
{
    jp_view_t *s = &w.snap;
    w.cmd_waiting = false;
    if (jp_due(w.now, w.cmd.expires_at - JP_UI_MARGIN_MS)) { /* expired before claim */
        s->play.seq = w.cmd.seq;
        s->play.kind = JP_PLAY_CANCELLED;
        s->play.replay = w.cmd.kind == JP_CMD_REPLAY;
        publish_done(w.cmd.seq);
        return;
    }
    w.claimed = true;
    w.begun = false;
    w.finished = false;
    w.claimed_at = w.now;
    s->claimed_seq = w.cmd.seq;
    s->phase = JP_FETCHING;
    s->deadline_ms = w.now + 10000;
    memset(&s->cur, 0, sizeof(s->cur));
    s->cur.seq = w.cmd.seq;
    s->cur.replay = w.cmd.kind == JP_CMD_REPLAY;
    if (s->cur.replay) {
        s->cur.who = w.cmd.target;
        s->cur.pos = -1;
        return;
    }
    int at = s->waiting.has_ids ? box_find(w.cmd.target.id) : w.cmd.at;
    if (at < 0) {
        /* 204: nothing to play for that id */
        w.claimed = false;
        s->play.seq = w.cmd.seq;
        s->play.kind = JP_PLAY_EMPTY;
        s->phase = JP_IDLE;
        s->deadline_ms = 0;
        publish_done(w.cmd.seq);
        return;
    }
    s->cur.who = s->waiting.has_ids ? s->waiting.q[at] : w.cmd.target;
    s->cur.pos = (int8_t)at;
    s->cur.in_waiting = true;
}

static void engine_begin(void)
{
    jp_view_t *s = &w.snap;
    w.begun = true;
    w.sid = (uint8_t)(w.sid == 255 ? 1 : w.sid + 1);
    s->cur.stream_id = w.sid;
    s->phase = JP_SOUNDING;
    if (!s->cur.replay) {
        if (s->waiting.has_ids) {
            box_remove(s->cur.who.id);
        } else if (s->waiting.count > 0) {
            s->waiting.count--;
        }
        s->cur.in_waiting = false;
        s->last_begun = s->cur.who;
    }
    w.sounding = true;
    w.paused = false;
    w.out_ms = 0;
}

/* The current message ends by itself. In a chained run the next waiting one is seeded at once. */
static void engine_end(jp_play_kind_t kind)
{
    jp_view_t *s = &w.snap;
    w.sounding = false;
    s->play.seq = w.cmd.seq;
    s->play.who = s->cur.who;
    s->play.kind = kind;
    s->play.replay = s->cur.replay;
    if (w.chain && !s->cur.replay && kind == JP_PLAY_HEARD && s->waiting.n > 0) {
        s->cur.run_continues = true;
        jp_who_t next = s->waiting.q[0];
        s->cur.who = next;
        s->cur.stream_id = 0;
        s->cur.pos = 0;
        s->cur.in_waiting = true;
        s->phase = JP_FETCHING;
        s->deadline_ms = w.now + 10000;
        w.begun = false;
        w.claimed_at = w.now;
        return;
    }
    s->cur.run_continues = false;
    w.claimed = false;
    w.finished = true;
    s->phase = JP_IDLE;
    s->deadline_ms = 0;
    publish_done(w.cmd.seq);
}

static void engine_stop(bool heard)
{
    jp_view_t *s = &w.snap;
    if (w.claimed) {
        w.sounding = false;
        w.claimed = false;
        s->play.seq = w.cmd.seq;
        s->play.who = s->cur.who;
        s->play.replay = s->cur.replay;
        if (!w.begun) {
            s->play.kind = JP_PLAY_CANCELLED;
        } else if (heard || w.out_ms >= 3000 || s->cur.replay) {
            s->play.kind = JP_PLAY_STOPPED_HEARD;
        } else {
            s->play.kind = JP_PLAY_RETURNED;
            if (s->waiting.has_ids && box_find(s->cur.who.id) < 0) {
                box_insert(s->cur.pos < s->waiting.n ? s->cur.pos : s->waiting.n, s->cur.who);
            }
        }
        s->cur.run_continues = false;
        s->phase = JP_IDLE;
        s->deadline_ms = 0;
        publish_done(w.cmd.seq);
    } else if (w.cmd_waiting) {
        w.cmd_waiting = false;
        s->play.seq = w.cmd.seq;
        s->play.kind = JP_PLAY_CANCELLED;
        publish_done(w.cmd.seq);
    }
}

static void engine_tick(void)
{
    if (!w.engine) return;
    jp_view_t *s = &w.snap;
    if (w.cmd_waiting && jp_due(w.now, w.cmd_since + w.claim_ms)) engine_claim();
    if (w.claimed && !w.begun && jp_due(w.now, w.claimed_at + w.fetch_ms)) engine_begin();
    if (w.claimed) s->deadline_ms = w.now + 22000; /* refreshed while bytes or out_ms move */
    if (w.sounding && !w.paused) w.out_ms += DT;
    if (w.send_waiting && jp_due(w.now, w.send_since + w.send_ms)) {
        w.send_waiting = false;
        s->send.seq = w.send_cmd.seq;
        s->send.kind = w.send_kind;
        publish_done(w.send_cmd.seq);
        w.leased = false;
        s->phase = JP_IDLE;
    }
}

/* ── the executor ─────────────────────────────────────────────────────────────────────────── */

static void perform(void)
{
    for (int i = 0; i < w.out.n; i++) {
        const sm_fx_t *f = &w.out.fx[i];
        if (w.nlog < (int)(sizeof(w.log) / sizeof(w.log[0]))) w.log[w.nlog++] = *f;
        switch (f->kind) {
        case SM_FX_SILENCE:
            w.silence++;
            w.cue_until = 0;
            w.reply = false;
            break;
        case SM_FX_CUE:
            if (w.ncue < 512) {
                w.cue_slot[w.ncue] = f->slot;
                w.cue[w.ncue++] = f->arg;
            }
            w.cue_until = w.now + FAKE_CUE_MS;
            break;
        case SM_FX_STOP:
            w.stop++;
            if (f->arg) w.stop_heard++;
            if (w.defer_stop && (w.claimed || w.cmd_waiting)) {
                w.stop_pending = true;
                w.stop_pending_heard = f->arg != 0;
                w.sounding = false;
            } else {
                engine_stop(f->arg != 0);
            }
            break;
        case SM_FX_PAUSE:
            if (f->arg) {
                w.pause_on++;
                w.paused = true;
            } else {
                w.pause_off++;
                w.paused = false;
            }
            break;
        case SM_FX_CAPTURE_OPEN:
            w.opened++;
            if (w.refuse_capture || w.leased) {
                w.refused_next = true;
            } else {
                w.capture_open = true;
                w.capture_ms = 0;
            }
            break;
        case SM_FX_CAPTURE_DROP:
            w.dropped++;
            w.capture_open = false;
            break;
        case SM_FX_POST: {
            const jp_cmd_t *c = &w.out.post;
            if (w.nposts < 256) w.posts[w.nposts++] = *c;
            if (w.post_fail) {
                w.post_failed_next = c->seq;
                break;
            }
            if (c->kind == JP_CMD_SEND) {
                w.leased = true;
                w.capture_open = false;
                w.send_cmd = *c;
                w.send_waiting = true;
                w.send_since = w.now;
                w.snap.phase = JP_SENDING;
            } else {
                w.cmd = *c;
                w.cmd_waiting = true;
                w.cmd_since = w.now;
            }
            break;
        }
        case SM_FX_SEND_TALK:
            w.send_talk++;
            w.capture_open = false;
            if (w.talk_ok) w.net = SM_NET_BUSY;
            break;
        case SM_FX_TALK_CLEAR:
            w.talk_clear++;
            if (w.net != SM_NET_BUSY) w.net = SM_NET_IDLE;
            break;
        case SM_FX_POLL_SOON:
            w.poll_soon++;
            break;
        case SM_FX_STANDBY:
            if (f->arg) {
                w.standby_on++;
                w.standby = true;
            } else {
                w.standby_off++;
                w.standby = false;
            }
            break;
        case SM_FX_BLANK:
            w.blank++;
            break;
        case SM_FX_DEEP_SLEEP:
            w.deep_sleep++;
            break;
        case SM_FX_NOD:
            w.nod++;
            break;
        case SM_FX_FLINCH:
            w.flinch++;
            break;
        case SM_FX_MUTE_COMMANDS:
            w.muted = f->arg != 0;
            break;
        }
    }
    w.pet_press += w.out.pet_press;
    w.tap_missed += w.out.tap_missed;
}

static void sample(void)
{
    sm_in_t *in = &w.in;
    memset(in, 0, sizeof(*in));
    in->now = w.now;
    in->snap = w.delivered != NULL ? *w.delivered : w.snap;
    in->post_failed = w.post_failed_next;
    in->capture_refused = w.refused_next;
    const bool cue = !jp_due(w.now, w.cue_until);
    in->audio.stream_id = w.sid;
    in->audio.out_ms = w.out_ms;
    in->audio.stream_live = w.sounding;
    in->audio.reply_busy = w.reply;
    in->audio.clip_busy = cue || w.reply;
    in->audio.speaker_busy = cue || w.reply || w.sounding;
    in->audio.arbiter = true;
    in->down = w.down;
    in->up = w.up;
    in->x = w.x;
    in->y = w.y;
    in->on_pet = w.x >= TALK_MARGIN_PX && w.x < FACE_W - TALK_MARGIN_PX && w.y >= TALK_MARGIN_PX &&
                 w.y < FACE_H - TALK_MARGIN_PX;
    in->gest_taps = w.gest_taps;
    in->swipe = w.swipe;
    in->over_y0 = w.over_y0;
    in->over_h = w.over_h;
    in->voice = w.voice;
    in->btn_short = w.btn_short;
    in->btn_hold = w.btn_hold;
    in->wake = w.wake;
    in->hearing = w.hearing;
    in->capture_ms = w.capture_ms;
    in->leased = w.leased;
    in->net = w.net;
    in->speech_ok = true;
    in->calibrating = w.calibrating;
    in->dark_wake = w.dark_wake;
    in->sound = true;
}

static void face_matches(void)
{
    jp_who_t f;
    const bool has = sm_face(&w.sm, &w.in.snap, &f);
    CHECK(has == !w.out.view.face_none, "I2: the drawn face is sm_face, every frame");
    CHECK(!has || memcmp(&f, &w.out.view.face, sizeof(f)) == 0, "I2: and the same person");
}

static void step(void)
{
    sample();
    sm_step(&w.sm, &w.in, &w.out);
    w.violations += w.out.violations != 0;
    if (!w.allow_violations) {
        CHECK(w.out.violations == 0, "no invariant is broken by a step");
        CHECK(sm_check(&w.sm, &w.in) == 0, "and the state after it holds I1-I7");
    }
    CHECK(w.out.n < SM_FX_MAX, "a frame never fills the effect list");
    face_matches();
    w.post_failed_next = JP_SEQ_NONE; /* consumed by this frame's sample */
    w.refused_next = false;
    perform();
    w.down = w.up = w.btn_short = w.btn_hold = w.wake = w.dark_wake = false;
    w.swipe = 0;
    w.voice = SM_VOICE_NONE;
    w.now += DT;
    engine_tick();
    if (w.capture_open) w.capture_ms += DT;
}

static void run_ms(uint32_t ms)
{
    const uint32_t until = w.now + ms;
    while (!jp_due(w.now, until)) step();
}

/* A press that is over within the frame: the down edge carries the point. */
static void tap_at(int x, int y)
{
    w.down = w.up = true;
    w.x = x;
    w.y = y;
    step();
}

/* A finger that lands and stays. */
static void press_at(int x, int y)
{
    w.down = true;
    w.finger = true;
    w.x = x;
    w.y = y;
    step();
}

static void lift(void)
{
    w.up = true;
    w.finger = false;
    step();
}

/* One gesture: a finger lands where nothing is (the old harness's swipe carried no point),
   travels for a frame, is recognised as a swipe, and lifts. */
static void swipe(int dir)
{
    w.down = true;
    w.x = -1;
    w.y = -1;
    step();
    w.swipe = dir;
    step();
    w.up = true;
    step();
}

static void say(sm_voice_t v)
{
    w.voice = v;
    step();
}

static sm_target_t target_at(int x, int y)
{
    sample();
    return sm_target(&w.sm, &w.in, x, y);
}

static void notice_centre(int *x, int *y)
{
    int b[4];
    sm_notice_box(w.over_y0, w.over_h, w.out.view.notice_big, b);
    *x = (b[0] + b[2]) / 2;
    *y = (b[1] + b[3]) / 2;
}

static void tap_the_notice(void)
{
    int x, y;
    notice_centre(&x, &y);
    tap_at(x, y);
}

/* Audible: claimed, begun, and the speaker reporting it. */
static bool is_on(void)
{
    return w.sm.mode == SM_PLAY && w.sm.play.seq != JP_SEQ_NONE && w.claimed && w.begun &&
           w.sounding && w.out_ms > 0;
}

static void until_on(void)
{
    for (int i = 0; i < 100 && !is_on(); i++) step();
    CHECK(is_on(), "the message became audible");
    step(); /* and the machine has seen it */
}

static void arrive(const char *id, bool dad)
{
    box_add(id, dad);
    step();
}

/* Notice, press, fetch, audible. */
static void play_a_message(bool dad)
{
    arrive("m1", dad);
    run_ms(400);
    tap_the_notice();
    until_on();
}

/* A message played whole, leaving the again/reply pair. */
static void offer_from(bool dad)
{
    play_a_message(dad);
    engine_end(JP_PLAY_HEARD);
    step();
    run_ms(400);
    CHECK(w.out.view.offer, "the pair is up");
}

static void hear_then_hush(void)
{
    w.hearing = true;
    run_ms(400);
    w.hearing = false;
    run_ms(LISTEN_HUSH_MS + 400);
}

/* A question to the pet, up and waiting for an answer. */
static void ask_pet(void)
{
    say(SM_VOICE_WAKE);
    CHECK(w.sm.mode == SM_LISTEN, "the name opens a turn");
    hear_then_hush();
    CHECK(w.sm.turn.state == SM_TURN_THINKING, "and the question went up");
}

/* Every target reachable on the glass, as a bitmask, sampled on an 8 px grid over the band. */
static uint32_t targets_mask(void)
{
    sample();
    uint32_t m = 0;
    for (int y = w.over_y0; y < w.over_h; y += 8) {
        for (int x = 0; x < FACE_W; x += 8) m |= 1u << sm_target(&w.sm, &w.in, x, y);
    }
    return m;
}

#define T(t) (1u << (t))

/* ══ PART ONE: every ui_tests.c test, ported (Appendix A) ═══════════════════════════════════ */

static void test_the_order_is_a_list_and_ends_with_the_pet(void)
{
    /* R: each mode's table, literally. Every press reaches something or is logged. */
    world_reset();
    step();
    CHECK(targets_mask() == T(SM_T_PET), "bare PET: the pet catches every press");

    arrive("m1", true);
    run_ms(400);
    CHECK(targets_mask() == (T(SM_T_NOTICE) | T(SM_T_PET)), "PET with a notice: the notice, the pet");

    world_reset();
    offer_from(false);
    CHECK(targets_mask() == (T(SM_T_EXIT) | T(SM_T_AGAIN) | T(SM_T_REPLY) | T(SM_T_PET)),
          "a fresh offer: the exit, again, reply, the pet");
    run_ms(REPEAT_MS);
    CHECK(targets_mask() == (T(SM_T_EXIT) | T(SM_T_AGAIN) | T(SM_T_REPLY) | T(SM_T_PET)),
          "a standing offer: the same four, by disc");

    world_reset();
    press_at(CENTRE_X, CENTRE_Y);
    run_ms(HOLD_TALK_MS + 100);
    lift();
    run_ms(EXIT_GRACE_MS);
    CHECK(targets_mask() == (T(SM_T_CELL_SISTER) | T(SM_T_CELL_DAD) | T(SM_T_CELL_CANCEL) |
                             T(SM_T_CELL_PET)),
          "GRID: four cells and nowhere to miss upright");

    world_reset();
    say(SM_VOICE_SEND_DAD);
    CHECK(targets_mask() == (T(SM_T_CROSS) | T(SM_T_TICK) | T(SM_T_IGNORED)),
          "RECORD: the cross, the tick, and an ignored miss");

    world_reset();
    const bool two[2] = {true, false};
    queue_is(2, two);
    step();
    run_ms(400);
    tap_the_notice();
    until_on();
    run_ms(EXIT_GRACE_MS);
    CHECK(targets_mask() == (T(SM_T_EXIT) | T(SM_T_FACE) | T(SM_T_PAUSE) | T(SM_T_SWALLOW)),
          "PLAY: the exit, the face and pause; no reply while it talks");
    tap_at(LEFT_X, DISC_Y);
    CHECK(w.sm.play.pause_on, "paused");
    run_ms(EXIT_GRACE_MS);
    CHECK(targets_mask() == (T(SM_T_EXIT) | T(SM_T_FACE) | T(SM_T_PAUSE) | T(SM_T_REPLY)),
          "PLAY, paused: and now reply");
    world_reset();
    play_a_message(true);
    run_ms(EXIT_GRACE_MS);
    CHECK(targets_mask() == (T(SM_T_EXIT) | T(SM_T_SWALLOW) | T(SM_T_PAUSE)),
          "PLAY with nothing to cycle to: the face quadrant swallows");
}

static void test_the_grid_is_modal_and_outranks_everything(void)
{
    /* R: in GRID every point is a cell or a swallow, with a notice waiting and an offer kept. */
    world_reset();
    offer_from(true);
    arrive("m2", false);
    run_ms(POPUP_BIG_MS + 400); /* a badge, so the pet centre is the pet */
    CHECK(w.sm.offer.up && !w.out.view.offer, "the offer is kept, hidden behind the notice");
    press_at(CENTRE_X, CENTRE_Y);
    run_ms(HOLD_TALK_MS + 100);
    CHECK(w.sm.mode == SM_GRID, "the hold opened the menu");
    lift();
    run_ms(EXIT_GRACE_MS);
    const int xs[5] = {10, FACE_W - 10, 10, FACE_W - 10, FACE_W / 2};
    const int ys[5] = {10, 10, FACE_H - 10, FACE_H - 10, FACE_H / 2};
    for (int i = 0; i < 5; i++) {
        const sm_target_t t = target_at(xs[i], ys[i]);
        CHECK(t == SM_T_CELL_SISTER || t == SM_T_CELL_DAD || t == SM_T_CELL_CANCEL ||
                  t == SM_T_CELL_PET || t == SM_T_SWALLOW,
              "the grid takes every press while it is up");
    }
    /* A run cannot coexist with GRID; a hold during PLAY is swallowed. */
    world_reset();
    play_a_message(false);
    press_at(FACE_X, FACE_Y);
    run_ms(HOLD_TALK_MS + 300);
    CHECK(w.sm.mode == SM_PLAY, "a hold during playback opens no menu");
}

static void test_the_notice_is_tappable_only_in_one_quadrant_today(void)
{
    world_reset();
    arrive("m1", true);
    run_ms(400);
    CHECK(w.out.view.notice_big, "the big notice is up");
    int b[4];
    sm_notice_box(0, FACE_H, true, b);
    CHECK(b[0] == 36 && b[1] == 117 && b[2] == 332 && b[3] == 331,
          "296x214 centred on the 368x448 frame");
    CHECK(target_at(120, 150) == SM_T_NOTICE, "the left of the notice plays it");
    CHECK(target_at(250, 150) == SM_T_NOTICE, "and the right of it too");
    CHECK(target_at(10, 20) != SM_T_NOTICE, "while bare black above the box is not a notice press");

    /* A standing offer does not swallow a press aimed at a new notice. */
    world_reset();
    offer_from(false);
    run_ms(REPEAT_MS);
    arrive("m2", true);
    run_ms(400);
    CHECK(target_at(250, 150) == SM_T_NOTICE, "the notice outranks the hidden pair's exit");
    CHECK(w.sm.offer.up, "and the pair it did not claim is still kept");

    /* The exit returns once a message actually runs. */
    tap_at(250, 150);
    until_on();
    run_ms(EXIT_GRACE_MS);
    CHECK(target_at(EXIT_X, EXIT_Y) == SM_T_EXIT, "the corner is the exit once it plays");
}

static void test_a_cue_no_longer_decides_what_a_press_means(void)
{
    /* R: a cue does not take the table away, in PET, GRID or PLAY. */
    world_reset();
    arrive("m1", true);
    run_ms(400);
    const uint32_t quiet = targets_mask();
    w.cue_until = w.now + 1000;
    CHECK(targets_mask() == quiet, "the notice's table is the same during a beep");
    w.cue_until = 0;

    world_reset();
    press_at(CENTRE_X, CENTRE_Y);
    run_ms(HOLD_TALK_MS + 100);
    lift();
    run_ms(EXIT_GRACE_MS);
    const uint32_t grid = targets_mask();
    w.cue_until = w.now + 1000;
    CHECK(targets_mask() == grid, "and so is the grid's");
    w.cue_until = 0;

    world_reset();
    play_a_message(true);
    run_ms(EXIT_GRACE_MS);
    const uint32_t playing = targets_mask();
    w.cue_until = w.now + 1000;
    CHECK(targets_mask() == playing, "and PLAY's does not change while a cue sounds");
}

static void test_the_exit_ends_a_message_while_it_is_audible(void)
{
    world_reset();
    play_a_message(false);
    run_ms(EXIT_GRACE_MS);
    CHECK(target_at(EXIT_X, EXIT_Y) == SM_T_EXIT, "the corner is the exit while a message plays");
    tap_at(EXIT_X, EXIT_Y);
    CHECK(fx_index(SM_FX_STOP, -1) >= 0, "and it ends the run rather than poking the pet");
    CHECK(w.sm.mode == SM_PET, "back to the pet");
}

static void test_the_notice_is_painted_through_its_own_arrival_cue(void)
{
    world_reset();
    arrive("m1", true);
    CHECK(!jp_due(w.now, w.cue_until), "the arrival cue is sounding");
    CHECK(w.out.view.notice, "the notice is on the glass while its own cue sounds");
    CHECK(target_at(CENTRE_X, CENTRE_Y) == SM_T_NOTICE, "and the target is armed to match");
    tap_the_notice();
    CHECK(!w.out.view.notice, "but a playing message owns the screen");
}

static void test_a_hold_after_a_poke_still_opens_the_menu(void)
{
    world_reset();
    step();
    w.cue_until = w.now + 600; /* the poke's own CUE_TOGGLE, still sounding */
    press_at(CENTRE_X, CENTRE_Y);
    CHECK(w.sm.down.target == SM_T_PET, "the down edge itself landed on the pet");
    run_ms(HOLD_TALK_MS + 50);
    CHECK(w.sm.mode == SM_GRID, "the menu opens over the panel's own beep");
}

static void test_the_controls_are_pressable_for_as_long_as_they_are_drawn(void)
{
    /* R: from the press, the transport is drawn and claimed (ignored) until it is audible. */
    world_reset();
    w.claim_ms = 400;
    w.fetch_ms = 400;
    arrive("m1", true);
    run_ms(400);
    tap_the_notice();
    for (int i = 0; i < 30 && !is_on(); i++) {
        CHECK(w.out.view.transport && w.out.view.run_bar, "the controls are drawn from the press");
        CHECK(target_at(LEFT_X, DISC_Y) == SM_T_IGNORED,
              "and the drawn pause button is claimed, never the pet");
        step();
    }
    CHECK(is_on(), "through pending, waiting and claimed-not-ON");
    CHECK(target_at(LEFT_X, DISC_Y) == SM_T_PAUSE, "and live once audible");

    world_reset();
    w.fetch_ms = 400;
    offer_from(true);
    tap_at(LEFT_X, DISC_Y);
    CHECK(w.sm.play.replay, "again");
    CHECK(w.out.view.transport, "a replay is drawn from the press too");
    CHECK(target_at(LEFT_X, DISC_Y) == SM_T_IGNORED, "and claimed likewise");
}

static void test_a_second_press_cannot_cancel_what_the_first_one_started(void)
{
    world_reset();
    arrive("m1", true);
    run_ms(400);
    CHECK(target_at(250, 150) == SM_T_NOTICE, "the notice wins the first press");
    tap_at(250, 150);
    CHECK(w.sm.mode == SM_PLAY, "and the play is taken");
    CHECK(target_at(250, 150) != SM_T_EXIT, "a press in the same place cannot cancel it");

    world_reset();
    play_a_message(false);
    run_ms(EXIT_GRACE_MS + 100);
    CHECK(target_at(EXIT_X, EXIT_Y) == SM_T_EXIT, "but it is a grace period, not a dead corner");

    world_reset();
    play_a_message(false);
    CHECK(target_at(EXIT_X, EXIT_Y) != SM_T_EXIT, "and a press in the first moments is not the way out");
    /* every phase: pending, claimed, audible */
    world_reset();
    w.claim_ms = 160;
    arrive("m1", true);
    run_ms(400);
    tap_the_notice();
    for (int i = 0; i < 7; i++) {
        CHECK(target_at(EXIT_X, EXIT_Y) == SM_T_IGNORED, "the grace runs from the press");
        step();
    }
}

static void test_the_face_follows_the_message_that_is_playing(void)
{
    world_reset();
    w.chain = true;
    const bool dad[2] = {true, true};
    queue_is(2, dad);
    step();
    run_ms(400);
    tap_the_notice();
    CHECK(w.out.view.run_bar, "the controls are up");
    CHECK(w.out.view.face.from_dad, "and the press-to-play window shows Dad");
    until_on();
    CHECK(w.out.view.face.from_dad, "still Dad while it plays");
    engine_end(JP_PLAY_HEARD); /* the chain seeds the next one */
    step();
    CHECK(w.out.view.run_bar, "the controls stay up across the chain");
    CHECK(w.out.view.face.from_dad, "and the second message from Dad shows Dad");
    run_ms(400);
    engine_end(JP_PLAY_HEARD);
    step();
    CHECK(w.out.view.offer && w.out.view.face.from_dad, "and the ended state agrees");
}

static void test_a_notice_does_not_show_through_the_playback_screen(void)
{
    world_reset();
    w.chain = true;
    const bool dad[2] = {true, true};
    queue_is(2, dad);
    step();
    run_ms(400);
    tap_the_notice();
    CHECK(w.out.view.run_bar, "the press raised the playback controls");
    CHECK(!w.out.view.notice, "and the notice for the one behind it is not painted over them");
    CHECK(target_at(CENTRE_X, CENTRE_Y) != SM_T_NOTICE, "nor pressable");
    until_on();
    engine_end(JP_PLAY_HEARD);
    step();
    CHECK(w.out.view.run_bar && !w.out.view.notice, "and nothing shows through the chain's gap");
    /* The run ends with one still waiting: the gap stays in PLAY until then. */
    w.chain = false;
    until_on();
    box_add("m3", true);
    engine_end(JP_PLAY_HEARD);
    step();
    CHECK(w.out.view.notice, "and the notice returns the moment the run is over");
}

static void test_a_swipe_changes_which_waiting_message_is_offered(void)
{
    world_reset();
    const bool dad_then_sister[2] = {true, false};
    queue_is(2, dad_then_sister);
    step();
    run_ms(400);
    CHECK(w.out.view.notice_big, "the notice is up for two messages");
    CHECK(w.out.view.face.from_dad, "opening on the oldest, Dad's");
    CHECK(w.sm.sel.index == 0, "the selection starts at the oldest");
    swipe(+1);
    CHECK(w.sm.sel.index == 1, "a swipe right moves on to the next one");
    CHECK(w.out.view.sel_n == 2, "and the drawing is told which one");
    CHECK(!w.out.view.face.from_dad, "and the face follows it: the sister's");
    CHECK(last_cue() == CUE_HEARD, "and the panel acknowledges the finger");
    swipe(-1);
    CHECK(w.sm.sel.index == 0 && w.out.view.face.from_dad, "a swipe left comes back");
    const int before = w.ncue;
    swipe(-1);
    CHECK(w.sm.sel.index == 0, "the oldest is where it stops going back");
    CHECK(w.ncue == before, "silently, because an end is an end");
    swipe(+1);
    swipe(+1);
    CHECK(w.sm.sel.index == 1, "the newest is where it stops going forward");
}

static void test_one_waiting_message_cannot_be_swiped_away(void)
{
    world_reset();
    arrive("m1", true);
    run_ms(400);
    swipe(+1);
    CHECK(w.sm.sel.index == 0 && w.out.view.face.from_dad, "one message: a swipe changes nothing");
    CHECK(w.stop == 0, "and nothing was stopped");
    world_reset();
    step();
    swipe(+1);
    CHECK(w.sm.sel.index == 0 && w.stop == 0, "an empty queue: likewise");
}

static void test_a_swipe_stops_the_message_it_moves_off(void)
{
    /* R: the selected id is the entry of S after the playing one, whether or not the snapshot
       still lists the playing id. */
    for (int listed = 0; listed < 2; listed++) {
        world_reset();
        const bool two[2] = {true, false};
        queue_is(2, two);
        step();
        run_ms(400);
        tap_the_notice();
        if (listed) {
            w.fetch_ms = 100000; /* claimed, not begun: `cur` is still in `waiting` */
            for (int i = 0; i < 10 && !w.claimed; i++) step();
            CHECK(w.claimed && w.snap.cur.in_waiting, "claimed and still listed");
        } else {
            until_on();
            CHECK(box_find("m1") < 0, "begun and no longer listed");
        }
        CHECK(w.stop == 0, "nothing has stopped it");
        swipe(+1);
        CHECK(strcmp(w.sm.sel.id, "m2") == 0, "the swipe moves to the one after the playing one");
        CHECK(w.stop == 1, "and stops what was playing");
        CHECK(w.pause_off == 1, "un-paused first, so the ring is never left held");
        CHECK(w.sm.mode == SM_PET, "and no deferred play survives for the one she left");
        w.stop = 0;
        swipe(+1);
        CHECK(w.stop == 0, "a swipe at the end of the queue stops nothing");
    }
}

static void test_a_swipe_is_not_also_a_hold(void)
{
    /* R: one gesture — a finger lands, travels, stays down past the hold. */
    world_reset();
    const bool two[2] = {true, false};
    queue_is(2, two);
    step();
    run_ms(POPUP_BIG_MS + 400); /* a badge: the pet centre is the pet */
    press_at(CENTRE_X, CENTRE_Y);
    w.swipe = 1;
    step();
    CHECK(w.sm.sel.index == 1, "the drag changed message");
    run_ms(HOLD_TALK_MS + 200);
    CHECK(w.sm.mode != SM_GRID, "and the same press does not also open the grid");
    lift();
    queue_is(0, NULL);
    step();
    press_at(CENTRE_X, CENTRE_Y);
    run_ms(HOLD_TALK_MS + 200);
    CHECK(w.sm.mode == SM_GRID, "a fresh press with no travel still opens it");
}

static void test_a_long_press_on_a_menu_is_just_a_press(void)
{
    world_reset();
    arrive("m1", true);
    run_ms(400);
    press_at(CENTRE_X, CENTRE_Y);
    run_ms(HOLD_TALK_MS + 300);
    CHECK(w.sm.mode != SM_GRID, "a hold on the notice must not also open the grid");

    world_reset();
    offer_from(true);
    press_at(CENTRE_X, CENTRE_Y);
    run_ms(HOLD_TALK_MS + 300);
    CHECK(w.sm.mode != SM_GRID, "nor a hold on a fresh again/reply pair");

    world_reset();
    step();
    press_at(CENTRE_X, CENTRE_Y);
    run_ms(HOLD_TALK_MS + 300);
    CHECK(w.sm.mode == SM_GRID, "a hold on the bare pet still opens the grid");
}

static void test_the_selected_message_is_the_one_that_plays(void)
{
    world_reset();
    const bool two[2] = {true, false};
    queue_is(2, two);
    step();
    run_ms(400);
    swipe(+1);
    tap_the_notice();
    CHECK(w.sm.mode == SM_PLAY, "the press was taken");
    CHECK(!w.out.view.face.from_dad, "the sister's face from the press, before any claim");
    run_ms(400);
    CHECK(plays() == 1, "and the command went out");
    CHECK(strcmp(last_post()->target.id, "m2") == 0, "for the message the finger pointed at");

    world_reset(); /* an old box: the index rides the command */
    old_box(2, true);
    step();
    run_ms(400);
    swipe(+1);
    tap_the_notice();
    run_ms(400);
    CHECK(plays() == 1 && last_post()->at == 1, "the position, on a box without ids");
}

static void test_the_numeral_says_which_of_how_many(void)
{
    world_reset();
    const bool three[3] = {true, false, true};
    queue_is(3, three);
    step();
    run_ms(400);
    tap_the_notice();
    CHECK(w.out.view.sel_m == 3, "the total is what is waiting");
    CHECK(w.out.view.sel_n == 1, "and the position is one-based");
    step();
    swipe(+1); /* during the pending play: it stops and returns to PET, with the selection moved */
    CHECK(w.sm.mode == SM_PET && w.sm.sel.index == 1, "the swipe unwinds the press and moves on");
    swipe(+1);
    CHECK(w.out.view.notice && w.out.view.sel_n == 3, "and the notice says 3");
}

static void test_a_new_message_sends_the_selection_home(void)
{
    /* U: a box with no ids, so the count rule stands in. */
    world_reset();
    old_box(2, true);
    step();
    run_ms(400);
    swipe(+1);
    CHECK(w.sm.sel.index == 1, "pointing at the second one");
    old_box(3, true);
    step();
    CHECK(w.sm.sel.index == 0, "a third arrives and the selection goes back to the oldest");
    swipe(+1);
    swipe(+1);
    CHECK(w.sm.sel.index == 2, "pointing at the newest");
    old_box(1, true);
    step();
    CHECK(w.sm.sel.index == 0, "and a queue that shrank cannot leave it past the end");
}

static void test_every_playback_control_answers_the_finger(void)
{
    world_reset();
    play_a_message(false);
    run_ms(EXIT_GRACE_MS + 100);
    tap_at(LEFT_X, DISC_Y);
    CHECK(w.paused && w.out.view.paused, "pause takes effect");
    CHECK(last_cue() == CUE_PAUSE, "and says so");
    tap_at(LEFT_X, DISC_Y);
    CHECK(!w.paused, "resume takes effect");
    CHECK(last_cue() == CUE_RESUME, "with its own sound");

    world_reset();
    play_a_message(false);
    run_ms(EXIT_GRACE_MS + 100);
    tap_at(RIGHT_X, DISC_Y);
    CHECK(w.sm.mode == SM_PLAY && !w.paused, "no reply while it talks: it plays on, untouched");
    tap_at(LEFT_X, DISC_Y);
    run_ms(400); /* the pause's own cue, which a microphone does not open over */
    tap_at(RIGHT_X, DISC_Y);
    CHECK(w.sm.mode == SM_RECORD, "paused, the reply is taken");
    CHECK(last_cue() == CUE_REPLY, "and answers the finger now");

    world_reset();
    play_a_message(false);
    run_ms(EXIT_GRACE_MS + 100);
    const int stops = w.stop;
    tap_at(EXIT_X, EXIT_Y);
    CHECK(w.stop > stops, "the corner ends the run");
    CHECK(last_cue() == CUE_STOP, "and says which she pressed");

    world_reset();
    arrive("m1", true);
    run_ms(400);
    tap_the_notice();
    CHECK(w.sm.mode == SM_PLAY && last_cue() == CUE_PLAY, "play, with the arriving shape");

    const cue_t used[4] = {CUE_PLAY, CUE_PAUSE, CUE_RESUME, CUE_REPLY};
    for (int i = 0; i < 4; i++) {
        for (int j = i + 1; j < 4; j++) CHECK(used[i] != used[j], "no two controls share a cue");
        CHECK(used[i] != CUE_STOP && used[i] != CUE_HEARD, "nor borrow the stop or the coin");
    }
}

static void test_the_draw_layers_are_a_list(void)
{
    /* R: per-mode composition; the grid is never covered. */
    world_reset();
    offer_from(true);
    arrive("m2", true);
    run_ms(POPUP_BIG_MS + 400);
    press_at(CENTRE_X, CENTRE_Y);
    run_ms(HOLD_TALK_MS + 100);
    const sm_view_t *v = &w.out.view;
    CHECK(v->grid, "the menu is drawn");
    CHECK(!v->notice && !v->offer && !v->pair && !v->transport && !v->confirm && v->face_none,
          "and nothing the panel offered is drawn over or under it");
    lift();
    world_reset();
    play_a_message(true);
    CHECK(v->transport && v->run_bar && v->exit_corner && !v->notice && !v->offer && !v->grid,
          "PLAY: the run bar and the exit, nothing else");
    world_reset();
    say(SM_VOICE_SEND_DAD);
    CHECK(v->confirm && v->listen_dot && !v->face_none && !v->transport,
          "RECORD: the tick and cross, the dot and the face");
    world_reset();
    say(SM_VOICE_WAKE);
    CHECK(v->confirm && v->listen_dot && v->face_none, "LISTEN: the pet's own, no person");
}

static void test_a_notification_arrives_and_a_tap_plays_it(void)
{
    world_reset();
    arrive("m1", true);
    CHECK(w.ncue == 1 && w.cue[0] == CUE_MESSAGE, "one arrival makes one sound");
    CHECK(w.cue_slot[0] == STREAM_SLOT_CHIME, "in the chime slot");
    CHECK(w.out.view.notice_big, "the notice arrives big");
    CHECK(w.out.view.face.from_dad, "with the sender's own face");
    run_ms(400);
    tap_the_notice();
    CHECK(w.sm.mode == SM_PLAY, "the press is taken at once");
    CHECK(last_cue() == CUE_PLAY, "and it answers the finger before the message");
    CHECK(plays() == 0, "the PLAY entered this frame is not posted on it");
    step();
    CHECK(plays() == 0, "nor while its own cue holds the speaker");
    CHECK(!w.out.view.notice, "the notice does not invite a second press");
    run_ms(400);
    CHECK(plays() == 1, "then the message is asked for, exactly once");
}

/* A message is listened to, not talked over: a word the recogniser hears while it plays would sound
   a cue, and the stream yields to cues. */
static void test_commands_are_deaf_while_a_message_plays(void)
{
    world_reset();
    arrive("m1", true);
    run_ms(400);
    CHECK(!w.muted, "commands are live while a message only waits");
    tap_the_notice();
    run_ms(400);
    CHECK(w.sm.mode == SM_PLAY && w.muted, "and deaf from the press that plays it");
    engine_end(JP_PLAY_HEARD);
    step();
    run_ms(400);
    CHECK(w.sm.mode == SM_PET && !w.muted, "and live again once it has ended");
}

static void test_a_press_does_not_evaporate_behind_its_own_cue(void)
{
    world_reset();
    arrive("m1", true);
    run_ms(400);
    tap_the_notice();
    for (int i = 0; i < 120; i++) {
        w.cue_until = w.now + FAKE_CUE_MS;
        step();
    }
    CHECK(w.sm.mode == SM_PLAY, "a busy speaker does not throw the press away");
    CHECK(plays() == 0, "nor play it into the noise");
    w.cue_until = 0;
    step();
    CHECK(plays() == 1, "it plays the moment the speaker is free");
    run_ms(400);
    CHECK(plays() == 1, "and once only");
}

static void test_a_press_during_the_arrival_cue_plays_the_message(void)
{
    world_reset();
    arrive("m1", true);
    CHECK(!jp_due(w.now, w.cue_until), "the arrival cue is still sounding");
    tap_the_notice();
    CHECK(w.sm.mode == SM_PLAY, "a press during the notification beep is acted on");
    run_ms(400);
    CHECK(plays() == 1, "and the message plays once the speaker is free");
}

static void test_the_run_controls_appear_from_the_press_not_the_stream(void)
{
    world_reset();
    arrive("m1", true);
    run_ms(400);
    tap_the_notice();
    CHECK(w.out.view.run_bar, "the controls are up on the frame of the press");
    CHECK(w.out.view.playing_glyph, "showing pause, because sound is coming");
    CHECK(target_at(LEFT_X, DISC_Y) == SM_T_IGNORED, "drawn while it is ignored");
    CHECK(w.out.view.exit_corner, "and the way out is drawn with them");
    CHECK(!w.out.view.pair, "and the ended state is not");
}

static void test_the_top_right_corner_ends_a_message_while_it_plays(void)
{
    /* R: one press — it stops and leaves, with no offer (owner Q2). */
    world_reset();
    play_a_message(false);
    CHECK(w.out.view.run_bar && w.out.view.exit_corner, "the exit is drawn while it plays");
    run_ms(EXIT_GRACE_MS + 100);
    const int stops = w.stop;
    tap_at(EXIT_X, EXIT_Y);
    CHECK(w.stop > stops, "pressing the drawn exit mid-message ends it");
    CHECK(!w.sounding, "the message stops with it");
    CHECK(w.sm.mode == SM_PET && !w.out.view.offer && !w.sm.offer.up, "and leaves, no pair");
}

static void test_the_pair_waits_for_a_finger_not_a_clock(void)
{
    world_reset();
    play_a_message(true);
    engine_end(JP_PLAY_HEARD);
    step();
    CHECK(w.sm.offer.up && w.out.view.offer, "the outcome was consumed and the offer set");
    CHECK(!w.out.view.offer_standing, "fresh at first");
    run_ms(REPEAT_MS);
    CHECK(w.out.view.offer && w.out.view.offer_standing, "standing at ten seconds, still drawn");
    run_ms(REPEAT_MS * 2);
    CHECK(w.out.view.offer && w.out.view.face.from_dad, "thirty seconds later, with his face");
    arrive("m2", false);
    CHECK(!w.out.view.offer && w.sm.offer.up, "hidden while a message waits, not cleared");
    box_remove("m2");
    run_ms(120000);
    CHECK(w.out.view.offer, "two minutes later, still there");
}

static void test_reply_records_to_whoever_just_spoke(void)
{
    world_reset();
    offer_from(true);
    const int opened = w.opened;
    tap_at(RIGHT_X, DISC_Y);
    CHECK(w.sm.mode == SM_RECORD, "reply opens a recording");
    CHECK(w.sm.rec.to == JP_TO_DAD, "addressed to the person who just spoke");
    CHECK(w.opened == opened + 1, "the microphone was opened once");
    CHECK(last_cue() == CUE_REPLY, "in the sound that means your turn");
    CHECK(!w.out.view.offer, "the pair steps aside for the tick and cross");
    CHECK(w.muted, "commands are deaf: a message must not also be a command");
    hear_then_hush();
    CHECK(sends() == 1 && last_post()->to == JP_TO_DAD, "the message was posted to dad");
    CHECK(w.send_talk == 0, "and never to the conversation");
    CHECK(!w.muted, "the mute lifts with the recording");
}

static void test_sending_does_not_raise_the_playback_controls(void)
{
    world_reset();
    w.send_ms = 6000;
    say(SM_VOICE_SEND_PANEL);
    hear_then_hush();
    CHECK(sends() == 1, "it was sent");
    CHECK(w.out.view.sending, "the send is in flight");
    CHECK(!w.out.view.transport && !w.out.view.run_bar && !w.out.view.pair,
          "an outgoing message raises no transport and no pair");
    arrive("m1", true);
    run_ms(200);
    tap_the_notice();
    CHECK(last_cue() == CUE_BUSY && w.sm.mode == SM_PET, "a notice press during a send is busy");
    run_ms(6000);
    CHECK(saw_cue(CUE_SENT) && !w.out.view.sending, "then it went");
    tap_the_notice();
    CHECK(w.out.view.transport, "and a press now raises the controls");
}

static void test_a_short_button_press_darkens_blanks_and_mutes(void)
{
    world_reset();
    offer_from(true);
    w.btn_short = true;
    step();
    CHECK(fx_index(SM_FX_SILENCE, -1) == 0, "the silence first");
    CHECK(fx_index(SM_FX_CUE, CUE_STOP) == 1, "then its own stop cue, at a later seq");
    CHECK(fx_index(SM_FX_STOP, -1) > 1 && fx_index(SM_FX_STANDBY, 1) > fx_index(SM_FX_STOP, -1) &&
              fx_index(SM_FX_BLANK, -1) > fx_index(SM_FX_STANDBY, 1),
          "then STOP, standby and a black frame");
    CHECK(w.standby && w.blank == 1, "the screen is off and painted dark");
    CHECK(w.muted, "commands are muted: screen off AND deaf");
    CHECK(!w.sm.offer.up, "nothing modal survives, including the pair");
    run_ms(400);
    w.btn_short = true;
    step();
    CHECK(w.standby_off == 1 && !w.standby, "a second press wakes it");
    CHECK(last_cue() == CUE_HEARD, "with the coin");
    CHECK(!w.muted, "and listening again");
    CHECK(w.deep_sleep == 0, "a short press never powers down");

    /* Every mode, and nothing opens the microphone after it. */
    for (int mode = 0; mode < 4; mode++) {
        world_reset();
        if (mode == 0) play_a_message(true);
        if (mode == 1) say(SM_VOICE_WAKE);
        if (mode == 2) say(SM_VOICE_SEND_PANEL);
        if (mode == 3) {
            press_at(CENTRE_X, CENTRE_Y);
            run_ms(HOLD_TALK_MS + 100);
            lift();
        }
        const int opened = w.opened;
        w.btn_short = true;
        step();
        CHECK(w.sm.mode == SM_PET && w.standby && w.out.view.mode == SM_PET, "standby from any mode");
        CHECK(fx_index(SM_FX_SILENCE, -1) == 0 && fx_index(SM_FX_CUE, CUE_STOP) == 1,
              "the same pattern from every mode");
        run_ms(5000);
        CHECK(w.opened == opened, "and no microphone opens after it");
    }
    /* A follow-up armed before standby never opens. */
    world_reset();
    ask_pet();
    w.net = SM_NET_SPOKE;
    step();
    w.reply = true;
    step();
    w.btn_short = true;
    step();
    w.reply = false;
    const int opened = w.opened;
    run_ms(1000);
    CHECK(w.opened == opened, "a follow-up never opens in the dark");
    /* A send continues. */
    world_reset();
    say(SM_VOICE_SEND_DAD);
    hear_then_hush();
    w.btn_short = true;
    step();
    run_ms(3000);
    CHECK(saw_cue(CUE_SENT), "a send continues, and says it went");
}

static void test_a_five_second_hold_requests_deep_sleep_exactly_once(void)
{
    world_reset();
    run_ms(5000);
    CHECK(w.deep_sleep == 0, "nothing happens before the five seconds are up");
    w.btn_hold = true;
    step();
    CHECK(w.deep_sleep == 1, "the hold asks for deep sleep");
    run_ms(3000);
    CHECK(w.deep_sleep == 1, "exactly once");
    CHECK(w.standby_on == 0, "and a hold is not a short press half-fired");
}

static void test_a_hold_on_the_pet_opens_the_grid_at_every_free_tap_count(void)
{
    for (int taps = 0; taps <= GESTURE_TAPS_MAX; taps++) {
        world_reset();
        w.gest_taps = taps;
        press_at(CENTRE_X, CENTRE_Y);
        run_ms(HOLD_TALK_MS + 200);
        if (gesture_reserved(taps)) {
            CHECK(w.sm.mode != SM_GRID && !w.out.view.grid, "a reserved count does not open it");
        } else {
            CHECK(w.sm.mode == SM_GRID && w.out.view.grid, "every other count opens the grid");
            CHECK(saw_cue(CUE_HEARD), "with the sound that means something has appeared");
        }
    }
    CHECK(gesture_reserved(GESTURE_TAPS_REBOOT), "the reboot count is still reserved");
    CHECK(!gesture_reserved(1) && !gesture_reserved(2), "and the low counts are free");
}

/* The frame a maintenance hold fires, `gesture_poll` has already reset the count; the finger is
   still down and must not open the grid then, nor on any frame of that press. */
static void test_a_maintenance_hold_never_opens_the_grid_as_it_fires(void)
{
    world_reset();
    w.gest_taps = GESTURE_TAPS_REBOOT;
    press_at(CENTRE_X, CENTRE_Y);
    run_ms(GESTURE_HOLD_MS);
    w.gest_taps = 0;
    run_ms(HOLD_TALK_MS + 500);
    CHECK(w.sm.mode != SM_GRID && !w.out.view.grid, "the firing frame does not open the grid");
    lift();
    press_at(CENTRE_X, CENTRE_Y);
    run_ms(HOLD_TALK_MS + 200);
    CHECK(w.sm.mode == SM_GRID, "and the next press's hold still does");
}

static void open_grid(void)
{
    press_at(CENTRE_X, CENTRE_Y);
    run_ms(HOLD_TALK_MS + 100);
    CHECK(w.sm.mode == SM_GRID, "the grid is up");
    lift();
    run_ms(100);
}

static void test_the_grid_reaches_all_four_people(void)
{
    /* R: ~200 ms after the hold, far from its point; the microphone is deaf to commands in LISTEN
       and in RECORD alike. */
    struct {
        int x, y;
    } cells[3] = {{FACE_W / 4, 60}, {FACE_W * 3 / 4, 60}, {FACE_W * 3 / 4, FACE_H - 60}};
    for (int i = 0; i < 3; i++) {
        world_reset();
        open_grid();
        tap_at(cells[i].x, cells[i].y);
        CHECK(w.sm.mode != SM_GRID, "the choice closes the menu");
        CHECK(last_cue() == CUE_LISTEN, "and opens a microphone");
        CHECK(w.muted, "an open microphone is deaf to commands");
        if (i == 2) {
            CHECK(w.sm.mode == SM_LISTEN, "the pet's icon starts a conversation");
        } else {
            CHECK(w.sm.mode == SM_RECORD, "a person's icon starts a message");
            CHECK(w.sm.rec.to == (i == 0 ? JP_TO_PANEL : JP_TO_DAD), "addressed to them");
        }
    }
    world_reset();
    open_grid();
    tap_at(60, FACE_H - 60);
    CHECK(w.sm.mode == SM_PET, "the cancel closes the menu");
    CHECK(w.opened == 0, "and the microphone never opened");
    CHECK(!w.out.pet_press, "the pet cannot be poked through a menu");
}

static void test_the_grid_closes_itself_but_a_stray_finger_does_not(void)
{
    world_reset();
    w.over_y0 = (FACE_H - FACE_W) / 2;
    w.over_h = w.over_y0 + FACE_W;
    open_grid();
    tap_at(FACE_W / 2, w.over_h + 10);
    CHECK(w.sm.mode == SM_GRID, "a finger off the band does not close the menu");
    CHECK(has_fx(SM_FX_FLINCH), "it is consumed rather than ignored");
    CHECK(!w.out.pet_press, "and the pet underneath is not poked");
    run_ms(SENDTO_MS + 400);
    CHECK(w.sm.mode == SM_PET, "but its own clock closes it");
}

static void test_a_tick_pressed_into_silence_refuses_to_send(void)
{
    world_reset();
    say(SM_VOICE_SEND_DAD);
    CHECK(w.out.view.confirm, "the tick and the cross are up");
    tap_at(RIGHT_X, DISC_Y);
    CHECK(sends() == 0, "nobody spoke, so nothing was sent");
    CHECK(last_cue() == CUE_OOPS, "and it says so");
    CHECK(w.sm.mode == SM_PET && w.dropped == 1, "the bytes were dropped");
}

static void test_the_cross_discards_and_the_pet_is_not_poked(void)
{
    world_reset();
    say(SM_VOICE_WAKE);
    w.hearing = true;
    run_ms(400);
    w.hearing = false;
    tap_at(LEFT_X, DISC_Y);
    CHECK(w.sm.mode == SM_PET && w.dropped == 1, "the cross ends the turn and drops it");
    CHECK(w.send_talk == 0, "nothing was uploaded");
    CHECK(last_cue() == CUE_STOP, "it answers instantly");

    world_reset();
    say(SM_VOICE_WAKE);
    const int flinch = w.flinch;
    tap_at(FACE_W / 2, 40);
    CHECK(w.sm.mode == SM_LISTEN, "the turn survives a stray finger");
    CHECK(w.flinch == flinch && !w.out.pet_press, "and the pet does not twitch at it");
    CHECK(w.out.tap_missed, "but it is logged");
}

static void test_a_named_turn_with_nobody_speaking_costs_nothing(void)
{
    world_reset();
    say(SM_VOICE_WAKE);
    const int cues = w.ncue;
    run_ms(LISTEN_LEAD_MS + 400);
    CHECK(w.sm.mode == SM_PET, "silence closes it");
    CHECK(w.send_talk == 0 && w.dropped == 1, "with nothing sent and the bytes dropped");
    CHECK(w.ncue == cues, "silently");
}

static void test_a_failed_turn_says_so_rather_than_going_quiet(void)
{
    /* R: the turn is THINKING on its entry frame, whatever the stale `net` says. */
    world_reset();
    say(SM_VOICE_WAKE);
    w.hearing = true;
    run_ms(400);
    w.hearing = false;
    for (int i = 0; i < 100 && w.send_talk == 0; i++) step();
    CHECK(w.send_talk == 1, "the turn went up");
    CHECK(w.sm.turn.state == SM_TURN_THINKING, "and the bubble is up on the entry frame");
    step();
    CHECK(w.out.view.think && !w.out.view.dash, "thinking, not failed");
    w.net = SM_NET_FAILED;
    step();
    CHECK(w.sm.turn.state == SM_TURN_FAILED, "a failure is a state");
    CHECK(saw_cue(CUE_OOPS), "with a gentle sound");
    CHECK(w.out.view.dash, "and a bewildered face");
    w.net = SM_NET_IDLE;
    run_ms(TALK_FAILED_MS + 400);
    CHECK(w.sm.turn.state == SM_TURN_NONE, "then the pet goes back to being a pet");
}

static void test_every_dash_is_counted_and_its_two_causes_told_apart(void)
{
    world_reset();
    CHECK(w.sm.dashes == 0, "nothing has gone wrong yet");
    ask_pet();
    w.net = SM_NET_FAILED;
    step();
    CHECK(w.sm.dashes == 1 && w.out.dash_why == SM_DASH_NET, "counted, and named the request");
    CHECK(strcmp(sm_dash_name(SM_DASH_NET), "net") == 0, "as \"net\"");
    const uint32_t first_at = w.sm.dash_at;
    CHECK(first_at != 0, "and stamped");
    w.net = SM_NET_IDLE;
    run_ms(TALK_FAILED_MS + 400);
    ask_pet();
    sm_dash_t why = SM_DASH_NONE;
    for (int i = 0; i < 1000 && w.sm.turn.state == SM_TURN_THINKING; i++) {
        step();
        if (w.out.dash_why != SM_DASH_NONE) why = w.out.dash_why;
    }
    CHECK(w.sm.dashes == 2 && why == SM_DASH_TIMEOUT, "the panel gave up, just as much a dash");
    CHECK(strcmp(sm_dash_name(SM_DASH_TIMEOUT), "timeout") == 0, "and not a network failure");
    CHECK(w.sm.dash_at > first_at, "the stamp is the latest dash");
    w.net = SM_NET_IDLE;
    run_ms(TALK_FAILED_MS + 400);
    ask_pet();
    w.net = SM_NET_SPOKE;
    step();
    CHECK(w.sm.turn.state == SM_TURN_NONE && w.sm.dashes == 2, "a good turn clears nothing");
}

static void test_a_reply_arms_a_follow_up_and_the_cap_ends_it(void)
{
    /* R: the reply is `reply_busy` and its falling edge. */
    world_reset();
    ask_pet();
    for (int turn = 1; turn <= FOLLOW_MAX_TURNS + 1; turn++) {
        w.net = SM_NET_SPOKE;
        step();
        CHECK(w.sm.turn.state == SM_TURN_NONE, "the bubble goes when the reply arrives");
        w.net = SM_NET_IDLE;
        w.reply = true;
        run_ms(800);
        w.reply = false;
        step();
        if (turn <= FOLLOW_MAX_TURNS) {
            CHECK(w.sm.mode == SM_LISTEN, "the microphone reopens by itself");
            CHECK(w.sm.talk.follow_turns == turn, "counting the turns since a deliberate start");
            hear_then_hush();
            CHECK(w.sm.turn.state == SM_TURN_THINKING, "and the next turn goes up");
        } else {
            CHECK(w.sm.mode == SM_PET, "past the cap it stops following up");
        }
    }
    CHECK(w.sm.talk.follow_turns == FOLLOW_MAX_TURNS, "the counter parks at the cap");
    say(SM_VOICE_WAKE);
    CHECK(w.sm.mode == SM_LISTEN && w.sm.talk.follow_turns == 0, "a deliberate start resets it");
}

static void test_stop_said_out_loud_leaves_every_state(void)
{
    world_reset();
    say(SM_VOICE_SEND_PANEL);
    say(SM_VOICE_STOP);
    CHECK(w.sm.mode == SM_PET && w.dropped == 1, "stop drops a recording");
    CHECK(w.sm.talk.follow_turns == FOLLOW_MAX_TURNS, "and closes the follow-up window");
    world_reset();
    ask_pet();
    say(SM_VOICE_STOP);
    CHECK(w.sm.turn.state == SM_TURN_NONE && w.talk_clear >= 1, "stop abandons a reply in flight");
}

static void test_pressing_the_face_cycles_through_the_waiting_messages(void)
{
    /* R: face presses past the notice press's grace. */
    world_reset();
    const bool three[3] = {true, false, true};
    queue_is(3, three);
    step();
    run_ms(400);
    tap_the_notice();
    until_on();
    run_ms(EXIT_GRACE_MS);
    CHECK(strcmp(w.sm.sel.id, "m1") == 0, "starting on the oldest");
    CHECK(target_at(FACE_X, FACE_Y) == SM_T_FACE, "a press on the sender's face reaches the face");
    tap_at(FACE_X, FACE_Y);
    CHECK(strcmp(w.sm.sel.id, "m2") == 0, "and moves to the next message");
    CHECK(w.stop >= 1, "the message that was playing is ended");
    CHECK(w.sm.mode == SM_PET, "and the menu is down");
}

static void test_the_face_is_not_a_control_with_one_message(void)
{
    world_reset();
    w.claim_ms = 160;
    arrive("m1", true);
    run_ms(400);
    tap_the_notice();
    for (int i = 0; i < 20; i++) {
        CHECK(target_at(FACE_X, FACE_Y) != SM_T_FACE, "with nothing to cycle to, no face control");
        step();
    }
}

static void test_pressing_replay_twice_does_not_pause_what_never_started(void)
{
    world_reset();
    w.fetch_ms = 600;
    offer_from(true);
    tap_at(LEFT_X, DISC_Y);
    CHECK(w.sm.mode == SM_PLAY && w.sm.play.replay, "the first press asks for a replay");
    CHECK(target_at(LEFT_X, DISC_Y) == SM_T_IGNORED, "the same disc is claimed, not PET");
    const int paused_before = w.pause_on;
    tap_at(LEFT_X, DISC_Y);
    CHECK(w.pause_on == paused_before, "and must not pause a replay that has not started");
    CHECK(w.sm.mode == SM_PLAY, "the replay is still on its way");
    until_on();
    tap_at(LEFT_X, DISC_Y);
    CHECK(w.pause_on > paused_before, "once it IS sounding, the same disc pauses");
}

static void test_a_held_stream_resumes_rather_than_being_thrown_away(void)
{
    world_reset();
    play_a_message(false);
    tap_at(LEFT_X, DISC_Y);
    CHECK(w.pause_on == 1 && w.paused, "a finger holds the message");
    CHECK(w.sm.play.pause_on, "with a deadline on the hold");
    run_ms(70000);
    CHECK(w.pause_off >= 1 && !w.paused, "it resumes rather than aborting");
    CHECK(!w.sm.play.pause_on, "and the deadline is spent");
}

static void test_the_notice_shrinks_to_a_badge_and_a_second_one_does_not_restore_it(void)
{
    world_reset();
    arrive("m1", true);
    CHECK(w.out.view.notice_big, "big to start with");
    run_ms(POPUP_BIG_MS + 400);
    CHECK(w.out.view.notice && !w.out.view.notice_big, "then a badge");
    CHECK(target_at(FACE_X, FACE_Y) == SM_T_NOTICE, "and still pressable");
    arrive("m2", true);
    CHECK(!w.out.view.notice_big, "a second message does not take the screen hostage again");
    CHECK(count_cue(CUE_MESSAGE) == 2, "it still makes a sound");
    run_ms(400);
    w.dark_wake = true;
    step();
    CHECK(w.out.view.notice_big, "waking to a message left overnight interrupts properly");
}

static void test_the_notice_stays_painted_while_a_cue_sounds(void)
{
    world_reset();
    arrive("m1", true);
    CHECK(w.out.view.notice_big && !jp_due(w.now, w.cue_until), "painted as its cue sounds");
    step();
    CHECK(w.out.view.notice_big, "and still a frame later, mid-cue");
    CHECK(target_at(CENTRE_X, CENTRE_Y) == SM_T_NOTICE, "with its target armed");
    run_ms(400);
    CHECK(w.out.view.notice_big && target_at(CENTRE_X, CENTRE_Y) == SM_T_NOTICE,
          "and the same when the panel goes quiet");
}

static void test_the_queue_count_only_sounds_on_the_way_up(void)
{
    /* U: no ids, so the count rise stands in. */
    world_reset();
    old_box(3, true);
    step();
    CHECK(w.ncue == 1 && w.cue[0] == CUE_MESSAGE, "three arriving at once is one sound");
    old_box(2, true);
    step();
    CHECK(w.ncue == 1, "one being played is none");
    old_box(0, true);
    step();
    CHECK(w.ncue == 1, "and emptying the queue is none");
    old_box(1, true);
    step();
    CHECK(w.ncue == 2, "a new one is news again");
}

static void send_one(void)
{
    say(SM_VOICE_SEND_PANEL);
    hear_then_hush();
    CHECK(sends() == 1, "a message is on its way");
}

static void test_what_the_box_said_about_a_message_is_answered_in_sound(void)
{
    /* R: from the send outcome's seq, each its own sentence, consumed once. */
    world_reset();
    send_one();
    run_ms(3000);
    CHECK(count_cue(CUE_SENT) == 1 && w.poll_soon == 1, "it went, and the twin may have answered");
    run_ms(3000);
    CHECK(count_cue(CUE_SENT) == 1, "the same snapshot again sounds nothing");

    world_reset();
    w.send_kind = JP_SEND_NOBODY;
    send_one();
    const char *said = NULL;
    for (int i = 0; i < 60; i++) {
        step();
        if (w.out.say != NULL) said = w.out.say;
    }
    CHECK(saw_cue(CUE_OOPS) && said != NULL, "nobody to send it to, and the caption says so");

    world_reset();
    w.send_kind = JP_SEND_FAILED;
    send_one();
    said = NULL;
    for (int i = 0; i < 60; i++) {
        step();
        if (w.out.say != NULL) said = w.out.say;
    }
    CHECK(saw_cue(CUE_OOPS) && said == NULL, "it did not go — a different sentence");

    world_reset();
    w.send_kind = JP_SEND_EMPTY;
    send_one();
    run_ms(3000);
    CHECK(last_cue() == CUE_STOP, "trimmed to nothing: dropped");
}

static void test_a_replay_still_reads_as_playing(void)
{
    world_reset();
    w.fetch_ms = 400;
    offer_from(false);
    tap_at(LEFT_X, DISC_Y);
    CHECK(w.sm.play.replay && w.sm.offer.up, "again asks the box and keeps the offer");
    CHECK(w.out.view.run_bar && w.out.view.playing_glyph, "the run bar shows pause from the press");
    until_on();
    step();
    CHECK(!w.out.view.run_bar && w.out.view.pair, "once audible the pair keeps the screen");
    CHECK(w.out.view.playing_glyph, "and reads as playing");
    CHECK(w.out.view.exit_corner, "with a way out");
    tap_at(LEFT_X, DISC_Y);
    CHECK(w.out.view.pair && !w.out.view.playing_glyph, "a held replay reads as held");
}

static void test_the_action_list_never_overflows(void)
{
    /* R: busier than any reachable frame — the button, a hold, a pause past its limit, a send
       outcome, a play backstop and an arrival at once (a send outcome cannot meet a PLAY, which
       is why the state is set by hand) — so a new branch either fits or fails loudly. */
    world_reset();
    play_a_message(true);
    tap_at(LEFT_X, DISC_Y); /* paused */
    w.engine = false;
    w.snap.deadline_ms = w.now - JP_UI_MARGIN_MS - 1;
    w.sm.send.seq = 999;
    w.sm.send.expires_at = w.now + JP_SEND_EXPIRE_MS;
    w.snap.send.seq = 999;
    w.snap.send.kind = JP_SEND_NOBODY;
    w.sm.play.pause_since = w.now - AUDIO_PAUSE_MAX_MS - 1;
    box_add("m9", false);
    w.btn_short = true;
    w.btn_hold = true;
    sample();
    sm_step(&w.sm, &w.in, &w.out);
    CHECK(w.out.n < SM_FX_MAX, "the busiest frame that can be built still fits, with room");
    CHECK(w.out.n >= 8, "and it really is a busy frame");
}


/* ══ PART TWO: the machine's own (§8.2, §8.5, §9) ═══════════════════════════════════════════ */

/* Publish a deferred stop's outcome, as the engine would after classifying it. */
static void release_stop(jp_play_kind_t kind)
{
    w.stop_pending = false;
    jp_view_t *s = &w.snap;
    w.claimed = false;
    w.cmd_waiting = false;
    s->play.seq = w.cmd.seq;
    s->play.who = s->cur.who;
    s->play.kind = kind;
    s->play.replay = s->cur.replay;
    if (kind == JP_PLAY_RETURNED && box_find(s->cur.who.id) < 0) {
        box_insert(s->cur.pos < s->waiting.n ? s->cur.pos : s->waiting.n, s->cur.who);
    }
    s->phase = JP_IDLE;
    s->deadline_ms = 0;
    publish_done(w.cmd.seq);
}

/* ── the frame boundary (§8.2 rule 1) ─────────────────────────────────────────────────────── */

static void test_turn_entered_this_frame_survives_stale_net(void)
{
    world_reset();
    say(SM_VOICE_WAKE);
    w.hearing = true;
    run_ms(400);
    w.hearing = false;
    w.talk_ok = false; /* the frame's `net` stays IDLE whatever the executor does */
    while (w.send_talk == 0) step();
    CHECK(w.sm.turn.state == SM_TURN_THINKING && w.net == SM_NET_IDLE,
          "IDLE on the entry frame is the world before the question went up");
    w.net = SM_NET_BUSY;
    step();
    CHECK(w.sm.turn.state == SM_TURN_THINKING, "and the turn is still in flight a frame later");
}

static void test_cap_not_read_from_previous_capture(void)
{
    world_reset();
    w.send_ms = 80;
    say(SM_VOICE_SEND_PANEL);
    w.hearing = true;
    for (int i = 0; i < 1000 && sends() == 0; i++) step();
    CHECK(sends() == 1, "the cap sent the recording");
    w.hearing = false;
    run_ms(400);
    w.capture_ms = CAPTURE_MAX_MS + 100; /* the executor's count, from the capture that ended */
    say(SM_VOICE_WAKE);
    CHECK(w.sm.mode == SM_LISTEN, "a listen opens on that frame");
    step();
    CHECK(w.sm.mode == SM_LISTEN && w.send_talk == 0, "and is not sent at once by the old count");
}

static void test_play_not_posted_in_frame_of_its_press_cue(void)
{
    world_reset();
    arrive("m1", true);
    run_ms(400);
    int x, y;
    notice_centre(&x, &y);
    w.down = w.up = true;
    w.x = x;
    w.y = y;
    sample();
    w.in.audio.speaker_busy = false; /* the frame-start facts: nothing sounding yet */
    sm_step(&w.sm, &w.in, &w.out);
    CHECK(w.sm.mode == SM_PLAY && !has_fx(SM_FX_POST), "a PLAY entered this frame waits a frame");
    CHECK(w.sm.play.seq == JP_SEQ_NONE, "play.seq is NONE until it is posted");
}

static void test_old_stop_outcome_does_not_finish_new_pending_play(void)
{
    world_reset();
    w.defer_stop = true;
    const bool two[2] = {true, false};
    queue_is(2, two);
    step();
    run_ms(400);
    tap_the_notice();
    until_on();
    run_ms(EXIT_GRACE_MS);
    const uint32_t old = w.sm.play.seq;
    tap_at(EXIT_X, EXIT_Y);
    run_ms(EXIT_GRACE_MS + 100);
    tap_the_notice();
    CHECK(w.sm.mode == SM_PLAY && w.sm.play.seq == JP_SEQ_NONE, "a new press, not yet posted");
    release_stop(JP_PLAY_CANCELLED);
    CHECK(w.snap.play.seq == old, "the old outcome lands");
    step();
    CHECK(w.sm.mode == SM_PLAY, "and does not finish or cancel the new press");
    run_ms(400);
    CHECK(w.sm.play.seq != JP_SEQ_NONE && w.sm.play.seq != old, "which posts with its own seq");
}

/* ── the press grace (§8.2 rule 2) ────────────────────────────────────────────────────────── */

static void test_pet_cell_bounce_does_not_drop_listen(void)
{
    world_reset();
    open_grid();
    tap_at(FACE_W * 3 / 4, FACE_H * 3 / 4);
    CHECK(w.sm.mode == SM_LISTEN, "the pet cell opens a conversation");
    tap_at(FACE_W * 3 / 4, FACE_H * 3 / 4); /* the same finger, lightening for a sample */
    CHECK(w.sm.mode == SM_LISTEN && w.dropped == 0 && !saw_cue(CUE_OOPS),
          "a bounce on the tick's quadrant drops nothing");
    CHECK(w.out.tap_missed, "and is logged");
    run_ms(EXIT_GRACE_MS);
    CHECK(target_at(FACE_W * 3 / 4, FACE_H * 3 / 4) == SM_T_TICK, "the tick is live after it");
}

static void test_notice_bounce_does_not_hit_face(void)
{
    world_reset();
    const bool two[2] = {true, false};
    queue_is(2, two);
    step();
    run_ms(POPUP_BIG_MS + 400);
    tap_at(FACE_X, FACE_Y); /* the badge, which is the face's quadrant */
    CHECK(w.sm.mode == SM_PLAY, "the badge plays");
    tap_at(FACE_X, FACE_Y);
    CHECK(w.sm.mode == SM_PLAY && w.stop == 0, "its bounce does not cycle to the next message");
}

static void test_grid_cancel_bounce_does_not_replay(void)
{
    world_reset();
    offer_from(true);
    run_ms(REPEAT_MS);
    open_grid();
    tap_at(FACE_W / 4, FACE_H * 3 / 4 + 30); /* the cancel cell, over the AGAIN disc */
    CHECK(w.sm.mode == SM_PET && w.out.view.offer, "cancelled, back to the standing pair");
    tap_at(FACE_W / 4, FACE_H * 3 / 4 + 30);
    CHECK(w.sm.mode == SM_PET && plays() == 1, "the bounce does not replay an old message");
}

static void test_cross_bounce_with_offer_does_not_replay(void)
{
    world_reset();
    offer_from(true);
    say(SM_VOICE_SEND_PANEL);
    CHECK(w.sm.mode == SM_RECORD && w.sm.offer.up, "a recording by voice keeps the offer");
    run_ms(EXIT_GRACE_MS);
    tap_at(LEFT_X, DISC_Y);
    CHECK(w.sm.mode == SM_PET && w.out.view.offer, "the cross returns to the pair");
    tap_at(LEFT_X, DISC_Y);
    CHECK(w.sm.mode == SM_PET && plays() == 1, "and its bounce is not AGAIN");
    run_ms(EXIT_GRACE_MS);
    CHECK(target_at(LEFT_X, DISC_Y) == SM_T_AGAIN, "which is live after the grace");
}

static void test_hold_opened_grid_junction_bounce_graced(void)
{
    world_reset();
    press_at(CENTRE_X, CENTRE_Y);
    run_ms(HOLD_TALK_MS + 100);
    lift();
    tap_at(CENTRE_X + 3, CENTRE_Y + 3);
    CHECK(w.sm.mode == SM_GRID && w.out.tap_missed, "a lightening finger at the junction is ignored");
}

static void test_deliberate_cell_tap_after_hold_acts(void)
{
    world_reset();
    press_at(CENTRE_X, CENTRE_Y);
    run_ms(HOLD_TALK_MS + 100);
    lift();
    tap_at(FACE_X, FACE_Y);
    CHECK(w.sm.mode == SM_RECORD && w.sm.rec.to == JP_TO_PANEL,
          "a deliberate tap on a face elsewhere acts at once");
}

static void test_pause_live_at_on_inside_grace(void)
{
    world_reset();
    play_a_message(true);
    CHECK(!jp_due(w.now, w.sm.play.since + EXIT_GRACE_MS), "still inside the press's grace");
    CHECK(target_at(LEFT_X, DISC_Y) == SM_T_PAUSE, "PAUSE is live the moment it is audible");
    tap_at(LEFT_X, DISC_Y);
    CHECK(w.paused, "and pauses");
}

static void test_failed_play_clears_mode_press_grace(void)
{
    world_reset();
    arrive("m1", true);
    run_ms(400);
    w.claim_ms = 160;
    tap_the_notice();
    while (plays() == 0) step();
    box_remove("m1"); /* played elsewhere before the claim: the box answers 204 */
    box_add("m2", false);
    for (int i = 0; i < 6 && w.sm.mode == SM_PLAY; i++) step();
    CHECK(!jp_due(w.now, w.sm.since + EXIT_GRACE_MS), "all inside the press's grace");
    CHECK(w.sm.mode == SM_PET && saw_cue(CUE_OOPS), "the PLAY failed inside the grace");
    CHECK(w.sm.mode_press_at == 0, "and left no grace behind");
    CHECK(target_at(CENTRE_X, CENTRE_Y) == SM_T_NOTICE, "so the notice is live at once");
}

static void test_exit_bounce_does_not_play_next_message(void)
{
    world_reset();
    const bool two[2] = {true, false};
    queue_is(2, two);
    step();
    run_ms(400);
    tap_the_notice();
    until_on();
    run_ms(EXIT_GRACE_MS);
    tap_at(250, 150);
    CHECK(w.sm.mode == SM_PET && w.out.view.notice, "the exit leaves to the next notice");
    tap_at(250, 150);
    CHECK(w.sm.mode == SM_PET && plays() == 1, "its bounce does not play it");
}

static void test_face_bounce_after_select_does_not_play(void)
{
    world_reset();
    const bool three[3] = {true, false, true};
    queue_is(3, three);
    step();
    run_ms(POPUP_BIG_MS + 400);
    tap_at(FACE_X, FACE_Y);
    until_on();
    run_ms(EXIT_GRACE_MS);
    tap_at(FACE_X, FACE_Y);
    CHECK(w.sm.mode == SM_PET, "the face moved on and stopped");
    tap_at(FACE_X, FACE_Y);
    CHECK(w.sm.mode == SM_PET && plays() == 1, "the bounce on the badge plays nothing");
}

/* ── the hold (E1.6, B2) ──────────────────────────────────────────────────────────────────── */

static void test_hold_at_pet_centre_with_badge(void)
{
    world_reset();
    arrive("m1", true);
    run_ms(POPUP_BIG_MS + 400);
    press_at(CENTRE_X, CENTRE_Y);
    run_ms(HOLD_TALK_MS + 100);
    CHECK(w.sm.mode == SM_GRID, "a badge does not block the menu");
}

static void test_hold_at_pet_centre_fresh_offer_is_reply(void)
{
    world_reset();
    offer_from(true);
    press_at(CENTRE_X, CENTRE_Y);
    run_ms(HOLD_TALK_MS + 300);
    CHECK(w.sm.mode == SM_RECORD && w.sm.rec.to == JP_TO_DAD, "a fresh pair's press is REPLY");
}

static void test_hold_at_pet_centre_standing_offer_opens_grid(void)
{
    world_reset();
    offer_from(true);
    run_ms(REPEAT_MS);
    press_at(CENTRE_X, CENTRE_Y);
    run_ms(HOLD_TALK_MS + 100);
    CHECK(w.sm.mode == SM_GRID, "a standing pair: the centre is the pet, and a hold opens the menu");
    CHECK(w.sm.offer.up, "the offer survives the grid");
}

static void test_hold_at_pet_centre_during_play_is_swallowed(void)
{
    world_reset();
    w.fetch_ms = 2000;
    arrive("m1", true);
    run_ms(400);
    tap_the_notice();
    run_ms(EXIT_GRACE_MS);
    press_at(CENTRE_X, CENTRE_Y);
    run_ms(HOLD_TALK_MS + 300);
    CHECK(w.sm.mode == SM_PLAY && w.opened == 0, "swallowed: no menu, no recording");
}

static void test_hold_on_upper_pet_during_fresh_offer_opens_nothing(void)
{
    world_reset();
    offer_from(true);
    press_at(120, 150);
    run_ms(HOLD_TALK_MS + 300);
    CHECK(w.sm.mode == SM_PET && w.out.view.offer, "nothing opens and the pair stays");
}

static void test_hold_in_exit_quadrant_of_standing_offer_dismisses(void)
{
    world_reset();
    offer_from(true);
    run_ms(REPEAT_MS);
    press_at(FACE_W / 2 + 10, 150); /* just inside the exit quadrant */
    CHECK(!w.sm.offer.up && last_cue() == CUE_STOP, "the exit dismisses the pair");
    run_ms(HOLD_TALK_MS + 300);
    CHECK(w.sm.mode == SM_PET, "and that hold is not a menu");
    lift();
    run_ms(EXIT_GRACE_MS);
    press_at(FACE_W / 2 + 10, 150);
    run_ms(HOLD_TALK_MS + 100);
    CHECK(w.sm.mode == SM_GRID, "a second hold opens it");
    lift();

    world_reset();
    offer_from(true);
    run_ms(REPEAT_MS);
    press_at(FACE_W / 2 - 10, 150); /* just outside it */
    run_ms(HOLD_TALK_MS + 100);
    CHECK(w.sm.mode == SM_GRID && w.sm.offer.up, "outside the quadrant it opens at once");
}

static void test_held_exit_does_not_open_grid(void)
{
    world_reset();
    play_a_message(true);
    run_ms(EXIT_GRACE_MS);
    press_at(EXIT_X, EXIT_Y + 60);
    run_ms(HOLD_TALK_MS + 300);
    CHECK(w.sm.mode == SM_PET, "a finger still down from the exit never becomes a hold");
}

static void test_held_face_does_not_open_grid(void)
{
    world_reset();
    const bool two[2] = {true, false};
    queue_is(2, two);
    step();
    run_ms(POPUP_BIG_MS + 400);
    tap_at(FACE_X, FACE_Y);
    until_on();
    run_ms(EXIT_GRACE_MS);
    press_at(FACE_X + 40, FACE_Y + 40);
    run_ms(HOLD_TALK_MS + 300);
    CHECK(w.sm.mode == SM_PET, "nor from the face");
}

static void test_held_ignored_notice_press_does_not_open_grid(void)
{
    world_reset();
    const bool two[2] = {true, false};
    queue_is(2, two);
    step();
    run_ms(400);
    tap_the_notice();
    until_on();
    run_ms(EXIT_GRACE_MS);
    tap_at(EXIT_X, EXIT_Y + 60);
    press_at(CENTRE_X, CENTRE_Y); /* inside the grace: the notice is ignored */
    CHECK(w.out.tap_missed, "the notice press was ignored");
    run_ms(HOLD_TALK_MS + 300);
    CHECK(w.sm.mode == SM_PET, "and an ignored notice press never becomes a hold");
}

static void test_hold_refused_while_turn_in_flight(void)
{
    world_reset();
    step();
    w.net = SM_NET_BUSY; /* a turn abandoned by voice is still on the network */
    press_at(CENTRE_X, CENTRE_Y);
    run_ms(HOLD_TALK_MS + 300);
    CHECK(w.sm.mode == SM_PET, "today's guard");
}

/* ── playback: exit, replay, reply ───────────────────────────────────────────────────────── */

static void test_exit_from_replay_is_one_press(void)
{
    world_reset();
    offer_from(true);
    tap_at(LEFT_X, DISC_Y);
    until_on();
    run_ms(EXIT_GRACE_MS);
    tap_at(EXIT_X, EXIT_Y);
    CHECK(w.sm.mode == SM_PET && !w.sm.offer.up && !w.out.view.offer, "one press stops and leaves");
    CHECK(w.stop >= 1 && last_cue() == CUE_STOP, "with the stop and its cue");
}

static void test_exit_does_not_resurrect_older_offer(void)
{
    world_reset();
    offer_from(true);
    arrive("m2", false);
    run_ms(400);
    tap_the_notice();
    until_on();
    run_ms(EXIT_GRACE_MS);
    tap_at(EXIT_X, EXIT_Y);
    CHECK(w.sm.mode == SM_PET && !w.sm.offer.up, "the old pair does not come back");
}

/* A reply starts from a paused message (0.4.2): the envelope is not there while it talks. */
static void pause_for_reply(void)
{
    tap_at(LEFT_X, DISC_Y);
    CHECK(w.sm.play.pause_on, "paused, so the envelope is up");
    run_ms(400); /* the pause's own cue */
}

static void test_reply_during_replay_goes_to_offer_sender(void)
{
    world_reset();
    offer_from(true);
    tap_at(LEFT_X, DISC_Y);
    until_on();
    run_ms(EXIT_GRACE_MS);
    pause_for_reply();
    tap_at(RIGHT_X, DISC_Y);
    CHECK(w.sm.mode == SM_RECORD && w.sm.rec.to == JP_TO_DAD, "the replay's sender is answered");
}

static void test_reply_during_playback_opens_a_working_recording(void)
{
    /* B1: no pair over the tick and cross, and the tick sends. */
    world_reset();
    play_a_message(true);
    pause_for_reply();
    tap_at(RIGHT_X, DISC_Y);
    CHECK(w.sm.mode == SM_RECORD, "the reply opens a recording");
    CHECK(w.stop_heard == 1, "and the stop counts as heard (§4)");
    run_ms(400); /* the STOPPED_HEARD outcome lands while recording */
    CHECK(!w.out.view.offer && !w.out.view.pair && w.out.view.confirm, "no pair over the tick");
    w.hearing = true;
    run_ms(400);
    w.hearing = false;
    run_ms(EXIT_GRACE_MS);
    tap_at(RIGHT_X, DISC_Y);
    CHECK(sends() == 1 && last_post()->to == JP_TO_DAD, "the tick sends it, to Dad");
}

static void test_reply_under_heard_min_counts_as_heard(void)
{
    world_reset();
    play_a_message(false);
    CHECK(w.out_ms < 3000, "well under the heard threshold");
    pause_for_reply();
    tap_at(RIGHT_X, DISC_Y);
    CHECK(fx_index(SM_FX_STOP, 1) >= 0, "REPLY's STOP carries heard");
    CHECK(fx_index(SM_FX_SILENCE, -1) < 0 && fx_index(SM_FX_STOP, 1) < fx_index(SM_FX_PAUSE, 0),
          "with no SILENCE to hoist it, the STOP goes before the PAUSE(0)");
    CHECK(w.snap.play.kind == JP_PLAY_STOPPED_HEARD, "so it is acknowledged, not handed back");
}

static void test_reply_refused_during_pet_reply_keeps_playing(void)
{
    world_reset();
    play_a_message(true);
    pause_for_reply();
    w.reply = true; /* a pet reply pre-empts the message */
    tap_at(RIGHT_X, DISC_Y);
    CHECK(last_cue() == CUE_BUSY && w.cue_slot[w.ncue - 1] == STREAM_SLOT_REFUSAL, "busy");
    CHECK(w.sm.mode == SM_PLAY && w.stop == 0, "and the message keeps playing");
    w.reply = false;
    run_ms(EXIT_GRACE_MS);
    tap_at(LEFT_X, DISC_Y); /* her own pause cue, still sounding */
    tap_at(LEFT_X, DISC_Y);
    tap_at(RIGHT_X, DISC_Y);
    CHECK(last_cue() == CUE_BUSY && w.sm.mode == SM_PLAY, "nor over one of the panel's cues");
}

static void test_face_ignored_during_replay(void)
{
    world_reset();
    offer_from(true);
    tap_at(LEFT_X, DISC_Y);
    until_on();
    box_add("m2", false); /* even with something waiting */
    run_ms(EXIT_GRACE_MS);
    CHECK(target_at(FACE_X, FACE_Y) == SM_T_IGNORED, "the face is not a control in a replay");
    swipe(+1);
    CHECK(w.sm.mode == SM_PLAY && w.stop == 0, "nor is a swipe");
}

static void test_replay_end_restamps_offer(void)
{
    world_reset();
    offer_from(true);
    run_ms(REPEAT_MS);
    CHECK(w.out.view.offer_standing, "standing");
    tap_at(LEFT_X, DISC_Y);
    until_on();
    engine_end(JP_PLAY_HEARD);
    step();
    CHECK(w.out.view.offer && !w.out.view.offer_standing, "a replay re-arms the pair");
}

static void test_replay_backstop_keeps_offer(void)
{
    world_reset();
    offer_from(true);
    w.claim_ms = 1000000; /* the engine never takes it */
    tap_at(LEFT_X, DISC_Y);
    bool fired = false;
    for (int i = 0; i < 600 && w.sm.mode == SM_PLAY; i++) {
        step();
        fired |= w.out.backstop;
    }
    CHECK(fired && last_cue() == CUE_OOPS, "the unclaimed backstop says so");
    CHECK(w.sm.mode == SM_PET && w.sm.offer.up, "and the replay keeps its offer");

    world_reset(); /* and a replay the box fails */
    offer_from(true);
    tap_at(LEFT_X, DISC_Y);
    until_on();
    engine_end(JP_PLAY_FAILED);
    step();
    CHECK(w.sm.mode == SM_PET && last_cue() == CUE_OOPS && w.out.view.offer,
          "a failed replay says so and keeps the pair");
}

static void test_expiry_in_play_sounds_oops(void)
{
    world_reset();
    w.engine = false;
    arrive("m1", true);
    run_ms(400);
    tap_the_notice();
    run_ms(400);
    CHECK(plays() == 1, "posted");
    w.snap.play.seq = last_post()->seq;
    w.snap.play.kind = JP_PLAY_CANCELLED;
    publish_done(last_post()->seq);
    step();
    CHECK(w.sm.mode == SM_PET && last_cue() == CUE_OOPS, "expired before claim: it says so");
}

static void test_short_stream_ends_run_without_refetch(void)
{
    world_reset();
    w.chain = true;
    const bool two[2] = {true, false};
    queue_is(2, two);
    step();
    run_ms(400);
    tap_the_notice();
    until_on();
    engine_end(JP_PLAY_SHORT);
    step();
    CHECK(w.sm.mode == SM_PET && last_cue() == CUE_OOPS, "a short stream is a failure");
    run_ms(1000);
    CHECK(plays() == 1, "and nothing is fetched again by itself");
}

static void test_offer_uses_play_outcome_sender(void)
{
    world_reset();
    play_a_message(true);
    engine_end(JP_PLAY_HEARD);
    w.snap.cur.who = who("zz", false); /* a later snapshot's cur, about somebody else */
    step();
    CHECK(w.sm.offer.who.from_dad && w.out.view.face.from_dad, "the offer comes from the outcome");
}

static void test_finish_and_exit_in_one_frame(void)
{
    world_reset();
    play_a_message(true);
    run_ms(EXIT_GRACE_MS);
    engine_end(JP_PLAY_HEARD);
    tap_at(EXIT_X, EXIT_Y);
    CHECK(w.sm.mode == SM_PET && !w.sm.offer.up, "the exit wins: one press, no pair");
    CHECK(fx_index(SM_FX_SILENCE, -1) >= 0 &&
              fx_index(SM_FX_CUE, CUE_STOP) > fx_index(SM_FX_SILENCE, -1),
          "with its stop pattern");
}

static void test_exit_during_pet_reply_silences_it_then_sounds_stop(void)
{
    world_reset();
    play_a_message(true);
    run_ms(EXIT_GRACE_MS);
    w.reply = true;
    tap_at(EXIT_X, EXIT_Y);
    const int sil = fx_index(SM_FX_SILENCE, -1), stp = fx_index(SM_FX_CUE, CUE_STOP);
    CHECK(sil >= 0 && stp > sil, "the silence cuts the reply, then the stop's own cue");
    CHECK(fx_index(SM_FX_PAUSE, 0) > sil && fx_index(SM_FX_STOP, -1) > sil, "PAUSE(0), STOP between");
    CHECK(w.out.fx[stp].slot == STREAM_SLOT_PRESS, "in the press slot, at a later seq");
    CHECK(!w.reply, "the reply is cut");
}

static void test_exit_cutting_reply_opens_no_follow_up(void)
{
    world_reset();
    offer_from(true);
    ask_pet();
    w.net = SM_NET_SPOKE;
    step();
    w.net = SM_NET_IDLE;
    w.reply = true;
    run_ms(400);
    const int opened = w.opened;
    tap_at(EXIT_X, EXIT_Y); /* the pair's exit, while the reply sounds */
    run_ms(1000);
    CHECK(w.opened == opened && w.sm.mode == SM_PET, "the cut reply opens no follow-up");
}

static void test_pause_in_run_gap_is_ignored(void)
{
    world_reset();
    w.chain = true;
    w.fetch_ms = 800;
    const bool two[2] = {true, false};
    queue_is(2, two);
    step();
    run_ms(400);
    tap_the_notice();
    until_on();
    engine_end(JP_PLAY_HEARD);
    step();
    CHECK(w.in.audio.stream_id != 0 && w.in.audio.out_ms > 0 && !w.in.audio.stream_live,
          "the gap: the last stream's facts linger");
    CHECK(target_at(LEFT_X, DISC_Y) == SM_T_IGNORED, "PAUSE is ignored there");
    tap_at(LEFT_X, DISC_Y);
    CHECK(w.pause_on == 0 && !w.sm.play.pause_on, "and arms nothing for a stream that ended");
    until_on();
}

static void test_pause_never_outlives_its_stream(void)
{
    /* I5: a pause pressed as the stream drained is not carried onto the next one in the run. */
    world_reset();
    w.chain = true;
    const bool two[2] = {true, false};
    queue_is(2, two);
    step();
    run_ms(400);
    tap_the_notice();
    until_on();
    tap_at(LEFT_X, DISC_Y);
    CHECK(w.sm.play.pause_on, "paused");
    engine_end(JP_PLAY_HEARD); /* it had already drained */
    w.paused = false;
    step();
    until_on();
    CHECK(!w.sm.play.pause_on && !w.out.view.paused, "the next message is not drawn as held");
    CHECK(w.pause_off >= 1, "and the request is withdrawn");
}

/* ── the selection set S ─────────────────────────────────────────────────────────────────── */

static void test_selection_held_while_stop_outcome_pending(void)
{
    world_reset();
    w.defer_stop = true;
    const bool three[3] = {true, false, true};
    queue_is(3, three);
    step();
    run_ms(400);
    tap_the_notice();
    until_on();
    run_ms(EXIT_GRACE_MS);
    tap_at(EXIT_X, EXIT_Y);
    for (int i = 0; i < 5; i++) {
        CHECK(strcmp(w.sm.sel.id, "m1") == 0, "the stopped message keeps the selection");
        CHECK(w.out.view.notice && w.out.view.face.from_dad, "and the notice shows its sender");
        step();
    }
    release_stop(JP_PLAY_RETURNED);
    step();
    CHECK(strcmp(w.sm.sel.id, "m1") == 0 && w.out.view.face.from_dad, "handed back: no gap");
}

static void test_notice_press_during_stop_hold_resolves_at_post(void)
{
    for (int returned = 0; returned < 2; returned++) {
        world_reset();
        w.defer_stop = true;
        const bool two[2] = {true, false};
        queue_is(2, two);
        step();
        run_ms(400);
        tap_the_notice();
        until_on();
        run_ms(EXIT_GRACE_MS);
        tap_at(250, 150);
        run_ms(EXIT_GRACE_MS + 100);
        tap_at(250, 150);
        CHECK(w.sm.mode == SM_PLAY && last_cue() == CUE_PLAY, "the press during the hold is taken");
        run_ms(800);
        CHECK(plays() == 1, "but not posted until the stop's outcome");
        release_stop(returned ? JP_PLAY_RETURNED : JP_PLAY_STOPPED_HEARD);
        run_ms(400);
        CHECK(plays() == 2, "then it posts");
        CHECK(strcmp(last_post()->target.id, returned ? "m1" : "m2") == 0,
              returned ? "the held id, handed back" : "what the selection re-resolves to");
    }
}

static void test_selection_reresolves_by_id_then_index(void)
{
    world_reset();
    const bool three[3] = {true, false, true};
    queue_is(3, three);
    step();
    run_ms(400);
    swipe(+1);
    CHECK(strcmp(w.sm.sel.id, "m2") == 0, "on the second");
    box_remove("m1");
    step();
    CHECK(strcmp(w.sm.sel.id, "m2") == 0, "an older one leaving keeps the id");
    box_remove("m2");
    step();
    CHECK(strcmp(w.sm.sel.id, "m3") == 0 && w.sm.sel.index == 0, "its leaving takes the index");
}

static void test_selection_order_stable_when_cur_leaves_waiting(void)
{
    world_reset();
    w.fetch_ms = 800;
    const bool three[3] = {true, false, true};
    queue_is(3, three);
    step();
    run_ms(400);
    swipe(+1);
    tap_the_notice();
    for (int i = 0; i < 10 && !w.claimed; i++) step();
    CHECK(w.snap.cur.in_waiting && strcmp(w.sm.sel.id, "m2") == 0, "claimed, listed, selected");
    CHECK(w.out.view.sel_n == 1 && w.out.view.sel_m == 3, "1/3 of the run this press started");
    until_on();
    CHECK(!w.snap.cur.in_waiting && strcmp(w.sm.sel.id, "m2") == 0, "begun, unlisted, same place");
    CHECK(w.out.view.sel_m == 3, "and the count does not move at the first byte");
    run_ms(EXIT_GRACE_MS);
    w.down = true; /* a swipe that began after the press */
    w.x = w.y = -1;
    step();
    w.swipe = -1;
    step();
    CHECK(strcmp(w.sm.sel.id, "m1") == 0, "left is still the one before it");
}

static void test_face_cycles_without_skipping(void)
{
    /* A6: the face moves to the next message, and the next press plays that one. */
    world_reset();
    const bool three[3] = {true, false, true};
    queue_is(3, three);
    step();
    run_ms(400);
    tap_the_notice();
    until_on();
    run_ms(EXIT_GRACE_MS);
    tap_at(FACE_X, FACE_Y);
    CHECK(strcmp(w.sm.sel.id, "m2") == 0, "the next one, not two along");
    run_ms(EXIT_GRACE_MS + 400);
    CHECK(strcmp(w.sm.sel.id, "m2") == 0, "and still after the stop's outcome");
    tap_the_notice();
    run_ms(400);
    CHECK(strcmp(last_post()->target.id, "m2") == 0, "the next press plays it");
}

static void test_face_select_of_mid_play_arrival_survives_return_to_pet(void)
{
    world_reset();
    play_a_message(true);
    arrive("m2", false);
    run_ms(EXIT_GRACE_MS);
    CHECK(target_at(FACE_X, FACE_Y) == SM_T_FACE, "an arrival makes the face live");
    tap_at(FACE_X, FACE_Y);
    run_ms(1500);
    CHECK(w.sm.mode == SM_PET && strcmp(w.sm.sel.id, "m2") == 0, "the pick survives PET");
}

static void test_single_message_face_dead_between_claim_and_stream(void)
{
    world_reset();
    w.fetch_ms = 2000;
    arrive("m1", true);
    run_ms(400);
    tap_the_notice();
    run_ms(EXIT_GRACE_MS);
    CHECK(w.claimed && w.snap.cur.in_waiting, "claimed, and the snapshot still lists it");
    CHECK(target_at(FACE_X, FACE_Y) == SM_T_SWALLOW, "one message: the face is dead");
    CHECK(w.out.view.sel_n == 1 && w.out.view.sel_m == 1, "and it is 1/1");
    until_on();
    CHECK(w.out.view.sel_m == 1 && target_at(FACE_X, FACE_Y) == SM_T_SWALLOW, "and after");
}

static void test_old_box_single_message_face_dead(void)
{
    world_reset();
    w.fetch_ms = 1000;
    old_box(1, true);
    step();
    run_ms(400);
    tap_the_notice();
    run_ms(EXIT_GRACE_MS);
    CHECK(w.claimed, "claimed");
    CHECK(target_at(FACE_X, FACE_Y) == SM_T_SWALLOW && w.out.view.sel_m == 1, "1/1, face dead");
    until_on();
    CHECK(target_at(FACE_X, FACE_Y) == SM_T_SWALLOW && w.out.view.sel_m == 1, "and after");
}

static void test_counter_counts_the_run_from_the_press(void)
{
    world_reset();
    w.chain = true;
    const bool three[3] = {true, false, true};
    queue_is(3, three);
    step();
    run_ms(400);
    swipe(+1);
    CHECK(w.out.view.sel_n == 2 && w.out.view.sel_m == 3, "she picked 2/3");
    tap_the_notice();
    until_on();
    CHECK(w.out.view.sel_n == 1 && w.out.view.sel_m == 3, "and the run reads 1/3");
    engine_end(JP_PLAY_HEARD);
    step();
    CHECK(w.out.view.sel_n == 2 && w.out.view.sel_m == 3, "2/3 from the seeding, no gap");
    until_on();
    CHECK(w.out.view.sel_n == 2 && w.out.view.sel_m == 3, "2/3 at the first byte");
    engine_end(JP_PLAY_HEARD);
    step();
    until_on();
    CHECK(w.out.view.sel_n == 3 && w.out.view.sel_m == 3, "3/3");
}

static void test_counter_skips_an_id_that_answered_204(void)
{
    world_reset();
    w.chain = true;
    const bool three[3] = {true, false, true};
    queue_is(3, three);
    step();
    run_ms(400);
    tap_the_notice();
    until_on();
    engine_end(JP_PLAY_HEARD); /* the run seeds m2 */
    step();
    CHECK(w.out.view.sel_n == 2, "2 from the seeding");
    box_remove("m2"); /* played elsewhere: 204, skipped without beginning */
    w.snap.cur.who = w.snap.waiting.q[0];
    w.snap.cur.pos = 0;
    step();
    until_on();
    CHECK(w.out.view.sel_n == 2 && w.out.view.sel_m == 2, "the skipped id does not advance it");
}

static void test_an_unseen_id_sends_the_selection_home(void)
{
    world_reset();
    const bool three[3] = {true, false, true};
    queue_is(3, three);
    step();
    run_ms(400);
    swipe(+1);
    swipe(+1);
    CHECK(strcmp(w.sm.sel.id, "m3") == 0, "pointing at the third");
    box_remove("m1");
    step();
    CHECK(strcmp(w.sm.sel.id, "m3") == 0, "a count drop from playing does not move it");
    arrive("m4", true);
    CHECK(w.sm.sel.index == 0 && strcmp(w.sm.sel.id, "m2") == 0, "an arrival in PET sends it home");
}

static void test_old_box_arrival_during_pending_press_keeps_its_target(void)
{
    world_reset();
    old_box(2, true);
    step();
    run_ms(400);
    swipe(+1);
    tap_the_notice();
    old_box(3, true); /* a third arrives before the press is posted */
    step();
    run_ms(400);
    CHECK(plays() == 1 && last_post()->at == 1, "the press still asks for what she chose");
}

static void test_later_swipe_at_clamped_end_stops_nothing(void)
{
    world_reset();
    const bool two[2] = {true, false};
    queue_is(2, two);
    step();
    run_ms(400);
    swipe(+1);
    tap_the_notice();
    until_on();
    run_ms(EXIT_GRACE_MS);
    swipe(+1);
    CHECK(w.sm.mode == SM_PLAY && w.stop == 0, "at the end, a later swipe stops nothing");
}

static void test_smear_from_notice_never_plays(void)
{
    for (int lag = 1; lag <= 3; lag += 2) {
        world_reset();
        arrive("m1", true);
        run_ms(400);
        int x, y;
        notice_centre(&x, &y);
        press_at(x, y);
        CHECK(w.sm.mode == SM_PLAY, "the down edge pressed the notice");
        for (int i = 1; i < lag; i++) step();
        w.swipe = 1;
        step();
        CHECK(w.sm.mode == SM_PET && last_cue() == CUE_HEARD, "the smear unwinds it");
        lift();
        run_ms(1000);
        CHECK(!w.begun && w.out_ms == 0, "and a lone message is never played by a smear");
    }
}

static void test_more_than_eight_waiting(void)
{
    world_reset();
    char id[16];
    for (int i = 0; i < 10; i++) {
        snprintf(id, sizeof(id), "m%d", i + 1);
        box_add(id, i % 2 == 0);
    }
    CHECK(w.snap.waiting.n == JPANEL_QUEUE_MAX && w.snap.waiting.count == 10, "eight of ten");
    step();
    run_ms(400);
    CHECK(w.out.view.sel_m == 10, "the counter adds the undescribed rest");
    for (int i = 0; i < 12; i++) swipe(+1);
    CHECK(w.sm.sel.index == JPANEL_QUEUE_MAX - 1, "the swipe clamps at the last described");
    const int chimes = count_cue(CUE_MESSAGE);
    box_remove("m1");
    w.snap.waiting.q[w.snap.waiting.n++] = who("m9", true); /* scrolls into the eight */
    step();
    CHECK(count_cue(CUE_MESSAGE) == chimes && !w.out.arrived, "a scroll-in is not an arrival");
    CHECK(strcmp(w.sm.sel.id, "m8") == 0, "and does not send the selection home");
}

static bool is_seen(const char *id)
{
    for (int j = 0; j < w.sm.seen_n; j++) {
        if (strcmp(w.sm.seen_ids[j], id) == 0) return true;
    }
    return false;
}

static void test_seen_ids_never_evicts_a_waiting_or_current_id(void)
{
    /* Seven stay waiting while forty others pass through the eighth place: the seen list is
       sixteen long, so it must evict, and it must never evict one that is still waiting. */
    world_reset();
    char id[16];
    for (int i = 0; i < 7; i++) {
        snprintf(id, sizeof(id), "w%d", i);
        box_add(id, true);
    }
    step();
    run_ms(400);
    for (int i = 0; i < 40; i++) {
        snprintf(id, sizeof(id), "x%d", i);
        box_add(id, false);
        run_ms(200);
        box_remove(id);
        step();
    }
    for (int i = 0; i < 7; i++) {
        snprintf(id, sizeof(id), "w%d", i);
        CHECK(is_seen(id), "churn never evicts a waiting id");
    }
    CHECK(count_cue(CUE_MESSAGE) == 41, "each passer-by chimed once, the seven never again");
}

/* ── new messages and the chime ──────────────────────────────────────────────────────────── */

static void test_each_new_id_chimes_once(void)
{
    world_reset();
    arrive("m1", true);
    run_ms(400);
    arrive("m2", false);
    run_ms(400);
    CHECK(count_cue(CUE_MESSAGE) == 2, "each new id chimes");
    box_remove("m1");
    step();
    box_add("m1", true); /* the same id again, e.g. handed back */
    run_ms(400);
    CHECK(count_cue(CUE_MESSAGE) == 2, "an id already announced is never announced again");
}

static void test_chime_owed_flag_without_ids(void)
{
    world_reset();
    ask_pet();
    old_box(1, true);
    step();
    CHECK(count_cue(CUE_MESSAGE) == 0, "no chime during THINK");
    w.net = SM_NET_SPOKE;
    step();
    w.net = SM_NET_IDLE;
    step();
    CHECK(count_cue(CUE_MESSAGE) == 1, "owed, and sounded when she can act");
    world_reset();
    ask_pet();
    old_box(1, true);
    step();
    old_box(0, true);
    step();
    w.net = SM_NET_SPOKE;
    step();
    run_ms(400);
    CHECK(count_cue(CUE_MESSAGE) == 0, "a count that fell to 0 drops the flag");
}

static void test_arrival_played_in_run_does_not_chime_after(void)
{
    world_reset();
    w.chain = true;
    play_a_message(true);
    arrive("m2", false);
    CHECK(count_cue(CUE_MESSAGE) == 1, "no chime during PLAY");
    engine_end(JP_PLAY_HEARD);
    step();
    until_on();
    engine_end(JP_PLAY_HEARD);
    run_ms(1000);
    CHECK(count_cue(CUE_MESSAGE) == 1, "played in the run it arrived during: never chimed");
}

static void test_arrival_during_think_chimes_after_reply(void)
{
    world_reset();
    ask_pet();
    arrive("m1", true);
    CHECK(count_cue(CUE_MESSAGE) == 0 && !w.out.view.notice, "THINK: no chime, no notice");
    w.net = SM_NET_SPOKE;
    w.reply = true; /* talk submits the reply before it says SPOKE */
    step();
    w.net = SM_NET_IDLE;
    run_ms(2000);
    CHECK(count_cue(CUE_MESSAGE) == 0, "not over the reply");
    w.reply = false;
    step();
    CHECK(count_cue(CUE_MESSAGE) == 1, "after it");
    CHECK(w.sm.mode == SM_PET, "and the owed chime disarmed the follow-up");
}

static void test_message_during_30s_recording_chimes_after(void)
{
    world_reset();
    say(SM_VOICE_SEND_DAD);
    w.hearing = true;
    run_ms(5000);
    arrive("m1", true);
    run_ms(20000);
    CHECK(count_cue(CUE_MESSAGE) == 0, "not into a recording");
    w.hearing = false;
    run_ms(LISTEN_HUSH_MS + 200);
    CHECK(sends() == 1 && count_cue(CUE_MESSAGE) == 1, "sent, then chimed");
    CHECK(w.out.view.notice_big, "with its big box, counted from now");
}

static void test_message_during_pet_reply_gets_chime_and_big_box(void)
{
    world_reset();
    w.reply = true;
    step();
    arrive("m1", true);
    run_ms(1000);
    CHECK(count_cue(CUE_MESSAGE) == 0, "the chime waits out the reply");
    w.reply = false;
    step();
    CHECK(count_cue(CUE_MESSAGE) == 1 && w.out.view.notice_big, "then chimes, big");
}

static void test_chime_waits_for_grid_to_close(void)
{
    world_reset();
    open_grid();
    arrive("m1", true);
    run_ms(2000);
    CHECK(count_cue(CUE_MESSAGE) == 0 && !w.out.view.notice, "nothing over the menu");
    run_ms(SENDTO_MS);
    CHECK(w.sm.mode == SM_PET && count_cue(CUE_MESSAGE) == 1, "it chimes when the grid closes");
    CHECK(w.out.view.notice_big, "and the box is big from first visibility");
}

static void test_notice_clock_starts_when_first_drawn(void)
{
    world_reset();
    ask_pet();
    arrive("m1", true);
    run_ms(POPUP_BIG_MS + 2000);
    CHECK(!w.out.view.notice, "hidden at arrival behind the bubble");
    w.net = SM_NET_SPOKE;
    step();
    CHECK(w.out.view.notice && w.out.view.notice_big, "big when it is first drawn");
}

static void test_a_returned_message_reappears_as_a_badge(void)
{
    world_reset();
    play_a_message(true);
    run_ms(EXIT_GRACE_MS);
    tap_at(EXIT_X, EXIT_Y);
    run_ms(400);
    CHECK(box_find("m1") >= 0, "handed back");
    CHECK(w.out.view.notice && !w.out.view.notice_big, "as a badge: owed no chime");
    CHECK(count_cue(CUE_MESSAGE) == 1, "and announced no second time");
}

static void test_poke_during_outcome_still_sounds_outcome(void)
{
    world_reset();
    send_one();
    w.engine = false;
    w.snap.send.seq = last_post()->seq;
    w.snap.send.kind = JP_SEND_SENT;
    tap_at(CENTRE_X, CENTRE_Y);
    CHECK(w.out.pet_press, "the pet was poked");
    CHECK(last_cue() == CUE_SENT && w.cue_slot[w.ncue - 1] == STREAM_SLOT_SEND_RESULT,
          "and the outcome sounds in its own slot");
}

static void test_standby_after_tick_still_sends(void)
{
    world_reset();
    say(SM_VOICE_SEND_DAD);
    w.hearing = true;
    run_ms(400);
    w.hearing = false;
    run_ms(EXIT_GRACE_MS);
    tap_at(RIGHT_X, DISC_Y);
    CHECK(sends() == 1, "the tick sent it");
    w.btn_short = true;
    step();
    run_ms(3000);
    CHECK(saw_cue(CUE_SENT), "standby does not cancel a send");
}

/* ── standby, voice, follow-up ───────────────────────────────────────────────────────────── */

static void test_standby_silences_then_sounds_its_own_stop(void)
{
    world_reset();
    w.reply = true;
    step();
    w.btn_short = true;
    step();
    CHECK(w.out.fx[0].kind == SM_FX_SILENCE, "the silence first");
    CHECK(w.out.fx[1].kind == SM_FX_CUE && w.out.fx[1].arg == CUE_STOP &&
              w.out.fx[1].slot == STREAM_SLOT_PRESS,
          "then CUE_STOP, at a later seq");
    CHECK(!w.reply, "the reply sounding is cut");
}

static void test_button_and_touch_in_one_frame_drops_touch(void)
{
    world_reset();
    arrive("m1", true);
    run_ms(400);
    int x, y;
    notice_centre(&x, &y);
    w.btn_short = true;
    w.voice = SM_VOICE_WAKE;
    tap_at(x, y);
    CHECK(w.standby && w.sm.mode == SM_PET, "standby");
    CHECK(!saw_cue(CUE_PLAY) && w.opened == 0, "the queued press and phrase are dropped");
    run_ms(1000);
    CHECK(plays() == 0, "nothing plays in the dark");
}

static void test_wake_from_standby_sounds_heard(void)
{
    world_reset();
    w.btn_short = true;
    step();
    w.btn_short = true;
    step();
    CHECK(!w.standby && last_cue() == CUE_HEARD, "a press in standby wakes it with the coin");
}

static void test_standby_during_think_reply_plays_without_follow_up(void)
{
    world_reset();
    ask_pet();
    w.btn_short = true;
    step();
    CHECK(w.talk_clear == 0, "no TALK_CLEAR: an answer in flight still plays when it lands");
    CHECK(w.sm.turn.state == SM_TURN_NONE, "but the machine's turn is over");
    w.net = SM_NET_SPOKE;
    w.reply = true;
    step();
    w.net = SM_NET_IDLE;
    run_ms(400);
    w.btn_short = true; /* woken while the reply still plays */
    step();
    run_ms(400);
    w.reply = false;
    const int opened = w.opened;
    run_ms(2000);
    CHECK(w.opened == opened, "and nothing follows it, even awake");
}

static void test_voice_stop_during_replay_abandons_turn_without_follow_up(void)
{
    world_reset();
    offer_from(true);
    ask_pet();
    tap_at(LEFT_X, DISC_Y);
    CHECK(w.sm.mode == SM_PLAY && w.sm.play.replay, "AGAIN works mid-turn");
    say(SM_VOICE_STOP);
    CHECK(w.sm.turn.state == SM_TURN_NONE && w.talk_clear >= 1, "the turn is abandoned");
    CHECK(w.sm.mode == SM_PLAY, "the replay plays on");
    w.net = SM_NET_SPOKE;
    step();
    w.reply = true;
    run_ms(400);
    w.reply = false;
    const int opened = w.opened;
    run_ms(1000);
    CHECK(w.opened == opened, "and the late answer opens no follow-up");
}

static void test_again_during_think_then_failed_turn_still_dashes(void)
{
    world_reset();
    offer_from(true);
    ask_pet();
    tap_at(LEFT_X, DISC_Y);
    until_on();
    w.net = SM_NET_FAILED;
    step();
    CHECK(w.sm.dashes == 1 && saw_cue(CUE_OOPS), "the turn's failure is counted in PLAY");
    engine_end(JP_PLAY_HEARD);
    step();
    CHECK(w.out.view.think && w.out.view.dash, "and the dash is drawn back in PET");
}

static void test_follow_up_open_handles_refusal(void)
{
    world_reset();
    ask_pet();
    w.net = SM_NET_SPOKE;
    step();
    w.net = SM_NET_IDLE;
    w.reply = true;
    step();
    w.leased = true; /* an upload holds the capture */
    w.reply = false;
    const int cues = w.ncue;
    step();
    CHECK(w.sm.mode == SM_PET && !w.sm.talk.follow_armed, "refused: disarmed, not deferred");
    CHECK(w.ncue == cues, "and silent: nobody asked for it");
    w.leased = false;

    world_reset(); /* the executor refuses the capture itself */
    ask_pet();
    w.net = SM_NET_SPOKE;
    step();
    w.net = SM_NET_IDLE;
    w.reply = true;
    step();
    w.refuse_capture = true;
    w.reply = false;
    step();
    CHECK(w.sm.mode == SM_LISTEN, "the follow-up opened");
    const int before = w.ncue;
    step();
    CHECK(w.sm.mode == SM_PET && w.ncue == before, "the refused capture returns silently");
}

static void test_chime_and_follow_up_in_one_frame(void)
{
    world_reset();
    ask_pet();
    w.net = SM_NET_SPOKE;
    step();
    w.net = SM_NET_IDLE;
    w.reply = true;
    step();
    arrive("m1", true);
    w.reply = false;
    step();
    CHECK(w.sm.mode == SM_PET && count_cue(CUE_MESSAGE) == 1, "the chime wins the reply's end");
}

static void test_follow_up_yields_to_pending_press(void)
{
    world_reset();
    arrive("m1", true);
    run_ms(400);
    ask_pet();
    w.net = SM_NET_SPOKE;
    step();
    w.net = SM_NET_IDLE;
    w.reply = true;
    step();
    tap_the_notice();
    CHECK(w.sm.mode == SM_PLAY, "pressed during the reply");
    w.reply = false;
    run_ms(400);
    CHECK(w.sm.mode == SM_PLAY && plays() == 1, "the press is served; no follow-up");
}

static void test_press_behind_pet_reply_is_served(void)
{
    world_reset();
    arrive("m1", true);
    run_ms(400);
    w.reply = true;
    tap_the_notice();
    run_ms(PLAY_BUF_MS);
    CHECK(w.sm.mode == SM_PLAY && plays() == 0, "it waits out a ten-second reply");
    w.reply = false;
    run_ms(200);
    CHECK(plays() == 1, "and is served");
}

static void test_wake_phrase_refused_during_failed_dash(void)
{
    world_reset();
    ask_pet();
    w.net = SM_NET_FAILED;
    step();
    w.net = SM_NET_IDLE;
    say(SM_VOICE_WAKE);
    CHECK(w.sm.mode == SM_PET && last_cue() == CUE_BUSY, "refused, and it says busy");
}

static void test_every_refusal_sounds_busy(void)
{
    CHECK(CUE_BUSY != CUE_STOP && CUE_BUSY != CUE_OOPS, "busy is its own sound");
    world_reset();
    play_a_message(true);
    say(SM_VOICE_WAKE);
    CHECK(last_cue() == CUE_BUSY && w.cue_slot[w.ncue - 1] == STREAM_SLOT_REFUSAL, "wake in PLAY");
    world_reset();
    step();
    w.leased = true;
    say(SM_VOICE_SEND_PANEL);
    CHECK(last_cue() == CUE_BUSY && w.sm.mode == SM_PET, "a phrase while leased");
    w.leased = false;
    world_reset();
    offer_from(true);
    ask_pet();
    tap_at(RIGHT_X, DISC_Y);
    CHECK(last_cue() == CUE_BUSY && w.sm.offer.up, "REPLY mid-turn, offer kept");
    world_reset();
    open_grid();
    w.cue_until = w.now + 1000;
    tap_at(FACE_X, FACE_Y);
    CHECK(last_cue() == CUE_BUSY && w.sm.mode == SM_PET, "a refused cell closes the grid");
}

static void test_notice_and_again_refused_during_send(void)
{
    world_reset();
    offer_from(true);
    w.send_ms = 10000;
    say(SM_VOICE_SEND_DAD);
    hear_then_hush();
    CHECK(w.out.view.sending && w.out.view.offer, "sending, with the pair drawn");
    tap_at(LEFT_X, DISC_Y);
    CHECK(last_cue() == CUE_BUSY && w.sm.mode == SM_PET, "AGAIN is busy");
    arrive("m2", false);
    run_ms(EXIT_GRACE_MS);
    tap_the_notice();
    CHECK(last_cue() == CUE_BUSY && w.sm.mode == SM_PET, "and so is the notice");
}

static void test_silent_recording_says_dropped(void)
{
    world_reset();
    open_grid();
    tap_at(FACE_X, FACE_Y);
    CHECK(w.sm.mode == SM_RECORD, "recording");
    run_ms(RECORD_LEAD_MS + 400);
    CHECK(w.sm.mode == SM_PET && last_cue() == CUE_STOP, "nobody spoke: dropped, said so");
    CHECK(w.cue_slot[w.ncue - 1] == STREAM_SLOT_SEND_RESULT, "in the send_result slot");
}

static void test_stray_touch_in_record_is_ignored_and_never_drops(void)
{
    world_reset();
    say(SM_VOICE_SEND_DAD);
    w.hearing = true;
    for (int i = 0; i < 20; i++) tap_at(20 + i * 15, 30 + i * 8);
    CHECK(w.sm.mode == SM_RECORD && w.dropped == 0, "stray touches never drop a recording");
    CHECK(w.tap_missed == 20 && w.flinch == 0, "they are logged, and nothing twitches");
    w.hearing = false;
}

static void test_upload_bytes_unchanged_under_listen_attempts(void)
{
    /* A5, the machine's half: while an upload holds the capture, nothing opens it. */
    world_reset();
    w.send_ms = 20000;
    send_one();
    CHECK(w.leased, "the upload holds lease 1");
    const int opened = w.opened;
    say(SM_VOICE_WAKE);
    CHECK(last_cue() == CUE_BUSY, "the name is refused, busy");
    say(SM_VOICE_SEND_DAD);
    CHECK(last_cue() == CUE_BUSY, "so is \"tell dad\"");
    run_ms(400);
    open_grid();
    tap_at(FACE_W * 3 / 4, FACE_H * 3 / 4 + 40);
    CHECK(last_cue() == CUE_BUSY, "and the grid's pet cell");
    tap_at(CENTRE_X, CENTRE_Y);
    CHECK(w.opened == opened && !w.capture_open, "the microphone never opened under the upload");
}

static void test_message_arriving_mid_run_joins_it(void)
{
    world_reset();
    w.chain = true;
    play_a_message(true);
    CHECK(w.out.view.sel_n == 1 && w.out.view.sel_m == 1, "1/1");
    arrive("m2", false);
    CHECK(w.out.view.sel_m == 2 && strcmp(w.sm.sel.id, "m1") == 0,
          "an arrival joins the run's count and leaves the selection on what plays");
    engine_end(JP_PLAY_HEARD);
    step();
    CHECK(w.sm.mode == SM_PLAY && w.out.view.sel_n == 2 && !w.out.view.face.from_dad,
          "and the run plays it next");
}

/* ── the engine's other answers ──────────────────────────────────────────────────────────── */

static void test_send_backstop_then_wedged_engine_reboots(void)
{
    /* The machine's half: the backstop sounds once; a late outcome is logged, never sounded. The
       watchdog's reboot is the executor's (jpanel_tests). */
    world_reset();
    w.send_ms = JP_SEND_EXPIRE_MS + JP_UI_MARGIN_MS + 10000;
    send_one();
    bool fired = false;
    for (int i = 0; i < 2000 && !fired; i++) {
        step();
        fired = w.out.backstop;
    }
    CHECK(fired && last_cue() == CUE_OOPS && !w.out.view.sending, "the send backstop says so");
    bool late = false;
    for (int i = 0; i < 200 && !late; i++) {
        step();
        late = w.out.late_outcome;
    }
    CHECK(late && !saw_cue(CUE_SENT), "the late outcome is logged and not sounded");
}

static void test_post_failure_sounds_oops(void)
{
    world_reset();
    arrive("m1", true);
    run_ms(400);
    w.post_fail = true;
    tap_the_notice();
    run_ms(400);
    CHECK(w.sm.mode == SM_PET && last_cue() == CUE_OOPS, "a PLAY that could not be posted");
    world_reset();
    w.post_fail = true;
    say(SM_VOICE_SEND_PANEL);
    hear_then_hush();
    CHECK(!w.out.view.sending && last_cue() == CUE_OOPS, "and a SEND");
}

static void test_capture_refused_returns_to_pet_with_busy(void)
{
    world_reset();
    w.refuse_capture = true;
    say(SM_VOICE_SEND_DAD);
    step();
    CHECK(w.sm.mode == SM_PET && last_cue() == CUE_BUSY && !w.sm.capture, "refused, said busy");
}

static void test_calibration_drops_recording_and_keeps_offer(void)
{
    world_reset();
    offer_from(true);
    say(SM_VOICE_SEND_PANEL);
    w.calibrating = true;
    step();
    CHECK(w.sm.mode == SM_PET && w.dropped == 1 && !saw_cue(CUE_OOPS), "dropped silently");
    CHECK(has_fx(SM_FX_STOP) && fx_index(SM_FX_PAUSE, 0) >= 0, "PAUSE(0) and STOP");
    run_ms(2000);
    w.calibrating = false;
    step();
    CHECK(w.out.view.offer, "the offer is kept");
    world_reset();
    ask_pet();
    w.calibrating = true;
    run_ms(3000);
    w.net = SM_NET_SPOKE; /* landed during calibration */
    w.calibrating = false;
    step();
    CHECK(w.sm.turn.state == SM_TURN_NONE && w.nod == 1 && w.sm.dashes == 0,
          "read on the first frame back: a reply, not a timeout");
    /* Longer than the turn's own timeout: the turn is still read, not recovered away. */
    world_reset();
    ask_pet();
    w.calibrating = true;
    run_ms(TALK_TIMEOUT_MS + 5000);
    CHECK(w.sm.turn.state == SM_TURN_THINKING && w.out.violations == 0,
          "a long calibration leaves the turn running");
    w.net = SM_NET_SPOKE;
    w.calibrating = false;
    step();
    CHECK(w.sm.turn.state == SM_TURN_NONE && w.nod == 1 && w.sm.dashes == 0,
          "and the reply that landed in it is still read");
    world_reset();
    ask_pet();
    w.calibrating = true;
    run_ms(TALK_TIMEOUT_MS + 5000);
    w.calibrating = false;
    step();
    CHECK(w.sm.dashes == 1 && last_cue() == CUE_OOPS, "while one that never came is a dash");
}

static void test_deadlines_survive_u32_wrap(void)
{
    world_reset();
    w.now = 0xFFFFFFFFu - 3000u;
    sm_init(&w.sm, w.now);
    open_grid();
    run_ms(SENDTO_MS + 400);
    CHECK(w.sm.mode == SM_PET, "the grid closes across the wrap");
    world_reset();
    w.now = 0xFFFFFFFFu - 3000u;
    sm_init(&w.sm, w.now);
    offer_from(true);
    CHECK(!w.out.view.offer_standing, "a fresh pair across it");
    run_ms(REPEAT_MS);
    CHECK(w.out.view.offer_standing, "and standing ten seconds on");
    world_reset();
    w.now = 0xFFFFFFFFu - 3000u;
    sm_init(&w.sm, w.now);
    arrive("m2", false);
    run_ms(400);
    tap_the_notice();
    for (int i = 0; i < 250; i++) {
        w.cue_until = w.now + FAKE_CUE_MS;
        step();
    }
    w.cue_until = 0;
    CHECK(w.sm.mode == SM_PET && last_cue() == CUE_OOPS, "a pending press expires across it");
}

/* I2 in the other direction: whoever REPLY would record to is the face drawn, every frame. */
static void reply_recipient_matches_face(void)
{
    sample();
    w.in.down = true;
    w.in.x = RIGHT_X;
    w.in.y = DISC_Y;
    if (sm_target(&w.sm, &w.in, RIGHT_X, DISC_Y) != SM_T_REPLY) return;
    jp_who_t face;
    if (!sm_face(&w.sm, &w.in.snap, &face)) return;
    sm_t probe = w.sm;
    sm_out_t out;
    sm_step(&probe, &w.in, &out);
    if (probe.mode != SM_RECORD) return; /* refused */
    CHECK(probe.rec.to == (face.from_dad ? JP_TO_DAD : JP_TO_PANEL) &&
              strcmp(probe.rec.who.from, face.from) == 0,
          "REPLY records to the face drawn");
}

static void test_sm_face_is_the_reply_recipient_every_frame(void)
{
    world_reset();
    w.chain = true;
    const bool q[3] = {true, false, true};
    queue_is(3, q);
    int replies = 0;
    for (int i = 0; i < 400; i++) {
        if (i == 5) tap_the_notice();
        if (w.sounding && w.out_ms > 1200) engine_end(JP_PLAY_HEARD);
        if (i == 200 && w.out.view.offer) tap_at(LEFT_X, DISC_Y);
        sample();
        replies += sm_target(&w.sm, &w.in, RIGHT_X, DISC_Y) == SM_T_REPLY;
        reply_recipient_matches_face();
        step();
    }
    CHECK(replies > 50, "and REPLY was live for much of it");
}


/* ── the storm (§8.5) ─────────────────────────────────────────────────────────────────────── */

static uint32_t rng;

static uint32_t rnd(void)
{
    rng ^= rng << 13;
    rng ^= rng >> 17;
    rng ^= rng << 5;
    return rng;
}

static int pick(int n)
{
    return (int)(rnd() % (uint32_t)n);
}

static bool chance(int per_mille)
{
    return pick(1000) < per_mille;
}

#define STORM_LAG 25 /* 2 s of snapshot delay at 80 ms a frame, under UI_MARGIN - LINK_STEP */
#define STORM_HIST 32
#define LINK_STEP 250

static void storm_arrive(int *next_id, bool ids)
{
    if (ids) {
        char id[16];
        snprintf(id, sizeof(id), "s%d", (*next_id)++);
        box_add(id, chance(500));
    } else {
        old_box(w.snap.waiting.count + 1, chance(500));
    }
}

static void storm_leave(bool ids)
{
    if (w.snap.waiting.count <= 0) return;
    if (ids) {
        if (w.snap.waiting.n > 0 && !(w.claimed && strcmp(w.snap.cur.who.id, w.snap.waiting.q[0].id) == 0)) {
            box_remove(w.snap.waiting.q[0].id);
        }
    } else {
        old_box(w.snap.waiting.count - 1, chance(500));
    }
}

/* A point a child might aim at, or anywhere at all. */
static void storm_point(int *x, int *y)
{
    static const int pts[][2] = {
        {CENTRE_X, CENTRE_Y}, {EXIT_X, EXIT_Y}, {FACE_X, FACE_Y}, {LEFT_X, DISC_Y},
        {RIGHT_X, DISC_Y},    {250, 150},       {92, 336},        {276, 336},
        {120, 150},           {184, 300},
    };
    if (chance(250)) {
        *x = pick(FACE_W);
        *y = pick(FACE_H);
        return;
    }
    const int i = pick((int)(sizeof(pts) / sizeof(pts[0])));
    *x = pts[i][0] + pick(21) - 10;
    *y = pts[i][1] + pick(21) - 10;
}

static void storm_run(uint32_t seed, int frames, bool ids, uint32_t start)
{
    world_reset();
    rng = seed;
    w.now = start;
    sm_init(&w.sm, w.now);
    w.snap.waiting.has_ids = ids;
    static jp_view_t hist[STORM_HIST];
    int head = 0, shown = 0;
    for (int i = 0; i < STORM_HIST; i++) hist[i] = w.snap;
    int next_id = 1, hold = 0, cal = 0, bounce = 0, bx = 0, by = 0;
    uint32_t talk_at = 0, reply_until = 0, jumped_at = w.now - 100000;
    int visits[SM_PLAY + 1] = {0};
    w.msg_len = 3000;

    for (int f = 0; f < frames; f++) {
        /* the box and the engine, which runs on its own task */
        if (chance(6)) storm_arrive(&next_id, ids);
        if (chance(2)) storm_leave(ids);
        if (w.cmd_waiting && chance(5)) w.claim_ms = (uint32_t)pick(3000); /* up to the margin */
        if (w.sounding && w.out_ms >= w.msg_len) {
            const int k = pick(20);
            w.chain = chance(500);
            engine_end(k < 16 ? JP_PLAY_HEARD : k < 18 ? JP_PLAY_SHORT : JP_PLAY_FAILED);
            w.msg_len = 600 + (uint32_t)pick(6000);
        }
        if (w.claimed && !w.begun) {
            /* overrun a published fetch deadline by up to LINK_STEP_MS */
            w.snap.deadline_ms = w.claimed_at + w.fetch_ms - (uint32_t)pick(LINK_STEP);
        }
        if (chance(3)) w.fetch_ms = 80 + (uint32_t)pick(1500);
        if (chance(3)) w.send_kind = (jp_send_kind_t)(1 + pick(4));
        if (chance(3)) w.send_ms = 300 + (uint32_t)pick(5000);

        /* talk */
        if (w.net == SM_NET_BUSY && talk_at == 0) talk_at = w.now + 500 + (uint32_t)pick(6000);
        if (talk_at != 0 && jp_due(w.now, talk_at)) {
            talk_at = 0;
            const int k = pick(10);
            w.net = k < 7 ? SM_NET_SPOKE : k < 9 ? SM_NET_FAILED : SM_NET_IDLE;
            if (w.net == SM_NET_SPOKE) {
                w.reply = true;
                reply_until = w.now + 500 + (uint32_t)pick(4000);
            }
        } else if (w.net == SM_NET_SPOKE || w.net == SM_NET_FAILED) {
            if (chance(300)) w.net = SM_NET_IDLE;
        }
        if (w.reply && jp_due(w.now, reply_until)) w.reply = false;
        w.hearing = w.capture_open && (chance(950) ? w.hearing : !w.hearing);
        if (w.capture_open && chance(20)) w.hearing = true;

        /* the finger, the voice, the button */
        if (cal > 0) {
            if (--cal == 0) w.calibrating = false;
        } else if (chance(1)) {
            cal = 20 + pick(40);
            w.calibrating = true;
        }
        if (hold > 0) {
            if (--hold == 0) {
                w.up = true;
                w.finger = false;
            }
        } else if (bounce > 0) {
            bounce = 0;
            w.down = w.up = true; /* the same press, repeated: a lightening finger */
            w.x = bx;
            w.y = by;
        } else if (chance(70)) {
            storm_point(&bx, &by);
            w.down = true;
            w.x = bx;
            w.y = by;
            if (chance(150)) {
                hold = 3 + pick(20);
                w.finger = true;
            } else {
                w.up = true;
                if (chance(200)) bounce = 1;
            }
        }
        if (hold > 0 && chance(60)) w.swipe = chance(500) ? 1 : -1;
        if (chance(5)) w.voice = (sm_voice_t)(1 + pick(4));
        if (chance(2)) w.btn_short = true;
        if (w.standby && chance(20)) w.wake = true;
        if (chance(1)) w.dark_wake = true;
        if (chance(3)) w.gest_taps = pick(3);

        /* the snapshot render gets: dropped (kept), delayed (at most 2 s), or duplicated */
        hist[head] = w.snap;
        const int newest = f;
        int lag = pick(STORM_LAG + 1);
        int want = newest - lag;
        if (want < shown) want = shown;
        if (newest - want > STORM_LAG) want = newest - STORM_LAG;
        if (want < 0) want = 0;
        shown = want;
        w.delivered = &hist[(head - (newest - shown) + STORM_HIST * 4) % STORM_HIST];

        const bool exempt = !jp_due(w.now, jumped_at + 3000);
        step();
        CHECK(!w.out.backstop || exempt, "no backstop fires against an engine that keeps time");
        CHECK(!w.out.late_outcome || exempt, "so no outcome is ever late");
        head = (head + 1) % STORM_HIST;
        visits[w.sm.mode]++;

        /* a clock jump, bounded below UI_MARGIN_MS - LINK_STEP_MS with the delay above */
        if (chance(2) && jp_due(w.now, jumped_at + 5000)) {
            const uint32_t j = (uint32_t)pick(400);
            w.now += j;
            jumped_at = w.now;
        }
    }
    w.delivered = NULL;
    /* A storm that never reached a mode proves nothing about it. */
    for (int m = SM_PET; m <= SM_PLAY; m++) CHECK(visits[m] > frames / 1000, "every mode is visited");
    CHECK(plays() > 20 && sends() > 5 && w.stop > 20 && count_cue(CUE_BUSY) > 5 &&
              count_cue(CUE_MESSAGE) > 5 && w.pause_on > 0 && w.standby_on > 0,
          "and the storm played, sent, stopped, refused, chimed, paused and slept");
}

static void test_storm(void)
{
    /* 300k seeded frames: snapshots dropped, delayed and duplicated; touches dropped and
       repeated; clock jumps and the u32 wrap; calibration; an engine overrunning its deadlines
       by a link step and claiming up to the margin; thirty-second recordings with arrivals. Every
       frame asserts I1-I7, the drawn face, and that no backstop fires. */
    storm_run(0x1234567u, 100000, true, 1000);
    storm_run(0x9e3779b9u, 100000, true, 0xFFFFFFFFu - 2000000u);
    storm_run(0xdeadbeefu, 60000, false, 5000);
    storm_run(0x0badf00du, 40000, true, 0x7FFFFF00u);
}

int main(void)
{
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
    test_commands_are_deaf_while_a_message_plays();
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
    test_a_maintenance_hold_never_opens_the_grid_as_it_fires();
    test_the_grid_reaches_all_four_people();
    test_the_grid_closes_itself_but_a_stray_finger_does_not();
    test_a_tick_pressed_into_silence_refuses_to_send();
    test_the_cross_discards_and_the_pet_is_not_poked();
    test_a_named_turn_with_nobody_speaking_costs_nothing();
    test_a_failed_turn_says_so_rather_than_going_quiet();
    test_every_dash_is_counted_and_its_two_causes_told_apart();
    test_a_reply_arms_a_follow_up_and_the_cap_ends_it();
    test_stop_said_out_loud_leaves_every_state();
    test_pressing_the_face_cycles_through_the_waiting_messages();
    test_the_face_is_not_a_control_with_one_message();
    test_pressing_replay_twice_does_not_pause_what_never_started();
    test_a_held_stream_resumes_rather_than_being_thrown_away();
    test_the_notice_shrinks_to_a_badge_and_a_second_one_does_not_restore_it();
    test_the_notice_stays_painted_while_a_cue_sounds();
    test_the_queue_count_only_sounds_on_the_way_up();
    test_what_the_box_said_about_a_message_is_answered_in_sound();
    test_a_replay_still_reads_as_playing();
    test_the_action_list_never_overflows();

    test_turn_entered_this_frame_survives_stale_net();
    test_cap_not_read_from_previous_capture();
    test_play_not_posted_in_frame_of_its_press_cue();
    test_old_stop_outcome_does_not_finish_new_pending_play();
    test_pet_cell_bounce_does_not_drop_listen();
    test_notice_bounce_does_not_hit_face();
    test_grid_cancel_bounce_does_not_replay();
    test_cross_bounce_with_offer_does_not_replay();
    test_hold_opened_grid_junction_bounce_graced();
    test_deliberate_cell_tap_after_hold_acts();
    test_pause_live_at_on_inside_grace();
    test_failed_play_clears_mode_press_grace();
    test_exit_bounce_does_not_play_next_message();
    test_face_bounce_after_select_does_not_play();
    test_hold_at_pet_centre_with_badge();
    test_hold_at_pet_centre_fresh_offer_is_reply();
    test_hold_at_pet_centre_standing_offer_opens_grid();
    test_hold_at_pet_centre_during_play_is_swallowed();
    test_hold_on_upper_pet_during_fresh_offer_opens_nothing();
    test_hold_in_exit_quadrant_of_standing_offer_dismisses();
    test_held_exit_does_not_open_grid();
    test_held_face_does_not_open_grid();
    test_held_ignored_notice_press_does_not_open_grid();
    test_hold_refused_while_turn_in_flight();
    test_exit_from_replay_is_one_press();
    test_exit_does_not_resurrect_older_offer();
    test_reply_during_replay_goes_to_offer_sender();
    test_reply_during_playback_opens_a_working_recording();
    test_reply_under_heard_min_counts_as_heard();
    test_reply_refused_during_pet_reply_keeps_playing();
    test_face_ignored_during_replay();
    test_replay_end_restamps_offer();
    test_replay_backstop_keeps_offer();
    test_expiry_in_play_sounds_oops();
    test_short_stream_ends_run_without_refetch();
    test_offer_uses_play_outcome_sender();
    test_finish_and_exit_in_one_frame();
    test_exit_during_pet_reply_silences_it_then_sounds_stop();
    test_exit_cutting_reply_opens_no_follow_up();
    test_pause_in_run_gap_is_ignored();
    test_pause_never_outlives_its_stream();
    test_selection_held_while_stop_outcome_pending();
    test_notice_press_during_stop_hold_resolves_at_post();
    test_selection_reresolves_by_id_then_index();
    test_selection_order_stable_when_cur_leaves_waiting();
    test_face_cycles_without_skipping();
    test_face_select_of_mid_play_arrival_survives_return_to_pet();
    test_single_message_face_dead_between_claim_and_stream();
    test_old_box_single_message_face_dead();
    test_counter_counts_the_run_from_the_press();
    test_counter_skips_an_id_that_answered_204();
    test_an_unseen_id_sends_the_selection_home();
    test_old_box_arrival_during_pending_press_keeps_its_target();
    test_later_swipe_at_clamped_end_stops_nothing();
    test_smear_from_notice_never_plays();
    test_more_than_eight_waiting();
    test_seen_ids_never_evicts_a_waiting_or_current_id();
    test_each_new_id_chimes_once();
    test_chime_owed_flag_without_ids();
    test_arrival_played_in_run_does_not_chime_after();
    test_arrival_during_think_chimes_after_reply();
    test_message_during_30s_recording_chimes_after();
    test_message_during_pet_reply_gets_chime_and_big_box();
    test_chime_waits_for_grid_to_close();
    test_notice_clock_starts_when_first_drawn();
    test_a_returned_message_reappears_as_a_badge();
    test_poke_during_outcome_still_sounds_outcome();
    test_standby_after_tick_still_sends();
    test_standby_silences_then_sounds_its_own_stop();
    test_button_and_touch_in_one_frame_drops_touch();
    test_wake_from_standby_sounds_heard();
    test_standby_during_think_reply_plays_without_follow_up();
    test_voice_stop_during_replay_abandons_turn_without_follow_up();
    test_again_during_think_then_failed_turn_still_dashes();
    test_follow_up_open_handles_refusal();
    test_chime_and_follow_up_in_one_frame();
    test_follow_up_yields_to_pending_press();
    test_press_behind_pet_reply_is_served();
    test_wake_phrase_refused_during_failed_dash();
    test_every_refusal_sounds_busy();
    test_notice_and_again_refused_during_send();
    test_silent_recording_says_dropped();
    test_stray_touch_in_record_is_ignored_and_never_drops();
    test_upload_bytes_unchanged_under_listen_attempts();
    test_message_arriving_mid_run_joins_it();
    test_send_backstop_then_wedged_engine_reboots();
    test_post_failure_sounds_oops();
    test_capture_refused_returns_to_pet_with_busy();
    test_calibration_drops_recording_and_keeps_offer();
    test_deadlines_survive_u32_wrap();
    test_sm_face_is_the_reply_recipient_every_frame();
    test_storm();
    printf("ok — %d checks (sm)\n", checks);
    return 0;
}
