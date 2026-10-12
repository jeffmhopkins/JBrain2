/* A talk to Jerv or Dave as the glass sees it, and the one state the three tasks share. The same file
 * runs on the device and in the sim: only talk_port_* differ (esp/svc_audio_es.c, or
 * svc_audio_fake.c), so the bubbles the sim draws are the ones the panel will. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_event.h"
#include "svc_audio.h"
#include "svc_audio_drv.h"
#include "svc_debug.h"
#include "svc_messages_fake.h"
#include "svc_net.h"
#include "svc_rtc.h"
#include "svc_settings.h"

/* The thread the current exchange belongs to, "jerv" or "dave". `agent` is written only by the
 * UI task (tk_set_agent at a hold's start), which is also the only reader of this. */
#define THREAD tk_agent_id((tk_agent_t)s_tk.agent)
/* Half a second of speech either way: the upload's slack for a radio that stalls, and the
 * reply's for a box that sends in bursts. Allocated per exchange, so between talks the heap
 * has it back (README "What 0.0.10 adds", the memory budget). */
#define RING_BYTES 16384

static tk_t s_tk;
static tk_ring_t s_ring;
static uint8_t *s_ring_mem;
static tk_stats_t s_st;
/* The reply's words: written by the main task under the lock, read by the UI after REPLY. */
static char s_heard[sizeof(((msg_t *)0)->text)];
static char s_reply[sizeof(((msg_t *)0)->text)];
static uint32_t s_next_id = 1000;
/* UI task's own. */
static bool s_holding;
static uint32_t s_speaking;
static const char *s_refused;

/* A hold to a person: timed, never sent (svc_audio_fake.h, TALK_PLACEHOLDER). */
static struct {
    bool on;
    char to[16];
    uint32_t since;
} s_person;
static uint32_t s_playing;
static uint32_t s_next_person_id = 900000;
/* Clear chat, per agent: the next talk to it asks the box for a new conversation. UI task's. */
static bool s_fresh[TK_AGENT__COUNT];

void svc_audio_fresh(const char *agent)
{
    tk_agent_t a;
    if (tk_agent_from(agent, &a)) s_fresh[a] = true;
}

bool svc_audio_fresh_pending(const char *agent)
{
    tk_agent_t a;
    return tk_agent_from(agent, &a) && s_fresh[a];
}

static void lock(void)
{
    talk_port_lock();
}

static void unlock(void)
{
    talk_port_unlock();
}

/* ---- the glass ---- */

bool svc_audio_talk_begin(talk_kind_t kind, const char *principal)
{
    s_refused = NULL;
    if (s_holding || s_person.on) return false;
    if (kind == TALK_TO_PERSON) {
        s_person.on = true;
        snprintf(s_person.to, sizeof s_person.to, "%s", principal ? principal : "");
        s_person.since = svc_rtc_uptime_ms();
        s_playing = 0;
        return true;
    }
    const char *why = talk_port_refusal();
    net_info_t n;
    svc_net_info(&n);
    if (!why && n.state != NET_CONNECTED) why = "Not connected: talking needs Wi-Fi";
    if (!why && !s_ring_mem && !(s_ring_mem = malloc(RING_BYTES))) why = "No memory to record";
    if (why) {
        s_refused = why;
        return false;
    }
    tk_agent_t agent = TK_AGENT_JERV;
    tk_agent_from(principal, &agent);
    const bool voice = svc_settings_get(SET_TTS) != 0 && talk_port_speaker();
    const uint32_t now = svc_rtc_uptime_ms();
    lock();
    const tk_begin_t b = tk_begin(&s_tk, now, voice, s_next_id);
    if (b == TK_BEGIN_OK) {
        tk_set_agent(&s_tk, agent, s_fresh[agent]);
        tk_ring_init(&s_ring, s_ring_mem, RING_BYTES);
    }
    unlock();
    if (b == TK_BEGIN_STOPPED) {
        s_st.stopped++;
        talk_port_kick();
        return false;
    }
    if (b == TK_BEGIN_BUSY) {
        s_refused = "One moment: still finishing the last one";
        return false;
    }
    s_holding = true;
    s_next_id += 2;
    s_st.talks++;
    talk_port_kick();
    talk_port_net();
    return true;
}

const char *svc_audio_talk_refused(void)
{
    return s_refused;
}

static talk_outcome_t person_end(bool send)
{
    const uint32_t held = svc_rtc_uptime_ms() - s_person.since;
    const talk_outcome_t o = talk_policy_end(held, send);
    msg_t m;
    if (talk_placeholder(o, held, s_next_person_id, &m)) {
        s_next_person_id++;
        svc_messages_fake_add(s_person.to, &m);
    }
    s_person.on = false;
    return o;
}

talk_outcome_t svc_audio_talk_end(bool send)
{
    if (s_person.on) return person_end(send);
    if (!s_holding) return TALK_CANCELLED;
    s_holding = false;
    char why[40] = "";
    const uint32_t now = svc_rtc_uptime_ms();
    lock();
    const talk_outcome_t o = tk_end(&s_tk, now, send);
    const uint32_t held = s_tk.held_ms ? s_tk.held_ms : now - s_tk.since_ms;
    const uint32_t id = s_tk.msg_id;
    if (o == TALK_FAILED) tk_take(&s_tk, why, sizeof why);
    unlock();
    talk_port_kick();
    if (o == TALK_SENT || o == TALK_CAPPED || o == TALK_FAILED) {
        msg_t m;
        talk_bubble_sent(id, held, &m);
        if (o == TALK_FAILED) {
            char line[80];
            snprintf(line, sizeof line, "Not sent: %s", why);
            talk_bubble_unsent(&m, line);
        } else {
            s_st.sent++;
        }
        svc_messages_fake_add(THREAD, &m);
    }
    return o;
}

bool svc_audio_talking(void)
{
    return s_holding || s_person.on;
}

uint32_t svc_audio_talk_ms(void)
{
    if (s_person.on) return svc_rtc_uptime_ms() - s_person.since;
    return s_holding ? svc_rtc_uptime_ms() - s_tk.since_ms : 0;
}

uint8_t svc_audio_level(void)
{
    return s_holding ? talk_port_level() : 0;
}

bool svc_audio_replying(void)
{
    lock();
    const bool r = tk_replying(&s_tk);
    unlock();
    return r;
}

static void refresh_thread(void)
{
    app_event_t ev = {.type = APP_EV_MSG_ARRIVED};
    snprintf(ev.u.principal, sizeof ev.u.principal, "%s", THREAD);
    app_events_post(&ev);
}

bool svc_audio_stop_reply(void)
{
    lock();
    const bool thinking = s_tk.phase == TK_SEND;
    const uint32_t id = s_tk.msg_id;
    const bool stopped = tk_stop(&s_tk);
    unlock();
    if (!stopped) return false;
    s_st.stopped++;
    talk_port_kick();
    msg_t m;
    if (thinking && svc_messages_find(THREAD, id, &m)) {
        talk_bubble_unsent(&m, "Stopped before the answer");
        snprintf(m.status, sizeof m.status, "Stopped");
        svc_messages_fake_add(THREAD, &m);
    }
    if (s_speaking) {
        s_speaking = 0;
        refresh_thread();
    }
    return true;
}

bool svc_audio_play(uint32_t msg_id)
{
    if (svc_audio_talking()) return false;
    s_playing = msg_id;
    return true;
}

void svc_audio_stop(void)
{
    s_playing = 0;
}

uint32_t svc_audio_playing(void)
{
    return s_speaking ? s_speaking : s_playing;
}

/* Listen on a stored reply needs the box to speak text it already sent; until it can,
 * svc_audio_can_speak is false and no Listen button is drawn to reach this. */
void svc_audio_speak(uint32_t msg_id)
{
    (void)msg_id;
}

bool svc_audio_can_speak(void)
{
    return false;
}

const char *svc_audio_state(void)
{
    return talk_port_state();
}

/* What the box answered, as bubbles. Not while KEY is still held: a failure mid-hold is the
 * release's to show (tk_end's TALK_FAILED), with the ring still up until then. */
void svc_audio_service(void)
{
    if (s_holding) return;
    char why[40];
    lock();
    const tk_res_t res = tk_take(&s_tk, why, sizeof why);
    const uint32_t id = s_tk.msg_id;
    const bool playing = s_tk.phase == TK_PLAY;
    const bool quiet = tk_quiet(&s_tk);
    char heard[sizeof s_heard], reply[sizeof s_reply];
    if (res == TK_RES_REPLY) {
        memcpy(heard, s_heard, sizeof heard);
        memcpy(reply, s_reply, sizeof reply);
    }
    unlock();
    msg_t m;
    const bool have = svc_messages_find(THREAD, id, &m);
    switch (res) {
    case TK_RES_REPLY:
        if (have) {
            talk_bubble_heard(&m, heard);
            svc_messages_fake_add(THREAD, &m);
        }
        talk_bubble_reply(id + 1, reply, &m);
        svc_messages_fake_add(THREAD, &m);
        s_speaking = playing ? id + 1 : 0;
        /* The box answered, so it has started the new conversation this asked for. */
        if (s_tk.fresh) s_fresh[s_tk.agent] = false;
        break;
    case TK_RES_NOTHING:
        if (have) {
            talk_bubble_unsent(&m, "Nothing heard. Hold KEY and speak.");
            snprintf(m.status, sizeof m.status, "Not heard");
            svc_messages_fake_add(THREAD, &m);
        }
        break;
    case TK_RES_FAILED:
        if (have) {
            char line[80];
            snprintf(line, sizeof line, "Not answered: %s", why);
            talk_bubble_unsent(&m, line);
            svc_messages_fake_add(THREAD, &m);
        }
        break;
    case TK_RES_SPOKEN:
        if (s_speaking) {
            s_speaking = 0;
            refresh_thread();
        }
        break;
    default: break;
    }
    if (quiet && s_speaking) {
        s_speaking = 0;
        refresh_thread();
    }
    if (quiet && s_ring_mem) {
        lock();
        tk_ring_init(&s_ring, NULL, 0);
        unlock();
        free(s_ring_mem);
        s_ring_mem = NULL;
    }
}

/* ---- the main task's half ---- */

bool talk_net_claim(bool *voice, tk_agent_t *agent, bool *fresh)
{
    lock();
    const bool mine = tk_net_claim(&s_tk);
    *voice = s_tk.voice;
    *agent = (tk_agent_t)s_tk.agent;
    *fresh = s_tk.fresh;
    unlock();
    return mine;
}

tk_net_t talk_net_next(bool *mic_off)
{
    lock();
    const tk_net_t n = tk_net_next(&s_tk);
    *mic_off = !s_tk.mic_on;
    unlock();
    return n;
}

uint32_t talk_net_take(void *dst, uint32_t cap)
{
    return tk_ring_read(&s_ring, dst, cap);
}

void talk_net_reply(const char *heard, const char *reply, bool audio_follows)
{
    lock();
    tk_copy(s_heard, sizeof s_heard, heard);
    tk_copy(s_reply, sizeof s_reply, reply);
    s_st.replies++;
    tk_net_reply(&s_tk, audio_follows);
    /* The recording is all sent and the microphone is off: the ring is the reply's now. */
    if (s_tk.phase == TK_PLAY) tk_ring_init(&s_ring, s_ring_mem, RING_BYTES);
    unlock();
    talk_port_kick();
}

void talk_net_nothing(void)
{
    lock();
    s_st.nothing++;
    tk_net_nothing(&s_tk);
    unlock();
}

void talk_net_fail(const char *why)
{
    lock();
    if (!s_tk.abort) s_st.failed++;
    tk_net_fail(&s_tk, why);
    unlock();
    talk_port_kick();
}

uint32_t talk_net_give(const void *src, uint32_t n)
{
    return talk_net_wants_audio() ? tk_ring_write(&s_ring, src, n) : 0;
}

bool talk_net_wants_audio(void)
{
    lock();
    const bool w = tk_net_wants_audio(&s_tk);
    unlock();
    return w;
}

void talk_net_end(uint32_t up_bytes, uint32_t down_bytes, uint32_t reply_ms)
{
    lock();
    s_st.up_bytes += up_bytes;
    s_st.down_bytes += down_bytes;
    if (reply_ms) {
        s_st.reply_ms = reply_ms;
        if (reply_ms > s_st.reply_ms_max) s_st.reply_ms_max = reply_ms;
    }
    tk_net_end(&s_tk);
    unlock();
    talk_port_kick();
}

/* ---- the audio task's half ---- */

tk_audio_t talk_audio_want(void)
{
    lock();
    const tk_audio_t w = tk_audio_want(&s_tk);
    unlock();
    return w;
}

void talk_audio_on(bool mic, bool spk)
{
    lock();
    s_tk.mic_on = mic;
    s_tk.spk_on = spk;
    unlock();
}

void talk_audio_put(const int16_t *pcm, uint32_t bytes, uint16_t peak_l, uint16_t peak_r)
{
    if (tk_ring_write(&s_ring, pcm, bytes) < bytes) s_st.overruns++;
    s_st.peak_l = peak_l;
    s_st.peak_r = peak_r;
}

uint32_t talk_audio_get(void *dst, uint32_t bytes, bool *more)
{
    lock();
    *more = !s_tk.net_done;
    unlock();
    return tk_ring_read(&s_ring, dst, bytes);
}

uint32_t talk_audio_queued(void)
{
    return tk_ring_used(&s_ring);
}

bool talk_audio_more(void)
{
    lock();
    const bool more = !s_tk.net_done;
    unlock();
    return more;
}

void talk_audio_drop(void)
{
    lock();
    tk_audio_drop(&s_tk);
    unlock();
    talk_port_kick();
}

void talk_audio_underrun(void)
{
    s_st.underruns++;
}

void talk_audio_drained(void)
{
    lock();
    tk_audio_drained(&s_tk);
    if (s_tk.res == TK_RES_SPOKEN) s_st.spoken++;
    unlock();
}

/* ---- reporting ---- */

static const char *const PHASE[] = {"idle", "recording", "thinking", "speaking"};

void svc_audio_note(char *out, size_t len)
{
    lock();
    const tk_stats_t st = s_st;
    unlock();
    tk_stats_note(&st, talk_port_state(), out, len);
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    lock();
    const tk_stats_t st = s_st;
    const tk_t t = s_tk;
    const uint32_t queued = tk_ring_used(&s_ring);
    unlock();
    dbg_json_str(w, "state", talk_port_state());
    dbg_json_str(w, "phase", PHASE[t.phase]);
    dbg_json_bool(w, "talking", svc_audio_talking());
    dbg_json_bool(w, "voice", t.voice);
    dbg_json_bool(w, "mic_on", t.mic_on);
    dbg_json_bool(w, "spk_on", t.spk_on);
    dbg_json_bool(w, "net_busy", t.net_busy);
    dbg_json_int(w, "speaking", s_speaking);
    dbg_json_int(w, "ring", s_ring_mem ? RING_BYTES : 0);
    dbg_json_int(w, "queued", queued);
    dbg_json_int(w, "talks", st.talks);
    dbg_json_int(w, "sent", st.sent);
    dbg_json_int(w, "replies", st.replies);
    dbg_json_int(w, "nothing_heard", st.nothing);
    dbg_json_int(w, "failed", st.failed);
    dbg_json_int(w, "stopped", st.stopped);
    dbg_json_int(w, "spoken", st.spoken);
    dbg_json_int(w, "up_bytes", st.up_bytes);
    dbg_json_int(w, "down_bytes", st.down_bytes);
    dbg_json_int(w, "reply_ms", st.reply_ms);
    dbg_json_int(w, "reply_ms_max", st.reply_ms_max);
    dbg_json_int(w, "underruns", st.underruns);
    dbg_json_int(w, "overruns", st.overruns);
    dbg_json_int(w, "mic_peak_l", st.peak_l);
    dbg_json_int(w, "mic_peak_r", st.peak_r);
    if (t.why[0]) dbg_json_str(w, "last_failure", t.why);
    talk_port_describe(w);
}

void svc_audio_init(void)
{
    tk_init(&s_tk);
    svc_debug_register("audio", describe, NULL);
}
