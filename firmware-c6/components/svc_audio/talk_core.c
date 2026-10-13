#include "talk_core.h"

#include <stdio.h>
#include <string.h>

/* ---- the ring ---- */

/* The counters cross tasks: a producer's bytes must be in the buffer before its count says so,
 * and a consumer's before it gives the space back. Acquire/release says exactly that and costs
 * nothing on a single core. */
static uint32_t ld(const uint32_t *p)
{
    return __atomic_load_n(p, __ATOMIC_ACQUIRE);
}

static void st(uint32_t *p, uint32_t v)
{
    __atomic_store_n(p, v, __ATOMIC_RELEASE);
}

void tk_ring_init(tk_ring_t *r, uint8_t *buf, uint32_t cap)
{
    r->buf = buf;
    r->cap = buf ? cap : 0;
    st(&r->head, 0);
    st(&r->tail, 0);
}

uint32_t tk_ring_used(const tk_ring_t *r)
{
    return ld(&r->head) - ld(&r->tail);
}

uint32_t tk_ring_free(const tk_ring_t *r)
{
    return r->cap - tk_ring_used(r);
}

uint32_t tk_ring_write(tk_ring_t *r, const void *src, uint32_t n)
{
    const uint32_t head = r->head;
    const uint32_t room = r->cap - (head - ld(&r->tail));
    if (n > room) n = room;
    const uint32_t at = r->cap ? head % r->cap : 0;
    const uint32_t first = n < r->cap - at ? n : r->cap - at;
    memcpy(r->buf + at, src, first);
    memcpy(r->buf, (const uint8_t *)src + first, n - first);
    st(&r->head, head + n);
    return n;
}

_Static_assert(TK_RING_PLAY >= TK_SPK_START_BYTES + TK_NET_SLICE,
               "a play's ring holds the speaker's start and one more slice from the network");

uint32_t tk_ring_bytes(tk_kind_t kind)
{
    return kind == TK_KIND_LISTEN || kind == TK_KIND_NOTICE ? TK_RING_PLAY : TK_RING_TALK;
}

uint32_t tk_reply_ring_bytes(uint32_t held)
{
    return held > TK_RING_PLAY ? TK_RING_PLAY : held;
}

uint32_t tk_ring_read(tk_ring_t *r, void *dst, uint32_t n)
{
    const uint32_t tail = r->tail;
    const uint32_t have = ld(&r->head) - tail;
    if (n > have) n = have;
    const uint32_t at = r->cap ? tail % r->cap : 0;
    const uint32_t first = n < r->cap - at ? n : r->cap - at;
    memcpy(dst, r->buf + at, first);
    memcpy((uint8_t *)dst + first, r->buf, n - first);
    st(&r->tail, tail + n);
    return n;
}

/* ---- the agents ---- */

static const char *const AGENT_IDS[TK_AGENT__COUNT] = {"jerv", "dave"};

const char *tk_agent_id(tk_agent_t a)
{
    return a < TK_AGENT__COUNT ? AGENT_IDS[a] : AGENT_IDS[TK_AGENT_JERV];
}

bool tk_agent_from(const char *id, tk_agent_t *out)
{
    for (int a = 0; id && a < TK_AGENT__COUNT; a++)
        if (!strcmp(id, AGENT_IDS[a])) {
            *out = (tk_agent_t)a;
            return true;
        }
    return false;
}

void tk_converse_path(tk_agent_t a, bool voice, bool fresh, char *out, size_t cap)
{
    snprintf(out, cap, "/endpoint/converse?persona=%s&voice=%s&jt=2&steps=1%s", tk_agent_id(a),
             voice ? "true" : "false", fresh ? "&new_thread=true" : "");
}

/* ---- the exchange ---- */

void tk_init(tk_t *t)
{
    memset(t, 0, sizeof *t);
}

void tk_copy(char *dst, size_t cap, const char *src)
{
    if (!cap) return;
    const size_t n = strnlen(src, cap - 1);
    memcpy(dst, src, n);
    dst[n] = '\0';
}

bool tk_can_begin(const tk_t *t)
{
    return t->phase == TK_IDLE && !t->net_busy && !t->mic_on && !t->spk_on;
}

tk_begin_t tk_begin(tk_t *t, uint32_t now_ms, bool voice, uint32_t msg_id)
{
    if (t->phase == TK_PLAY) {
        tk_stop(t);
        return TK_BEGIN_STOPPED;
    }
    /* A dropped exchange still has the radio or the codec until its owner notices (≤ 20 ms on
     * the codec, one bounded network call on the radio): a new hold waits for both. */
    if (!tk_can_begin(t)) return TK_BEGIN_BUSY;
    t->phase = TK_REC;
    t->since_ms = now_ms;
    t->held_ms = 0;
    t->msg_id = msg_id;
    t->voice = voice;
    t->abort = false;
    t->net_done = false;
    t->res = TK_RES_NONE;
    t->why[0] = '\0';
    t->kind = TK_KIND_AGENT;
    return TK_BEGIN_OK;
}

void tk_set_post(tk_t *t)
{
    t->kind = TK_KIND_POST;
    t->fresh = false;
}

tk_begin_t tk_listen(tk_t *t, uint32_t msg_id)
{
    if (t->phase != TK_IDLE || t->net_busy || t->mic_on || t->spk_on) return TK_BEGIN_BUSY;
    t->phase = TK_SEND;
    t->kind = TK_KIND_LISTEN;
    t->since_ms = 0;
    t->held_ms = 0;
    t->msg_id = msg_id;
    t->voice = true;
    t->fresh = false;
    t->abort = false;
    t->net_done = false;
    t->res = TK_RES_NONE;
    t->why[0] = '\0';
    return TK_BEGIN_OK;
}

void tk_set_notice(tk_t *t)
{
    t->kind = TK_KIND_NOTICE;
}

void tk_set_agent(tk_t *t, tk_agent_t agent, bool fresh)
{
    t->agent = (uint8_t)(agent < TK_AGENT__COUNT ? agent : TK_AGENT_JERV);
    t->fresh = fresh;
}

talk_outcome_t tk_end(tk_t *t, uint32_t now_ms, bool send)
{
    if (t->phase != TK_REC) return t->res == TK_RES_FAILED ? TALK_FAILED : TALK_CANCELLED;
    t->held_ms = now_ms - t->since_ms;
    const talk_outcome_t o = talk_policy_end(t->held_ms, send);
    if (o == TALK_SENT || o == TALK_CAPPED) {
        t->phase = TK_SEND;
    } else {
        t->abort = true;
        t->phase = TK_IDLE;
    }
    return o;
}

bool tk_replying(const tk_t *t)
{
    return t->phase == TK_SEND || t->phase == TK_PLAY;
}

bool tk_stop(tk_t *t)
{
    if (!tk_replying(t)) return false;
    t->abort = true;
    t->phase = TK_IDLE;
    t->res = TK_RES_NONE;
    return true;
}

tk_res_t tk_take(tk_t *t, char *why, size_t why_len)
{
    const tk_res_t r = t->res;
    if (why) tk_copy(why, why_len, t->why);
    t->res = TK_RES_NONE;
    return r;
}

bool tk_quiet(const tk_t *t)
{
    return t->phase == TK_IDLE && !t->net_busy && !t->mic_on && !t->spk_on;
}

bool tk_net_claim(tk_t *t)
{
    const bool listen = (t->kind == TK_KIND_LISTEN || t->kind == TK_KIND_NOTICE) && t->phase == TK_SEND;
    if ((t->phase != TK_REC && !listen) || t->net_busy || t->abort) return false;
    t->net_busy = true;
    t->net_done = false;
    return true;
}

tk_net_t tk_net_next(const tk_t *t)
{
    if (t->abort) return TK_NET_ABORT;
    if (t->phase == TK_REC) return TK_NET_UPLOAD;
    if (t->phase == TK_SEND) return TK_NET_FINISH;
    return TK_NET_ABORT;
}

void tk_net_reply(tk_t *t, bool audio_follows)
{
    if (t->abort || t->phase != TK_SEND) return;
    t->res = TK_RES_REPLY;
    t->phase = t->voice && audio_follows ? TK_PLAY : TK_IDLE;
}

void tk_net_nothing(tk_t *t)
{
    if (t->abort || t->phase != TK_SEND) return;
    t->res = TK_RES_NOTHING;
    t->phase = TK_IDLE;
}

void tk_net_fail(tk_t *t, const char *why)
{
    if (t->abort) return;
    t->res = TK_RES_FAILED;
    tk_copy(t->why, sizeof t->why, why);
    t->phase = TK_IDLE;
}

bool tk_net_wants_audio(const tk_t *t)
{
    return t->phase == TK_PLAY && !t->abort;
}

void tk_net_end(tk_t *t)
{
    t->net_busy = false;
    t->net_done = true;
}

tk_audio_t tk_audio_want(const tk_t *t)
{
    if (t->abort) return TK_AUDIO_OFF;
    if (t->phase == TK_REC) return TK_AUDIO_MIC;
    if (t->phase == TK_PLAY) return TK_AUDIO_SPK;
    return TK_AUDIO_OFF;
}

void tk_audio_drained(tk_t *t)
{
    if (t->phase != TK_PLAY || !t->net_done) return;
    t->phase = TK_IDLE;
    t->res = TK_RES_SPOKEN;
}

void tk_audio_drop(tk_t *t)
{
    if (t->phase == TK_PLAY) t->phase = TK_IDLE;
}

/* ---- samples ---- */

static uint16_t mag(int16_t v)
{
    return v < 0 ? (uint16_t)(-(int32_t)v > 32767 ? 32767 : -(int32_t)v) : (uint16_t)v;
}

void talk_mono(const int16_t *lr, size_t frames, int16_t *out, uint16_t *peak_l, uint16_t *peak_r)
{
    uint16_t pl = 0, pr = 0;
    for (size_t i = 0; i < frames; i++) {
        const int16_t l = lr[2 * i], r = lr[2 * i + 1];
        if (mag(l) > pl) pl = mag(l);
        if (mag(r) > pr) pr = mag(r);
        out[i] = (int16_t)(((int32_t)l + r) / 2);
    }
    *peak_l = pl;
    *peak_r = pr;
}

uint8_t talk_level(uint16_t peak)
{
    /* 32 (-60 dBFS) and under is a quiet room; each doubling is ~6 dB, ten steps to full. */
    unsigned bits = 0;
    while (peak >> bits) bits++;
    return bits <= 5 ? 0 : (uint8_t)((bits - 5) * 10);
}

/* ---- the reply stream ---- */

enum { TS_MAGIC, TS_TYPE, TS_LEN0, TS_LEN1, TS_FLAG, TS_TEXT, TS_BODY_PCM, TS_SKIP, TS_DONE, TS_BAD };

static const char MAGIC[3] = {'J', 'T', '2'};

void tk_stream_init(tk_stream_t *s)
{
    memset(s, 0, sizeof *s);
}

/* `s` holds `n` bytes of what may have been longer; cut it on a character with an ellipsis when
 * it was, so nothing shows half a letter. */
static void finish_text(char *s, uint16_t n, size_t cap, bool cut)
{
    static const char ELL[] = "…";
    if (cut) {
        size_t keep = n;
        if (keep > cap - sizeof ELL) keep = cap - sizeof ELL;
        while (keep && ((unsigned char)s[keep] & 0xC0) == 0x80) keep--;
        memcpy(s + keep, ELL, sizeof ELL);
        return;
    }
    s[n] = '\0';
}

void tk_fit(char *dst, size_t cap, const char *src)
{
    if (!cap) return;
    const size_t n = strnlen(src, cap);
    if (n < cap) {
        memcpy(dst, src, n + 1);
        return;
    }
    memcpy(dst, src, cap - 1);
    finish_text(dst, (uint16_t)(cap - 1), cap, true);
}

/* The records whose first payload byte is a number rather than text: ANSWER's and END's flags,
 * a step's index. */
static bool has_flag(uint8_t type)
{
    return type == TS_ANSWER || type == TS_END || type == TS_STEP_START || type == TS_STEP_DONE;
}

/* The record just finished: what it was. */
static tk_stream_ev_t finished(tk_stream_t *s)
{
    s->records++;
    const uint16_t body = (uint16_t)(s->len - (has_flag(s->type) && s->len ? 1 : 0));
    finish_text(s->text, s->text_n, sizeof s->text, body > s->text_n);
    s->st = s->type == TS_END ? TS_DONE : TS_TYPE;
    switch (s->type) {
    case TS_HEARD: return TS_EV_HEARD;
    case TS_STATUS: return TS_EV_STATUS;
    case TS_ANSWER: return TS_EV_ANSWER;
    case TS_END: return TS_EV_END;
    case TS_STEP_START: return TS_EV_STEP_START;
    case TS_STEP_DONE: return TS_EV_STEP_DONE;
    default: return TS_EV_ALIVE;
    }
}

size_t tk_stream_feed(tk_stream_t *s, const uint8_t *b, size_t n, tk_stream_ev_t *ev,
                      const uint8_t **pcm, size_t *pcm_n)
{
    *ev = TS_EV_NONE;
    *pcm = NULL;
    *pcm_n = 0;
    if (s->st == TS_DONE) return n;
    size_t i = 0;
    while (i < n && s->st != TS_BAD) {
        /* A speech slice and a skipped record move in runs: never a byte at a time. */
        if (s->st == TS_BODY_PCM || s->st == TS_SKIP) {
            size_t k = (size_t)(s->len - s->seen);
            if (k > n - i) k = n - i;
            const bool speech = s->st == TS_BODY_PCM;
            const uint8_t *at = b + i;
            i += k;
            s->seen = (uint16_t)(s->seen + k);
            if (s->seen == s->len) {
                s->st = TS_TYPE;
                s->records++;
            }
            if (speech && k) {
                *pcm = at;
                *pcm_n = k;
                *ev = TS_EV_PCM;
                return i;
            }
            continue;
        }
        const uint8_t c = b[i++];
        switch (s->st) {
        case TS_MAGIC:
            if (c != (uint8_t)MAGIC[s->seen]) {
                s->st = TS_BAD;
                *ev = TS_EV_BAD;
                return i;
            }
            if (++s->seen == sizeof MAGIC) s->st = TS_TYPE;
            break;
        case TS_TYPE:
            s->type = c;
            s->st = TS_LEN0;
            break;
        case TS_LEN0:
            s->lo = c;
            s->st = TS_LEN1;
            break;
        case TS_LEN1:
            s->len = (uint16_t)(s->lo | (c << 8));
            s->seen = 0;
            s->text_n = 0;
            s->flags = 0;
            if (s->len > TS_MAX_RECORD) {
                s->st = TS_BAD;
                *ev = TS_EV_BAD;
                return i;
            }
            switch (s->type) {
            case TS_HEARD:
            case TS_STATUS: s->st = TS_TEXT; break;
            case TS_ANSWER:
            case TS_END:
            case TS_STEP_START:
            case TS_STEP_DONE: s->st = TS_FLAG; break;
            case TS_ALIVE: s->st = TS_TEXT; break;
            case TS_PCM: s->st = TS_BODY_PCM; break;
            default:
                s->st = TS_SKIP;
                s->skipped++;
                break;
            }
            if (!s->len && s->st != TS_SKIP && s->st != TS_BODY_PCM) {
                *ev = finished(s);
                return i;
            }
            if (!s->len) {
                s->st = TS_TYPE;
                s->records++;
            }
            break;
        case TS_FLAG:
            s->flags = c;
            s->st = TS_TEXT;
            if (++s->seen == s->len) {
                *ev = finished(s);
                return i;
            }
            break;
        case TS_TEXT:
            if (s->text_n < sizeof s->text - 1) s->text[s->text_n++] = (char)c;
            if (++s->seen == s->len) {
                *ev = finished(s);
                return i;
            }
            break;
        default: break;
        }
    }
    return i;
}

bool tk_stream_done(const tk_stream_t *s)
{
    return s->st == TS_DONE;
}

bool tk_stream_bad(const tk_stream_t *s)
{
    return s->st == TS_BAD;
}

bool tk_stream_step_ok(const tk_stream_t *s)
{
    /* The ok byte is the record's text: absent says nothing failed. */
    return !s->text_n || s->text[0] != 0;
}

/* ---- the steps ---- */

void tk_steps_init(tk_steps_t *s)
{
    memset(s, 0, sizeof *s);
}

static work_step_t *shown_step(tk_steps_t *s, uint8_t index)
{
    for (uint8_t i = 0; i < s->n; i++)
        if (s->shown[i].index == index) return &s->shown[i];
    return NULL;
}

void tk_steps_start(tk_steps_t *s, uint8_t index, const char *label)
{
    work_step_t *st = shown_step(s, index);
    if (!st) {
        if (index < s->next) return;
        s->next = (uint16_t)(index + 1);
        s->total++;
        if (s->n == WORK_SHOWN) {
            memmove(s->shown, s->shown + 1, sizeof s->shown[0] * (WORK_SHOWN - 1));
            s->n--;
        }
        st = &s->shown[s->n++];
        st->index = index;
        st->state = WORK_RUNNING;
    }
    tk_fit(st->label, sizeof st->label, label);
}

void tk_steps_done(tk_steps_t *s, uint8_t index, bool ok)
{
    work_step_t *st = shown_step(s, index);
    if (st) st->state = ok ? WORK_OK : WORK_FAILED;
}

uint16_t tk_steps_folded(const tk_steps_t *s)
{
    return (uint16_t)(s->total - s->n);
}

void tk_steps_summary(const tk_steps_t *s, uint32_t ms, char *out, size_t cap)
{
    if (!cap) return;
    out[0] = '\0';
    if (!s->total) return;
    char t[16];
    if (ms < 60000) snprintf(t, sizeof t, "%lu s", (unsigned long)(ms / 1000));
    else talk_clock(ms, t, sizeof t);
    snprintf(out, cap, "%u step%s · %s", (unsigned)s->total, s->total == 1 ? "" : "s", t);
}

/* ---- the voice's start ---- */

bool tk_spk_may_start(uint32_t since_amp_ms, uint32_t queued, bool more)
{
    if (since_amp_ms < TK_SPK_SETTLE_MS) return false;
    return queued >= TK_SPK_START_BYTES || !more;
}

/* ---- bubbles ---- */

void talk_bubble_sent(uint32_t id, uint32_t held_ms, msg_t *out)
{
    memset(out, 0, sizeof *out);
    out->id = id;
    out->mine = true;
    out->voice = true;
    out->pending = true;
    out->dur_s = (uint16_t)((held_ms > TALK_MAX_MS ? TALK_MAX_MS : held_ms) / 1000);
    snprintf(out->text, sizeof out->text, "%s", TALK_HEARING);
}

void talk_bubble_heard(msg_t *m, const char *heard)
{
    m->pending = false;
    tk_fit(m->text, sizeof m->text, heard);
    m->status[0] = '\0';
}

void talk_bubble_unsent(msg_t *m, const char *why)
{
    m->pending = false;
    snprintf(m->text, sizeof m->text, "%s", why);
    snprintf(m->status, sizeof m->status, "Not sent");
}

void talk_bubble_reply(uint32_t id, const char *reply, msg_t *out)
{
    memset(out, 0, sizeof *out);
    out->id = id;
    out->played = true;
    tk_fit(out->text, sizeof out->text, reply);
}

/* ---- telemetry ---- */

void tk_stats_note(const tk_stats_t *s, const char *state, char *out, size_t len)
{
    snprintf(out, len,
             "audio=%s,talks:%lu,sent:%lu,heard:%lu,none:%lu,fail:%lu,stop:%lu,up:%luK,down:%luK,"
             "ms:%lu/%lu,under:%lu,over:%lu",
             state, (unsigned long)s->talks, (unsigned long)s->sent, (unsigned long)s->replies,
             (unsigned long)s->nothing, (unsigned long)s->failed, (unsigned long)s->stopped,
             (unsigned long)(s->up_bytes / 1024), (unsigned long)(s->down_bytes / 1024),
             (unsigned long)s->reply_ms, (unsigned long)s->reply_ms_max,
             (unsigned long)s->underruns, (unsigned long)s->overruns);
}
