/* END TO END: the panel's state machine against the real engine and the real arbiter (plan §8.5).
 *
 * `run_sm_tests` drives `panel_sm.c` against fakes and `run_jpanel_tests` drives `jpanel_core.c`
 * against a fake arbiter, so neither can see a fault that lives in the seam between them — the
 * order a frame's effects reach the speaker, a snapshot that is one write behind, a lease that is
 * taken on one task and released on another. This suite wires the three real modules together the
 * way `display.c` does: every 40 ms of fake time the arbiter ticks and a render frame samples the
 * engine's snapshot and the speaker's facts, steps the machine and executes its effects (the stop
 * word hoisted before its silence, the gen stamped on each PLAY, lease 1 taken before a SEND),
 * while the engine runs its own loop against `jpanel_tests.c`'s scripted box.
 *
 * It reuses that file whole — the box, the fake link and the clock — rather than a second copy of
 * them; only its `main` is left out. */

#define JP_E2E 1
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-function"
#include "jpanel_tests.c"
#pragma GCC diagnostic pop

#include "confirm.h"
#include "face.h"
#include "panel_sm.h"

/* `jpanel_tests.c` reads this to fake a busy speaker; the real arbiter answers for itself here. */
bool fake_stream_clip_busy;

#define E_EXIT_X 276
#define E_EXIT_Y 112

static sm_t E_sm;
static sm_in_t E_in;
static sm_out_t E_out;
static jp_view_t E_snap;
static int16_t E_play[CUE_MAX_SAMPLES];
static stream_cue_buf_t E_bufs[STREAM_SLOT_N];

/* The render side's own state, as `display.c` keeps it. */
static uint32_t e_post_failed;
static bool e_refused, e_capture;
static uint32_t e_capture_at;
static bool e_down, e_up, e_hearing;
static int e_x, e_y;
static sm_voice_t e_voice;
static int e_cues[STREAM_SLOT_N][CUE_COUNT];
static int e_frames, e_violations;
static void (*e_watch)(void);

static void e_execute(void)
{
    bool hoisted[SM_FX_MAX] = {false};
    for (int i = 0; i < E_out.n; i++) {
        const sm_fx_t *f = &E_out.fx[i];
        switch (f->kind) {
        case SM_FX_SILENCE: {
            bool any = false, heard = false;
            for (int j = i + 1; j < E_out.n; j++) {
                if (E_out.fx[j].kind != SM_FX_STOP || hoisted[j]) continue;
                any = true;
                heard |= E_out.fx[j].arg != 0;
                hoisted[j] = true;
            }
            if (any) stream_stop(&g_st, heard);
            stream_silence(&g_st);
            break;
        }
        case SM_FX_STOP:
            if (!hoisted[i]) stream_stop(&g_st, f->arg != 0);
            break;
        case SM_FX_CUE:
            e_cues[f->slot][f->arg]++;
            (void)stream_cue_play(&g_st, (stream_slot_t)f->slot, (cue_t)f->arg);
            break;
        case SM_FX_PAUSE:
            stream_pause_req(&g_st, (uint8_t)f->slot, f->arg != 0);
            break;
        case SM_FX_CAPTURE_OPEN:
            if (stream_leased(&g_st)) {
                e_refused = true;
            } else {
                e_capture = true;
                e_capture_at = g_now;
            }
            break;
        case SM_FX_CAPTURE_DROP:
            e_capture = false;
            break;
        case SM_FX_POST: {
            jp_cmd_t c = E_out.post;
            if (c.kind == JP_CMD_SEND) {
                const size_t bytes = (size_t)(g_now - e_capture_at) * (AUDIO_RATE / 1000) * 2;
                e_capture = false;
                if (!stream_lease_take(&g_st, STREAM_LEASE_SEND)) {
                    e_post_failed = c.seq;
                    break;
                }
                for (size_t k = 0; k < bytes / 2 && k < sizeof(g_capture) / 2; k++) {
                    g_capture[k] = (int16_t)(k * 5);
                }
                c.pcm = g_capture;
                c.bytes = bytes < sizeof(g_capture) ? bytes : sizeof(g_capture);
                if (!post(c)) {
                    stream_lease_release(&g_st, STREAM_LEASE_SEND);
                    e_post_failed = c.seq;
                }
            } else {
                c.gen = stream_gen(&g_st);
                if (!post(c)) e_post_failed = c.seq;
            }
            break;
        }
        case SM_FX_POLL_SOON:
            jpanel_core_poll_soon();
            break;
        case SM_FX_SEND_TALK:
        case SM_FX_TALK_CLEAR:
        case SM_FX_STANDBY:
        case SM_FX_BLANK:
        case SM_FX_DEEP_SLEEP:
        case SM_FX_NOD:
        case SM_FX_FLINCH:
        case SM_FX_MUTE_COMMANDS:
            break;
        }
    }
}

/* One render pass, as `display.c` runs it: sample, step, execute. */
static void e_frame(void)
{
    (void)jpanel_core_snapshot(&E_snap);
    memset(&E_in, 0, sizeof(E_in));
    E_in.now = g_now;
    E_in.snap = E_snap;
    E_in.post_failed = e_post_failed;
    E_in.capture_refused = e_refused;
    e_post_failed = JP_SEQ_NONE;
    e_refused = false;
    E_in.audio = stream_facts(&g_st);
    E_in.down = e_down;
    E_in.up = e_up;
    E_in.x = e_x;
    E_in.y = e_y;
    E_in.on_pet = e_x >= TALK_MARGIN_PX && e_x < FACE_W - TALK_MARGIN_PX && e_y >= TALK_MARGIN_PX &&
                  e_y < FACE_H - TALK_MARGIN_PX;
    E_in.voice = e_voice;
    E_in.over_y0 = 0;
    E_in.over_h = FACE_H;
    E_in.hearing = e_hearing && e_capture;
    E_in.capture_ms = e_capture ? (int)(g_now - e_capture_at) : 0;
    E_in.leased = stream_leased(&g_st);
    E_in.net = SM_NET_IDLE;
    E_in.speech_ok = true;
    E_in.sound = true;
    e_down = e_up = false;
    e_voice = SM_VOICE_NONE;
    sm_step(&E_sm, &E_in, &E_out);
    e_frames++;
    if (E_out.violations != 0) e_violations++;
    e_execute();
    if (e_watch != NULL) e_watch();
}

static void e_reset(box_mode_t mode)
{
    reset(mode);
    stream_cue_buf_t *bufs[STREAM_SLOT_N];
    for (int i = 0; i < STREAM_SLOT_N; i++) bufs[i] = &E_bufs[i];
    stream_init(&g_st, g_ring, E_play, bufs);
    stream_set_arbiter(&g_st, true);
    memset(&E_snap, 0, sizeof(E_snap));
    sm_init(&E_sm, g_now);
    e_post_failed = JP_SEQ_NONE;
    e_refused = e_capture = e_down = e_up = e_hearing = false;
    e_voice = SM_VOICE_NONE;
    memset(e_cues, 0, sizeof(e_cues));
    e_frames = e_violations = 0;
    e_watch = NULL;
    g_tick = e_frame;
}

static void e_tap(int x, int y)
{
    e_down = e_up = true;
    e_x = x;
    e_y = y;
    drive(80);
}

static void e_tap_notice(void)
{
    int box[4];
    sm_notice_box(0, FACE_H, true, box);
    e_tap((box[0] + box[2]) / 2, (box[1] + box[3]) / 2);
}

/* Runs the engine until `ok()` holds, at most `max_ms`. The engine's step can span a whole request,
   so anything that must happen DURING one is done by a watcher inside the render frame, as a
   finger would. */
static bool e_until(bool (*ok)(void), uint32_t max_ms)
{
    const uint32_t end = g_now + max_ms;
    while (!ok() && !jp_due(g_now, end)) jpanel_core_step();
    return ok();
}

static bool e_audible(void)
{
    const stream_facts_t f = E_in.audio;
    return E_sm.mode == SM_PLAY && f.stream_id != 0 && f.stream_id == E_snap.cur.stream_id &&
           f.out_ms > 0 && f.stream_live;
}

static bool e_in_pet(void) { return E_sm.mode == SM_PET; }

/* ── the stop's hold, across the real seam ─────────────────────────────────────────────────── */

static bool e_stop_done, e_pressed_exit;
static uint32_t e_stopped_at_ms;
static int e_held_wrong, e_held_frames;

static void watch_stop_then_selection(void)
{
    if (!e_pressed_exit && e_audible() && E_in.audio.out_ms >= 300 &&
        jp_due(g_now, E_sm.play.since + EXIT_GRACE_MS)) {
        /* Her finger lands on the exit early in the message, below the heard threshold. */
        e_pressed_exit = true;
        e_stopped_at_ms = E_in.audio.out_ms;
        e_down = e_up = true;
        e_x = E_EXIT_X;
        e_y = E_EXIT_Y;
        return;
    }
    if (e_pressed_exit && !e_stop_done && E_sm.mode == SM_PET) e_stop_done = true;
    if (!e_stop_done || !E_out.view.notice) return;
    e_held_frames++;
    if (strcmp(E_out.view.face.id, "m1") != 0) e_held_wrong++;
}

static bool e_stopped(void) { return e_stop_done; }

/* A child stops a message she has barely heard. On a box with the delivery header the engine
   hands it back (RETURNED) and re-inserts it at its old place in the same snapshot write as the
   outcome; until then the machine holds the selection on it. So the notice never shows the next
   message for a frame — the stopped one keeps its place, with no gap, end to end. */
static void test_selection_held_while_stop_outcome_pending(void)
{
    e_reset(BOX_NEW);
    add_msg("m1", "Tamsin", false, 6000);
    add_msg("m2", "Dad", true, 6000);
    poll_now();
    drive(400);
    CHECK(E_out.view.notice && strcmp(E_out.view.face.id, "m1") == 0,
          "e2e: the notice is about the oldest message");
    e_stop_done = e_pressed_exit = false;
    e_held_wrong = e_held_frames = 0;
    e_watch = watch_stop_then_selection;
    e_tap_notice();
    CHECK(E_sm.mode == SM_PLAY, "e2e: the notice press enters PLAY");
    CHECK(e_until(e_stopped, 10000), "e2e: she stops it while it is audible");
    drive(4000);
    e_watch = NULL;
    CHECK(e_stopped_at_ms < 2000, "e2e: below the heard threshold");
    CHECK(e_held_frames > 0, "e2e: the notice is drawn after the stop");
    CHECK(e_held_wrong == 0, "e2e: and every frame of it is the stopped message, never the next");
    const msg_t *m1 = find_msg("m1");
    CHECK(m1 != NULL && !m1->played && m1->returns == 1, "e2e: the box took it back, unplayed");
    CHECK(E_snap.waiting.n == 2 && strcmp(E_snap.waiting.q[0].id, "m1") == 0,
          "e2e: at its old place in the queue");
    CHECK(e_cues[STREAM_SLOT_PRESS][CUE_STOP] == 1, "e2e: the exit sounded its own stop");
    CHECK(e_violations == 0, "e2e: no invariant broke across the seam");
    deadlines_held("e2e stop hold");
}

static bool e_was_audible;

static void watch_audible(void)
{
    if (e_audible()) e_was_audible = true;
}

static bool e_ended(void) { return e_was_audible && e_in_pet(); }

/* The offer is set from the play outcome itself, through the real snapshot. */
static void test_e2e_heard_message_offers_again_and_reply(void)
{
    e_reset(BOX_NEW);
    add_msg("m1", "Dad", true, 2000);
    poll_now();
    drive(400);
    e_was_audible = false;
    e_watch = watch_audible;
    e_tap_notice();
    CHECK(e_until(e_ended, 12000), "e2e: the message plays, and ends by itself");
    drive(400);
    e_watch = NULL;
    CHECK(E_out.view.offer && E_out.view.face.from_dad && strcmp(E_out.view.face.id, "m1") == 0,
          "e2e: AGAIN/REPLY is offered for the message that played, from Dad");
    const msg_t *m1 = find_msg("m1");
    CHECK(m1 != NULL && m1->played, "e2e: and the box was told it was heard");
    CHECK(e_cues[STREAM_SLOT_PRESS][CUE_PLAY] == 1, "e2e: the press sounded once");
    CHECK(e_violations == 0, "e2e: no invariant broke");
    deadlines_held("e2e heard");
}

static int e_sending_frames, e_sending_unleased, e_listened;
static bool e_asked;

static void watch_send(void)
{
    if (E_out.view.sending) {
        e_sending_frames++;
        if (!stream_leased(&g_st)) e_sending_unleased++;
        /* Mid-upload she says the pet's name: the microphone must stay shut over the buffer. */
        if (!e_asked && e_sending_frames == 5) {
            e_asked = true;
            e_voice = SM_VOICE_WAKE;
        }
    }
    if (E_sm.mode == SM_LISTEN) e_listened++;
}

static bool e_sent(void) { return e_cues[STREAM_SLOT_SEND_RESULT][CUE_SENT] > 0; }

/* A recording is sent under lease 1, taken by render before the post and released by the engine
   at the SEND's end — so a microphone cannot open over the buffer being uploaded, and is free
   again once it has gone. */
static void test_e2e_send_holds_the_lease_until_it_ends(void)
{
    e_reset(BOX_NEW);
    poll_now();
    drive(200);
    g_upload_bps = 32000;
    g_transcribe_ms = 1500;
    e_voice = SM_VOICE_SEND_PANEL;
    drive(200);
    CHECK(E_sm.mode == SM_RECORD && e_capture, "e2e: the phrase opens a recording");
    e_sending_frames = e_sending_unleased = e_listened = 0;
    e_asked = false;
    e_watch = watch_send;
    e_hearing = true;
    drive(2000);
    e_hearing = false;
    CHECK(e_until(e_sent, 30000), "e2e: the hush sends it, and the box confirms it");
    drive(400);
    e_watch = NULL;
    CHECK(e_sending_frames > 5, "e2e: sending is drawn while it goes");
    CHECK(e_sending_unleased == 0, "e2e: lease 1 is held for every frame of it");
    CHECK(e_asked && e_listened == 0, "e2e: no microphone opens over the upload");
    CHECK(e_cues[STREAM_SLOT_REFUSAL][CUE_BUSY] >= 1, "e2e: the refusal says busy");
    CHECK(!stream_leased(&g_st) && !E_out.view.sending, "e2e: and the lease is released");
    CHECK(count_req("/jpanel/send", 0) == 1, "e2e: exactly one upload");
    CHECK(e_violations == 0, "e2e: no invariant broke");
    deadlines_held("e2e send");
}

int main(void)
{
    test_selection_held_while_stop_outcome_pending();
    test_e2e_heard_message_offers_again_and_reply();
    test_e2e_send_holds_the_lease_until_it_ends();
    if (failures > 0) {
        fprintf(stderr, "e2e: %d of %d checks FAILED\n", failures, checks);
        return 1;
    }
    printf("e2e: %d checks OK\n", checks);
    return 0;
}
