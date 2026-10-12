/* The voice-post engine against a scripted box, a fake clock and a fake arbiter (plan §10).
 *
 * The box has two modes. TODAY is the 0.3.39 box contract as it is deployed: `/waiting` without
 * ids, positional `/next?at=`, `X-Jpanel-Id` on every head, no delivery header, `/played` always
 * 204 and no `/returned` route (the tunnel answers 404). NEW is the W3 contract: ids in
 * `/waiting`, `/next?id=`, `X-Jpanel-Delivery` and `X-Jpanel-Duration-Ms`, and `/returned`
 * answering 204/409/404. 0.4.0 must work fully against both (§15).
 *
 * The fake link enforces deadlines the way `link.c` does (§7.5a): every wait is clamped to what is
 * left and ticks `on_progress` every 250 ms; a SEND's deadline moves with its upload and then
 * becomes the head budget; a body is bounded per gap, not in total; cancellation is checked between
 * steps. Every 40 ms of fake time the harness checks the UI's two backstops and the engine
 * watchdog's condition against the published snapshot, so "deadlines are true by construction" is
 * asserted on every test, not only the ones named for it. */

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "fake_stream.h"
#include "jpanel_core.h"
#include "reach.h"

static int checks;
static int failures;
#define CHECK(c, msg)                                                         \
    do {                                                                      \
        checks++;                                                             \
        if (!(c)) {                                                           \
            failures++;                                                       \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, (msg)); \
        }                                                                     \
    } while (0)

/* ── SHA-256, test-only (the device uses mbedTLS; dev-setup.sh stays unchanged) ─────────── */

typedef struct {
    uint32_t h[8];
    uint8_t buf[64];
    size_t n;
    uint64_t len;
} sha_t;

static const uint32_t K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};

#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void sha_block(sha_t *s, const uint8_t *p)
{
    uint32_t w[64];
    for (int i = 0; i < 16; i++) {
        w[i] = (uint32_t)p[i * 4] << 24 | (uint32_t)p[i * 4 + 1] << 16 |
               (uint32_t)p[i * 4 + 2] << 8 | p[i * 4 + 3];
    }
    for (int i = 16; i < 64; i++) {
        const uint32_t s0 = ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ (w[i - 15] >> 3);
        const uint32_t s1 = ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = s->h[0], b = s->h[1], c = s->h[2], d = s->h[3], e = s->h[4], f = s->h[5],
             g = s->h[6], h = s->h[7];
    for (int i = 0; i < 64; i++) {
        const uint32_t t1 = h + (ROR(e, 6) ^ ROR(e, 11) ^ ROR(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i];
        const uint32_t t2 = (ROR(a, 2) ^ ROR(a, 13) ^ ROR(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
        h = g;
        g = f;
        f = e;
        e = d + t1;
        d = c;
        c = b;
        b = a;
        a = t1 + t2;
    }
    s->h[0] += a;
    s->h[1] += b;
    s->h[2] += c;
    s->h[3] += d;
    s->h[4] += e;
    s->h[5] += f;
    s->h[6] += g;
    s->h[7] += h;
}

static void sha_init(sha_t *s)
{
    static const uint32_t h0[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                   0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    memcpy(s->h, h0, sizeof(h0));
    s->n = 0;
    s->len = 0;
}

static void sha_add(sha_t *s, const uint8_t *d, size_t n)
{
    s->len += n;
    while (n > 0) {
        const size_t t = n < 64 - s->n ? n : 64 - s->n;
        memcpy(s->buf + s->n, d, t);
        s->n += t;
        d += t;
        n -= t;
        if (s->n == 64) {
            sha_block(s, s->buf);
            s->n = 0;
        }
    }
}

static void sha_done(sha_t *s, uint8_t out[32])
{
    const uint64_t bits = s->len * 8;
    const uint8_t one = 0x80, zero = 0;
    sha_add(s, &one, 1);
    while (s->n != 56) sha_add(s, &zero, 1);
    for (int i = 7; i >= 0; i--) {
        const uint8_t b = (uint8_t)(bits >> (i * 8));
        sha_add(s, &b, 1);
    }
    for (int i = 0; i < 8; i++) {
        out[i * 4] = (uint8_t)(s->h[i] >> 24);
        out[i * 4 + 1] = (uint8_t)(s->h[i] >> 16);
        out[i * 4 + 2] = (uint8_t)(s->h[i] >> 8);
        out[i * 4 + 3] = (uint8_t)s->h[i];
    }
}

static void sha_hex(const uint8_t *d, size_t n, char out[65])
{
    sha_t s;
    uint8_t dig[32];
    sha_init(&s);
    sha_add(&s, d, n);
    sha_done(&s, dig);
    for (int i = 0; i < 32; i++) snprintf(&out[i * 2], 3, "%02x", dig[i]);
}

static void test_sha_known_answer(void)
{
    char hex[65];
    sha_hex((const uint8_t *)"abc", 3, hex);
    CHECK(strcmp(hex, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad") == 0,
          "the test SHA-256 is SHA-256");
}

static void scopy(char *dst, size_t cap, const char *src)
{
    size_t n = strlen(src);
    if (n >= cap) n = cap - 1;
    memcpy(dst, src, n);
    dst[n] = '\0';
}

/* ── the fake world ───────────────────────────────────────────────────────────────────────── */

static uint32_t g_now;
static stream_t g_st;
static int16_t g_ring[STREAM_RING_SAMPLES];
static sha_t g_sha;

typedef void (*tick_fn)(void);
static tick_fn g_tick;
static void (*g_yield)(const char *point);

/* What the UI and the watchdog would conclude, every 40 ms. */
static int g_backstop, g_unclaimed, g_wd;
static uint32_t g_last_hb, g_hb_at;

typedef struct {
    uint32_t seq, expires_at;
    bool claimed;
    int ends;
} posted_t;
static posted_t g_posted[256];
static int g_nposted;
static uint32_t g_seq = 100;

static jp_cmd_t g_q[4];
static int g_qn;

static jp_view_t g_last;
static jp_play_outcome_t g_outs[64];
static int g_nouts;
static jp_view_t g_pubs[4096];
static int g_sent_count;
static bool g_quiet;
static int g_npubs;

static jp_view_t snap(void)
{
    jp_view_t v;
    memset(&v, 0, sizeof(v));
    (void)jpanel_core_snapshot(&v);
    return v;
}

static void check_backstops(void)
{
    if (g_quiet) return;
    const jp_view_t v = snap();
    if (v.phase != JP_IDLE && v.deadline_ms != 0 && jp_due(g_now, v.deadline_ms + JP_UI_MARGIN_MS)) {
        g_backstop++;
    }
    if (v.hb != g_last_hb) {
        g_last_hb = v.hb;
        g_hb_at = g_now;
    }
    if (jp_due(g_now, g_hb_at + 60000) &&
        (v.phase == JP_IDLE || jp_due(g_now, v.deadline_ms + JP_UI_MARGIN_MS + 60000))) {
        g_wd++;
    }
    for (int i = 0; i < g_nposted; i++) {
        posted_t *p = &g_posted[i];
        if (v.claimed_seq == p->seq) p->claimed = true;
        if (!p->claimed && !jp_seq_ge(v.done_seq, p->seq) &&
            jp_due(g_now, p->expires_at + JP_UI_MARGIN_MS)) {
            g_unclaimed++;
        }
    }
}

static void advance(uint32_t ms)
{
    /* The arbiter's codec chunk: the fake ignores it, the real one (`e2e_tests.c`) writes it. */
    static int16_t out[AUDIO_CHUNK];
    while (ms > 0) {
        const uint32_t step = ms < 40 ? ms : 40;
        g_now += step;
        ms -= step;
        (void)stream_tick(&g_st, g_now, out, (size_t)step * (AUDIO_RATE / 1000));
        if (g_tick != NULL) g_tick();
        check_backstops();
    }
}

static void record(void)
{
    if (g_quiet) return;
    const jp_view_t v = snap();
    if (memcmp(&v.send, &g_last.send, sizeof(v.send)) != 0 && v.send.kind == JP_SEND_SENT) {
        g_sent_count++;
    }
    if (g_npubs < (int)(sizeof(g_pubs) / sizeof(g_pubs[0]))) g_pubs[g_npubs++] = v;
    if (v.done_seq != g_last.done_seq) {
        for (int i = 0; i < g_nposted; i++) {
            if (g_posted[i].seq == v.done_seq) g_posted[i].ends++;
        }
    }
    if (memcmp(&v.play, &g_last.play, sizeof(v.play)) != 0 && v.play.seq != 0 && g_nouts < 64) {
        g_outs[g_nouts++] = v.play;
    }
    for (int i = 0; i < g_nposted; i++) {
        if (v.claimed_seq == g_posted[i].seq) g_posted[i].claimed = true;
    }
    g_last = v;
}

/* ── the scripted box ─────────────────────────────────────────────────────────────────────── */

typedef enum { BOX_TODAY = 0, BOX_NEW } box_mode_t;

typedef struct {
    char id[40];
    char from[32];
    bool dad;
    uint32_t ms;
    bool played, gone;
    int deliveries, returns;
    int fetched, replayed;
    char first_from[32]; /* changes per poll in the storm test */
} msg_t;

static box_mode_t g_mode;
static msg_t g_msgs[16];
static int g_nmsgs;
static uint32_t g_latency_ms;
static uint32_t g_head_delay_ms;
static uint32_t g_pre_wait_ms;   /* the socket wait / HTTPS lock before a request starts */
static uint32_t g_body_gap_ms;   /* network time per 4 KB body chunk */
static uint32_t g_upload_bps;    /* SEND upload speed; 0 = instant */
static uint32_t g_transcribe_ms; /* the box's work after the upload */
static int g_send_status[8];
static int g_nsend_status, g_send_at;
static char g_sibling[32];
static int g_poll_gen; /* the storm test renames every entry per poll */

typedef struct {
    const char *route;
    int status;   /* -1: no answer */
    int times;    /* how many requests it applies to; <0 forever */
    int skip;     /* let this many matching requests through first */
    long cut_after;
    bool no_id, bad_sha, hang, uncancellable;
} override_t;
static override_t g_ov[8];
static int g_nov;

typedef struct {
    char method[8];
    char path[128];
    char body[160];
    uint32_t at, deadline;
    bool cancellable;
    link_upload_mode_t mode;
    int status;
    bool timed_out, stopped;
    uint32_t sounded_ms; /* the stream's audible ms when an ack arrived */
} req_log_t;
static req_log_t g_log[512];
static int g_nlog;

static int16_t sample_of(const msg_t *m, uint32_t i) { return (int16_t)((i * 31u + (uint8_t)m->id[0] * 7u) & 0x3fff); }

static void msg_pcm(const msg_t *m, uint8_t **out, size_t *n)
{
    const uint32_t samples = m->ms * (AUDIO_RATE / 1000);
    int16_t *p = malloc((size_t)samples * 2 + 2);
    for (uint32_t i = 0; i < samples; i++) p[i] = sample_of(m, i);
    *out = (uint8_t *)p;
    *n = (size_t)samples * 2;
}

static msg_t *add_msg(const char *id, const char *from, bool dad, uint32_t ms)
{
    msg_t *m = &g_msgs[g_nmsgs++];
    memset(m, 0, sizeof(*m));
    scopy(m->id, sizeof(m->id), id);
    scopy(m->from, sizeof(m->from), from);
    m->dad = dad;
    m->ms = ms;
    return m;
}

static msg_t *find_msg(const char *id)
{
    for (int i = 0; i < g_nmsgs; i++) {
        if (strcmp(g_msgs[i].id, id) == 0) return &g_msgs[i];
    }
    return NULL;
}

static bool eligible(const msg_t *m) { return !m->played && !m->gone && m->deliveries < 5; }

static void add_override(override_t o) { g_ov[g_nov++] = o; }

static override_t *match(const char *path)
{
    for (int i = 0; i < g_nov; i++) {
        override_t *o = &g_ov[i];
        if (o->times == 0 || strstr(path, o->route) == NULL) continue;
        if (o->skip > 0) {
            o->skip--;
            continue;
        }
        if (o->times > 0) o->times--;
        return o;
    }
    return NULL;
}

enum { W_OK = 0, W_CANCEL, W_TIMEOUT };

/* A link wait: clamped to the deadline, ticking progress every 250 ms, cancellable between steps. */
static int lwait(const link_req_t *req, uint32_t deadline, uint32_t ms, bool cancellable)
{
    while (true) {
        if (cancellable && req->cancelled != NULL && req->cancelled(req->ctx)) return W_CANCEL;
        if (ms == 0) return W_OK;
        const uint32_t step = ms < 250 ? ms : 250;
        if (deadline != 0 && (int32_t)(g_now + step - deadline) > 0) {
            if ((int32_t)(deadline - g_now) > 0) advance(deadline - g_now);
            return W_TIMEOUT;
        }
        advance(step);
        ms -= step;
        if (req->on_progress != NULL) req->on_progress(req->ctx, LINK_PHASE_TICK, deadline);
    }
}

static void build_waiting(jp_waiting_t *w)
{
    memset(w, 0, sizeof(*w));
    g_poll_gen++;
    w->has_ids = g_mode == BOX_NEW;
    for (int i = 0; i < g_nmsgs; i++) {
        const msg_t *m = &g_msgs[i];
        if (!eligible(m)) continue;
        if (w->n < JPANEL_QUEUE_MAX) {
            jp_who_t *q = &w->q[w->n++];
            if (w->has_ids) scopy(q->id, sizeof(q->id), m->id);
            if (m->first_from[0] == '#') {
                snprintf(q->from, sizeof(q->from), "g%d", g_poll_gen);
            } else {
                scopy(q->from, sizeof(q->from), m->from);
            }
            q->from_dad = m->dad;
        }
        w->count++;
    }
    if (g_nmsgs > 0 && g_msgs[0].first_from[0] == '#') {
        snprintf(w->sibling, sizeof(w->sibling), "g%d", g_poll_gen);
    } else {
        scopy(w->sibling, sizeof(w->sibling), g_sibling);
    }
}

static msg_t *nth_eligible(int at)
{
    for (int i = 0; i < g_nmsgs; i++) {
        if (!eligible(&g_msgs[i])) continue;
        if (at-- == 0) return &g_msgs[i];
    }
    return NULL;
}

static bool qparam(const char *path, const char *key, char *out, size_t cap)
{
    const char *q = strchr(path, '?');
    if (q == NULL) return false;
    char pat[16];
    snprintf(pat, sizeof(pat), "%s=", key);
    const char *p = strstr(q, pat);
    if (p == NULL) return false;
    p += strlen(pat);
    size_t n = 0;
    while (p[n] && p[n] != '&' && n + 1 < cap) {
        out[n] = p[n];
        n++;
    }
    out[n] = '\0';
    return true;
}

static bool json_str(const char *body, const char *key, char *out, size_t cap)
{
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\":\"", key);
    const char *p = strstr(body, pat);
    if (p == NULL) return false;
    p += strlen(pat);
    size_t n = 0;
    while (p[n] && p[n] != '"' && n + 1 < cap) {
        out[n] = p[n];
        n++;
    }
    out[n] = '\0';
    return true;
}

static long json_num(const char *body, const char *key)
{
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\":", key);
    const char *p = strstr(body, pat);
    return p == NULL ? -1 : strtol(p + strlen(pat), NULL, 10);
}

static link_res_t serve_audio(const link_req_t *req, req_log_t *lg, msg_t *m, bool replay,
                              override_t *o)
{
    link_res_t res = {.status = -1, .err = ""};
    const int w = lwait(req, req->deadline_ms, g_latency_ms + g_head_delay_ms, true);
    if (w != W_OK) {
        res.stopped = w == W_CANCEL;
        res.timed_out = w == W_TIMEOUT;
        res.err = w == W_TIMEOUT ? "timeout" : "";
        return res;
    }
    if (m == NULL) {
        res.status = replay ? 404 : 204;
        res.stopped = req->on_head(req->ctx, res.status) != 0;
        return res;
    }
    if (!replay) {
        m->deliveries++;
        m->fetched++;
    } else {
        m->replayed++;
    }
    uint8_t *pcm;
    size_t n;
    msg_pcm(m, &pcm, &n);
    char hex[65];
    sha_hex(pcm, n, hex);
    if (o != NULL && o->bad_sha) hex[0] = hex[0] == '0' ? '1' : '0';
    char num[16];
    if (o == NULL || !o->no_id) req->on_header(req->ctx, "x-jpanel-id", m->id);
    if (!replay) {
        req->on_header(req->ctx, "X-Jpanel-From", m->from);
        req->on_header(req->ctx, "X-Jpanel-From-Kind", m->dad ? "owner" : "panel");
    }
    req->on_header(req->ctx, "X-Jpanel-Sha256", hex);
    if (!replay && g_mode == BOX_NEW) {
        snprintf(num, sizeof(num), "%d", m->deliveries);
        req->on_header(req->ctx, "X-Jpanel-Delivery", num);
        snprintf(num, sizeof(num), "%u", (unsigned)m->ms);
        req->on_header(req->ctx, "X-Jpanel-Duration-Ms", num);
    }
    res.status = 200;
    if (req->on_head(req->ctx, 200) != 0) {
        res.stopped = true;
        free(pcm);
        return res;
    }
    size_t off = 0;
    const long cut = o != NULL ? o->cut_after : -1;
    while (off < n) {
        if (g_body_gap_ms > 0) {
            const int gw = lwait(req, g_now + 20000, g_body_gap_ms, true);
            if (gw == W_CANCEL) {
                res.stopped = true;
                break;
            }
            if (gw == W_TIMEOUT) {
                res.cut = true;
                break;
            }
        } else if (req->cancelled != NULL && req->cancelled(req->ctx)) {
            res.stopped = true;
            break;
        }
        size_t c = n - off < 4096 ? n - off : 4096;
        if (cut >= 0 && off + c > (size_t)cut) c = (size_t)cut - off;
        if (c > 0 && req->sink(req->ctx, pcm + off, c) != 0) {
            res.stopped = true;
            off += c;
            break;
        }
        off += c;
        res.got = off;
        if (cut >= 0 && off >= (size_t)cut) {
            res.cut = true;
            break;
        }
    }
    lg->stopped = res.stopped;
    free(pcm);
    return res;
}

static link_res_t box_request(void *ctx, const link_req_t *req)
{
    (void)ctx;
    req_log_t *lg = &g_log[g_nlog < 511 ? g_nlog++ : 511];
    memset(lg, 0, sizeof(*lg));
    scopy(lg->method, sizeof(lg->method), req->method);
    scopy(lg->path, sizeof(lg->path), req->path);
    if (req->body != NULL && strcmp(req->content_type ? req->content_type : "", "application/json") == 0) {
        snprintf(lg->body, sizeof(lg->body), "%.*s", (int)req->body_len, (const char *)req->body);
    }
    lg->at = g_now;
    lg->deadline = req->deadline_ms;
    lg->cancellable = req->cancelled != NULL;
    lg->mode = req->upload_mode;
    link_res_t res = {.status = -1, .err = ""};
    override_t *o = match(req->path);
    const bool cancellable = o == NULL || !o->uncancellable;
    uint32_t deadline = req->deadline_ms;
    int w = lwait(req, deadline, g_pre_wait_ms, cancellable);
    if (w == W_OK && o != NULL && o->hang) w = lwait(req, deadline, 3600000u, cancellable);
    if (w != W_OK) {
        res.stopped = w == W_CANCEL;
        res.timed_out = w == W_TIMEOUT;
        res.err = w == W_TIMEOUT ? "timeout" : "";
        lg->stopped = res.stopped;
        lg->timed_out = res.timed_out;
        return res;
    }
    const char *p = req->path;
    if (strncmp(p, "/jpanel/send", 12) == 0) {
        /* The upload, frame by frame; the link moves the deadline with it (PROGRESS mode). */
        size_t off = 0;
        const size_t frame = 4096;
        while (off < req->body_len) {
            const size_t c = req->body_len - off < frame ? req->body_len - off : frame;
            const uint32_t ms = g_upload_bps ? (uint32_t)((uint64_t)c * 1000 / g_upload_bps) : 0;
            w = lwait(req, deadline, ms, false);
            if (w != W_OK) break;
            off += c;
            if (req->upload_mode == LINK_DEADLINE_PROGRESS) {
                deadline = g_now + 20000;
                req->on_progress(req->ctx, LINK_PHASE_UPLOAD, deadline);
            }
        }
        if (w == W_OK && req->upload_mode == LINK_DEADLINE_PROGRESS) {
            deadline = g_now + req->head_budget_ms;
            req->on_progress(req->ctx, LINK_PHASE_HEAD, deadline);
        }
        if (w == W_OK) w = lwait(req, deadline, g_transcribe_ms + g_latency_ms, false);
        if (w != W_OK) {
            res.timed_out = true;
            res.err = "timeout";
            lg->timed_out = true;
            return res;
        }
        char hex[65];
        sha_hex(req->body, req->body_len, hex);
        int st = 200;
        if (g_send_at < g_nsend_status) st = g_send_status[g_send_at++];
        if (o != NULL) st = o->status;
        if (st == 200 && req->hdr_value != NULL && strcmp(req->hdr_value, hex) != 0) st = 422;
        res.status = st;
        lg->status = st;
        return res;
    }
    if (o != NULL && o->status != 200 && o->status != 0) {
        w = lwait(req, deadline, g_latency_ms, cancellable);
        res.status = o->status;
        if (o->status < 0) res.err = "connect";
        if (o->status >= 0 && req->on_head != NULL) res.stopped = req->on_head(req->ctx, o->status) != 0;
        lg->status = res.status;
        return res;
    }
    if (strcmp(p, "/jpanel/waiting") == 0) {
        w = lwait(req, deadline, g_latency_ms + (o != NULL ? 0 : 0), cancellable);
        if (w != W_OK) {
            res.stopped = w == W_CANCEL;
            res.timed_out = w == W_TIMEOUT;
            return res;
        }
        jp_waiting_t wt;
        build_waiting(&wt);
        size_t n = sizeof(wt) < req->cap ? sizeof(wt) : req->cap;
        res.status = 200;
        lg->status = 200;
        /* A 200 head, then a body that stalls after `cut_after` bytes until something gives. */
        if (o != NULL && o->cut_after >= 0) {
            if ((size_t)o->cut_after < n) n = (size_t)o->cut_after;
            w = lwait(req, deadline, 3600000u, cancellable);
            res.stopped = lg->stopped = w == W_CANCEL;
            res.timed_out = lg->timed_out = w == W_TIMEOUT;
        }
        memcpy(req->buf, &wt, n);
        res.got = n;
        return res;
    }
    if (strncmp(p, "/jpanel/next", 12) == 0) {
        char v[64];
        msg_t *m = NULL;
        if (g_mode == BOX_NEW && qparam(p, "id", v, sizeof(v))) {
            m = find_msg(v);
            if (m != NULL && !eligible(m)) m = NULL;
        } else {
            const int at = qparam(p, "at", v, sizeof(v)) ? atoi(v) : 0;
            m = nth_eligible(at);
        }
        res = serve_audio(req, lg, m, false, o);
        lg->status = res.status;
        lg->stopped = res.stopped;
        return res;
    }
    if (strncmp(p, "/jpanel/message/", 16) == 0) {
        char id[64];
        scopy(id, sizeof(id), p + 16);
        char *slash = strchr(id, '/');
        if (slash != NULL) *slash = '\0';
        msg_t *m = find_msg(id);
        res = serve_audio(req, lg, m != NULL && !m->gone ? m : NULL, true, o);
        lg->status = res.status;
        return res;
    }
    w = lwait(req, deadline, g_latency_ms, false);
    if (w != W_OK) {
        res.timed_out = true;
        lg->timed_out = true;
        return res;
    }
    const stream_facts_t f = stream_facts(&g_st);
    lg->sounded_ms = f.out_ms;
    char id[64] = "";
    (void)json_str(lg->body, "id", id, sizeof(id));
    msg_t *m = find_msg(id);
    if (strcmp(p, "/jpanel/played") == 0) {
        if (m != NULL) m->played = true;
        res.status = g_mode == BOX_NEW && m == NULL ? 404 : 204;
    } else if (strcmp(p, "/jpanel/returned") == 0) {
        if (g_mode == BOX_TODAY) {
            res.status = 404;
        } else if (m != NULL && !m->played && m->deliveries == json_num(lg->body, "delivery") &&
                   m->returns < 5) {
            m->deliveries--;
            m->returns++;
            res.status = 204;
        } else {
            res.status = 409;
        }
    } else {
        res.status = 404;
    }
    lg->status = res.status;
    return res;
}

/* ── io ───────────────────────────────────────────────────────────────────────────────────── */

static bool io_take(void *ctx, jp_cmd_t *out, uint32_t wait_ms)
{
    (void)ctx;
    while (g_qn == 0 && wait_ms > 0) {
        const uint32_t step = wait_ms < 40 ? wait_ms : 40;
        advance(step);
        wait_ms -= step;
    }
    if (g_qn == 0) return false;
    *out = g_q[0];
    memmove(&g_q[0], &g_q[1], (size_t)(g_qn - 1) * sizeof(g_q[0]));
    g_qn--;
    return true;
}

static bool io_queue_empty(void *ctx)
{
    (void)ctx;
    return g_qn == 0;
}

static uint32_t io_now(void *ctx)
{
    (void)ctx;
    return g_now;
}

static void io_sleep(void *ctx, uint32_t ms)
{
    (void)ctx;
    advance(ms);
}

static bool io_parse(void *ctx, const uint8_t *body, size_t n, jp_waiting_t *out)
{
    (void)ctx;
    if (n != sizeof(*out)) return false;
    memcpy(out, body, n);
    return true;
}

static void io_sha_begin(void *ctx)
{
    (void)ctx;
    sha_init(&g_sha);
}

static void io_sha_update(void *ctx, const uint8_t *d, size_t n)
{
    (void)ctx;
    sha_add(&g_sha, d, n);
}

static void io_sha_finish(void *ctx, uint8_t out[JP_SHA256_LEN])
{
    (void)ctx;
    sha_done(&g_sha, out);
}

static void io_yield(void *ctx, const char *point)
{
    (void)ctx;
    if (strcmp(point, "published") == 0) record();
    if (g_yield != NULL) g_yield(point);
}

static void reset_at(box_mode_t mode, uint32_t start)
{
    g_now = start;
    stream_init(&g_st, g_ring, NULL, NULL);
    stream_set_arbiter(&g_st, true);
    g_tick = NULL;
    g_yield = NULL;
    g_backstop = g_unclaimed = g_wd = 0;
    g_last_hb = 0;
    g_hb_at = g_now;
    g_nposted = 0;
    g_qn = 0;
    memset(&g_last, 0, sizeof(g_last));
    g_nouts = 0;
    g_npubs = 0;
    g_sent_count = 0;
    g_quiet = false;
    g_mode = mode;
    g_nmsgs = 0;
    g_latency_ms = 30;
    g_head_delay_ms = 0;
    g_pre_wait_ms = 0;
    g_body_gap_ms = 0;
    g_upload_bps = 0;
    g_transcribe_ms = 0;
    g_nsend_status = g_send_at = 0;
    snprintf(g_sibling, sizeof(g_sibling), "Tamsin");
    g_poll_gen = 0;
    g_nov = 0;
    g_nlog = 0;
    fake_stream_clip_busy = false;
    reach_reset_for_test();
    static const jpanel_io_t io = {
        .ctx = NULL,
        .request = box_request,
        .take = io_take,
        .queue_empty = io_queue_empty,
        .now_ms = io_now,
        .sleep_ms = io_sleep,
        .parse_waiting = io_parse,
        .sha_begin = io_sha_begin,
        .sha_update = io_sha_update,
        .sha_finish = io_sha_finish,
        .yield = io_yield,
        .stream = &g_st,
        .poll_first_ms = 3600000u,
    };
    jpanel_core_init(&io);
    g_last = snap();
}

static void reset(box_mode_t mode) { reset_at(mode, 1000); }

/* ── render's side ────────────────────────────────────────────────────────────────────────── */

static bool post(jp_cmd_t c)
{
    if (g_qn >= 4) return false;
    g_q[g_qn++] = c;
    g_posted[g_nposted++] = (posted_t){.seq = c.seq, .expires_at = c.expires_at};
    return true;
}

static uint32_t post_play(const char *id, int at)
{
    const jp_view_t v = snap();
    jp_cmd_t c = {.kind = JP_CMD_PLAY, .seq = ++g_seq, .gen = stream_gen(&g_st),
                  .expires_at = g_now + JP_PLAY_EXPIRE_MS, .at = at};
    for (int i = 0; i < v.waiting.n; i++) {
        if ((id[0] && strcmp(v.waiting.q[i].id, id) == 0) || (!id[0] && i == at)) {
            c.target = v.waiting.q[i];
            if (id[0]) c.at = i;
        }
    }
    scopy(c.target.id, sizeof(c.target.id), id);
    (void)post(c);
    return c.seq;
}

static uint32_t post_replay(const jp_who_t *who)
{
    jp_cmd_t c = {.kind = JP_CMD_REPLAY, .seq = ++g_seq, .gen = stream_gen(&g_st),
                  .expires_at = g_now + JP_PLAY_EXPIRE_MS, .target = *who};
    (void)post(c);
    return c.seq;
}

static int16_t g_capture[600000];

static uint32_t post_send(size_t bytes, jp_to_t to)
{
    const bool leased = stream_lease_take(&g_st, STREAM_LEASE_SEND);
    CHECK(leased, "render takes lease 1 before posting a SEND");
    for (size_t i = 0; i < bytes / 2 && i < sizeof(g_capture) / 2; i++) g_capture[i] = (int16_t)(i * 3);
    jp_cmd_t c = {.kind = JP_CMD_SEND, .seq = ++g_seq, .expires_at = g_now + JP_SEND_EXPIRE_MS,
                  .to = to, .pcm = g_capture, .bytes = bytes};
    (void)post(c);
    return c.seq;
}

/* ── driving ──────────────────────────────────────────────────────────────────────────────── */

static void drive(uint32_t ms)
{
    const uint32_t end = g_now + ms;
    while (!jp_due(g_now, end)) jpanel_core_step();
}

static bool done(uint32_t seq) { return jp_seq_ge(snap().done_seq, seq); }

static void drive_done(uint32_t seq, uint32_t max_ms)
{
    const uint32_t end = g_now + max_ms;
    while (!done(seq) && !jp_due(g_now, end)) jpanel_core_step();
    /* Then let the trailing ack and poll settle. */
    drive(500);
}

static void poll_now(void)
{
    jpanel_core_poll_soon();
    const int before = g_nlog;
    const uint32_t end = g_now + 5000;
    while (!jp_due(g_now, end)) {
        jpanel_core_step();
        bool polled = false;
        for (int i = before; i < g_nlog; i++) polled |= strcmp(g_log[i].path, "/jpanel/waiting") == 0;
        if (polled) break;
    }
}

static int count_req(const char *prefix, int from)
{
    int n = 0;
    for (int i = from; i < g_nlog; i++) n += strncmp(g_log[i].path, prefix, strlen(prefix)) == 0;
    return n;
}

static const req_log_t *last_req(const char *prefix)
{
    for (int i = g_nlog - 1; i >= 0; i--) {
        if (strncmp(g_log[i].path, prefix, strlen(prefix)) == 0) return &g_log[i];
    }
    return NULL;
}

static int ends_of(uint32_t seq)
{
    for (int i = 0; i < g_nposted; i++) {
        if (g_posted[i].seq == seq) return g_posted[i].ends;
    }
    return -1;
}

static bool all_ended_once(void)
{
    for (int i = 0; i < g_nposted; i++) {
        if (g_posted[i].ends != 1) return false;
    }
    return true;
}

static void deadlines_held(const char *what)
{
    char m[160];
    snprintf(m, sizeof(m), "%s: no claimed backstop fired (%d)", what, g_backstop);
    CHECK(g_backstop == 0, m);
    snprintf(m, sizeof(m), "%s: no unclaimed backstop fired (%d)", what, g_unclaimed);
    CHECK(g_unclaimed == 0, m);
    snprintf(m, sizeof(m), "%s: the engine watchdog never fired (%d)", what, g_wd);
    CHECK(g_wd == 0, m);
}

static uint32_t g_stop_at_ms;
static bool g_stop_heard, g_stopped;

static void stop_when_audible(void)
{
    if (g_stopped) return;
    const stream_facts_t f = stream_facts(&g_st);
    if (f.stream_id != 0 && f.out_ms >= g_stop_at_ms && f.stream_live) {
        g_stopped = true;
        stream_stop(&g_st, g_stop_heard);
    }
}

static void arm_stop(uint32_t at_ms, bool heard)
{
    g_stop_at_ms = at_ms;
    g_stop_heard = heard;
    g_stopped = false;
    g_tick = stop_when_audible;
}

/* ── tests ────────────────────────────────────────────────────────────────────────────────── */

static void test_new_box_play_heard_acks_with_heard_ms(void)
{
    reset(BOX_NEW);
    add_msg("a1", "Tamsin", false, 3000);
    poll_now();
    CHECK(snap().waiting.count == 1 && snap().waiting.has_ids, "the poll lists the id");
    const uint32_t seq = post_play("a1", 0);
    drive_done(seq, 20000);
    const jp_view_t v = snap();
    CHECK(v.play.seq == seq && v.play.kind == JP_PLAY_HEARD, "a whole message is heard");
    CHECK(strcmp(v.play.who.id, "a1") == 0 && strcmp(v.play.who.from, "Tamsin") == 0,
          "the outcome carries who it was");
    CHECK(strcmp(v.last_begun.id, "a1") == 0, "AGAIN targets what began");
    const req_log_t *ack = last_req("/jpanel/played");
    CHECK(ack != NULL && strstr(ack->body, "\"id\":\"a1\"") != NULL, "acked by id");
    CHECK(ack != NULL && json_num(ack->body, "heard_ms") >= 2900, "with the audible ms");
    CHECK(last_req("/jpanel/next?id=a1&at=0") != NULL, "fetched by id, with `at` riding along");
    CHECK(g_msgs[0].played && g_msgs[0].fetched == 1, "played once on the box");
    CHECK(v.waiting.count == 0, "nothing waits");
    CHECK(v.phase == JP_IDLE && !v.cur.run_continues, "back to idle");
    CHECK(all_ended_once(), "the command ended exactly once");
    deadlines_held("heard");
}

static void test_old_box_positional_fetch_still_acks_header_id(void)
{
    reset(BOX_TODAY);
    add_msg("b1", "Dad", true, 1500);
    poll_now();
    CHECK(!snap().waiting.has_ids && snap().waiting.count == 1, "today's box sends no ids");
    const uint32_t seq = post_play("", 0);
    drive_done(seq, 20000);
    const jp_view_t v = snap();
    CHECK(last_req("/jpanel/next?at=0") != NULL, "positional fetch");
    CHECK(v.play.kind == JP_PLAY_HEARD, "heard, not heard_unacked: the head carried an id");
    const req_log_t *ack = last_req("/jpanel/played");
    CHECK(ack != NULL && strstr(ack->body, "\"id\":\"b1\"") != NULL, "acks the head's X-Jpanel-Id");
    CHECK(v.play.who.from_dad && strcmp(v.play.who.from, "Dad") == 0, "the head's sender wins");
    CHECK(g_msgs[0].played, "the box retired it");
    deadlines_held("old box");
}

static void test_heard_unacked_only_without_header_id(void)
{
    reset(BOX_TODAY);
    add_msg("c1", "Tamsin", false, 1000);
    add_override((override_t){.route = "/jpanel/next", .status = 200, .times = 1, .cut_after = -1,
                              .no_id = true});
    poll_now();
    const uint32_t seq = post_play("", 0);
    drive_done(seq, 20000);
    CHECK(snap().play.kind == JP_PLAY_HEARD_UNACKED, "no id on the head: heard, nothing to ack");
    CHECK(count_req("/jpanel/played", 0) == 0, "no acknowledgement without an id");
}

static void test_against_0_3_39_box_short_stop_acks(void)
{
    reset(BOX_TODAY);
    add_msg("d1", "Tamsin", false, 5000);
    poll_now();
    arm_stop(500, false);
    const uint32_t seq = post_play("", 0);
    drive_done(seq, 20000);
    CHECK(snap().play.kind == JP_PLAY_STOPPED_HEARD, "a stop on today's box is heard");
    CHECK(count_req("/jpanel/played", 0) == 1, "and acknowledged, as 0.3.39 does");
    CHECK(count_req("/jpanel/returned", 0) == 0, "never handed back without a delivery header");
    CHECK(count_req("/jpanel/next", 0) == 1, "nothing chains on after a stop");
    CHECK(all_ended_once(), "one end");
}

static int g_ret_pub_ok = -1;
static void watch_returned(const char *point)
{
    if (strcmp(point, "published") != 0 || g_ret_pub_ok >= 0) return;
    const jp_view_t v = snap();
    if (v.play.kind != JP_PLAY_RETURNED) return;
    bool listed = false;
    for (int i = 0; i < v.waiting.n; i++) listed |= strcmp(v.waiting.q[i].id, "e1") == 0;
    g_ret_pub_ok = listed && v.waiting.count == 2 && v.waiting.q[0].id[0] == 'e' && v.cur.in_waiting;
}

static void test_stop_in_preroll_returns_not_acks(void)
{
    reset(BOX_NEW);
    add_msg("e1", "Tamsin", false, 5000);
    add_msg("e2", "Dad", true, 2000);
    poll_now();
    arm_stop(500, false);
    g_ret_pub_ok = -1;
    g_yield = watch_returned;
    const uint32_t seq = post_play("e1", 0);
    drive_done(seq, 20000);
    const jp_view_t v = snap();
    CHECK(v.play.kind == JP_PLAY_RETURNED, "a stop under the threshold hands it back");
    CHECK(g_ret_pub_ok == 1, "re-inserted at its old place in the SAME write as the outcome");
    CHECK(count_req("/jpanel/played", 0) == 0, "not acknowledged");
    const req_log_t *r = last_req("/jpanel/returned");
    CHECK(r != NULL && strstr(r->body, "\"id\":\"e1\"") && json_num(r->body, "delivery") == 1,
          "returned with the delivery the head named");
    CHECK(g_msgs[0].deliveries == 0 && g_msgs[0].returns == 1, "the box refunded the attempt");
    CHECK(count_req("/jpanel/waiting", 0) >= 2, "and a poll followed");
    CHECK(snap().waiting.count == 2 && strcmp(snap().waiting.q[0].id, "e1") == 0,
          "the poll agrees: it is still first");
    jp_stats_t st;
    jpanel_core_stats(&st);
    CHECK(st.returned == 1, "counted");
}

static void test_stop_after_threshold_acks_with_heard_ms(void)
{
    reset(BOX_NEW);
    add_msg("f1", "Tamsin", false, 5000);
    poll_now();
    arm_stop(2500, false);
    const uint32_t seq = post_play("f1", 0);
    drive_done(seq, 20000);
    CHECK(snap().play.kind == JP_PLAY_STOPPED_HEARD, "past the threshold, a stop is heard");
    const req_log_t *ack = last_req("/jpanel/played");
    CHECK(ack != NULL && json_num(ack->body, "heard_ms") >= 2000, "acked with heard_ms");
    CHECK(count_req("/jpanel/returned", 0) == 0, "not returned");
}

static void test_reply_stop_counts_heard(void)
{
    reset(BOX_NEW);
    add_msg("g1", "Tamsin", false, 5000);
    poll_now();
    arm_stop(400, true);
    const uint32_t seq = post_play("g1", 0);
    drive_done(seq, 20000);
    CHECK(snap().play.kind == JP_PLAY_STOPPED_HEARD, "a REPLY stop counts as heard (§4)");
    CHECK(count_req("/jpanel/played", 0) == 1, "and is acknowledged");
    /* As `stream.c` keeps it: the bit stays in the word, but the next plain stop no longer
       carries it. */
    stream_stop(&g_st, false);
    CHECK((stream_stop_word(&g_st) & 1u) == 0, "classification ends the heard bit's stickiness");
}

static void test_short_message_threshold_is_80_percent(void)
{
    reset(BOX_NEW);
    add_msg("h1", "Tamsin", false, 1500);
    poll_now();
    arm_stop(1300, false);
    const uint32_t seq = post_play("h1", 0);
    drive_done(seq, 20000);
    CHECK(snap().play.kind == JP_PLAY_STOPPED_HEARD, "80% of a short message is heard");
}

static void test_returned_404_counts_no_refund(void)
{
    reset(BOX_NEW);
    add_msg("i1", "Tamsin", false, 5000);
    add_override((override_t){.route = "/jpanel/returned", .status = 404, .times = -1, .cut_after = -1});
    poll_now();
    arm_stop(300, false);
    const uint32_t seq = post_play("i1", 0);
    drive_done(seq, 20000);
    jp_stats_t st;
    jpanel_core_stats(&st);
    CHECK(snap().play.kind == JP_PLAY_RETURNED, "returned");
    CHECK(st.no_refund == 1 && !snap().ack_owed, "a 404 clears the slot and counts no_refund");
    CHECK(count_req("/jpanel/returned", 0) == 1, "and is not retried");

    reset(BOX_NEW);
    add_msg("i2", "Tamsin", false, 5000);
    add_override((override_t){.route = "/jpanel/returned", .status = 409, .times = -1, .cut_after = -1});
    poll_now();
    arm_stop(300, false);
    const uint32_t s2 = post_play("i2", 0);
    drive_done(s2, 20000);
    jpanel_core_stats(&st);
    CHECK(st.no_refund == 0 && !snap().ack_owed && count_req("/jpanel/returned", 0) == 1,
          "a 409 clears without counting");
}

static uint32_t g_stop_when;
static void stop_at_time(void)
{
    if (g_stop_when != 0 && jp_due(g_now, g_stop_when)) {
        g_stop_when = 0;
        stream_stop(&g_st, false);
    }
}

static void test_stop_during_5s_head_delay_cancels_request(void)
{
    reset(BOX_NEW);
    add_msg("j1", "Tamsin", false, 3000);
    poll_now();
    g_head_delay_ms = 5000;
    const uint32_t seq = post_play("j1", 0);
    const uint32_t t0 = g_now;
    g_stop_when = t0 + 1000;
    g_tick = stop_at_time;
    while (!done(seq) && !jp_due(g_now, t0 + 20000)) jpanel_core_step();
    g_tick = NULL;
    const uint32_t stopped_at = t0 + 1000;
    CHECK(done(seq), "the PLAY ended");
    CHECK((int32_t)(g_now - stopped_at) <= 300, "within a link step of the stop");
    const jp_view_t v = snap();
    CHECK(v.play.kind == JP_PLAY_CANCELLED, "a stop before a byte cancels");
    CHECK(last_req("/jpanel/next")->stopped, "the request itself was abandoned");
    CHECK(g_msgs[0].deliveries == 0, "the head never came, so no attempt was spent");
    CHECK(v.last_begun.id[0] == '\0', "a fetch that never began is not AGAIN's target");
    CHECK(count_req("/jpanel/played", 0) == 0 && count_req("/jpanel/returned", 0) == 0,
          "nothing to acknowledge");
    CHECK(v.waiting.count == 1 && v.cur.in_waiting, "it still waits");
    /* And the next press, at the new gen, plays (test_stop_with_no_stream_then_play_starts). */
    g_head_delay_ms = 0;
    const uint32_t s2 = post_play("j1", 0);
    drive_done(s2, 20000);
    CHECK(snap().play.kind == JP_PLAY_HEARD, "a fresh press after the stop plays");
    deadlines_held("head-delay stop");
}

static void test_stop_with_no_stream_then_play_starts(void)
{
    reset(BOX_NEW);
    add_msg("k1", "Tamsin", false, 1000);
    poll_now();
    stream_stop(&g_st, false);
    stream_stop(&g_st, false);
    drive(1000);
    const uint32_t seq = post_play("k1", 0);
    drive_done(seq, 20000);
    CHECK(snap().play.kind == JP_PLAY_HEARD, "a stop with nothing playing leaves the next press whole");
}

/* A1, every point: a stop at each named interleave point of a three-message run, on both boxes.
   Whatever the point: the PLAY ends exactly once, nothing is fetched after the stop, nothing that
   did not sound is acknowledged on the new box, the speaker is free, and a fresh press plays. */
static const char *g_stop_point;
static int g_stop_k, g_stop_seen, g_stop_log;
static void stop_at_point(const char *point)
{
    if (g_stop_log >= 0 || strcmp(point, g_stop_point) != 0) return;
    if (g_stop_seen++ != g_stop_k) return;
    stream_stop(&g_st, false);
    g_stop_log = g_nlog;
}

static void test_stop_at_every_interleave_point(void)
{
    static const char *points[] = {"claimed", "head", "chunk", "classified", "acked", "publishing"};
    for (int mode = 0; mode < 2; mode++) {
        for (size_t p = 0; p < sizeof(points) / sizeof(points[0]); p++) {
            for (int k = 0; k < 3; k++) {
                reset(mode == 0 ? BOX_TODAY : BOX_NEW);
                add_msg("m1", "Tamsin", false, 1000);
                add_msg("m2", "Dad", true, 1000);
                add_msg("m3", "Tamsin", false, 1000);
                poll_now();
                g_stop_point = points[p];
                g_stop_k = k;
                g_stop_seen = 0;
                g_stop_log = -1;
                g_yield = stop_at_point;
                const uint32_t seq = post_play(mode == 0 ? "" : "m1", 0);
                drive_done(seq, 60000);
                g_yield = NULL;
                char m[128];
                snprintf(m, sizeof(m), "%s #%d (%s box): the PLAY ended exactly once", points[p], k,
                         mode ? "new" : "today's");
                CHECK(ends_of(seq) == 1, m);
                snprintf(m, sizeof(m), "%s #0 (%s box): the point was reached", points[p],
                         mode ? "new" : "today's");
                CHECK(k > 0 || g_stop_log >= 0, m);
                if (g_stop_log >= 0) {
                    snprintf(m, sizeof(m), "%s #%d: nothing is fetched after the stop", points[p], k);
                    int after = count_req("/jpanel/next", g_stop_log);
                    /* A stop inside the fetch's own head callback is that fetch, not a new one. */
                    CHECK(after == 0, m);
                }
                for (int i = 0; i < g_nlog; i++) {
                    if (mode == 1 && strcmp(g_log[i].path, "/jpanel/played") == 0) {
                        snprintf(m, sizeof(m), "%s #%d: an ack only for what sounded", points[p], k);
                        CHECK(g_log[i].sounded_ms > 0, m);
                    }
                }
                for (int i = 0; i < g_nmsgs; i++) {
                    snprintf(m, sizeof(m), "%s #%d: no message fetched twice", points[p], k);
                    CHECK(g_msgs[i].fetched <= 1, m);
                }
                const jp_view_t v = snap();
                snprintf(m, sizeof(m), "%s #%d: idle and the speaker free", points[p], k);
                CHECK(v.phase == JP_IDLE && !stream_facts(&g_st).stream_live, m);
                poll_now();
                const jp_view_t w = snap();
                if (w.waiting.count > 0) {
                    const uint32_t s2 = post_play(mode == 0 ? "" : w.waiting.q[0].id, 0);
                    drive_done(s2, 60000);
                    snprintf(m, sizeof(m), "%s #%d: a fresh press plays afterwards", points[p], k);
                    CHECK(snap().play.seq == s2 && (snap().play.kind == JP_PLAY_HEARD), m);
                }
                snprintf(m, sizeof(m), "%s #%d", points[p], k);
                deadlines_held(m);
            }
        }
    }
}

static void test_run_gap_stop_never_acks_unsounded(void)
{
    for (int mode = 0; mode < 2; mode++) {
        reset(mode == 0 ? BOX_TODAY : BOX_NEW);
        add_msg("n1", "Tamsin", false, 1000);
        add_msg("n2", "Dad", true, 1000);
        poll_now();
        g_stop_point = "acked";
        g_stop_k = 0;
        g_stop_seen = 0;
        g_stop_log = -1;
        g_yield = stop_at_point;
        const uint32_t seq = post_play(mode == 0 ? "" : "n1", 0);
        drive_done(seq, 30000);
        CHECK(g_msgs[0].played, "the first was heard and acknowledged");
        CHECK(!g_msgs[1].played && g_msgs[1].deliveries == 0, "the second never sounded, never acked");
        CHECK(count_req("/jpanel/next", 0) == 1, "and was never even fetched");
        CHECK(ends_of(seq) == 1 && !snap().cur.run_continues, "the run ended");
    }
}

static uint32_t g_next_seq;
static bool g_queued;
static void queue_during_drain(void)
{
    const jp_view_t v = snap();
    if (!g_queued && v.phase == JP_SOUNDING && stream_facts(&g_st).out_ms > 200) {
        g_queued = true;
        g_next_seq = post_play("o2", 1);
    }
}

static void test_next_queued_during_drain_classifies_previous_first(void)
{
    reset(BOX_NEW);
    add_msg("o1", "Tamsin", false, 2000);
    poll_now();
    add_msg("o2", "Dad", true, 1000);
    g_queued = false;
    g_tick = queue_during_drain;
    const uint32_t seq = post_play("o1", 0);
    drive_done(seq, 20000);
    g_tick = NULL;
    drive_done(g_next_seq, 20000);
    int done_at = -1, claim_at = -1;
    for (int i = 0; i < g_npubs; i++) {
        if (done_at < 0 && g_pubs[i].done_seq == seq) done_at = i;
        if (claim_at < 0 && g_pubs[i].claimed_seq == g_next_seq) claim_at = i;
    }
    CHECK(g_queued && done_at >= 0 && claim_at > done_at,
          "the previous message is classified and ended before the next is claimed");
    CHECK(g_outs[0].seq == seq && g_outs[0].kind == JP_PLAY_HEARD, "and it was heard");
    CHECK(all_ended_once(), "each command ended once");
}

static void test_chain_fetch_failure_returns_to_idle(void)
{
    for (int mode = 0; mode < 2; mode++) {
        reset(mode == 0 ? BOX_TODAY : BOX_NEW);
        add_msg("p1", "Tamsin", false, 1000);
        add_msg("p2", "Dad", true, 1000);
        add_msg("p3", "Dad", true, 1000);
        add_override((override_t){.route = "/jpanel/next", .status = 500, .times = 1, .skip = 1,
                                  .cut_after = -1});
        poll_now();
        const uint32_t seq = post_play(mode == 0 ? "" : "p1", 0);
        drive_done(seq, 30000);
        const jp_view_t v = snap();
        CHECK(done(seq) && v.play.kind == JP_PLAY_FAILED && !v.cur.run_continues,
              "a chained fetch that fails ends the run with a failure");
        CHECK(v.phase == JP_IDLE, "back to idle (A2)");
        const int fetches = count_req("/jpanel/next", 0);
        drive(60000);
        CHECK(fetches == 2 && count_req("/jpanel/next", 0) == 2, "no second fetch, ever");
        CHECK(g_msgs[0].played && !g_msgs[1].played, "the first stays heard");
        deadlines_held("chain failure");
    }
}

static void test_run_204_skips_to_next(void)
{
    reset(BOX_NEW);
    add_msg("q1", "Tamsin", false, 1000);
    add_msg("q2", "Dad", true, 1000);
    add_msg("q3", "Tamsin", false, 1000);
    poll_now();
    g_msgs[1].gone = true; /* the owner discarded it after the poll */
    const uint32_t seq = post_play("q1", 0);
    drive_done(seq, 30000);
    CHECK(g_msgs[0].played && g_msgs[2].played, "the run went on past the 204");
    int heard = 0, other = 0;
    for (int i = 0; i < g_nouts; i++) {
        if (g_outs[i].seq != seq) continue;
        if (g_outs[i].kind == JP_PLAY_HEARD) heard++;
        else other++;
    }
    CHECK(heard >= 2 && other == 0, "a 204 for a run's next id publishes no outcome");
    CHECK(ends_of(seq) == 1, "one end");

    reset(BOX_NEW);
    add_msg("q4", "Tamsin", false, 1000);
    add_msg("q5", "Dad", true, 1000);
    poll_now();
    g_msgs[1].gone = true;
    const uint32_t s2 = post_play("q4", 0);
    drive_done(s2, 30000);
    const jp_view_t v = snap();
    CHECK(v.play.kind == JP_PLAY_HEARD && strcmp(v.play.who.id, "q4") == 0 && !v.cur.run_continues,
          "with none left, the run ends on the previous message's outcome");
}

static void test_pressed_204_is_empty(void)
{
    reset(BOX_NEW);
    add_msg("r1", "Tamsin", false, 1000);
    poll_now();
    g_msgs[0].gone = true;
    const uint32_t seq = post_play("r1", 0);
    drive_done(seq, 10000);
    CHECK(snap().play.kind == JP_PLAY_EMPTY, "a 204 for the pressed message is empty");
    CHECK(snap().waiting.count == 0, "and it leaves the list");
}

static void test_short_stream_ends_run_without_refetch(void)
{
    for (int variant = 0; variant < 2; variant++) {
        reset(BOX_NEW);
        add_msg("s1", "Tamsin", false, 2000);
        add_msg("s2", "Dad", true, 1000);
        add_override((override_t){.route = "/jpanel/next", .status = 200, .times = 1,
                                  .cut_after = variant == 0 ? 20000 : -1, .bad_sha = variant == 1});
        poll_now();
        const uint32_t seq = post_play("s1", 0);
        drive_done(seq, 30000);
        const jp_view_t v = snap();
        CHECK(v.play.kind == JP_PLAY_SHORT && !v.cur.run_continues,
              variant ? "a digest mismatch is short" : "a cut stream is short");
        CHECK(count_req("/jpanel/played", 0) == 0, "never acknowledged");
        CHECK(count_req("/jpanel/next", 0) == 1, "and the run ends without a refetch");
        CHECK(!g_msgs[0].played, "it stays unplayed on the box");
    }
}

static void test_lost_ack_is_retried_and_never_replays(void)
{
    reset(BOX_NEW);
    add_msg("t1", "Tamsin", false, 1000);
    add_msg("t2", "Dad", true, 1000);
    add_override((override_t){.route = "/jpanel/played", .status = -1, .times = 3, .cut_after = -1});
    poll_now();
    const uint32_t seq = post_play("t1", 0);
    drive_done(seq, 30000);
    jp_view_t v = snap();
    CHECK(v.play.kind == JP_PLAY_HEARD && !v.cur.run_continues && done(seq),
          "a failed ack ends the run (run_continues false)");
    CHECK(v.ack_owed, "the ack is owed");
    CHECK(count_req("/jpanel/next", 0) == 1, "and nothing is fetched on top of it");
    /* test_owed_played_id_filtered_from_waiting */
    poll_now();
    v = snap();
    bool listed = false;
    for (int i = 0; i < v.waiting.n; i++) listed |= strcmp(v.waiting.q[i].id, "t1") == 0;
    CHECK(!listed && v.waiting.count == 1, "the poll drops the owed id");
    drive(20000);
    CHECK(!snap().ack_owed && g_msgs[0].played, "retried with backoff until it landed");
    CHECK(count_req("/jpanel/played", 0) == 4, "three failures, then one success");
    jp_stats_t st;
    jpanel_core_stats(&st);
    CHECK(st.ack_retries == 3 && st.ack_owed == 1, "counted");
    CHECK(g_msgs[0].fetched == 1, "never replayed");
    const uint32_t s2 = post_play("t2", 0);
    drive_done(s2, 20000);
    CHECK(snap().play.kind == JP_PLAY_HEARD && g_msgs[0].fetched == 1, "the next run plays only t2");
}

static void test_play_while_ack_owed_retries_once_then_fails(void)
{
    reset(BOX_NEW);
    add_msg("u1", "Tamsin", false, 1000);
    add_msg("u2", "Dad", true, 1000);
    add_override((override_t){.route = "/jpanel/played", .status = 503, .times = -1, .cut_after = -1});
    poll_now();
    const uint32_t seq = post_play("u1", 0);
    drive_done(seq, 30000);
    const int acks = count_req("/jpanel/played", 0);
    const uint32_t s2 = post_play("u2", 0);
    drive_done(s2, 20000);
    CHECK(snap().play.kind == JP_PLAY_FAILED && strcmp(snap().play.reason, "ack-pending") == 0,
          "a PLAY behind an owed ack retries it once, then fails");
    CHECK(count_req("/jpanel/played", 0) >= acks + 1, "it did retry first");
    CHECK(g_msgs[1].fetched == 0, "and fetched nothing");
}

static void test_ack_4xx_is_terminal(void)
{
    reset(BOX_NEW);
    add_msg("v1", "Tamsin", false, 1000);
    add_msg("v2", "Dad", true, 1000);
    add_override((override_t){.route = "/jpanel/played", .status = 400, .times = 1, .cut_after = -1});
    poll_now();
    const uint32_t seq = post_play("v1", 0);
    drive_done(seq, 30000);
    CHECK(!snap().ack_owed && count_req("/jpanel/played", 0) == 2, "a 400 is not retried");
    CHECK(g_msgs[1].played, "and the run went on");

    reset(BOX_NEW);
    add_msg("v3", "Tamsin", false, 1000);
    add_override((override_t){.route = "/jpanel/played", .status = 408, .times = 1, .cut_after = -1});
    poll_now();
    const uint32_t s2 = post_play("v3", 0);
    drive_done(s2, 30000);
    drive(5000);
    CHECK(count_req("/jpanel/played", 0) == 2 && g_msgs[0].played, "a 408 is retried");
}

static void test_ack_gives_up_after_10_min(void)
{
    reset(BOX_NEW);
    add_msg("w1", "Tamsin", false, 1000);
    add_override((override_t){.route = "/jpanel/played", .status = -1, .times = -1, .cut_after = -1});
    poll_now();
    const uint32_t seq = post_play("w1", 0);
    drive_done(seq, 30000);
    const uint32_t t0 = g_now;
    drive(9 * 60000);
    CHECK(snap().ack_owed, "still owed at nine minutes");
    drive(2 * 60000);
    CHECK(!snap().ack_owed, "given up after ten");
    const int n = count_req("/jpanel/played", 0);
    CHECK(n >= 20 && n <= 30, "backoff capped at 30 s");
    (void)t0;
    deadlines_held("ack give-up");
}

static void test_play_expiry_exceeds_ack_and_https_poll(void)
{
    CHECK(JP_PLAY_EXPIRE_MS - JP_UI_MARGIN_MS > 20000u + 10000u,
          "a PLAY stays claimable through an HTTPS poll and an ack");
    reset(BOX_NEW);
    add_msg("x1", "Tamsin", false, 1000);
    poll_now();
    /* An uncancellable 20 s poll, then an owed ack whose retry takes its whole 10 s. */
    add_override((override_t){.route = "/jpanel/waiting", .status = 200, .times = 1, .cut_after = -1,
                              .hang = true, .uncancellable = true});
    jpanel_core_poll_soon();
    jpanel_core_step();
    const uint32_t seq = post_play("x1", 0);
    drive_done(seq, 60000);
    CHECK(snap().play.kind == JP_PLAY_HEARD, "claimed and played after the stalled poll");
    deadlines_held("expiry");
}

static void test_claim_near_expiry_never_trips_unclaimed_backstop(void)
{
    reset(BOX_NEW);
    add_msg("y1", "Tamsin", false, 1000);
    poll_now();
    jp_cmd_t c = {.kind = JP_CMD_PLAY, .seq = ++g_seq, .gen = stream_gen(&g_st),
                  .expires_at = g_now + JP_UI_MARGIN_MS - 1};
    snprintf(c.target.id, sizeof(c.target.id), "y1");
    (void)post(c);
    drive_done(c.seq, 10000);
    CHECK(snap().play.seq == c.seq && snap().play.kind == JP_PLAY_CANCELLED &&
              strcmp(snap().play.reason, "expired") == 0,
          "inside the margin it is ended, not claimed");
    CHECK(g_msgs[0].fetched == 0, "and never fetched");
    jp_cmd_t d = c;
    d.seq = ++g_seq;
    d.expires_at = g_now + JP_UI_MARGIN_MS + 600;
    (void)post(d);
    drive_done(d.seq, 10000);
    CHECK(snap().play.seq == d.seq && snap().play.kind == JP_PLAY_HEARD,
          "just outside it, claimed and played");
    deadlines_held("claim margin");
}

static void test_expired_and_stale_commands_end_at_once(void)
{
    reset(BOX_NEW);
    add_msg("z1", "Tamsin", false, 1000);
    poll_now();
    jp_cmd_t c = {.kind = JP_CMD_PLAY, .seq = ++g_seq, .gen = stream_gen(&g_st),
                  .expires_at = g_now + JP_PLAY_EXPIRE_MS};
    snprintf(c.target.id, sizeof(c.target.id), "z1");
    stream_stop(&g_st, false);
    (void)post(c);
    drive_done(c.seq, 5000);
    CHECK(snap().play.kind == JP_PLAY_CANCELLED && strcmp(snap().play.reason, "stale") == 0,
          "a stale-gen PLAY ends at once");
    const uint32_t s = post_send(3200, JP_TO_DAD);
    g_q[g_qn - 1].expires_at = g_now + 1000;
    drive_done(s, 5000);
    CHECK(snap().send.seq == s && snap().send.kind == JP_SEND_FAILED &&
              strcmp(snap().send.reason, "expired") == 0,
          "an expired SEND ends before claim");
    CHECK(!stream_leased(&g_st), "and its lease is released (A5)");
    CHECK(count_req("/jpanel/send", 0) == 0, "nothing was uploaded");
    CHECK(all_ended_once(), "both ended once");
}

static bool g_lease_ok;
static uint32_t g_send_seq;
static void lease_free_at_outcome(const char *point)
{
    if (strcmp(point, "published") != 0) return;
    if (snap().send.seq == g_send_seq && stream_leased(&g_st)) g_lease_ok = false;
}

static void test_send_outcomes(void)
{
    static const struct {
        int status;
        jp_send_kind_t kind;
        const char *why;
    } cases[] = {{200, JP_SEND_SENT, ""},          {409, JP_SEND_NOBODY, "nobody"},
                 {400, JP_SEND_EMPTY, "empty"},     {413, JP_SEND_FAILED, "too-long"},
                 {500, JP_SEND_FAILED, "http-500"}, {-1, JP_SEND_FAILED, "connect"}};
    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        reset(BOX_NEW);
        g_send_status[0] = cases[i].status;
        g_nsend_status = 1;
        if (cases[i].status < 0) {
            add_override((override_t){.route = "/jpanel/send", .status = -1, .times = 1, .cut_after = -1,
                                      .hang = true});
        }
        g_lease_ok = true;
        g_yield = lease_free_at_outcome;
        g_send_seq = post_send(32000, JP_TO_PANEL);
        drive_done(g_send_seq, 120000);
        const jp_view_t v = snap();
        char m[96];
        snprintf(m, sizeof(m), "HTTP %d maps to its outcome", cases[i].status);
        CHECK(v.send.seq == g_send_seq && v.send.kind == cases[i].kind, m);
        if (cases[i].status >= 0) {
            snprintf(m, sizeof(m), "HTTP %d names its reason", cases[i].status);
            CHECK(strcmp(v.send.reason, cases[i].why) == 0, m);
        }
        CHECK(g_lease_ok && !stream_leased(&g_st), "lease 1 is free before the outcome is visible");
        CHECK(all_ended_once(), "one end");
        CHECK(last_req("/jpanel/send?to=panel") != NULL, "addressed");
        CHECK(!last_req("/jpanel/send")->cancellable, "a SEND is never cancellable");
        deadlines_held("send");
    }
}

static void test_422_retry_restarts_first_frame_bound(void)
{
    reset(BOX_NEW);
    g_pre_wait_ms = 3000;
    g_send_status[0] = 422;
    g_send_status[1] = 200;
    g_nsend_status = 2;
    const uint32_t seq = post_send(64000, JP_TO_DAD);
    drive_done(seq, 120000);
    CHECK(snap().send.kind == JP_SEND_SENT, "a 422 is retried once and then sent");
    CHECK(count_req("/jpanel/send", 0) == 2, "two uploads");
    const req_log_t *second = last_req("/jpanel/send");
    CHECK(second->deadline == second->at + 8000 + 20000,
          "the retry's first-frame bound restarts at its own start");
    CHECK(second->mode == LINK_DEADLINE_PROGRESS, "in PROGRESS mode");

    reset(BOX_NEW);
    g_send_status[0] = 422;
    g_send_status[1] = 422;
    g_nsend_status = 2;
    const uint32_t s2 = post_send(64000, JP_TO_DAD);
    drive_done(s2, 120000);
    CHECK(snap().send.kind == JP_SEND_FAILED && strcmp(snap().send.reason, "corrupt") == 0,
          "twice is not a bad packet");
    CHECK(count_req("/jpanel/send", 0) == 2, "and is not tried a third time");
}

static void stop_mid_upload(void)
{
    if (!g_stopped && snap().phase == JP_SENDING && count_req("/jpanel/send", 0) == 1) {
        g_stopped = true;
        stream_stop(&g_st, false);
    }
}

static void test_send_is_exempt_from_stop(void)
{
    reset(BOX_NEW);
    g_upload_bps = 20000;
    g_stopped = false;
    g_tick = stop_mid_upload;
    const uint32_t seq = post_send(100000, JP_TO_PANEL);
    drive_done(seq, 120000);
    CHECK(g_stopped, "a stop arrived mid-upload");
    CHECK(snap().send.kind == JP_SEND_SENT, "and the send went anyway (E1.2)");
}

static void test_35s_send_on_slow_link_survives_transcription(void)
{
    reset(BOX_NEW);
    g_pre_wait_ms = 8000;  /* the socket wait */
    g_upload_bps = 10000;  /* 10 KB/s */
    g_transcribe_ms = 60000;
    const size_t bytes = (size_t)35 * 32000; /* 35 s at 16 kHz 16-bit */
    const uint32_t seq = post_send(bytes, JP_TO_DAD);
    drive_done(seq, 400000);
    CHECK(snap().send.seq == seq && g_sent_count == 1, "exactly one SENT");
    CHECK(!stream_leased(&g_st), "lease released");
    deadlines_held("35 s send");
}

static void test_engine_meets_published_deadlines(void)
{
    reset(BOX_NEW);
    g_pre_wait_ms = 8000;   /* the socket wait, before every request */
    g_body_gap_ms = 3000;   /* a slow body: 3 s per 4 KB */
    g_upload_bps = 10000;
    g_transcribe_ms = 60000;
    add_msg("dl1", "Tamsin", false, 2000);
    poll_now();
    const uint32_t p = post_play("dl1", 0);
    drive_done(p, 120000);
    CHECK(snap().play.kind == JP_PLAY_HEARD, "a slow body still plays");
    const uint32_t s = post_send(320000, JP_TO_DAD);
    drive_done(s, 400000);
    CHECK(snap().send.kind == JP_SEND_SENT, "a slow upload with a long transcription is sent");
    /* `ws_send` blocking 20 s, and the HTTPS lock: waits past the request's own budget fail by
       the deadline, and the backstop still never fires. */
    g_pre_wait_ms = 25000;
    add_msg("dl2", "Tamsin", false, 1000);
    const uint32_t p2 = post_play("", 0);
    drive_done(p2, 120000);
    CHECK(snap().play.seq == p2 && snap().play.kind == JP_PLAY_FAILED &&
              strcmp(snap().play.reason, "timeout") == 0,
          "a wait past the deadline fails at the deadline");
    deadlines_held("published deadlines");
}

static void pause_for_60s(void)
{
    static uint32_t paused_at;
    const stream_facts_t f = stream_facts(&g_st);
    if (!g_stopped && f.stream_id != 0 && f.out_ms >= 1000) {
        g_stopped = true;
        paused_at = g_now;
        stream_pause_req(&g_st, f.stream_id, true);
    } else if (g_stopped && paused_at != 0 && jp_due(g_now, paused_at + 60000)) {
        paused_at = 0;
        stream_pause_req(&g_st, f.stream_id, false);
    }
}

static void test_60s_pause_does_not_trip_watchdog(void)
{
    reset(BOX_NEW);
    add_msg("pa1", "Tamsin", false, 3000);
    poll_now();
    g_stopped = false;
    g_tick = pause_for_60s;
    const uint32_t seq = post_play("pa1", 0);
    drive_done(seq, 120000);
    CHECK(g_stopped && snap().play.kind == JP_PLAY_HEARD, "paused for a minute, then heard");
    deadlines_held("60 s pause");
}

static void test_idle_engine_bumps_hb_every_pass(void)
{
    reset(BOX_NEW);
    uint32_t hb = snap().hb;
    bool every = true;
    for (int i = 0; i < 20; i++) {
        jpanel_core_step();
        const uint32_t now = snap().hb;
        every &= now != hb;
        hb = now;
    }
    CHECK(every, "every idle pass is a published heartbeat");
    CHECK(snap().phase == JP_IDLE && snap().deadline_ms == 0, "IDLE publishes no deadline");
    drive(120000);
    deadlines_held("idle");
}

static bool g_torn_ok;
static void read_inside_publish(const char *point)
{
    if (strcmp(point, "publishing") != 0) return;
    jp_view_t v;
    memset(&v, 0x5a, sizeof(v));
    const bool got = jpanel_core_snapshot(&v);
    const unsigned char *b = (const unsigned char *)&v;
    bool untouched = true;
    for (size_t i = 0; i < sizeof(v); i++) untouched &= b[i] == 0x5a;
    if (got || !untouched) g_torn_ok = false;
}

/* The storm's writer: polls as fast as it can while a reader copies. Every poll renames every
   entry and the sibling to the same "g<n>", so a torn copy shows two different names. */
static atomic_bool g_storm_stop;
static void *storm_reader(void *arg)
{
    int *bad = arg;
    jp_view_t v;
    memset(&v, 0, sizeof(v));
    while (!atomic_load(&g_storm_stop)) {
        if (!jpanel_core_snapshot(&v)) continue;
        if (v.waiting.n > v.waiting.count) (*bad)++;
        for (int i = 0; i < v.waiting.n; i++) {
            if (strcmp(v.waiting.q[i].from, v.waiting.sibling) != 0) (*bad)++;
        }
    }
    return NULL;
}

static void test_snapshot_is_never_torn(void)
{
    reset(BOX_NEW);
    add_msg("sn1", "Tamsin", false, 1000);
    g_torn_ok = true;
    g_yield = read_inside_publish;
    poll_now();
    jpanel_core_step();
    g_yield = NULL;
    jp_stats_t st;
    jpanel_core_stats(&st);
    CHECK(g_torn_ok, "a read inside the write window fails and leaves the caller's copy alone");
    CHECK(st.snapshot_retries > 0, "counted");

    reset(BOX_NEW);
    for (int i = 0; i < 8; i++) {
        char id[8];
        snprintf(id, sizeof(id), "st%d", i);
        add_msg(id, "x", false, 1000)->first_from[0] = '#';
    }
    g_latency_ms = 0;
    int bad = 0;
    /* Render is the snapshot's only reader; the harness's own bookkeeping reads it from the
       engine's thread, so it stands down while a real reader runs. */
    g_quiet = true;
    atomic_store(&g_storm_stop, false);
    pthread_t th;
    pthread_create(&th, NULL, storm_reader, &bad);
    for (int i = 0; i < 20000; i++) {
        jpanel_core_poll_soon();
        jpanel_core_step();
    }
    atomic_store(&g_storm_stop, true);
    pthread_join(th, NULL);
    g_quiet = false;
    CHECK(bad == 0, "a concurrent reader never sees a torn snapshot (A7)");
}

static void test_send_outcome_survives_concurrent_chain(void)
{
    reset(BOX_NEW);
    add_msg("cc1", "Tamsin", false, 1000);
    add_msg("cc2", "Dad", true, 1000);
    poll_now();
    const uint32_t s = post_send(32000, JP_TO_DAD);
    const uint32_t p = post_play("cc1", 0);
    drive_done(p, 60000);
    const jp_view_t v = snap();
    CHECK(v.send.seq == s && v.send.kind == JP_SEND_SENT, "the send's outcome is still there");
    CHECK(v.play.seq == p && v.play.kind == JP_PLAY_HEARD && g_msgs[1].played,
          "beside the run's own (A8)");
    bool lost = false;
    for (int i = 0; i < g_npubs; i++) {
        if (jp_seq_ge(g_pubs[i].done_seq, s) && g_pubs[i].send.seq != s) lost = true;
    }
    CHECK(!lost, "no publish after the send ended lost its outcome");
}

static void arrive_mid_run(void)
{
    if (g_queued || stream_facts(&g_st).out_ms < 300) return;
    g_queued = true;
    add_msg("mr3", "Dad", true, 1000);
    jpanel_core_poll_soon();
}

static void test_message_arriving_mid_run_joins_it(void)
{
    reset(BOX_NEW);
    add_msg("mr1", "Tamsin", false, 1000);
    add_msg("mr2", "Tamsin", false, 1000);
    poll_now();
    g_queued = false;
    g_tick = arrive_mid_run;
    const uint32_t seq = post_play("mr1", 0);
    drive_done(seq, 60000);
    CHECK(g_msgs[0].played && g_msgs[1].played && g_msgs[2].played, "the arrival joined the run");
    CHECK(ends_of(seq) == 1, "one end");

    /* An arrival during the run's LAST message does not join: it is announced instead. */
    reset(BOX_NEW);
    add_msg("mr1", "Tamsin", false, 1000);
    poll_now();
    g_queued = false;
    g_tick = arrive_mid_run;
    const uint32_t s2 = post_play("mr1", 0);
    drive_done(s2, 60000);
    CHECK(g_msgs[0].played && !g_msgs[1].played && g_msgs[1].fetched == 0,
          "an arrival in the last message waits for its own press");
    CHECK(snap().waiting.count == 1, "and is listed for the notice");
}

static void stop_rr2_in_preroll(void)
{
    const stream_facts_t f = stream_facts(&g_st);
    if (!g_stopped && strcmp(snap().cur.who.id, "rr2") == 0 && f.out_ms >= 300 && f.stream_live &&
        f.stream_id == snap().cur.stream_id) {
        g_stopped = true;
        stream_stop(&g_st, false);
    }
}

static void test_run_never_replays_a_returned_message(void)
{
    reset(BOX_NEW);
    add_msg("rr1", "Tamsin", false, 1000);
    add_msg("rr2", "Dad", true, 5000);
    add_msg("rr3", "Tamsin", false, 1000);
    poll_now();
    g_stopped = false;
    g_tick = stop_rr2_in_preroll;
    const uint32_t seq = post_play("rr1", 0);
    drive_done(seq, 60000);
    g_tick = NULL;
    CHECK(g_msgs[0].played, "rr1 heard");
    CHECK(snap().play.kind == JP_PLAY_RETURNED && strcmp(snap().play.who.id, "rr2") == 0,
          "rr2 handed back");
    CHECK(g_msgs[1].fetched == 1 && g_msgs[2].fetched == 0, "the run did not go on, or back");
    CHECK(snap().waiting.count == 2 && strcmp(snap().waiting.q[0].id, "rr2") == 0,
          "rr2 is first again");
    const uint32_t s2 = post_play("rr2", 0);
    drive_done(s2, 60000);
    CHECK(g_msgs[1].played && g_msgs[2].played && g_msgs[1].fetched == 2,
          "a fresh press plays it and the run carries on");
}

static void test_replay_never_continues_a_run(void)
{
    reset(BOX_NEW);
    add_msg("rp1", "Tamsin", false, 1000);
    poll_now();
    const uint32_t seq = post_play("rp1", 0);
    drive_done(seq, 20000);
    add_msg("rp2", "Dad", true, 1000);
    poll_now();
    const int nexts = count_req("/jpanel/next", 0);
    const int acks = count_req("/jpanel/played", 0);
    const jp_who_t again = snap().last_begun;
    const uint32_t r = post_replay(&again);
    drive_done(r, 20000);
    const jp_view_t v = snap();
    CHECK(v.play.seq == r && v.play.replay && v.play.kind == JP_PLAY_HEARD, "a replay is heard");
    CHECK(strcmp(v.play.who.from, "Tamsin") == 0, "with the sender REPLAY carried");
    CHECK(last_req("/jpanel/message/rp1/pcm") != NULL, "from its own route");
    CHECK(count_req("/jpanel/next", 0) == nexts && g_msgs[1].fetched == 0, "it never walks on");
    CHECK(count_req("/jpanel/played", 0) == acks, "and is never acknowledged");
    CHECK(!v.cur.run_continues && v.cur.pos == -1, "a replay's cur has no place in the list");

    add_override((override_t){.route = "/jpanel/message/", .status = 404, .times = 1, .cut_after = -1});
    const uint32_t r2 = post_replay(&again);
    drive_done(r2, 20000);
    CHECK(snap().play.kind == JP_PLAY_FAILED && snap().play.replay, "a replay that fails says so");
    arm_stop(200, false);
    const uint32_t r3 = post_replay(&again);
    drive_done(r3, 20000);
    CHECK(snap().play.kind == JP_PLAY_CANCELLED && snap().play.replay, "a stopped replay is cancelled");
}

static void test_last_begun_only_at_first_byte(void)
{
    reset(BOX_NEW);
    add_msg("lb1", "Tamsin", false, 1000);
    poll_now();
    const uint32_t a = post_play("lb1", 0);
    drive_done(a, 20000);
    add_msg("lb2", "Dad", true, 1000);
    poll_now();
    CHECK(strcmp(snap().last_begun.id, "lb1") == 0, "set at a stream's first byte");
    add_override((override_t){.route = "/jpanel/next", .status = 500, .times = 1, .cut_after = -1});
    const uint32_t b = post_play("lb2", 0);
    drive_done(b, 20000);
    CHECK(snap().play.kind == JP_PLAY_FAILED, "the second failed before a byte");
    CHECK(strcmp(snap().last_begun.id, "lb1") == 0, "so AGAIN still targets the first");
}

static jp_view_t g_at_claim, g_at_byte;
static bool g_byte_seen;
static void watch_seed(const char *point)
{
    if (strcmp(point, "claimed") == 0) g_at_claim = snap();
    if (strcmp(point, "chunk") == 0 && !g_byte_seen) {
        g_byte_seen = true;
        g_at_byte = snap();
    }
}

static void test_cur_seeded_before_request(void)
{
    reset(BOX_NEW);
    add_msg("cs1", "Tamsin", false, 1000);
    add_msg("cs2", "Dad", true, 1000);
    poll_now();
    g_byte_seen = false;
    g_yield = watch_seed;
    const uint32_t seq = post_play("cs2", 1);
    drive_done(seq, 20000);
    CHECK(g_at_claim.claimed_seq == seq && g_at_claim.phase == JP_FETCHING, "claimed before the request");
    CHECK(g_at_claim.cur.who.from_dad && strcmp(g_at_claim.cur.who.from, "Dad") == 0,
          "cur is seeded from the waiting entry");
    CHECK(g_at_claim.cur.pos == 1 && g_at_claim.cur.in_waiting && g_at_claim.waiting.count == 2,
          "at its position, still listed until the stream begins");
    CHECK(g_at_byte.phase == JP_SOUNDING && !g_at_byte.cur.in_waiting && g_at_byte.waiting.count == 1 &&
              strcmp(g_at_byte.waiting.q[0].id, "cs1") == 0,
          "at the first byte it leaves the list");
    CHECK(g_at_byte.cur.stream_id != 0, "with its stream id");

    reset(BOX_TODAY);
    add_msg("cs3", "Tamsin", false, 1000);
    add_msg("cs4", "Dad", true, 1000);
    poll_now();
    g_byte_seen = false;
    g_yield = watch_seed;
    const uint32_t s2 = post_play("", 1);
    drive_done(s2, 20000);
    CHECK(g_at_claim.cur.pos == 1 && g_at_claim.cur.in_waiting && g_at_claim.cur.who.from_dad,
          "without ids, seeded by position");
    CHECK(g_at_byte.waiting.count == 1 && !g_at_byte.cur.in_waiting, "and leaves by position");
    CHECK(last_req("/jpanel/next?at=1") != NULL, "fetched by position");
}

static void test_no_poll_while_speaker_busy(void)
{
    reset(BOX_NEW);
    fake_stream_clip_busy = true;
    jpanel_core_poll_soon();
    drive(5000);
    CHECK(count_req("/jpanel/waiting", 0) == 0, "no poll while a cue or reply sounds");
    fake_stream_clip_busy = false;
    drive(1000);
    CHECK(count_req("/jpanel/waiting", 0) == 1, "and it happens once the speaker is free");
}

static uint32_t g_poll_play_seq;
static void post_during_poll(void)
{
    if (g_poll_play_seq == 0 && snap().phase == JP_POLLING) g_poll_play_seq = post_play("qp1", 0);
}

static void test_queued_command_cancels_poll(void)
{
    reset(BOX_NEW);
    add_msg("qp1", "Tamsin", false, 1000);
    poll_now();
    add_override((override_t){.route = "/jpanel/waiting", .status = 200, .times = 1, .cut_after = -1,
                              .hang = true});
    g_poll_play_seq = 0;
    g_tick = post_during_poll;
    jpanel_core_poll_soon();
    const uint32_t t0 = g_now;
    while (!(g_poll_play_seq != 0 && done(g_poll_play_seq)) && !jp_due(g_now, t0 + 30000)) {
        jpanel_core_step();
    }
    g_tick = NULL;
    CHECK(g_poll_play_seq != 0 && done(g_poll_play_seq) && snap().play.kind == JP_PLAY_HEARD,
          "the PLAY posted mid-poll was served");
    const req_log_t *hung = NULL;
    for (int i = 0; i < g_nlog && hung == NULL; i++) {
        if (strcmp(g_log[i].path, "/jpanel/waiting") == 0 && g_log[i].stopped) hung = &g_log[i];
    }
    CHECK(hung != NULL, "the queued command cancelled the poll");
    CHECK((int32_t)(g_now - t0) < 10000, "promptly, not at the poll's deadline");
    const int polls = count_req("/jpanel/waiting", 0);
    drive(2000);
    CHECK(count_req("/jpanel/waiting", 0) > polls, "and the poll ran again afterwards");
}

/* A command that cancels a poll after its 200 head has cut the body short itself: that is a
   yield, not the box sending bad JSON, and the poll is asked for again. */
static void test_poll_cancelled_mid_body_is_a_yield(void)
{
    reset(BOX_NEW);
    add_msg("qp1", "Tamsin", false, 1000);
    poll_now();
    add_override((override_t){.route = "/jpanel/waiting", .status = 200, .times = 1, .cut_after = 10});
    /* No head latency, so the command lands while the body stalls, not before the head. */
    g_latency_ms = 0;
    g_poll_play_seq = 0;
    g_tick = post_during_poll;
    jpanel_core_poll_soon();
    const uint32_t t0 = g_now;
    while (!(g_poll_play_seq != 0 && done(g_poll_play_seq)) && !jp_due(g_now, t0 + 30000)) {
        jpanel_core_step();
    }
    g_tick = NULL;
    CHECK(g_poll_play_seq != 0 && done(g_poll_play_seq), "mid-body: the PLAY was served");
    bool cut_mid_body = false;
    for (int i = 0; i < g_nlog; i++) {
        if (strcmp(g_log[i].path, "/jpanel/waiting") == 0 && g_log[i].status == 200 &&
            g_log[i].stopped) {
            cut_mid_body = true;
        }
    }
    CHECK(cut_mid_body, "mid-body: the command cancelled the poll after its 200 head");
    int fails = 0;
    const char *err = "";
    uint32_t ago = 0;
    reach_faults(REACH_POLL, &fails, &err, &ago, g_now);
    CHECK(fails == 0, "mid-body: a cancelled poll body is not a reach failure");
    const int polls = count_req("/jpanel/waiting", 0);
    drive(2000);
    CHECK(count_req("/jpanel/waiting", 0) > polls, "mid-body: and the poll is asked for again");
}

/* `msg_err` names the last failure, not whatever reason the outcome after it carried. */
static void test_msg_err_survives_the_next_outcome(void)
{
    reset(BOX_NEW);
    add_msg("e1", "Tamsin", false, 2000);
    add_msg("e2", "Dad", true, 1000);
    add_override((override_t){.route = "/jpanel/next", .status = 200, .times = 1, .cut_after = 20000});
    poll_now();
    uint32_t seq = post_play("e1", 0);
    drive_done(seq, 30000);
    CHECK(snap().play.kind == JP_PLAY_SHORT && strcmp(jpanel_core_err(), "short") == 0,
          "msg_err: a cut message is noted short");
    poll_now();
    seq = post_play("e1", 0);
    drive_done(seq, 30000);
    CHECK(snap().play.kind == JP_PLAY_HEARD && snap().play.reason[0] == '\0',
          "msg_err: the next outcome carries no reason");
    CHECK(strcmp(jpanel_core_err(), "short") == 0, "msg_err: and the failure's name survives it");
}

static void test_no_audio_fails_at_once(void)
{
    reset(BOX_NEW);
    add_msg("na1", "Tamsin", false, 1000);
    poll_now();
    stream_set_arbiter(&g_st, false);
    const uint32_t seq = post_play("na1", 0);
    const uint32_t t0 = g_now;
    while (!done(seq) && !jp_due(g_now, t0 + 20000)) jpanel_core_step();
    CHECK(snap().play.kind == JP_PLAY_FAILED && strcmp(snap().play.reason, "no-audio") == 0,
          "no arbiter: failed at once");
    CHECK((int32_t)(g_now - t0) < 1000, "without waiting for a backstop");
}

static void test_deadlines_survive_u32_wrap(void)
{
    reset_at(BOX_NEW, 0xFFFFFFFFu - 4000u);
    add_msg("wr1", "Tamsin", false, 3000);
    add_msg("wr2", "Dad", true, 1000);
    poll_now();
    const uint32_t seq = post_play("wr1", 0);
    drive_done(seq, 30000);
    CHECK(g_msgs[0].played && g_msgs[1].played, "a run across the wrap");
    const uint32_t s = post_send(32000, JP_TO_DAD);
    drive_done(s, 30000);
    CHECK(snap().send.kind == JP_SEND_SENT, "a send across it");
    deadlines_held("u32 wrap");
}

/* Every command ends exactly once, whatever the box, the link and the child do. */
static uint32_t g_rng = 12345;
static uint32_t rnd(uint32_t n)
{
    g_rng = g_rng * 1103515245u + 12345u;
    return (g_rng >> 8) % n;
}

static void chaos_tick(void)
{
    if (rnd(400) == 0) stream_stop(&g_st, rnd(2) == 0);
    if (rnd(600) == 0) fake_stream_clip_busy = !fake_stream_clip_busy;
}

static void test_exactly_one_outcome_per_command(void)
{
    for (int round = 0; round < 6; round++) {
        reset(round % 2 ? BOX_NEW : BOX_TODAY);
        for (int i = 0; i < 6; i++) {
            char id[8];
            snprintf(id, sizeof(id), "x%d%d", round, i);
            add_msg(id, i % 2 ? "Dad" : "Tamsin", i % 2, 500 + rnd(3000));
        }
        add_override((override_t){.route = "/jpanel/next", .status = 500, .times = 2, .skip = 3,
                                  .cut_after = -1});
        add_override((override_t){.route = "/jpanel/played", .status = -1, .times = 2, .skip = 1,
                                  .cut_after = -1});
        add_override((override_t){.route = "/jpanel/next", .status = 200, .times = 1, .skip = 6,
                                  .cut_after = 9000});
        g_tick = chaos_tick;
        poll_now();
        for (int k = 0; k < 40; k++) {
            const jp_view_t v = snap();
            const uint32_t r = rnd(10);
            if (g_qn < 4) {
                if (r < 5 && v.waiting.count > 0) {
                    const int at = (int)rnd((uint32_t)(v.waiting.n > 0 ? v.waiting.n : 1));
                    (void)post_play(v.waiting.has_ids ? v.waiting.q[at].id : "", at);
                } else if (r < 7 && v.last_begun.id[0]) {
                    (void)post_replay(&v.last_begun);
                } else if (r < 8 && !stream_leased(&g_st)) {
                    (void)post_send(3200 * (1 + rnd(20)), rnd(2) ? JP_TO_DAD : JP_TO_PANEL);
                } else if (r < 9) {
                    jpanel_core_poll_soon();
                }
            }
            drive(rnd(6000));
        }
        g_tick = NULL;
        fake_stream_clip_busy = false;
        drive(120000);
        char m[64];
        if (getenv("JP_DEBUG")) {
            int kinds[16] = {0};
            for (int i = 0; i < g_nouts; i++) kinds[g_outs[i].kind]++;
            fprintf(stderr, "round %d: posted %d, outcomes", round, g_nposted);
            for (int i = 0; i < 9; i++) fprintf(stderr, " %d", kinds[i]);
            fprintf(stderr, ", requests %d\n", g_nlog);
        }
        snprintf(m, sizeof(m), "round %d: every command ended exactly once", round);
        CHECK(g_nposted >= 10, "the chaos actually posted");
        CHECK(all_ended_once(), m);
        CHECK(snap().phase == JP_IDLE && !stream_leased(&g_st), "and the engine is idle and unleased");
        for (int i = 0; i < g_nmsgs; i++) {
            CHECK(!g_msgs[i].played || g_msgs[i].fetched >= 1, "nothing acked that was not fetched");
        }
        deadlines_held(m);
    }
}

#ifndef JP_E2E
int main(void)
{
    test_sha_known_answer();
    test_new_box_play_heard_acks_with_heard_ms();
    test_old_box_positional_fetch_still_acks_header_id();
    test_heard_unacked_only_without_header_id();
    test_against_0_3_39_box_short_stop_acks();
    test_stop_in_preroll_returns_not_acks();
    test_stop_after_threshold_acks_with_heard_ms();
    test_reply_stop_counts_heard();
    test_short_message_threshold_is_80_percent();
    test_returned_404_counts_no_refund();
    test_stop_during_5s_head_delay_cancels_request();
    test_stop_with_no_stream_then_play_starts();
    test_stop_at_every_interleave_point();
    test_run_gap_stop_never_acks_unsounded();
    test_next_queued_during_drain_classifies_previous_first();
    test_chain_fetch_failure_returns_to_idle();
    test_run_204_skips_to_next();
    test_pressed_204_is_empty();
    test_short_stream_ends_run_without_refetch();
    test_lost_ack_is_retried_and_never_replays();
    test_play_while_ack_owed_retries_once_then_fails();
    test_ack_4xx_is_terminal();
    test_ack_gives_up_after_10_min();
    test_play_expiry_exceeds_ack_and_https_poll();
    test_claim_near_expiry_never_trips_unclaimed_backstop();
    test_expired_and_stale_commands_end_at_once();
    test_send_outcomes();
    test_422_retry_restarts_first_frame_bound();
    test_send_is_exempt_from_stop();
    test_35s_send_on_slow_link_survives_transcription();
    test_engine_meets_published_deadlines();
    test_60s_pause_does_not_trip_watchdog();
    test_idle_engine_bumps_hb_every_pass();
    test_snapshot_is_never_torn();
    test_send_outcome_survives_concurrent_chain();
    test_message_arriving_mid_run_joins_it();
    test_run_never_replays_a_returned_message();
    test_replay_never_continues_a_run();
    test_last_begun_only_at_first_byte();
    test_cur_seeded_before_request();
    test_no_poll_while_speaker_busy();
    test_queued_command_cancels_poll();
    test_poll_cancelled_mid_body_is_a_yield();
    test_msg_err_survives_the_next_outcome();
    test_no_audio_fails_at_once();
    test_deadlines_survive_u32_wrap();
    test_exactly_one_outcome_per_command();
    if (failures > 0) {
        fprintf(stderr, "jpanel: %d of %d checks FAILED\n", failures, checks);
        return 1;
    }
    printf("jpanel: %d checks OK\n", checks);
    return 0;
}
#endif
