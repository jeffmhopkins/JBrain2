/* THE TWINS' PANEL IN A BROWSER, RUNNING ITS OWN CODE.
 *
 * Compiled to WebAssembly by `build.sh` and driven by `docs/mocks/jpanel-s3/index.html`. Nothing
 * on the page is a picture of the panel: the state machine (`panel_sm.c`), the voice-post engine
 * (`jpanel_core.c`), the speaker's arbiter (`stream.c`), the cues (`cue.c`), the pet (`face.c`,
 * `rig.c`, `emotion.c`) and every overlay (`overlay.c`, `confirm.c`, `font.c`) are the firmware's
 * own files, wired the way `display.c` wires them. The box is `host/jpanel_tests.c`'s scripted box,
 * included whole as `host/e2e_tests.c` includes it.
 *
 * WHAT IS NOT THE FIRMWARE, and so is where the mock can lie: this file's frame (the input
 * sampling and the executor are copies of `display.c`'s, not calls into it), the talk turn (a timer
 * standing in for `talk.c`'s request), the recogniser (the page's buttons stand in for the
 * phrases and the VAD), and the screen's dim/dark stages. Orientation, calibration, the maintenance
 * gestures, voice actions and colours are not modelled at all.
 *
 * TIME IS THE SCRIPTED BOX'S CLOCK. Everything — the engine's waits, the box's latency, the
 * render frame — advances through `advance()`, which ticks the arbiter and calls the frame every
 * 40 ms of fake time. The frame calls `js_frame(now)`, which Asyncify turns into a suspension: the page
 * resumes it when the wall clock has caught up, so the panel runs at real speed (or faster). */

#define JP_E2E 1
#define JP_MOCK_VOICE mock_voice
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
static int16_t mock_voice(bool dad, uint32_t i);

#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-function"
#pragma GCC diagnostic ignored "-Wunused-variable"
#include "../host/jpanel_tests.c"
#pragma GCC diagnostic pop

#include <math.h>

#include "caption.h"
#include "confirm.h"
#include "cue_names.gen.h"
#include "emotion.h"
#include "face.h"
#include "font.h"
#include "overlay.h"
#include "panel_sm.h"
#include "rig.h"
#include "screen.h"

#define EXPORT(name) __attribute__((export_name(#name)))
#define IMPORT(name) __attribute__((import_module("env"), import_name(#name)))

IMPORT(js_frame) void js_frame(uint32_t now_ms);
IMPORT(js_audio) void js_audio(const int16_t *pcm, uint32_t n);
IMPORT(js_event) void js_event(const char *line);

bool fake_stream_clip_busy;

/* The panel's own name and its twin's: placeholders, never a child's real name. */
#define MOCK_NAME "QUORRA"

static uint16_t s_fb[FACE_W * FACE_H];
static sm_t s_sm;
static sm_in_t s_in;
static sm_out_t s_out;
static jp_view_t s_snap;
static int16_t s_play[CUE_MAX_SAMPLES];
static stream_cue_buf_t s_bufs[STREAM_SLOT_N];
static char s_line[256];

/* Asyncify's save area: the suspended C stack lives here while the page waits. */
static uint8_t s_async[64 * 1024] __attribute__((aligned(8)));

/* ── what the page sets ────────────────────────────────────────────────────────────────────── */

static bool p_down_edge, p_up_edge, p_finger, p_hearing;
/* She talks for this long each time a microphone opens, then goes quiet; 0 leaves it to the
   page's hold button. On by default: a recording that hears nothing for RECORD_LEAD_MS is
   dropped, which is right on the glass and baffling on a bench nobody told. */
static uint32_t p_autotalk_ms = 2500;
static uint32_t r_frame_at;
static int p_x = -1, p_y = -1, p_down_x = -1, p_down_y = -1;
static bool p_swipe_latched;
static sm_voice_t p_voice;
static bool p_btn_short, p_btn_hold;
static bool p_speech_ok = true;
static int p_talk_outcome;        /* 0 a reply, 1 the request fails, 2 nothing was understood */
static uint32_t p_talk_ms = 4000; /* the box's turn: whisper, the model, the voice */
static uint32_t p_reply_ms = 2500;
static int p_msgs;

/* ── the render side's own state, as `display.c` keeps it ──────────────────────────────────── */

static uint32_t r_post_failed = JP_SEQ_NONE;
static bool r_refused, r_capture;
static uint32_t r_capture_at;
static bool r_wake_next, r_dark_wake_next;
static screen_stage_t r_sleep = SCREEN_AWAKE;
static uint32_t r_active_at;
static const char *r_result_word;
static uint16_t r_result_colour;
static uint32_t r_result_until;
static sm_net_t r_net = SM_NET_IDLE;
static uint32_t r_net_at;
static bool r_talk_leased;
static int16_t r_reply[8000 * 4];
static uint32_t r_reply_id;
static face_state_t r_fst;
static rig_walk_t r_walk;
static action_t r_action;
static uint32_t r_action_at;
static float r_flinch;
static int r_frame;
static caption_t r_cap;
static sm_mode_t r_mode_was;
static int r_log_seen;

static void say(const char *kind, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
static void say(const char *kind, const char *fmt, ...)
{
    int n = snprintf(s_line, sizeof(s_line), "%u %s ", (unsigned)g_now, kind);
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(s_line + n, sizeof(s_line) - (size_t)n, fmt, ap);
    va_end(ap);
    js_event(s_line);
}

static const char *cue_name(int c)
{
    return c >= 0 && c < CUE_COUNT && CUE_NAMES[c] != NULL ? CUE_NAMES[c] : "?";
}

static const char *const SLOT_NAMES[STREAM_SLOT_N] = {"press", "result", "fail", "refusal", "chime"};
static const char *const MODE_NAMES[] = {"PET", "GRID", "LISTEN", "RECORD", "PLAY"};

/* A voice, not a tone: a pitch that wanders, in syllables, so a message reads as somebody talking. */
static int16_t mock_voice(bool dad, uint32_t i)
{
    const float t = (float)i / (float)AUDIO_RATE;
    const float f0 = (dad ? 120.0f : 260.0f) * (1.0f + 0.12f * sinf(t * 2.1f) + 0.05f * sinf(t * 7.3f));
    const float syll = 0.5f + 0.5f * sinf(t * 2.0f * (float)M_PI * 3.2f);
    const float env = syll * syll * (0.6f + 0.4f * sinf(t * 1.3f));
    const float ph = 2.0f * (float)M_PI * f0 * t;
    const float v = sinf(ph) + 0.5f * sinf(2.0f * ph) + 0.25f * sinf(3.0f * ph);
    return (int16_t)(v * env * 2600.0f);
}

static void codec(const int16_t *pcm, size_t n) { js_audio(pcm, (uint32_t)n); }

/* ── the talk turn, standing in for `talk.c` ──────────────────────────────────────────────── */

static void talk_tick(void)
{
    if (r_net != SM_NET_BUSY) return;
    /* The upload is over a second in: the capture buffer is free again (lease 2). */
    if (r_talk_leased && jp_due(g_now, r_net_at + 1000)) {
        stream_lease_release(&g_st, STREAM_LEASE_TALK);
        r_talk_leased = false;
    }
    if (!jp_due(g_now, r_net_at + p_talk_ms)) return;
    if (p_talk_outcome == 1) {
        r_net = SM_NET_FAILED;
        say("talk", "the turn failed");
        return;
    }
    if (p_talk_outcome == 2) {
        r_net = SM_NET_IDLE;
        say("talk", "nothing was understood");
        return;
    }
    uint32_t samples = p_reply_ms * (AUDIO_RATE / 1000);
    if (samples > sizeof(r_reply) / 2) samples = sizeof(r_reply) / 2;
    for (uint32_t k = 0; k < samples; k++) r_reply[k] = mock_voice(false, k + 40000);
    stream_reply(&g_st, r_reply, samples, ++r_reply_id);
    r_net = SM_NET_SPOKE;
    say("talk", "the pet answers (%u ms)", (unsigned)p_reply_ms);
}

/* ── the executor, as `display.c` runs it ─────────────────────────────────────────────────── */

static bool execute(uint32_t now)
{
    bool nod = false;
    bool hoisted[SM_FX_MAX] = {false};
    for (int i = 0; i < s_out.n; i++) {
        const sm_fx_t *f = &s_out.fx[i];
        switch (f->kind) {
        case SM_FX_SILENCE: {
            bool any = false, heard = false;
            for (int j = i + 1; j < s_out.n; j++) {
                if (s_out.fx[j].kind != SM_FX_STOP || hoisted[j]) continue;
                any = true;
                heard |= s_out.fx[j].arg != 0;
                hoisted[j] = true;
            }
            if (any) stream_stop(&g_st, heard);
            stream_silence(&g_st);
            say("fx", "silence%s", any ? (heard ? " + stop (heard)" : " + stop") : "");
            break;
        }
        case SM_FX_STOP:
            if (!hoisted[i]) {
                stream_stop(&g_st, f->arg != 0);
                say("fx", "stop%s", f->arg ? " (heard)" : "");
            }
            break;
        case SM_FX_CUE:
            (void)stream_cue_play(&g_st, (stream_slot_t)f->slot, (cue_t)f->arg);
            say("cue", "%s (%s slot)", cue_name(f->arg), SLOT_NAMES[f->slot]);
            if (f->slot == STREAM_SLOT_SEND_RESULT) {
                overlay_result((cue_t)f->arg, s_out.say != NULL, &r_result_word, &r_result_colour);
                r_result_until = now + 2500;
            }
            break;
        case SM_FX_PAUSE:
            stream_pause_req(&g_st, (uint8_t)f->slot, f->arg != 0);
            say("fx", "%s", f->arg ? "pause" : "resume");
            break;
        case SM_FX_CAPTURE_OPEN:
            if (stream_leased(&g_st)) {
                r_refused = true;
                say("fx", "microphone refused (leased)");
            } else {
                r_capture = true;
                r_capture_at = now;
                if (p_autotalk_ms) say("voice", "she talks for %u ms", (unsigned)p_autotalk_ms);
                say("fx", "microphone open");
            }
            break;
        case SM_FX_CAPTURE_DROP:
            if (r_capture) say("fx", "microphone dropped");
            r_capture = false;
            break;
        case SM_FX_POST: {
            jp_cmd_t c = s_out.post;
            if (c.kind == JP_CMD_SEND) {
                size_t bytes = (size_t)(now - r_capture_at) * (AUDIO_RATE / 1000) * 2;
                r_capture = false;
                if (!stream_lease_take(&g_st, STREAM_LEASE_SEND)) {
                    r_post_failed = c.seq;
                    say("fx", "send refused: the buffer is leased");
                    break;
                }
                if (bytes > sizeof(g_capture)) bytes = sizeof(g_capture);
                for (size_t k = 0; k < bytes / 2; k++) g_capture[k] = mock_voice(false, (uint32_t)k);
                c.pcm = g_capture;
                c.bytes = bytes;
                if (!post(c)) {
                    stream_lease_release(&g_st, STREAM_LEASE_SEND);
                    r_post_failed = c.seq;
                }
                say("post", "SEND to %s, %u ms of audio", c.to == JP_TO_DAD ? "dad" : "sister",
                    (unsigned)(bytes / 2 / (AUDIO_RATE / 1000)));
            } else {
                c.gen = stream_gen(&g_st);
                if (!post(c)) r_post_failed = c.seq;
                say("post", "%s", c.kind == JP_CMD_PLAY ? "PLAY" : "REPLAY");
            }
            if (g_nposted > 200) g_nposted = 0;
            break;
        }
        case SM_FX_SEND_TALK:
            r_capture = false;
            if (!stream_lease_take(&g_st, STREAM_LEASE_TALK)) {
                say("talk", "capture lease held — not sending");
                break;
            }
            r_talk_leased = true;
            r_net = SM_NET_BUSY;
            r_net_at = now;
            say("talk", "asking the box");
            break;
        case SM_FX_TALK_CLEAR:
            if (r_net != SM_NET_BUSY) r_net = SM_NET_IDLE;
            break;
        case SM_FX_POLL_SOON:
            jpanel_core_poll_soon();
            break;
        case SM_FX_STANDBY:
            if (f->arg) {
                r_sleep = SCREEN_DARK;
                say("fx", "standby: screen off");
            } else {
                r_sleep = SCREEN_AWAKE;
                r_active_at = now;
                say("fx", "standby ends");
            }
            break;
        case SM_FX_BLANK:
            break;
        case SM_FX_DEEP_SLEEP:
            say("fx", "deep sleep (the panel would power off here)");
            break;
        case SM_FX_NOD:
            nod = true;
            break;
        case SM_FX_FLINCH:
            r_flinch = 1.0f;
            break;
        case SM_FX_MUTE_COMMANDS:
            break;
        }
    }
    return nod;
}

/* ── one frame: sample, step, execute, draw ───────────────────────────────────────────────── */

static int bob_step(int frame)
{
    const int k = frame % 20;
    if (k <= 5) return k;
    if (k <= 15) return 10 - k;
    return k - 20;
}

static void sweep_box(void)
{
    /* The scripted box was written for suites that end; a page left open must not run off its
       arrays. Heard and withdrawn messages go, and the request log is only read for new lines. */
    int w = 0;
    for (int i = 0; i < g_nmsgs; i++) {
        if (g_msgs[i].played || g_msgs[i].gone) continue;
        g_msgs[w++] = g_msgs[i];
    }
    g_nmsgs = w;
    for (; r_log_seen < g_nlog && r_log_seen < 511; r_log_seen++) {
        const req_log_t *l = &g_log[r_log_seen];
        say("http", "%s %s", l->method, l->path);
    }
    if (g_nlog >= 400) {
        g_nlog = 0;
        r_log_seen = 0;
    }
    if (g_npubs > 4000) g_npubs = 0;
    if (g_nouts > 60) g_nouts = 0;
}

static void frame(void)
{
    /* ONE RENDER FRAME PER 40 ms OF PANEL TIME, as `display.c` runs. The box's waits advance the
       clock in steps that are not multiples of 40, and a frame on each of them ran every per-frame
       animation (the bob, the flinch, the eyes' easing) up to twice as fast. Presses wait in their
       latches for the next frame, as they do on the glass. */
    if (r_frame_at != 0 && !jp_due(g_now, r_frame_at + 40)) return;
    r_frame_at = g_now;
    const uint32_t now = g_now;
    talk_tick();
    sweep_box();

    bool tapped = p_down_edge, up = p_up_edge;
    p_down_edge = p_up_edge = false;
    bool wake = r_wake_next, dark_wake = r_dark_wake_next;
    r_wake_next = r_dark_wake_next = false;
    bool woke_by_touch = false;
    if (r_sleep == SCREEN_DARK && (tapped || p_finger)) {
        /* A finger on a dark screen buys the screen and nothing else. */
        woke_by_touch = true;
        dark_wake = true;
        if (s_sm.standby) wake = true;
        r_sleep = SCREEN_AWAKE;
        tapped = up = false;
        p_finger = false;
        p_down_x = p_down_y = -1;
    }
    if (tapped) {
        p_down_x = p_x;
        p_down_y = p_y;
        p_swipe_latched = false;
    }
    int swipe = 0;
    if (p_finger && !p_swipe_latched && p_down_x >= 0) {
        const int dx = p_x - p_down_x, dy = p_y - p_down_y;
        const int adx = dx < 0 ? -dx : dx, ady = dy < 0 ? -dy : dy;
        if (adx >= SWIPE_MIN_PX && adx > ady) {
            swipe = dx > 0 ? 1 : -1;
            p_swipe_latched = true;
        }
    }

    (void)jpanel_core_snapshot(&s_snap);
    const stream_facts_t facts = stream_facts(&g_st);
    memset(&s_in, 0, sizeof(s_in));
    s_in.now = now;
    s_in.snap = s_snap;
    s_in.post_failed = r_post_failed;
    s_in.capture_refused = r_refused;
    r_post_failed = JP_SEQ_NONE;
    r_refused = false;
    s_in.audio = facts;
    s_in.down = tapped;
    s_in.up = up;
    s_in.x = p_x;
    s_in.y = p_y;
    s_in.on_pet = p_down_x >= TALK_MARGIN_PX && p_down_x < FACE_W - TALK_MARGIN_PX &&
                  p_down_y >= TALK_MARGIN_PX && p_down_y < FACE_H - TALK_MARGIN_PX;
    s_in.swipe = swipe;
    s_in.voice = p_voice;
    s_in.btn_short = p_btn_short;
    s_in.btn_hold = p_btn_hold;
    p_voice = SM_VOICE_NONE;
    p_btn_short = p_btn_hold = false;
    s_in.over_y0 = 0;
    s_in.over_h = FACE_H;
    s_in.wake = wake;
    s_in.hearing = r_capture && (p_hearing || (p_autotalk_ms && now - r_capture_at < p_autotalk_ms));
    s_in.capture_ms = r_capture ? (int)(now - r_capture_at) : 0;
    s_in.leased = stream_leased(&g_st);
    s_in.net = r_net;
    s_in.speech_ok = p_speech_ok;
    s_in.dark_wake = dark_wake;
    s_in.sound = true;
    sm_step(&s_sm, &s_in, &s_out);
    if (s_out.violations != 0) say("bug", "invariant broken: 0x%x", (unsigned)s_out.violations);
    if (s_out.tap_missed) say("touch", "press ignored (a grace, or not a target)");
    if (s_out.rim_hold) say("touch", "hold on the rim, not the pet");
    if (s_out.backstop) say("bug", "a UI backstop fired");
    if (s_out.late_outcome) say("note", "an outcome arrived after its backstop");
    if (s_out.dash_why != SM_DASH_NONE) say("talk", "no reply (%s)", sm_dash_name(s_out.dash_why));
    if (s_out.say != NULL) {
        caption_say(&r_cap, s_out.say);
        say("say", "%s", s_out.say);
    }
    if ((r_mode_was == SM_RECORD || r_mode_was == SM_LISTEN) && s_sm.mode == SM_PET && !s_in.hearing &&
        r_capture && !s_out.view.sending) {
        bool posted = false;
        for (int i = 0; i < s_out.n; i++) posted |= s_out.fx[i].kind == SM_FX_POST || s_out.fx[i].kind == SM_FX_SEND_TALK;
        if (!posted) say("note", "nothing was heard, so it was dropped (the panel waits %d s for a first word)",
                         (r_mode_was == SM_RECORD ? RECORD_LEAD_MS : LISTEN_LEAD_MS) / 1000);
    }
    if (s_sm.mode != r_mode_was) {
        say("mode", "%s -> %s", MODE_NAMES[r_mode_was], MODE_NAMES[s_sm.mode]);
        r_mode_was = s_sm.mode;
    }
    if (execute(now)) {
        r_action = ACT_NOD;
        r_action_at = now;
    }
    if (s_out.pet_press && r_action == ACT_NONE) r_flinch = 1.0f;

    /* The screen's stages, simplified: standby is pinned dark, anything else dims and darkens on
       the idle clock, and a person or a message brings it back. */
    const bool used = tapped || p_finger || woke_by_touch || facts.speaker_busy || s_out.arrived ||
                      sm_busy(&s_sm, &s_snap);
    if (used) {
        r_active_at = now;
        const bool wanted_back = s_out.arrived || tapped || p_finger || woke_by_touch;
        if (r_sleep != SCREEN_AWAKE && (!s_sm.standby || wanted_back)) {
            if (s_sm.standby) r_wake_next = true;
            if (r_sleep == SCREEN_DARK) r_dark_wake_next = true;
            r_sleep = SCREEN_AWAKE;
        }
    } else if (s_sm.standby) {
        r_sleep = SCREEN_DARK;
    } else {
        r_sleep = screen_stage(now - r_active_at);
    }
    if (r_result_word != NULL && jp_due(now, r_result_until)) r_result_word = NULL;
    caption_tick(&r_cap, now, FACE_W);

    /* ── draw ── */
    const sm_view_t *view = &s_out.view;
    if (r_sleep == SCREEN_DARK) {
        memset(s_fb, 0, sizeof(s_fb));
        js_frame(g_now);
        return;
    }
    float p = 0.0f;
    if (r_action != ACT_NONE) {
        const int dur = rig_spec(r_action)->dur_ms;
        const uint32_t el = now - r_action_at;
        if (dur <= 0 || (int)el >= dur) {
            r_action = ACT_NONE;
        } else {
            p = (float)el / (float)dur;
        }
    }
    const bool conversing = view->mode == SM_LISTEN || view->mode == SM_RECORD || view->think;
    const bool dozing = screen_dozing(r_sleep, conversing, facts.speaker_busy);
    face_params_t target;
    emotion_resolve(dozing                    ? FACE_SLEEPY
                    : view->mode == SM_LISTEN ? FACE_CURIOUS
                    : view->dash              ? FACE_BEWILDERED
                                              : rig_spec(r_action)->face,
                    &target);
    emotion_approach_face(&r_fst.eyes, &target, target.rate);
    emotion_approach_face(&r_fst.eyes, &target, target.rate);
    rig_for(r_action, p, 1.0f, now, &r_fst.rig);
    rig_walk(&r_walk, 0.0f, &r_fst.rig);
    rig_figure(r_action, p, 1.0f, now, r_fst.eyes.face_ang, &r_fst.fig);
    r_fst.bob = bob_step(r_frame++);
    const uint32_t blink = now % 4200;
    r_fst.open = dozing ? 0.0f : blink < 120 ? 1.0f - blink / 120.0f : blink < 240 ? (blink - 120) / 120.0f : 1.0f;
    r_fst.startle = r_flinch;
    if (facts.speaker_busy) {
        const float t = (float)now * 0.001f;
        float open = 0.55f + 0.30f * sinf(t * 6.3f) + 0.15f * sinf(t * 2.7f + 1.1f);
        r_fst.talk = open < 0.25f ? 0.25f : open > 1.0f ? 1.0f : open;
    } else {
        r_fst.talk = 0.0f;
    }
    r_fst.fig.oy += 18.0f * r_flinch;
    r_flinch *= 0.82f;
    if (r_flinch < 0.02f) r_flinch = 0.0f;
    face_draw(s_fb, 0, &r_fst);
    font_draw(s_fb, FACE_W, FACE_H, 16, 18, 2, MOCK_NAME, SWAP16(0x8410));
    if (dozing) {
        const uint32_t phase = (now / 700u) % 4u;
        char z[4] = {0};
        for (uint32_t i = 0; i < phase && i < 3; i++) z[i] = 'Z';
        if (z[0] != '\0') {
            font_draw(s_fb, FACE_W, FACE_H, FACE_W / 2 + 26 + (int)phase * 4, 40 - (int)phase * 6, 2,
                      z, SWAP16(0x8410));
        }
    }
    caption_draw(&r_cap, s_fb, FACE_W, FACE_H, SWAP16(0xCE79));
    const overlay_ctx_t oc = {
        .over_y0 = 0,
        .over_h = FACE_H,
        .now = now,
        .sibling = s_snap.waiting.sibling,
        .result_word = r_result_word,
        .result_colour = r_result_colour,
    };
    overlay_draw(s_fb, view, &oc);
    js_frame(g_now);
}

/* ── the page's entry points ──────────────────────────────────────────────────────────────── */

EXPORT(sim_fb) uint16_t *sim_fb(void) { return s_fb; }
EXPORT(sim_async) uint8_t *sim_async(void) { return s_async; }
EXPORT(sim_async_size) uint32_t sim_async_size(void) { return sizeof(s_async); }
EXPORT(sim_w) int sim_w(void) { return FACE_W; }
EXPORT(sim_h) int sim_h(void) { return FACE_H; }

EXPORT(sim_init) void sim_init(void)
{
    reset(BOX_NEW);
    stream_cue_buf_t *bufs[STREAM_SLOT_N];
    for (int i = 0; i < STREAM_SLOT_N; i++) bufs[i] = &s_bufs[i];
    stream_init(&g_st, g_ring, s_play, bufs);
    stream_set_arbiter(&g_st, true);
    sm_init(&s_sm, g_now);
    face_rest(&r_fst);
    caption_reset(&r_cap);
    r_active_at = g_now;
    g_latency_ms = 150;
    g_upload_bps = 64000;
    g_transcribe_ms = 1500;
    g_quiet = true;
    g_tick = frame;
    g_codec = codec;
}

/* Runs for ever; `js_frame` suspends it every frame. */
EXPORT(sim_run) void sim_run(void)
{
    jpanel_core_poll_soon();
    for (;;) jpanel_core_step();
}

/* 0 down, 1 move, 2 up — in overlay pixels, which upright are the panel's. */
EXPORT(sim_touch) void sim_touch(int kind, int x, int y)
{
    p_x = x;
    p_y = y;
    if (kind == 0) {
        p_down_edge = true;
        p_finger = true;
    } else if (kind == 2) {
        if (p_finger) p_up_edge = true;
        p_finger = false;
    }
}

EXPORT(sim_voice) void sim_voice(int v) { p_voice = (sm_voice_t)v; }
EXPORT(sim_hearing) void sim_hearing(int on) { p_hearing = on != 0; }
EXPORT(sim_button) void sim_button(int hold)
{
    if (hold) {
        p_btn_hold = true;
    } else {
        p_btn_short = true;
    }
}

/* A message arrives on the box, and the box's nudge makes the panel poll for it. */
EXPORT(sim_message) void sim_message(int from_dad, int ms)
{
    if (g_nmsgs >= 15) return;
    char id[16];
    snprintf(id, sizeof(id), "m%d", ++p_msgs);
    add_msg(id, from_dad ? "Dad" : g_sibling, from_dad != 0, (uint32_t)ms);
    say("box", "a message from %s (%d ms)", from_dad ? "Dad" : g_sibling, ms);
    jpanel_core_poll_soon();
}

/* The box's knobs. */
EXPORT(sim_set) void sim_set(int knob, int value)
{
    switch (knob) {
    case 0: /* offline: every request fails to connect */
        g_nov = 0;
        if (value) add_override((override_t){.route = "/jpanel", .status = -1, .times = -1, .cut_after = -1});
        say("box", value ? "unreachable" : "reachable");
        break;
    case 1: g_latency_ms = (uint32_t)value; break;
    case 2: g_upload_bps = (uint32_t)value; break;
    case 3: g_transcribe_ms = (uint32_t)value; break;
    case 4: /* the next SEND's status: 200, 404 (nobody to send to), 500 */
        g_nsend_status = 1;
        g_send_at = 0;
        g_send_status[0] = value;
        break;
    case 5: p_speech_ok = value != 0; break;
    case 6: p_talk_outcome = value; break;
    case 7: p_talk_ms = (uint32_t)value; break;
    case 8: g_mode = value ? BOX_NEW : BOX_TODAY; break;
    case 9: p_autotalk_ms = (uint32_t)value; break;
    }
}

/* One line of JSON the page shows beside the glass. */
static char s_state[2048];
EXPORT(sim_state) const char *sim_state(void)
{
    const sm_view_t *v = &s_out.view;
    int n = snprintf(s_state, sizeof(s_state),
                     "{\"now\":%u,\"mode\":\"%s\",\"think\":%d,\"dash\":%d,\"notice\":%d,"
                     "\"notice_big\":%d,\"offer\":%d,\"standing\":%d,\"pair\":%d,\"exit\":%d,"
                     "\"sel\":\"%d/%d\",\"run_bar\":%d,\"transport\":%d,\"paused\":%d,"
                     "\"sending\":%d,\"grid\":%d,\"confirm\":%d,\"dot\":%d,\"standby\":%d,"
                     "\"screen\":%d,\"capture\":%d,\"leased\":%d,\"net\":%d,\"speaker\":%d,"
                     "\"out_ms\":%u,\"phase\":%d,\"waiting\":%d,\"queue\":[",
                     (unsigned)g_now, MODE_NAMES[s_sm.mode], v->think, v->dash, v->notice,
                     v->notice_big, v->offer, v->offer_standing, v->pair, v->exit_corner, v->sel_n,
                     v->sel_m, v->run_bar, v->transport, v->paused, v->sending, v->grid, v->confirm,
                     v->listen_dot, s_sm.standby, (int)r_sleep, r_capture, stream_leased(&g_st),
                     (int)r_net, s_in.audio.speaker_busy, (unsigned)s_in.audio.out_ms,
                     (int)s_snap.phase, s_snap.waiting.count);
    for (int i = 0; i < g_nmsgs && n < (int)sizeof(s_state) - 80; i++) {
        const msg_t *m = &g_msgs[i];
        n += snprintf(s_state + n, sizeof(s_state) - (size_t)n,
                      "%s{\"id\":\"%s\",\"from\":\"%s\",\"ms\":%u,\"deliveries\":%d}", i ? "," : "",
                      m->id, m->from, (unsigned)m->ms, m->deliveries);
    }
    snprintf(s_state + n, sizeof(s_state) - (size_t)n, "]}");
    return s_state;
}

/* What a press at (x, y) would hit now, by the machine's own hit test: the page paints these over
   the glass, so a control drawn where its target is not (I3) can be seen rather than inferred. */
static const char *const TARGET_NAMES[] = {
    [SM_T_NONE] = "", [SM_T_IGNORED] = "ignored", [SM_T_PET] = "pet", [SM_T_NOTICE] = "notice",
    [SM_T_EXIT] = "exit", [SM_T_AGAIN] = "again", [SM_T_REPLY] = "reply", [SM_T_FACE] = "face",
    [SM_T_PAUSE] = "pause", [SM_T_TICK] = "tick", [SM_T_CROSS] = "cross",
    [SM_T_CELL_SISTER] = "sister", [SM_T_CELL_DAD] = "dad", [SM_T_CELL_CANCEL] = "cancel",
    [SM_T_CELL_PET] = "talk", [SM_T_SWALLOW] = "swallowed",
};
EXPORT(sim_target) int sim_target(int x, int y) { return (int)sm_target(&s_sm, &s_in, x, y); }
EXPORT(sim_target_name) const char *sim_target_name(int t)
{
    return t >= 0 && t < (int)(sizeof(TARGET_NAMES) / sizeof(TARGET_NAMES[0])) && TARGET_NAMES[t]
               ? TARGET_NAMES[t]
               : "?";
}
