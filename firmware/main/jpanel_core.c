/* The voice-post engine (plan §7.3). See jpanel_core.h for the contract.
 *
 * ONE SERIAL LOOP, ONE SNAPSHOT. Every request — poll, fetch, acknowledgement, send — runs here, one
 * at a time, and everything render may know is published through one seqlocked `jp_view_t`. The
 * 0.3.39 bugs (A1, A2, A4, A7, A8) were all two writers sharing a flag; here each word has one.
 *
 * THE STOP IS THE GEN, NOT A FLAG. Render bumps the stop word; the engine never clears or resets
 * anything a stop wrote, it only compares gens. So a stop during the fetch window cannot be undone
 * by the head arriving later (A1): the head callback, the sink and the classification all see the
 * stale gen. */

#include "jpanel_core.h"

#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include "reach.h"

#ifdef ESP_PLATFORM
#include "esp_attr.h"
#endif
#ifndef EXT_RAM_BSS_ATTR
#define EXT_RAM_BSS_ATTR
#endif

/* A poll, a fetch's head: a request still running after this is broken, not slow. */
#define JP_HTTP_MS 20000u
/* An acknowledgement is a tiny POST; ten seconds is generous and keeps the PLAY expiry (35 s)
   above an HTTPS poll plus an ack (§7.1). */
#define JP_ACK_MS 10000u
#define JP_POLL_EVERY_MS 30000u
/* `link.c`'s LINK_STALL_MS and LINK_UP_WAIT_MS: the engine publishes deadlines the link
   enforces, so they must be the same numbers. */
#define JP_LINK_STALL_MS 20000u
#define JP_LINK_UP_WAIT_MS 8000u
/* W1 bounds the box's transcription at 60 s inside `_transcribe`; 15 s of slop for the rest. */
#define JP_SEND_HEAD_MS (60000u + 15000u)
#define JP_SOUND_SLOP_MS 2000u
/* Owner question 1 (§13): a stop at or past this counts as heard. */
#define JP_HEARD_MIN_MS 2000u
#define JP_ACK_GIVE_UP_MS 600000u
#define JP_ACK_BACKOFF_FIRST_MS 1000u
#define JP_ACK_BACKOFF_MAX_MS 30000u
/* The idle heartbeat: today's bounded queue wait. */
#define JP_IDLE_WAIT_MS 250u
#define JP_DRAIN_STEP_MS 20u
/* A press plays CUE_PLAY first and the fetch crosses a task boundary, so the ring can still be
   draining a cue's predecessor when the head lands. Bounded: "busy" also means a message is
   already playing, and blocking forever on that would stop the acks with it. */
#define STREAM_WAIT_MAX_MS 600u
#define STREAM_WAIT_STEP_MS 20u
/* One retry, for the one failure a retry can fix: 422, the box hashed something else. */
#define SEND_ATTEMPTS 2
#define JP_RUN_MAX 16
#define JP_TRIED_MAX 32

static jpanel_io_t s_io;
static stream_t *s_st;

/* The seqlock (§7.2). `s_v` is the engine's working copy; `s_pub` is what render copies. */
static _Atomic uint32_t s_lock;
EXT_RAM_BSS_ATTR static jp_view_t s_pub;
EXT_RAM_BSS_ATTR static jp_view_t s_v;
/* Render's scratch: a torn read must leave the caller's previous copy untouched. */
EXT_RAM_BSS_ATTR static jp_view_t s_read;
static _Atomic uint32_t s_snapshot_retries;
static atomic_bool s_poll_flag;
static uint32_t s_next_poll;
static jp_stats_t s_stats;
static const char *volatile s_err = "";

/* The claimed PLAY or REPLAY, which a run keeps claimed across messages. */
static struct {
    bool active;
    jp_cmd_t cmd;
    bool first; /* the pressed message is the one in flight */
    bool by_id; /* the box sent ids at the claim: the run walks ids, not positions */
} s_job;

EXT_RAM_BSS_ATTR static char s_run[JP_RUN_MAX][JP_ID_MAX];
static int s_run_n;
EXT_RAM_BSS_ATTR static char s_tried[JP_TRIED_MAX][JP_ID_MAX];
static int s_tried_n;

/* One fetch, from the request to its classification. */
static struct {
    bool open;       /* stream_begin succeeded and it is not classified yet */
    bool replay;
    bool first_byte;
    bool body_done;
    bool cut;
    bool timed_out;
    bool verified;
    uint32_t gen;
    uint8_t stream_id;
    int status;
    const char *err;
    const char *fail; /* why the stream could not begin */
    uint32_t total;
    uint8_t odd;
    bool have_odd;
    char req_id[JP_ID_MAX];
    char id[JP_ID_MAX]; /* X-Jpanel-Id: what the box says it served */
    char from[JP_NAME_MAX];
    bool from_set, kind_set, from_dad;
    char sha[72];
    bool delivery_set;
    uint32_t delivery;
    uint32_t duration_ms;
    uint32_t last_out_ms, last_arb_hb;
} s_f;

/* Aligned for the ring: the sink hands it over as samples. */
EXT_RAM_BSS_ATTR static int16_t s_chunk[2048];

typedef enum { ACK_NONE = 0, ACK_PLAYED, ACK_RETURNED } ack_kind_t;

/* The one acknowledgement slot (§7.3 step 2). RAM only: after a reboot the box may offer the
   message again, bounded by its cap (E2.5). */
static struct {
    ack_kind_t kind;
    char id[JP_ID_MAX];
    uint32_t heard_ms;
    uint32_t delivery;
    uint32_t first_at, next_at, backoff;
} s_ack;

static uint32_t now_ms(void) { return s_io.now_ms(s_io.ctx); }

static void yield(const char *point)
{
    if (s_io.yield != NULL) s_io.yield(s_io.ctx, point);
}

static void copy(char *dst, size_t cap, const char *src)
{
    snprintf(dst, cap, "%s", src != NULL ? src : "");
}

static void publish(void)
{
    const uint32_t s = atomic_load_explicit(&s_lock, memory_order_relaxed);
    atomic_store_explicit(&s_lock, s + 1, memory_order_relaxed);
    atomic_thread_fence(memory_order_release);
    yield("publishing");
    memcpy(&s_pub, &s_v, sizeof(s_pub));
    atomic_store_explicit(&s_lock, s + 2, memory_order_release);
    yield("published");
}

static void beat(void) { s_v.hb++; }

static void fail_note(const char *why)
{
    s_err = why;
    s_stats.msg_bad++;
}

/* An id rides a URL and a JSON body; the box only ever sends uuids, so anything else is treated
   as no id rather than escaped. */
static bool id_ok(const char *id)
{
    if (id[0] == '\0') return false;
    for (const char *p = id; *p; p++) {
        const char c = *p;
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              c == '-')) {
            return false;
        }
    }
    return true;
}

/* `/api/jpanel/…`, NOT `/api/endpoint/jpanel/…`: a 404 from `/waiting` reads as an empty house,
 * which is how the wrong prefix once shipped. Pinned from the box by
 * `test_the_panel_facing_routes_are_where_the_firmware_looks`. CHECKED, because snprintf
 * truncates silently and a truncated path is that same quiet 404. */
static bool jpath(char *out, size_t cap, const char *path)
{
    const int n = snprintf(out, cap, "/jpanel%s", path);
    return n > 0 && n < (int)cap;
}

/* ── the published waiting list ───────────────────────────────────────────────────────────── */

static int waiting_find(const char *id)
{
    const jp_waiting_t *w = &s_v.waiting;
    if (!w->has_ids || id[0] == '\0') return -1;
    for (int i = 0; i < w->n; i++) {
        if (strcmp(w->q[i].id, id) == 0) return i;
    }
    return -1;
}

static void waiting_normalise(void)
{
    jp_waiting_t *w = &s_v.waiting;
    if (w->count < 0) w->count = 0;
    if (w->n > w->count) w->n = w->count;
    if (w->n < 0) w->n = 0;
}

static void waiting_remove_at(int i)
{
    jp_waiting_t *w = &s_v.waiting;
    if (i < 0 || i >= w->n) return;
    memmove(&w->q[i], &w->q[i + 1], (size_t)(w->n - i - 1) * sizeof(w->q[0]));
    w->n--;
}

static void cur_in_waiting_refresh(void)
{
    if (s_v.waiting.has_ids && s_v.cur.who.id[0] != '\0') {
        s_v.cur.in_waiting = waiting_find(s_v.cur.who.id) >= 0;
    }
}

/* The stream's first byte: `cur` leaves the published list, as 0.3.39's `fetch_head` did, so the
   badge never counts the message that is playing. `cur.in_waiting` says which side of that edge
   the machine is looking at. Without ids the position is the only handle (today's rule). */
static void waiting_take_cur(void)
{
    jp_waiting_t *w = &s_v.waiting;
    if (!s_v.cur.in_waiting) return;
    const int i = w->has_ids ? waiting_find(s_v.cur.who.id) : s_v.cur.pos;
    if (w->count > 0) w->count--;
    waiting_remove_at(i);
    waiting_normalise();
    s_v.cur.in_waiting = false;
}

/* A RETURNED message goes back where it was, in the SAME write as its outcome, so the machine's
   selection never sees a frame where the stopped message is nowhere (§7.3). Safe before
   `/returned` answers: the box has not counted it played, so the next poll agrees. A full `q`
   drops its last described entry; the count still includes it. */
static void waiting_reinsert(const jp_who_t *who, int pos)
{
    jp_waiting_t *w = &s_v.waiting;
    if (waiting_find(who->id) >= 0) return;
    w->count++;
    if (pos < 0) pos = 0;
    if (pos >= JPANEL_QUEUE_MAX) return;
    if (pos > w->n) pos = w->n;
    if (w->n == JPANEL_QUEUE_MAX) w->n--;
    memmove(&w->q[pos + 1], &w->q[pos], (size_t)(w->n - pos) * sizeof(w->q[0]));
    w->q[pos] = *who;
    w->n++;
    w->has_ids = true;
}

/* A 204 means the box has no such message for us: the owner discarded it, the other twin's
   panel played it, or the position is past a queue that shrank. */
static void waiting_forget(const char *id, int at)
{
    jp_waiting_t *w = &s_v.waiting;
    if (w->has_ids) {
        const int i = waiting_find(id);
        if (i >= 0) {
            waiting_remove_at(i);
            if (w->count > 0) w->count--;
        }
    } else {
        (void)at;
        w->count = 0;
        w->n = 0;
    }
    waiting_normalise();
    cur_in_waiting_refresh();
}

/* ── the run ──────────────────────────────────────────────────────────────────────────────── */

static bool tried(const char *id)
{
    for (int i = 0; i < s_tried_n; i++) {
        if (strcmp(s_tried[i], id) == 0) return true;
    }
    return false;
}

static void run_add(const char *id)
{
    if (id[0] == '\0' || tried(id) || s_run_n >= JP_RUN_MAX) return;
    for (int i = 0; i < s_run_n; i++) {
        if (strcmp(s_run[i], id) == 0) return;
    }
    copy(s_run[s_run_n++], JP_ID_MAX, id);
}

static void run_mark_tried(const char *id)
{
    if (id[0] == '\0' || tried(id)) return;
    if (s_tried_n < JP_TRIED_MAX) copy(s_tried[s_tried_n++], JP_ID_MAX, id);
}

/* True while the run has somewhere to go: an id not yet attempted, or (by id) messages the box
   counts but has not described yet, which a poll between messages will name. */
static bool run_has_next(void)
{
    if (!s_job.by_id) return s_v.waiting.count > 0;
    for (int i = 0; i < s_run_n; i++) {
        if (!tried(s_run[i])) return true;
    }
    return s_v.waiting.count > s_v.waiting.n;
}

/* ── publishing an end ────────────────────────────────────────────────────────────────────── */

/* Every exit publishes `done_seq` and its outcome in ONE write, so the machine can never see a
   finished command without its outcome or the other way round. */
static void end_play(uint32_t seq, const jp_who_t *who, bool replay, jp_play_kind_t kind,
                     const char *reason)
{
    s_v.play.seq = seq;
    s_v.play.who = *who;
    s_v.play.kind = kind;
    s_v.play.replay = replay;
    copy(s_v.play.reason, sizeof(s_v.play.reason), reason);
    s_v.cur.run_continues = false;
    s_v.done_seq = seq;
    s_v.phase = JP_IDLE;
    s_v.deadline_ms = 0;
    s_job.active = false;
    if (kind == JP_PLAY_FAILED || kind == JP_PLAY_SHORT) fail_note(s_v.play.reason);
    publish();
}

/* A run whose next id is gone ends on the previous message's outcome, which is already
   published with this seq. */
static void end_run_on_previous(void)
{
    s_v.cur.run_continues = false;
    s_v.done_seq = s_job.cmd.seq;
    s_v.phase = JP_IDLE;
    s_v.deadline_ms = 0;
    s_job.active = false;
    publish();
}

static void end_send(uint32_t seq, jp_send_kind_t kind, const char *reason)
{
    /* Released BEFORE the outcome is published: render may open a capture the frame it sees the
       outcome, and the lease is what says the buffer is free (A5). */
    stream_lease_release(s_st, STREAM_LEASE_SEND);
    s_v.send.seq = seq;
    s_v.send.kind = kind;
    copy(s_v.send.reason, sizeof(s_v.send.reason), reason);
    s_v.done_seq = seq;
    s_v.phase = JP_IDLE;
    s_v.deadline_ms = 0;
    publish();
}

/* ── link callbacks ───────────────────────────────────────────────────────────────────────── */

static void on_progress(void *ctx, link_phase_t phase, uint32_t deadline_ms)
{
    (void)ctx;
    beat();
    if (phase != LINK_PHASE_TICK && s_v.phase == JP_SENDING) s_v.deadline_ms = deadline_ms;
    publish();
}

/* A STOP abandons the fetch at the link's next step: nothing about a stopped message is worth
   another byte. Sends and acks pass no `cancelled`. */
static bool fetch_cancelled(void *ctx)
{
    (void)ctx;
    return stream_stale(s_st, s_f.gen);
}

/* A queued command outranks a poll, which is only ever "how is the house". */
static bool poll_cancelled(void *ctx)
{
    (void)ctx;
    return !s_io.queue_empty(s_io.ctx);
}

/* Case-insensitive: the socket hands headers over lower-cased, the HTTPS path as spelled. */
static void on_header(void *ctx, const char *key, const char *value)
{
    (void)ctx;
    if (strcasecmp(key, "X-Jpanel-Id") == 0) {
        copy(s_f.id, sizeof(s_f.id), value);
        if (!id_ok(s_f.id)) s_f.id[0] = '\0';
    } else if (strcasecmp(key, "X-Jpanel-From") == 0) {
        copy(s_f.from, sizeof(s_f.from), value);
        s_f.from_set = true;
    } else if (strcasecmp(key, "X-Jpanel-From-Kind") == 0) {
        /* The kind, not the name: the name is the owner's to change (migration 0211). */
        s_f.from_dad = strcasecmp(value, "owner") == 0;
        s_f.kind_set = true;
    } else if (strcasecmp(key, "X-Jpanel-Sha256") == 0) {
        copy(s_f.sha, sizeof(s_f.sha), value);
    } else if (strcasecmp(key, "X-Jpanel-Delivery") == 0) {
        s_f.delivery = (uint32_t)strtoul(value, NULL, 10);
        s_f.delivery_set = true;
    } else if (strcasecmp(key, "X-Jpanel-Duration-Ms") == 0) {
        s_f.duration_ms = (uint32_t)strtoul(value, NULL, 10);
    }
}

/* The one place a stream begins: every fetch and every replay waits out the press cue's
   predecessor here (`test_every_way_of_starting_a_message_waits_for_the_speaker`). With no
   arbiter it refuses at once rather than waiting for a backstop. */
static bool stream_begin_waiting(void)
{
    if (!stream_facts(s_st).arbiter) {
        s_f.fail = "no-audio";
        return false;
    }
    uint32_t waited = 0;
    while (!stream_begin(s_st, s_f.gen, &s_f.stream_id)) {
        if (stream_stale(s_st, s_f.gen)) {
            s_f.fail = "stopped";
            return false;
        }
        if (waited >= STREAM_WAIT_MAX_MS) {
            s_f.fail = "speaker-busy";
            return false;
        }
        s_io.sleep_ms(s_io.ctx, STREAM_WAIT_STEP_MS);
        waited += STREAM_WAIT_STEP_MS;
        beat();
    }
    if (waited > s_stats.msg_waited_ms) s_stats.msg_waited_ms = waited;
    return true;
}

static int on_head(void *ctx, int status)
{
    (void)ctx;
    s_f.status = status;
    yield("head");
    if (status != 200) return 1;
    if (stream_stale(s_st, s_f.gen)) return 1;
    if (!stream_begin_waiting()) return 1;
    s_f.open = true;
    return 0;
}

static void sounding_deadline(uint32_t now)
{
    s_v.deadline_ms = now + JP_LINK_STALL_MS + JP_SOUND_SLOP_MS;
}

/* What can move the SOUNDING deadline: bytes arriving, audible ms advancing, or — for a paused
   stream — the arbiter still ticking. A dead arbiter moves none of them. */
static void sounding_tick(void)
{
    const stream_facts_t f = stream_facts(s_st);
    const uint32_t out = f.stream_id == s_f.stream_id ? f.out_ms : 0;
    if (out != s_f.last_out_ms || (f.arb_hb != s_f.last_arb_hb && f.stream_live)) {
        sounding_deadline(now_ms());
    }
    s_f.last_out_ms = out;
    s_f.last_arb_hb = f.arb_hb;
}

static void first_byte(void)
{
    s_f.first_byte = true;
    s_v.cur.stream_id = s_f.stream_id;
    if (!s_f.replay) {
        /* The header wins over the seed, so a box that disagrees with its own poll is believed. */
        if (s_f.from_set) copy(s_v.cur.who.from, sizeof(s_v.cur.who.from), s_f.from);
        if (s_f.kind_set) s_v.cur.who.from_dad = s_f.from_dad;
        waiting_take_cur();
        if (s_f.id[0] != '\0') copy(s_v.cur.who.id, sizeof(s_v.cur.who.id), s_f.id);
        /* AGAIN's target: 0.3.39's `s_in_id`, set only where a stream really began, so a PLAY that
           failed before its first byte never becomes the replay. */
        s_v.last_begun = s_v.cur.who;
    }
    s_v.phase = JP_SOUNDING;
    sounding_deadline(now_ms());
    const stream_facts_t f = stream_facts(s_st);
    s_f.last_arb_hb = f.arb_hb;
    publish();
}

/* Socket to ring at the speed of the speaker: a full ring is "not yet" and waiting here IS the
   backpressure; a stale gen is "never" and ends the request (A1's sink check). */
static int on_body(void *ctx, const uint8_t *data, size_t n)
{
    (void)ctx;
    if (stream_stale(s_st, s_f.gen)) return 1;
    if (n == 0) return 0;
    if (!s_f.first_byte) first_byte();
    sounding_deadline(now_ms());
    uint8_t *buf = (uint8_t *)s_chunk;
    while (n > 0) {
        size_t avail = 0;
        /* An odd byte is carried, never offered: the ring deals in samples, and a lone byte
           offered forever would hang the task that also acks. */
        if (s_f.have_odd) buf[avail++] = s_f.odd;
        s_f.have_odd = false;
        const size_t take = n < sizeof(s_chunk) - avail ? n : sizeof(s_chunk) - avail;
        memcpy(buf + avail, data, take);
        data += take;
        n -= take;
        avail += take;
        if (avail % 2 == 1) {
            s_f.odd = buf[avail - 1];
            s_f.have_odd = true;
            avail--;
        }
        s_io.sha_update(s_io.ctx, buf, avail);
        s_f.total += (uint32_t)avail;
        size_t off = 0;
        const size_t samples = avail / 2;
        while (off < samples) {
            const size_t took = stream_write(s_st, s_chunk + off, samples - off);
            if (took == 0) {
                if (stream_stale(s_st, s_f.gen)) return 1;
                s_io.sleep_ms(s_io.ctx, JP_DRAIN_STEP_MS);
                beat();
                sounding_tick();
                publish();
                continue;
            }
            off += took;
        }
    }
    yield("chunk");
    return 0;
}

static void hex32(const uint8_t d[JP_SHA256_LEN], char out[65])
{
    for (int i = 0; i < JP_SHA256_LEN; i++) snprintf(&out[i * 2], 3, "%02x", d[i]);
    out[64] = '\0';
}

/* One GET into the ring. True when a stream began (it then drains over the next steps and is
   classified in step 1); false when it never did, with the reason in `s_f`. */
static bool fetch_one(const char *id, int at, bool replay)
{
    memset(&s_f, 0, sizeof(s_f));
    s_f.replay = replay;
    s_f.gen = s_job.cmd.gen;
    s_f.status = -1;
    s_f.err = "";
    copy(s_f.req_id, sizeof(s_f.req_id), id);
    char rel[96];
    char path[112];
    if (at < 0) at = 0;
    if (at > 127) at = 127;
    /* By id when the box sent ids; `at` rides along because a rolled-back box honours it and
       ignores `id` (§6). Replays use their own route so they never spend a delivery. */
    if (replay) {
        snprintf(rel, sizeof(rel), "/message/%s/pcm", id);
    } else if (id[0] != '\0') {
        snprintf(rel, sizeof(rel), "/next?id=%s&at=%d", id, at);
    } else {
        snprintf(rel, sizeof(rel), "/next?at=%d", at);
    }
    if (!jpath(path, sizeof(path), rel)) {
        s_f.err = "url-too-long";
        return false;
    }
    const uint32_t now = now_ms();
    s_v.phase = JP_FETCHING;
    s_v.deadline_ms = now + JP_HTTP_MS;
    publish();
    /* A stop that landed while this was being set up costs no request at all. */
    if (stream_stale(s_st, s_f.gen)) return false;
    s_io.sha_begin(s_io.ctx);
    const link_req_t req = {.method = "GET",
                            .path = path,
                            .timeout_ms = (int)JP_HTTP_MS,
                            .sink = on_body,
                            .on_header = on_header,
                            .on_head = on_head,
                            .deadline_ms = s_v.deadline_ms,
                            .upload_mode = LINK_DEADLINE_ABSOLUTE,
                            .on_progress = on_progress,
                            .cancelled = fetch_cancelled};
    const link_res_t res = s_io.request(s_io.ctx, &req);
    if (res.status >= 0 || s_f.status < 0) s_f.status = res.status;
    s_f.err = res.err != NULL ? res.err : "";
    s_f.cut = res.cut;
    s_f.timed_out = res.timed_out;
    if (s_f.have_odd) {
        /* A body that ends on half a sample was cut; hashed so the mismatch is honest. */
        s_io.sha_update(s_io.ctx, &s_f.odd, 1);
        s_f.total++;
        s_f.have_odd = false;
    }
    uint8_t digest[JP_SHA256_LEN];
    s_io.sha_finish(s_io.ctx, digest);
    if (!s_f.open) return false;
    stream_end(s_st);
    s_f.body_done = true;
    char got[65];
    hex32(digest, got);
    /* TRUE WHEN THE BOX DID NOT SAY: a box too old to send a digest keeps the assurance it always
       had, rather than every message playing forever unacknowledged. */
    s_f.verified = s_f.sha[0] == '\0' || strcasecmp(s_f.sha, got) == 0;
    return true;
}

/* ── acknowledging ────────────────────────────────────────────────────────────────────────── */

static void ack_arm(ack_kind_t kind, const char *id, uint32_t heard_ms, uint32_t delivery)
{
    const uint32_t now = now_ms();
    s_ack.kind = kind;
    copy(s_ack.id, sizeof(s_ack.id), id);
    s_ack.heard_ms = heard_ms;
    s_ack.delivery = delivery;
    s_ack.first_at = now;
    s_ack.next_at = now;
    s_ack.backoff = JP_ACK_BACKOFF_FIRST_MS;
}

static void ack_clear(void)
{
    s_ack.kind = ACK_NONE;
    s_ack.id[0] = '\0';
    s_v.ack_owed = false;
}

/* One attempt; true when the slot is clear afterwards. Never cancelled: a stop must not cost an
   acknowledgement. 204 clears; 409/404 clear (a 404 on `/returned` is a tunnel that does not
   allow the path, so the refund did not happen); any other 4xx but 408/429 is terminal; the rest
   backs off 1, 2, 4 … 30 s for up to ten minutes (E5.2). */
static bool ack_attempt(void)
{
    char path[32];
    char body[128];
    const bool played = s_ack.kind == ACK_PLAYED;
    int bn;
    if (played) {
        bn = snprintf(body, sizeof(body), "{\"id\":\"%s\",\"heard_ms\":%u}", s_ack.id,
                      (unsigned)s_ack.heard_ms);
    } else {
        bn = snprintf(body, sizeof(body), "{\"id\":\"%s\",\"delivery\":%u}", s_ack.id,
                      (unsigned)s_ack.delivery);
    }
    if (bn <= 0 || bn >= (int)sizeof(body) ||
        !jpath(path, sizeof(path), played ? "/played" : "/returned")) {
        ack_clear();
        return true;
    }
    const jp_phase_t was = s_v.phase;
    const uint32_t was_deadline = s_v.deadline_ms;
    s_v.phase = JP_ACKING;
    s_v.deadline_ms = now_ms() + JP_ACK_MS;
    publish();
    const link_req_t req = {.method = "POST",
                            .path = path,
                            .content_type = "application/json",
                            .body = (const uint8_t *)body,
                            .body_len = (size_t)bn,
                            .timeout_ms = (int)JP_ACK_MS,
                            .deadline_ms = s_v.deadline_ms,
                            .upload_mode = LINK_DEADLINE_ABSOLUTE,
                            .on_progress = on_progress};
    const link_res_t res = s_io.request(s_io.ctx, &req);
    const int st = res.status;
    const uint32_t now = now_ms();
    if (st >= 200 && st < 300) {
        ack_clear();
    } else if (st == 409 || st == 404) {
        if (st == 404 && !played) s_stats.no_refund++;
        ack_clear();
    } else if (st >= 400 && st < 500 && st != 408 && st != 429) {
        ack_clear();
    } else {
        s_stats.ack_retries++;
        if (!s_v.ack_owed) s_stats.ack_owed++;
        s_v.ack_owed = true;
        if (jp_due(now, s_ack.first_at + JP_ACK_GIVE_UP_MS)) {
            /* Given up: the box may offer it again, bounded by its delivery cap (E2.5). */
            ack_clear();
        } else {
            s_ack.next_at = now + s_ack.backoff;
            s_ack.backoff = s_ack.backoff * 2 > JP_ACK_BACKOFF_MAX_MS ? JP_ACK_BACKOFF_MAX_MS
                                                                       : s_ack.backoff * 2;
        }
    }
    s_v.phase = was;
    s_v.deadline_ms = was_deadline;
    publish();
    yield("acked");
    return s_ack.kind == ACK_NONE;
}

/* ── the poll ─────────────────────────────────────────────────────────────────────────────── */

static void apply_waiting(const jp_waiting_t *in)
{
    s_v.waiting = *in;
    jp_waiting_t *w = &s_v.waiting;
    if (w->n > JPANEL_QUEUE_MAX) w->n = JPANEL_QUEUE_MAX;
    for (int i = 0; i < w->n; i++) {
        w->q[i].id[JP_ID_MAX - 1] = '\0';
        w->q[i].from[JP_NAME_MAX - 1] = '\0';
        if (!id_ok(w->q[i].id)) w->q[i].id[0] = '\0';
    }
    w->sibling[JP_NAME_MAX - 1] = '\0';
    /* A message whose PLAYED ack is still owed was heard; the box just has not been told. */
    if (s_ack.kind == ACK_PLAYED) {
        const int i = waiting_find(s_ack.id);
        if (i >= 0) {
            waiting_remove_at(i);
            if (w->count > 0) w->count--;
        }
    }
    waiting_normalise();
    if (s_job.active && s_job.by_id) {
        for (int i = 0; i < w->n; i++) run_add(w->q[i].id);
    }
    cur_in_waiting_refresh();
}

static void do_poll(void)
{
    const char *why = NULL;
    /* 1,536 bytes (§7.6): eight entries with ids and 31-byte names, the scalars and a sibling,
       with room. A cut body parses as nothing and reads as an empty house, so it is sized, and
       `test_the_panel_can_hold_the_longest_poll_the_box_will_send` recomputes it from the box. */
    EXT_RAM_BSS_ATTR static char body[1536];
    char path[32];
    const uint32_t now = now_ms();
    atomic_store(&s_poll_flag, false);
    s_next_poll = now + JP_POLL_EVERY_MS;
    if (!jpath(path, sizeof(path), "/waiting")) return;
    const jp_phase_t was = s_v.phase;
    const uint32_t was_deadline = s_v.deadline_ms;
    s_v.phase = JP_POLLING;
    s_v.deadline_ms = now + JP_HTTP_MS;
    publish();
    const link_req_t req = {.method = "GET",
                            .path = path,
                            .timeout_ms = (int)JP_HTTP_MS,
                            .buf = (uint8_t *)body,
                            .cap = sizeof(body) - 1,
                            .deadline_ms = s_v.deadline_ms,
                            .upload_mode = LINK_DEADLINE_ABSOLUTE,
                            .on_progress = on_progress,
                            .cancelled = poll_cancelled};
    const link_res_t res = s_io.request(s_io.ctx, &req);
    if (res.stopped && res.status != 200) {
        /* Yielded to a command, not failed: ask again once it is done. */
        atomic_store(&s_poll_flag, true);
    } else if (res.status < 0) {
        why = res.timed_out ? "timeout" : (res.err != NULL && res.err[0] ? res.err : "connect");
    } else if (res.status != 200) {
        static char code[16];
        snprintf(code, sizeof(code), "http-%d", res.status);
        why = code;
    } else if (res.cut) {
        why = "cut";
    } else if (res.got == 0) {
        why = "empty-body";
    } else {
        EXT_RAM_BSS_ATTR static jp_waiting_t parsed;
        memset(&parsed, 0, sizeof(parsed));
        body[res.got < sizeof(body) ? res.got : sizeof(body) - 1] = '\0';
        if (s_io.parse_waiting(s_io.ctx, (const uint8_t *)body, res.got, &parsed)) {
            apply_waiting(&parsed);
            reach_ok(REACH_POLL, now_ms());
        } else {
            why = "bad-json";
        }
    }
    if (why != NULL) reach_fail(REACH_POLL, why, now_ms());
    s_v.phase = was;
    s_v.deadline_ms = was_deadline;
    publish();
}

static bool poll_wanted(void)
{
    return atomic_load(&s_poll_flag) || jp_due(now_ms(), s_next_poll);
}

/* ── playing ──────────────────────────────────────────────────────────────────────────────── */

/* `cur` is seeded BEFORE the request goes out, from the waiting entry the box already described
   (0.3.39's `seed_sender`); the header overwrites it when it lands. */
static void cur_seed(const char *id, int at, const jp_who_t *fallback)
{
    const jp_waiting_t *w = &s_v.waiting;
    if (at > 127) at = 127;
    int i = waiting_find(id);
    if (i < 0 && !w->has_ids && at >= 0 && at < w->n) i = at;
    s_v.cur.who = i >= 0 ? w->q[i] : *fallback;
    if (id[0] != '\0') copy(s_v.cur.who.id, sizeof(s_v.cur.who.id), id);
    s_v.cur.pos = (int8_t)(i >= 0 ? i : (w->has_ids ? -1 : at));
    s_v.cur.in_waiting = w->has_ids ? i >= 0 : at < w->count;
    s_v.cur.stream_id = 0;
}

static void claim(const jp_cmd_t *c)
{
    s_job.active = true;
    s_job.cmd = *c;
    s_v.claimed_seq = c->seq;
    s_v.cur.seq = c->seq;
    s_v.cur.gen = c->gen;
    s_v.cur.replay = c->kind == JP_CMD_REPLAY;
    s_v.cur.run_continues = false;
    s_v.phase = JP_FETCHING;
    s_v.deadline_ms = now_ms() + JP_HTTP_MS;
    if (c->kind == JP_CMD_REPLAY) {
        s_v.cur.who = c->target;
        s_v.cur.pos = -1;
        s_v.cur.in_waiting = false;
        s_v.cur.stream_id = 0;
    } else {
        cur_seed(c->target.id, c->at, &c->target);
    }
    publish();
    yield("claimed");
}

static jp_who_t outcome_who(void)
{
    jp_who_t who = s_v.cur.who;
    if (s_f.id[0] != '\0') copy(who.id, sizeof(who.id), s_f.id);
    return who;
}

/* The outcome of a fetch whose stream never began. */
static void end_unbegun(void)
{
    const jp_who_t who = outcome_who();
    const bool replay = s_f.replay;
    if (stream_stale(s_st, s_f.gen)) {
        end_play(s_job.cmd.seq, &who, replay, JP_PLAY_CANCELLED, "stopped");
        return;
    }
    char why[JP_REASON_MAX];
    if (s_f.fail != NULL) {
        copy(why, sizeof(why), s_f.fail);
    } else if (s_f.timed_out) {
        copy(why, sizeof(why), "timeout");
    } else if (s_f.status < 0) {
        copy(why, sizeof(why), s_f.err[0] ? s_f.err : "connect");
    } else {
        snprintf(why, sizeof(why), "http-%d", s_f.status);
    }
    end_play(s_job.cmd.seq, &who, replay, JP_PLAY_FAILED, why);
}

/* The next message of the claimed PLAY: the pressed one first, then the run, oldest first. */
static void play_next(void)
{
    for (;;) {
        char id[JP_ID_MAX] = "";
        int at = 0;
        /* A stop between messages ends the run on the message that was heard; nothing more is
           fetched, so nothing unsounded can be acknowledged. */
        if (!s_job.first && stream_stale(s_st, s_job.cmd.gen)) {
            end_run_on_previous();
            return;
        }
        if (s_job.first) {
            copy(id, sizeof(id), s_job.cmd.target.id);
            at = s_job.cmd.at;
        } else if (s_job.by_id) {
            int k = 0;
            while (k < s_run_n && tried(s_run[k])) k++;
            if (k >= s_run_n) {
                end_run_on_previous();
                return;
            }
            copy(id, sizeof(id), s_run[k]);
            const int i = waiting_find(id);
            at = i >= 0 ? i : 0;
        } else if (s_v.waiting.count <= 0) {
            end_run_on_previous();
            return;
        }
        const bool pressed = s_job.first;
        s_job.first = false;
        run_mark_tried(id);
        /* Only the pressed message may fall back to what render drew; a run's next id that the
           list no longer describes waits for its header rather than borrowing a sender. */
        const jp_who_t none = {0};
        cur_seed(id, at, pressed ? &s_job.cmd.target : &none);
        s_v.cur.run_continues = !pressed;
        if (fetch_one(id, at, false)) return;
        if (!stream_stale(s_st, s_f.gen) && s_f.status == 204 && s_f.fail == NULL) {
            waiting_forget(id, at);
            atomic_store(&s_poll_flag, true);
            if (!pressed) continue;
            const jp_who_t who = outcome_who();
            end_play(s_job.cmd.seq, &who, false, JP_PLAY_EMPTY, "gone");
            return;
        }
        end_unbegun();
        return;
    }
}

static uint32_t heard_threshold(void)
{
    /* "Or 80% of a short one" (E2.4): a 1.5 s message stopped at 1.3 s was heard. */
    const uint32_t most = s_f.duration_ms / 5 * 4;
    if (s_f.duration_ms > 0 && most < JP_HEARD_MIN_MS) return most;
    return JP_HEARD_MIN_MS;
}

/* Step 1: the stream has drained or been stopped. Classify it, publish the outcome and
   `run_continues` BEFORE acknowledging, then acknowledge, then continue the run. */
static void finish_stream(void)
{
    const stream_facts_t facts = stream_facts(s_st);
    const uint32_t word = stream_stop_word(s_st);
    const bool stopped = stream_stale(s_st, s_f.gen);
    const bool heard_bit = stopped && (word & 1u) != 0;
    const uint32_t out_ms = facts.stream_id == s_f.stream_id ? facts.out_ms : 0;
    const jp_who_t who = outcome_who();
    const bool have_id = who.id[0] != '\0' && id_ok(who.id);
    jp_play_kind_t kind;
    const char *reason = "";
    ack_kind_t ack = ACK_NONE;

    if (s_f.replay) {
        if (stopped) {
            kind = JP_PLAY_CANCELLED;
            reason = "stopped";
        } else if (s_f.total < 2 || s_f.cut || !s_f.verified) {
            kind = JP_PLAY_SHORT;
            reason = s_f.total < 2 ? "empty" : "short";
        } else {
            kind = JP_PLAY_HEARD;
        }
    } else if (stopped) {
        /* Her choice, not a fault. From F3 a stop under the threshold hands the message back —
           but only when the box said which delivery this was; otherwise it acks as 0.3.39 does
           (§6). A REPLY stop sets the heard bit: she is answering it. */
        const bool heard = s_f.first_byte && (heard_bit || out_ms >= heard_threshold());
        if (s_f.delivery_set && have_id && !heard) {
            kind = JP_PLAY_RETURNED;
            ack = ACK_RETURNED;
        } else if (!s_f.first_byte) {
            kind = JP_PLAY_CANCELLED;
            reason = "stopped";
        } else {
            kind = JP_PLAY_STOPPED_HEARD;
            if (have_id) ack = ACK_PLAYED;
        }
    } else if (s_f.total < 2) {
        kind = JP_PLAY_SHORT;
        reason = "empty";
    } else if (s_f.cut || !s_f.verified) {
        /* NOT ACKNOWLEDGED: acking a message that stopped mid-sentence would retire it, and
           nobody would ever know the rest existed. It stays unplayed and the notice returns. */
        kind = JP_PLAY_SHORT;
        reason = "short";
    } else if (have_id) {
        kind = JP_PLAY_HEARD;
        ack = ACK_PLAYED;
    } else {
        kind = JP_PLAY_HEARD_UNACKED;
    }

    s_stats.msg_bytes = s_f.total;
    if (kind == JP_PLAY_HEARD || kind == JP_PLAY_HEARD_UNACKED ||
        kind == JP_PLAY_STOPPED_HEARD) {
        s_stats.msg_ms = out_ms;
        s_stats.msg_ok++;
    }
    const bool fresh = !stopped;
    bool continues = !s_f.replay && fresh &&
                     (kind == JP_PLAY_HEARD || kind == JP_PLAY_HEARD_UNACKED) && run_has_next();

    s_v.play.seq = s_job.cmd.seq;
    s_v.play.who = who;
    s_v.play.kind = kind;
    s_v.play.replay = s_f.replay;
    copy(s_v.play.reason, sizeof(s_v.play.reason), reason);
    if (kind == JP_PLAY_SHORT) fail_note(s_v.play.reason);
    if (kind == JP_PLAY_RETURNED) {
        waiting_reinsert(&who, s_v.cur.pos);
        cur_in_waiting_refresh();
        s_stats.returned++;
        atomic_store(&s_poll_flag, true);
    }
    s_v.cur.run_continues = continues;
    if (!continues) {
        s_v.done_seq = s_job.cmd.seq;
        s_job.active = false;
        s_v.phase = JP_IDLE;
        s_v.deadline_ms = 0;
    } else {
        s_v.phase = JP_FETCHING;
        s_v.deadline_ms = now_ms() + JP_HTTP_MS;
    }
    s_f.open = false;
    publish();
    stream_classified(s_st, word >> 1);
    yield("classified");

    if (ack != ACK_NONE) {
        ack_arm(ack, who.id, out_ms, s_f.delivery);
        if (!ack_attempt() && continues) {
            /* A failed ack ends the run: fetching on would hand the same message back (A4). */
            continues = false;
            end_run_on_previous();
        }
    }
    if (!continues) {
        /* The badge reflects what just happened. */
        atomic_store(&s_poll_flag, true);
        return;
    }
    /* The only poll a run makes, and only while the speaker is idle (today's guard), so an id
       that arrived mid-run joins it. `continues` was decided before it, as in 0.3.39. */
    const bool undescribed = s_job.by_id && s_v.waiting.count > s_v.waiting.n;
    if ((atomic_load(&s_poll_flag) || undescribed) && !stream_facts(s_st).speaker_busy) do_poll();
    play_next();
}

/* ── sending ──────────────────────────────────────────────────────────────────────────────── */

static const char *s_send_why;

static jp_send_kind_t do_send_once(const jp_cmd_t *c, const char *hex, bool *corrupt)
{
    char path[48];
    char rel[32];
    jp_send_kind_t out = JP_SEND_FAILED;
    /* Set at each `goto done`: the label cannot tell which branch reached it, and "her message did
       not go" without which half is a report with nowhere to land. */
    const char *why = "unknown";
    snprintf(rel, sizeof(rel), "/send?to=%s", c->to == JP_TO_DAD ? "dad" : "panel");
    if (!jpath(path, sizeof(path), rel)) {
        why = "url-too-long";
        goto done;
    }
    /* The first frame must leave within the socket wait plus a stall, restarted at a 422 retry's
       start (§7.5a); after that the link moves the deadline with the upload, then gives the head
       the box's transcription budget. */
    s_v.deadline_ms = now_ms() + JP_LINK_UP_WAIT_MS + JP_LINK_STALL_MS;
    publish();
    const link_req_t req = {.method = "POST",
                            .path = path,
                            .content_type = "application/octet-stream",
                            /* Named here as what the panel sends, in `on_header` as what the box
                               answers, and once on the box. */
                            .hdr_name = hex[0] ? "X-Jpanel-Sha256" : NULL,
                            .hdr_value = hex[0] ? hex : NULL,
                            .body = (const uint8_t *)c->pcm,
                            .body_len = c->bytes,
                            .timeout_ms = (int)JP_HTTP_MS,
                            .deadline_ms = s_v.deadline_ms,
                            .upload_mode = LINK_DEADLINE_PROGRESS,
                            .head_budget_ms = JP_SEND_HEAD_MS,
                            .on_progress = on_progress};
    const link_res_t res = s_io.request(s_io.ctx, &req);
    const int status = res.status;
    if (status < 0) {
        why = res.timed_out ? "timeout" : (res.err != NULL && res.err[0] ? res.err : "connect");
        goto done;
    }
    if (status == 409) {
        /* "The other panel" is only obvious with exactly two; the box refuses to guess. */
        why = "nobody";
        out = JP_SEND_NOBODY;
        goto done;
    }
    if (status == 400) {
        why = "empty";
        out = JP_SEND_EMPTY;
        goto done;
    }
    if (status == 413) {
        why = "too-long";
        goto done;
    }
    if (status == 422) {
        /* The box hashed what arrived and got something else; the buffer is the good copy. */
        *corrupt = true;
        why = "corrupt";
        goto done;
    }
    if (status != 200) {
        /* Static: `reach_fail` keeps the pointer. One task sends. */
        static char code[16];
        snprintf(code, sizeof(code), "http-%d", status);
        why = code;
        goto done;
    }
    out = JP_SEND_SENT;
    why = "";

done:
    s_send_why = why;
    /* Nobody-to-send-to and nothing-said are not faults: the box answered. Counting them would
       bury the failures that LOSE a message under a house with one panel. */
    if (out == JP_SEND_SENT) {
        reach_ok(REACH_SEND, now_ms());
    } else if (out != JP_SEND_NOBODY && out != JP_SEND_EMPTY) {
        reach_fail(REACH_SEND, why, now_ms());
    }
    return out;
}

static void do_send(const jp_cmd_t *c)
{
    s_v.claimed_seq = c->seq;
    s_v.phase = JP_SENDING;
    s_v.deadline_ms = now_ms() + JP_LINK_UP_WAIT_MS + JP_LINK_STALL_MS;
    publish();
    yield("claimed");
    /* Said before the bytes go, so a connection that ends cleanly two thirds of the way through
       is a refusal rather than a stored half-sentence. Hashed once: the leased buffer cannot
       change under it (A5). */
    char hex[65] = "";
    if (c->pcm != NULL && c->bytes > 0) {
        uint8_t digest[JP_SHA256_LEN];
        s_io.sha_begin(s_io.ctx);
        s_io.sha_update(s_io.ctx, (const uint8_t *)c->pcm, c->bytes);
        s_io.sha_finish(s_io.ctx, digest);
        hex32(digest, hex);
    }
    jp_send_kind_t out = JP_SEND_FAILED;
    for (int attempt = 1; attempt <= SEND_ATTEMPTS; attempt++) {
        bool corrupt = false;
        out = do_send_once(c, hex, &corrupt);
        if (!corrupt) break;
    }
    end_send(c->seq, out, s_send_why);
    if (out == JP_SEND_SENT) atomic_store(&s_poll_flag, true);
}

/* ── the loop ─────────────────────────────────────────────────────────────────────────────── */

static void run_command(const jp_cmd_t *c)
{
    const uint32_t now = now_ms();
    /* The claim margin (§7.1): never claim within JP_UI_MARGIN_MS of expiry, so the UI's
       unclaimed backstop at expires_at + margin cannot fire on a command claimed in time. A
       claimed SEND is never re-checked. */
    const bool expired = jp_due(now, c->expires_at - JP_UI_MARGIN_MS);
    if (c->kind == JP_CMD_SEND) {
        if (expired) {
            end_send(c->seq, JP_SEND_FAILED, "expired");
            return;
        }
        do_send(c);
        return;
    }
    if (expired || stream_stale(s_st, c->gen)) {
        end_play(c->seq, &c->target, c->kind == JP_CMD_REPLAY, JP_PLAY_CANCELLED,
                 expired ? "expired" : "stale");
        return;
    }
    claim(c);
    if (c->kind == JP_CMD_REPLAY) {
        /* Never acknowledged and never a run: "again" must not ack a second listen or walk on
           into the next unheard message. */
        if (!id_ok(c->target.id)) {
            end_play(c->seq, &c->target, true, JP_PLAY_FAILED, "no-id");
            return;
        }
        if (!fetch_one(c->target.id, 0, true)) end_unbegun();
        return;
    }
    if (s_ack.kind != ACK_NONE && !ack_attempt()) {
        end_play(c->seq, &s_v.cur.who, false, JP_PLAY_FAILED, "ack-pending");
        return;
    }
    s_job.first = true;
    s_job.by_id = s_v.waiting.has_ids;
    s_run_n = 0;
    s_tried_n = 0;
    if (s_job.by_id) {
        for (int i = 0; i < s_v.waiting.n; i++) run_add(s_v.waiting.q[i].id);
    }
    play_next();
}

static bool stream_finished(void)
{
    if (stream_stale(s_st, s_f.gen)) return true;
    return s_f.body_done && !stream_facts(s_st).stream_live;
}

void jpanel_core_init(const jpanel_io_t *io)
{
    s_io = *io;
    s_st = io->stream;
    memset(&s_v, 0, sizeof(s_v));
    memset(&s_job, 0, sizeof(s_job));
    memset(&s_f, 0, sizeof(s_f));
    memset(&s_ack, 0, sizeof(s_ack));
    memset(&s_stats, 0, sizeof(s_stats));
    s_v.cur.pos = -1;
    s_run_n = 0;
    s_tried_n = 0;
    s_err = "";
    atomic_store(&s_snapshot_retries, 0);
    atomic_store(&s_poll_flag, false);
    s_next_poll = now_ms() + io->poll_first_ms;
    atomic_store(&s_lock, 0);
    publish();
}

void jpanel_core_step(void)
{
    beat();
    if (s_f.open) {
        if (!stream_finished()) {
            sounding_tick();
            publish();
            s_io.sleep_ms(s_io.ctx, JP_DRAIN_STEP_MS);
            return;
        }
        finish_stream();
        return;
    }
    if (s_ack.kind != ACK_NONE && jp_due(now_ms(), s_ack.next_at)) (void)ack_attempt();
    const bool poll_now = poll_wanted() && !stream_facts(s_st).speaker_busy;
    jp_cmd_t c;
    if (s_io.take(s_io.ctx, &c, poll_now ? 0 : JP_IDLE_WAIT_MS)) {
        run_command(&c);
        return;
    }
    /* Never while the speaker is busy: a poll is a TLS handshake on the HTTPS fallback, and the
       audio task feeds I2S from the same core's spare cycles. */
    if (poll_wanted() && !stream_facts(s_st).speaker_busy) do_poll();
    publish();
}

void jpanel_core_poll_soon(void) { atomic_store(&s_poll_flag, true); }

bool jpanel_core_snapshot(jp_view_t *out)
{
    for (int tries = 0; tries < 4; tries++) {
        const uint32_t s1 = atomic_load_explicit(&s_lock, memory_order_acquire);
        if (s1 & 1u) continue;
        memcpy(&s_read, &s_pub, sizeof(s_read));
        atomic_thread_fence(memory_order_acquire);
        const uint32_t s2 = atomic_load_explicit(&s_lock, memory_order_relaxed);
        if (s1 == s2) {
            *out = s_read;
            return true;
        }
    }
    atomic_fetch_add(&s_snapshot_retries, 1);
    return false;
}

bool jpanel_core_health(jp_health_t *out)
{
    for (int tries = 0; tries < 4; tries++) {
        const uint32_t s1 = atomic_load_explicit(&s_lock, memory_order_acquire);
        if (s1 & 1u) continue;
        const jp_health_t h = {.hb = s_pub.hb, .deadline_ms = s_pub.deadline_ms,
                               .idle = s_pub.phase == JP_IDLE};
        atomic_thread_fence(memory_order_acquire);
        if (atomic_load_explicit(&s_lock, memory_order_relaxed) == s1) {
            *out = h;
            return true;
        }
    }
    return false;
}

void jpanel_core_stats(jp_stats_t *out)
{
    *out = s_stats;
    out->snapshot_retries = atomic_load(&s_snapshot_retries);
}

const char *jpanel_core_err(void) { return s_err; }
