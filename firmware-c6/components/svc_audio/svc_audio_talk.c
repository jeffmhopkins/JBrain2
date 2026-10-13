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
#include "svc_post_drv.h"
#include "svc_rtc.h"
#include "svc_settings.h"

/* The thread the current exchange belongs to, "jerv" or "dave". `agent` is written only by the
 * UI task (tk_set_agent at a hold's start), which is also the only reader of this. */
#define THREAD tk_agent_id((tk_agent_t)s_tk.agent)
static tk_t s_tk;
static tk_ring_t s_ring;
/* Set and cleared under the lock: a talk's ring is taken on the UI task at KEY down, a play's on
 * the main task once the box has answered (talk_net_ring), and the UI task frees either. Its size
 * is the exchange's (tk_ring_bytes: 16 KB for a talk, 6 KB for a play since 0.0.18); allocated
 * per exchange, so between them the heap has it back. */
static uint8_t *s_ring_mem;
static uint32_t s_ring_bytes;
/* The play's internal heap as it went (0.0.18, main task's, under the lock): before its GET, once
   the box answered, with the ring taken, and the lowest seen while the audio streamed. The last
   two apart are what the network holds behind the ring. */
static uint32_t s_play_mem[4];
/* The message playing's audio that went to the speaker: the audio task's count, under the lock. */
static uint32_t s_played_bytes;
/* The box answered a play with its audio (main task, under the lock): the UI task marks the
   message heard on its next pass and clears this. */
static bool s_listen_started;
static tk_stats_t s_st;
/* The reply as it streams in (JT2): written by the main task under the lock, read by the UI task.
 * What was heard and the status line each carry a count that grows with every change, so the UI
 * redraws exactly when one moved; the answer is read once, on REPLY. */
static char s_heard[sizeof(((msg_t *)0)->text)];
static char s_status[64];
static char s_reply[MSG_LONG_TEXT];
/* The tools the agent ran (JT2 STEP records, 0.0.14): fixed, ~270 bytes, never the heap. Written
 * by the main task under the lock; the UI copies them out (svc_audio_work). */
static tk_steps_t s_steps;
static uint32_t s_heard_seq, s_status_seq;
/* When the question went (KEY's release): the working line's clock and the answer's summary. */
static uint32_t s_sent_ms;
/* UI task's own: the counts it last drew. */
static uint32_t s_heard_drawn, s_status_drawn;
static uint32_t s_next_id = 1000;
/* UI task's own. */
static bool s_holding;
static uint32_t s_speaking;
static const char *s_refused;

/* A voice message to a twin, or one of hers played (0.0.16): her thread, and the box path the
 * main task takes (written by the UI task before the exchange is claimable, read under the lock). */
static char s_person[16];
static char s_target[POST_PATH_BYTES];
static bool s_listen_ok;
/* The message playing, 0 when none: the UI task's. */
static uint32_t s_playing;
/* Clear chat, per agent: the next talk to it asks the box for a new conversation. UI task's. */
static bool s_fresh[TK_AGENT__COUNT];
/* The hold in progress was KEY outside its conversation (TALK_TO_NEW_AGENT): sent, it replaces
 * this panel's view of the thread with the new exchange. UI task's. */
static bool s_new;

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

void svc_audio_continue(const char *agent)
{
    tk_agent_t a;
    if (tk_agent_from(agent, &a)) s_fresh[a] = false;
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

/* The exchange's ring, `bytes` long. One the last exchange left behind of another size is given
   back first: only the UI task starts an exchange and the main task takes a play's ring inside
   one, so whoever calls this is the only one who could be touching it. Allocated outside the lock
   (a critical section on the device). */
static bool ring_alloc(uint32_t bytes)
{
    lock();
    uint8_t *old = s_ring_mem;
    const bool fits = old && s_ring_bytes == bytes;
    if (old && !fits) {
        s_ring_mem = NULL;
        s_ring_bytes = 0;
        tk_ring_init(&s_ring, NULL, 0);
    }
    unlock();
    if (fits) return true;
    free(old);
    uint8_t *mem = malloc(bytes);
    if (!mem) return false;
    lock();
    s_ring_mem = mem;
    s_ring_bytes = bytes;
    unlock();
    return true;
}

bool talk_ring_held(void)
{
    return talk_ring_bytes() != 0;
}

uint32_t talk_ring_bytes(void)
{
    lock();
    const uint32_t n = s_ring_mem ? s_ring_bytes : 0;
    unlock();
    return n;
}

/* What stops an exchange before it starts: the codec, the radio, and for a hold the ring's
   memory. A play's ring waits for the box's answer (talk_net_ring). */
static const char *refusal(const char *offline, bool ring)
{
    const char *why = talk_port_refusal();
    net_info_t n;
    svc_net_info(&n);
    if (!why && n.state != NET_CONNECTED) why = offline;
    if (!why && ring && !ring_alloc(TK_RING_TALK)) why = "No memory to record";
    return why;
}

bool svc_audio_talk_begin(talk_kind_t kind, const char *principal)
{
    s_refused = NULL;
    if (s_holding) return false;
    s_new = false;
    const bool person = kind == TALK_TO_PERSON;
    char target[sizeof s_target] = "";
    if (person && !svc_post_send_path(principal, target, sizeof target)) {
        s_refused = "Not sent: her panel is not known yet";
        return false;
    }
    /* The ring only for a hold that can start: KEY over a play still being fetched must not take
       16 KB while the play's own ring is on its way (0.0.18). */
    lock();
    const bool can = tk_can_begin(&s_tk);
    unlock();
    const char *why = refusal(person ? "Not connected: messages need Wi-Fi"
                                     : "Not connected: talking needs Wi-Fi",
                              can);
    if (why) {
        s_refused = why;
        return false;
    }
    tk_agent_t agent = TK_AGENT_JERV;
    tk_agent_from(principal, &agent);
    const bool voice = !person && svc_settings_get(SET_TTS) != 0 && talk_port_speaker();
    const uint32_t now = svc_rtc_uptime_ms();
    lock();
    /* Gone idle since the check: this hold has no ring, so it is the next one that starts. */
    const tk_begin_t b = !can && tk_can_begin(&s_tk) ? TK_BEGIN_BUSY
                                                     : tk_begin(&s_tk, now, voice, s_next_id);
    if (b == TK_BEGIN_OK) {
        tk_set_agent(&s_tk, agent, s_fresh[agent] || kind == TALK_TO_NEW_AGENT);
        if (person) {
            tk_set_post(&s_tk);
            memcpy(s_target, target, sizeof s_target);
            snprintf(s_person, sizeof s_person, "%s", principal);
        }
        tk_ring_init(&s_ring, s_ring_mem, s_ring_bytes);
        s_heard[0] = s_status[0] = '\0';
        s_heard_seq = s_status_seq = 0;
        tk_steps_init(&s_steps);
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
    s_new = kind == TALK_TO_NEW_AGENT;
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

/* The thread the exchange's bubbles land in: the agent's, or the twin's. */
static const char *exchange_thread(void)
{
    return s_tk.kind == TK_KIND_AGENT ? THREAD : s_person;
}

talk_outcome_t svc_audio_talk_end(bool send)
{
    if (!s_holding) return TALK_CANCELLED;
    s_holding = false;
    char why[40] = "";
    const uint32_t now = svc_rtc_uptime_ms();
    lock();
    const talk_outcome_t o = tk_end(&s_tk, now, send);
    const uint32_t held = s_tk.held_ms ? s_tk.held_ms : now - s_tk.since_ms;
    const uint32_t id = s_tk.msg_id;
    const bool post = s_tk.kind == TK_KIND_POST;
    if (o == TALK_FAILED) tk_take(&s_tk, why, sizeof why);
    unlock();
    talk_port_kick();
    if (o == TALK_SENT || o == TALK_CAPPED) {
        s_sent_ms = now;
        /* A new conversation: the box was asked for one at the hold's start, so this panel's
           view starts over with it too — until the box answers, a later talk keeps asking. */
        if (s_new) {
            svc_messages_clear(THREAD);
            s_fresh[s_tk.agent] = true;
        }
    }
    s_new = false;
    if (o == TALK_SENT || o == TALK_CAPPED || o == TALK_FAILED) {
        msg_t m;
        talk_bubble_sent(id, held, &m);
        if (post) {
            if (held > POST_MAX_MS) m.dur_s = POST_MAX_MS / 1000;
            snprintf(m.text, sizeof m.text, "%s", TALK_POSTING);
        }
        if (o == TALK_FAILED) {
            char line[80];
            snprintf(line, sizeof line, "Not sent: %s", why);
            talk_bubble_unsent(&m, line);
        } else {
            s_st.sent++;
        }
        svc_messages_fake_add(post ? s_person : THREAD, &m);
    }
    return o;
}

bool svc_audio_talking(void)
{
    return s_holding;
}

uint32_t svc_audio_talk_ms(void)
{
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

bool svc_audio_speaking(void)
{
    lock();
    const bool r = s_tk.phase == TK_PLAY;
    unlock();
    return r;
}

bool svc_audio_work(const char *thread, svc_work_t *out)
{
    if (!thread || s_tk.kind != TK_KIND_AGENT || strcmp(thread, THREAD) || s_holding) return false;
    const uint32_t now = svc_rtc_uptime_ms();
    lock();
    const bool on = s_tk.phase == TK_SEND && !s_tk.abort && s_heard_seq;
    if (on) {
        tk_fit(out->phase, sizeof out->phase, s_status[0] ? s_status : "Thinking…");
        out->n = s_steps.n;
        memcpy(out->step, s_steps.shown, sizeof out->step);
        out->folded = tk_steps_folded(&s_steps);
        out->seq = s_status_seq;
    }
    unlock();
    if (on) out->elapsed_ms = now - s_sent_ms;
    return on;
}

static void refresh(const char *thread)
{
    app_event_t ev = {.type = APP_EV_MSG_ARRIVED};
    snprintf(ev.u.principal, sizeof ev.u.principal, "%s", thread);
    app_events_post(&ev);
}

static void refresh_thread(void)
{
    refresh(exchange_thread());
}

/* A play is over, however it ended: the bubble's pause goes back to play. */
static void play_over(void)
{
    if (!s_playing) return;
    s_playing = 0;
    refresh(s_person);
}

bool svc_audio_stop_reply(void)
{
    lock();
    const bool thinking = s_tk.phase == TK_SEND;
    const uint32_t id = s_tk.msg_id;
    const uint8_t kind = s_tk.kind;
    const bool stopped = tk_stop(&s_tk);
    unlock();
    if (!stopped) return false;
    s_st.stopped++;
    talk_port_kick();
    msg_t m;
    if (kind == TK_KIND_LISTEN) {
        play_over();
        return true;
    }
    const char *thread = exchange_thread();
    if (thinking && svc_messages_find(thread, id, &m)) {
        talk_bubble_unsent(&m, kind == TK_KIND_POST ? "Stopped before it was sent"
                                                    : "Stopped before the answer");
        snprintf(m.status, sizeof m.status, kind == TK_KIND_POST ? "Not sent" : "Stopped");
        svc_messages_fake_add(thread, &m);
    }
    if (s_speaking) {
        s_speaking = 0;
        refresh_thread();
    }
    return true;
}

bool svc_audio_play(uint32_t msg_id)
{
    s_refused = NULL;
    char thread[16], target[sizeof s_target];
    if (s_holding) return false;
    if (!svc_post_pcm_path(msg_id, thread, target, sizeof target)) {
        s_refused = "That message is not on the box";
        return false;
    }
    const char *why = talk_port_speaker() ? refusal("Not connected: playing needs Wi-Fi", false)
                                          : "No sound: the speaker did not start";
    if (why) {
        s_refused = why;
        return false;
    }
    uint8_t *stale = NULL;
    lock();
    const tk_begin_t b = tk_listen(&s_tk, msg_id);
    if (b == TK_BEGIN_OK) {
        memcpy(s_target, target, sizeof s_target);
        snprintf(s_person, sizeof s_person, "%s", thread);
        s_listen_ok = false;
        s_listen_started = false;
        s_played_bytes = 0;
        /* A ring the last exchange left (not yet given back) is a talk's 16 KB: a play takes its
           own, smaller one once the box answers (talk_net_ring). */
        stale = s_ring_mem;
        s_ring_mem = NULL;
        s_ring_bytes = 0;
        tk_ring_init(&s_ring, NULL, 0);
    }
    unlock();
    free(stale);
    if (b != TK_BEGIN_OK) {
        s_refused = "One moment: still finishing the last one";
        return false;
    }
    s_playing = msg_id;
    s_st.plays++;
    refresh(thread);
    talk_port_kick();
    talk_port_net();
    return true;
}

void svc_audio_stop(void)
{
    lock();
    const bool mine = s_tk.kind == TK_KIND_LISTEN;
    unlock();
    if (mine) svc_audio_stop_reply();
    play_over();
}

uint32_t svc_audio_playing(void)
{
    return s_speaking ? s_speaking : s_playing;
}

uint32_t svc_audio_played_ms(void)
{
    if (!s_playing) return 0;
    lock();
    const uint32_t bytes = s_played_bytes;
    unlock();
    return bytes / (TALK_RATE / 1000u * 2u);
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

static void free_ring(void)
{
    lock();
    uint8_t *mem = s_ring_mem;
    s_ring_mem = NULL;
    s_ring_bytes = 0;
    tk_ring_init(&s_ring, NULL, 0);
    unlock();
    free(mem);
}

/* Her message's audio has started (0.0.17, owner: "if I even hit play once on it"): heard here at
   once, every badge with it, and its stamp queued for the box. A message already heard is left
   alone, so a replay sends no second stamp and can never push a waiting one out of the list. */
static void listen_heard(uint32_t id)
{
    msg_t m;
    if (!svc_messages_find(s_person, id, &m) || m.played) return;
    svc_post_heard(s_person, id);
    refresh(s_person);
}

/* A voice message to a twin, or one of hers played: how it ended, as her thread shows it. */
static void person_service(uint8_t kind, tk_res_t res, const char *why, const char *heard,
                           bool verified, bool quiet, bool started)
{
    msg_t m;
    if (kind == TK_KIND_LISTEN) {
        if (started) listen_heard(s_tk.msg_id);
        /* Heard once its audio started (listen_heard); the digest still judges the audio itself. */
        if (res == TK_RES_FAILED || res == TK_RES_NOTHING || (res == TK_RES_SPOKEN && !verified))
            s_st.play_failed++;
        if (res == TK_RES_SPOKEN || res == TK_RES_FAILED || res == TK_RES_NOTHING || quiet)
            play_over();
    } else if (res != TK_RES_NONE && svc_messages_find(s_person, s_tk.msg_id, &m)) {
        if (res == TK_RES_REPLY) {
            talk_bubble_heard(&m, heard[0] ? heard : POST_NO_TEXT);
            snprintf(m.status, sizeof m.status, "%s", TALK_POSTED);
            s_st.posted++;
        } else if (res == TK_RES_NOTHING) {
            talk_bubble_unsent(&m, "Nothing heard. Hold KEY and speak.");
        } else if (res == TK_RES_FAILED) {
            char line[80];
            snprintf(line, sizeof line, "Not sent: %s", why);
            talk_bubble_unsent(&m, line);
        }
        svc_messages_fake_add(s_person, &m);
        if (res == TK_RES_REPLY) svc_post_sent(s_person);
    }
    if (quiet) free_ring();
}

/* What the box answered, as bubbles. Not while KEY is still held: a failure mid-hold is the
 * release's to show (tk_end's TALK_FAILED), with the ring still up until then. */
void svc_audio_service(void)
{
    if (s_holding) return;
    char why[40];
    static char heard[sizeof s_heard];
    lock();
    const uint8_t kind = s_tk.kind;
    if (kind != TK_KIND_AGENT) {
        const tk_res_t res = tk_take(&s_tk, why, sizeof why);
        const bool quiet = tk_quiet(&s_tk);
        const bool verified = s_listen_ok;
        const bool started = s_listen_started;
        s_listen_started = false;
        memcpy(heard, s_heard, sizeof heard);
        unlock();
        person_service(kind, res, why, heard, verified, quiet, started);
        return;
    }
    const tk_res_t res = tk_take(&s_tk, why, sizeof why);
    const uint32_t id = s_tk.msg_id;
    const bool playing = s_tk.phase == TK_PLAY;
    const bool quiet = tk_quiet(&s_tk);
    const bool heard_moved = s_heard_seq != s_heard_drawn;
    const bool status_moved = s_status_seq != s_status_drawn;
    s_heard_drawn = s_heard_seq;
    s_status_drawn = s_status_seq;
    if (heard_moved || res == TK_RES_REPLY) memcpy(heard, s_heard, sizeof heard);
    /* The whole answer goes straight from the main task's buffer to the thread's: 1 KB is not
       copied through this task's stack. */
    if (res == TK_RES_REPLY) svc_messages_fake_long(THREAD, id + 1, s_reply);
    unlock();
    msg_t m;
    const bool have = svc_messages_find(THREAD, id, &m);
    /* What was heard replaces "Transcribing…" the moment the box has it (JT2), long before the
       answer; the status line under it redraws with each step the agent takes. */
    if (heard_moved && have && heard[0] && res != TK_RES_REPLY) {
        talk_bubble_heard(&m, heard);
        svc_messages_fake_add(THREAD, &m);
    } else if (status_moved && res == TK_RES_NONE) {
        refresh_thread();
    }
    switch (res) {
    case TK_RES_REPLY:
        if (have && heard[0]) {
            talk_bubble_heard(&m, heard);
            svc_messages_fake_add(THREAD, &m);
        }
        lock();
        talk_bubble_reply(id + 1, s_reply, &m);
        /* The steps collapse to one dim line over the answer (the PWA's "Worked · 2 steps"). */
        tk_steps_summary(&s_steps, svc_rtc_uptime_ms() - s_sent_ms, m.status, sizeof m.status);
        unlock();
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
    if (quiet) free_ring();
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

tk_kind_t talk_net_kind(char *path, size_t cap)
{
    lock();
    const tk_kind_t kind = (tk_kind_t)s_tk.kind;
    tk_copy(path, cap, kind == TK_KIND_AGENT ? "" : s_target);
    unlock();
    return kind;
}

bool talk_net_ring(void)
{
    lock();
    const uint8_t kind = s_tk.kind;
    unlock();
    if (!ring_alloc(tk_ring_bytes((tk_kind_t)kind))) return false;
    lock();
    tk_ring_init(&s_ring, s_ring_mem, s_ring_bytes);
    unlock();
    return true;
}

void talk_net_mem(uint32_t before, uint32_t answered, uint32_t ringed, uint32_t low)
{
    lock();
    s_play_mem[0] = before;
    s_play_mem[1] = answered;
    s_play_mem[2] = ringed;
    s_play_mem[3] = low;
    unlock();
}

void talk_net_listened(bool verified)
{
    lock();
    s_listen_ok = verified;
    unlock();
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

void talk_net_heard(const char *heard)
{
    lock();
    tk_fit(s_heard, sizeof s_heard, heard);
    s_status[0] = '\0';
    s_heard_seq++;
    unlock();
}

void talk_net_status(const char *status)
{
    lock();
    tk_fit(s_status, sizeof s_status, status);
    s_status_seq++;
    unlock();
}

/* A step is a change like a status line: the same count moves, so the UI redraws once for it. */
void talk_net_step_start(uint8_t index, const char *label)
{
    lock();
    tk_steps_start(&s_steps, index, label);
    s_status_seq++;
    unlock();
}

void talk_net_step_done(uint8_t index, bool ok)
{
    lock();
    tk_steps_done(&s_steps, index, ok);
    s_status_seq++;
    unlock();
}

void talk_net_reply(const char *reply, bool audio_follows)
{
    lock();
    tk_fit(s_reply, sizeof s_reply, reply);
    s_status[0] = '\0';
    s_st.replies++;
    tk_net_reply(&s_tk, audio_follows);
    if (s_tk.kind == TK_KIND_LISTEN && s_tk.phase == TK_PLAY) s_listen_started = true;
    /* The recording is all sent and the microphone is off: the ring is the reply's now. */
    if (s_tk.phase == TK_PLAY) tk_ring_init(&s_ring, s_ring_mem, s_ring_bytes);
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

void talk_audio_played(uint32_t bytes)
{
    lock();
    s_played_bytes += bytes;
    unlock();
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
    const uint32_t ring = s_ring_mem ? s_ring_bytes : 0;
    uint32_t mem[4];
    memcpy(mem, s_play_mem, sizeof mem);
    char status[sizeof s_status];
    memcpy(status, s_status, sizeof status);
    unlock();
    dbg_json_str(w, "state", talk_port_state());
    static const char *const KIND[] = {"agent", "message", "play"};
    dbg_json_str(w, "phase", PHASE[t.phase]);
    dbg_json_str(w, "kind", KIND[t.kind < 3 ? t.kind : 0]);
    dbg_json_int(w, "playing", s_playing);
    dbg_json_int(w, "plays", st.plays);
    dbg_json_int(w, "play_failed", st.play_failed);
    dbg_json_int(w, "posted", st.posted);
    dbg_json_bool(w, "talking", svc_audio_talking());
    dbg_json_bool(w, "voice", t.voice);
    dbg_json_bool(w, "mic_on", t.mic_on);
    dbg_json_bool(w, "spk_on", t.spk_on);
    dbg_json_bool(w, "net_busy", t.net_busy);
    dbg_json_int(w, "speaking", s_speaking);
    if (t.phase == TK_SEND && status[0]) dbg_json_str(w, "status", status);
    dbg_json_int(w, "steps", s_steps.total);
    dbg_json_int(w, "ring", ring);
    /* The last play's internal heap, in bytes (0.0.18): what the board can settle that the host
       cannot — `play_ring_free - play_low` is the network's backlog behind the ring. */
    dbg_json_int(w, "play_before", mem[0]);
    dbg_json_int(w, "play_answered", mem[1]);
    dbg_json_int(w, "play_ring_free", mem[2]);
    dbg_json_int(w, "play_low", mem[3]);
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
