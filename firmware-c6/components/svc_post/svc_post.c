/* The voice post service: the box's last messages with each twin in the people's threads, what
 * the glass asked of the box, and the box's answers applied on the UI task. Requests, answers and
 * the pickup's tag cross between the tasks under the port's lock; everything else here is the UI
 * task's alone. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "minutes.h"
#include "svc_debug.h"
#include "svc_messages_fake.h"
#include "svc_post.h"
#include "svc_post_drv.h"
#include "svc_rtc.h"
#include "svc_settings.h"
#include "svc_siblings.h"

#define QUEUE 4
/* Box messages' bubble ids: past svc_convo's switched bubbles (500000 up) and the talks' (1000
 * up, by two), so an id names one bubble wherever it is. */
#define LOCAL_ID0 700000u
/* The stamps' list goes to flash at most this often: a burst of plays is one write. */
#define STAMPS_SAVE_MS 2000

typedef struct {
    char device[POST_ID_BYTES]; /* the twin's panel this thread is bound to; "" none */
    bool want;                  /* a fetch is due */
    bool loaded;                /* the box has answered since the binding */
    /* The last list's box ids (what is new is judged against them) ... */
    post_seen_t seen;
    uint32_t local[POST_KEEP]; /* ... and their bubbles' */
} slot_t;

static slot_t s_slot[SIB_THREADS];
static uint32_t s_next_local = LOCAL_ID0;

static void (*s_wake)(void);
static post_req_t s_queue[QUEUE];
static uint8_t s_queued;
typedef struct {
    post_req_t req;
    int status;
    char *body;
    size_t len;
} done_t;
static done_t s_done[QUEUE];
static uint8_t s_ndone;
/* The pickup's tag (main task's, under the lock) and whether it moved since the UI looked. */
static post_tag_t s_tag;
static bool s_tag_moved;

/* "Played" stamps the box has not confirmed: kept in NVS, sent one at a time, the head retried
 * after post_backoff_ms. The UI task's alone. */
static post_stamps_t s_stamps;
static bool s_stamp_out;      /* one is with the main task */
static uint8_t s_stamp_fails; /* the head's failures in a row */
static uint32_t s_stamp_due;  /* uptime ms the head may go */
static bool s_stamps_dirty;
static uint32_t s_stamps_changed;

static uint8_t s_arrived;
static struct {
    uint32_t asked, ok, failed, arrivals, chimes, heard, stamp_retries, stamps_dropped;
    int last;
} s_st;

static int slot_of(const char *thread)
{
    for (uint8_t s = 0; s < SIB_THREADS; s++) {
        const char *t = svc_siblings_thread(s);
        if (t && thread && !strcmp(t, thread)) return s;
    }
    return -1;
}

void svc_post_set_wake(void (*wake)(void))
{
    s_wake = wake;
}

/* A request for the main task. A thread already waiting to be fetched is not asked twice. */
static void ask(post_rq_t kind, uint8_t slot, const char *path)
{
    post_req_t r = {.kind = (uint8_t)kind, .slot = slot};
    snprintf(r.path, sizeof r.path, "%s", path);
    post_port_lock();
    uint8_t at = s_queued;
    for (uint8_t i = 0; i < s_queued; i++)
        if (!strcmp(s_queue[i].path, r.path)) at = i;
    if (at == QUEUE) {
        /* The stamp in flight, if it is the one giving way, is sent again on the next pass. */
        if (s_queue[0].kind == POST_RQ_PLAYED) s_stamp_out = false;
        memmove(s_queue, s_queue + 1, sizeof s_queue[0] * (QUEUE - 1));
        at = QUEUE - 1;
    } else if (at == s_queued) {
        s_queued++;
    }
    s_queue[at] = r;
    post_port_unlock();
    s_st.asked++;
    if (s_wake) s_wake();
}

void svc_post_tag(const char *tag)
{
    post_port_lock();
    if (post_tag_feed(&s_tag, tag)) s_tag_moved = true;
    post_port_unlock();
}

bool svc_post_take(post_req_t *out)
{
    post_port_lock();
    const bool any = s_queued > 0;
    if (any) {
        *out = s_queue[0];
        memmove(s_queue, s_queue + 1, sizeof s_queue[0] * (QUEUE - 1));
        s_queued--;
    }
    post_port_unlock();
    return any;
}

void svc_post_done(const post_req_t *req, int status, char *body, size_t len)
{
    char *drop = NULL;
    post_port_lock();
    if (s_ndone == QUEUE) {
        drop = s_done[0].body;
        memmove(s_done, s_done + 1, sizeof s_done[0] * (QUEUE - 1));
        s_ndone--;
    }
    s_done[s_ndone++] = (done_t){*req, status, body, len};
    post_port_unlock();
    free(drop);
}

/* ---- the glass ---- */

void svc_post_open(const char *thread)
{
    const int s = slot_of(thread);
    if (s >= 0) s_slot[s].want = true;
}

bool svc_post_loaded(const char *thread)
{
    const int s = slot_of(thread);
    return s >= 0 && s_slot[s].loaded;
}

bool svc_post_send_path(const char *thread, char *out, size_t cap)
{
    const int s = slot_of(thread);
    return s >= 0 && post_send_path(s_slot[s].device, out, cap);
}

static int find_local(const slot_t *sl, uint32_t local_id)
{
    for (uint8_t i = 0; i < sl->seen.n; i++)
        if (sl->local[i] == local_id) return i;
    return -1;
}

bool svc_post_pcm_path(uint32_t local_id, char thread[16], char *out, size_t cap)
{
    for (uint8_t s = 0; s < SIB_THREADS; s++) {
        const int i = find_local(&s_slot[s], local_id);
        if (i < 0 || !svc_siblings_thread(s)) continue;
        snprintf(thread, 16, "%s", svc_siblings_thread(s));
        return post_pcm_path(s_slot[s].device, s_slot[s].seen.id[i], out, cap);
    }
    return false;
}

void svc_post_sent(const char *thread)
{
    svc_post_open(thread);
}

static void stamps_changed(void)
{
    s_stamps_dirty = true;
    s_stamps_changed = svc_rtc_uptime_ms();
}

/* The head of the stamps' list to the main task, when it is due and none is out. */
static void send_stamp(void)
{
    if (s_stamp_out || !s_stamps.n || (int32_t)(svc_rtc_uptime_ms() - s_stamp_due) < 0) return;
    char path[POST_PATH_BYTES];
    if (!post_played_path(s_stamps.s[0].device, s_stamps.s[0].id, path, sizeof path)) return;
    s_stamp_out = true;
    ask(POST_RQ_PLAYED, SIB_THREADS, path);
}

void svc_post_heard(const char *thread, uint32_t local_id)
{
    const int s = slot_of(thread);
    if (s < 0) return;
    slot_t *sl = &s_slot[s];
    const int i = find_local(sl, local_id);
    if (i < 0) return;
    s_st.heard++;
    svc_messages_mark_played(thread, local_id);
    if (s_stamps.n == POST_STAMPS) s_st.stamps_dropped++;
    if (post_stamps_add(&s_stamps, sl->device, sl->seen.id[i])) stamps_changed();
    send_stamp();
}

/* ---- applying the box's answers ---- */

static void reset(slot_t *sl, const char *device)
{
    memset(sl, 0, sizeof *sl);
    snprintf(sl->device, sizeof sl->device, "%s", device);
}

static void apply_thread(uint8_t s, const post_thread_t *t)
{
    slot_t *sl = &s_slot[s];
    const char *thread = svc_siblings_thread(s);
    msg_t m[POST_KEEP];
    uint32_t local[POST_KEEP];
    for (uint8_t i = 0; i < t->n; i++) {
        post_line_t l = t->line[i];
        /* Heard here and its stamp not yet confirmed: it stays played, whatever this list says
           (the stamp list retries on its own clock). */
        if (!l.mine && post_stamps_has(&s_stamps, sl->device, l.id)) l.played = true;
        /* A message already listed keeps its bubble's id: a refresh never restarts a play. */
        local[i] = 0;
        for (uint8_t k = 0; k < sl->seen.n; k++)
            if (!strcmp(sl->seen.id[k], l.id)) local[i] = sl->local[k];
        if (!local[i]) local[i] = s_next_local++;
        post_to_msg(&l, local[i], &m[i]);
    }
    /* After the ids were matched: this makes the new list the one judged against next time. */
    const uint8_t arrived = post_arrivals(&sl->seen, t);
    memcpy(sl->local, local, sizeof local[0] * t->n);
    sl->loaded = true;
    s_arrived += arrived;
    s_st.arrivals += arrived;
    if (thread) svc_messages_fake_set(thread, m, t->n);
}

/* A stamp's answer: confirmed (or the message gone) leaves the list; anything else is tried
 * again after the backoff. The answer is the head's, the only one ever out. */
static void apply_stamp(const done_t *d)
{
    s_stamp_out = false;
    if (!s_stamps.n) return;
    char head[POST_PATH_BYTES];
    const bool is_head = post_played_path(s_stamps.s[0].device, s_stamps.s[0].id, head, sizeof head) &&
                         !strcmp(head, d->req.path);
    if (!is_head) return;
    if (post_stamp_done(d->status)) {
        post_stamps_remove(&s_stamps, s_stamps.s[0].device, s_stamps.s[0].id);
        s_stamp_fails = 0;
        s_stamp_due = 0;
        stamps_changed();
        return;
    }
    if (s_stamp_fails < 255) s_stamp_fails++;
    s_st.stamp_retries++;
    s_stamp_due = svc_rtc_uptime_ms() + post_backoff_ms(s_stamp_fails);
}

static void apply(const done_t *d)
{
    s_st.last = d->status;
    const bool ok = d->req.kind == POST_RQ_PLAYED ? d->status == 204 : d->status == 200;
    if (ok) s_st.ok++;
    else s_st.failed++;
    if (d->req.kind == POST_RQ_PLAYED) {
        apply_stamp(d);
        return;
    }
    if (d->req.kind != POST_RQ_THREAD || d->req.slot >= SIB_THREADS) return;
    slot_t *sl = &s_slot[d->req.slot];
    char want[POST_PATH_BYTES];
    /* An answer for a panel this thread is no longer bound to is somebody else's. */
    if (!post_thread_path(sl->device, want, sizeof want) || strcmp(want, d->req.path)) return;
    if (!ok || !d->body) return;
    post_thread_t *t = malloc(sizeof *t);
    if (!t) return;
    if (post_parse_thread(d->body, d->len, t)) apply_thread(d->req.slot, t);
    else s_st.failed++;
    free(t);
}

void svc_post_service(void)
{
    post_port_lock();
    const bool moved = s_tag_moved;
    s_tag_moved = false;
    post_port_unlock();
    for (uint8_t s = 0; s < SIB_THREADS; s++) {
        slot_t *sl = &s_slot[s];
        sibling_t sib;
        const bool bound = svc_siblings_slot(s, &sib);
        const char *device = bound ? sib.device_id : "";
        if (strcmp(sl->device, device)) {
            /* A different panel: what this thread showed was somebody else's conversation. */
            const bool had = sl->device[0] != '\0';
            reset(sl, device);
            if (had && svc_siblings_thread(s)) svc_messages_fake_set(svc_siblings_thread(s), NULL, 0);
            sl->want = bound;
        }
        if (moved && bound) sl->want = true;
        char path[POST_PATH_BYTES];
        if (sl->want && post_thread_path(sl->device, path, sizeof path)) ask(POST_RQ_THREAD, s, path);
        sl->want = false;
    }
    for (;;) {
        post_port_lock();
        const bool any = s_ndone > 0;
        done_t d;
        if (any) {
            d = s_done[0];
            memmove(s_done, s_done + 1, sizeof s_done[0] * (QUEUE - 1));
            s_ndone--;
        }
        post_port_unlock();
        if (!any) break;
        apply(&d);
        free(d.body);
    }
    send_stamp();
    if (s_stamps_dirty && svc_rtc_uptime_ms() - s_stamps_changed >= STAMPS_SAVE_MS) {
        s_stamps_dirty = false;
        post_port_save(&s_stamps);
    }
}

bool svc_post_take_chime(bool busy)
{
    const uint8_t n = s_arrived;
    s_arrived = 0;
    rtc_time_t now;
    svc_rtc_now(&now);
    const uint16_t min = (uint16_t)(now.h * 60 + now.m);
    const bool night = now.valid && min_window_contains(min, (uint16_t)svc_settings_get(SET_NIGHT_FROM),
                                                        (uint16_t)svc_settings_get(SET_NIGHT_TO));
    const bool chime = post_chime(n, svc_settings_get(SET_NOTIF_CHIME) != 0, night, busy);
    if (chime) s_st.chimes++;
    return chime;
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    post_port_lock();
    char tag[POST_TAG_BYTES];
    memcpy(tag, s_tag.last, sizeof tag);
    const uint8_t queued = s_queued;
    post_port_unlock();
    dbg_json_str(w, "tag", tag);
    dbg_json_int(w, "queued", queued);
    dbg_json_int(w, "arrivals", s_st.arrivals);
    dbg_json_int(w, "chimes", s_st.chimes);
    dbg_json_int(w, "heard", s_st.heard);
    dbg_json_int(w, "stamps_waiting", s_stamps.n);
    dbg_json_int(w, "stamp_fails", s_stamp_fails);
    dbg_json_int(w, "stamp_retries", s_st.stamp_retries);
    dbg_json_int(w, "stamps_dropped", s_st.stamps_dropped);
    dbg_json_int(w, "last_status", s_st.last);
    for (uint8_t s = 0; s < SIB_THREADS; s++) {
        const char *thread = svc_siblings_thread(s);
        if (!thread) continue;
        dbg_json_obj_begin(w, thread);
        dbg_json_str(w, "device_id", s_slot[s].device);
        dbg_json_bool(w, "loaded", s_slot[s].loaded);
        dbg_json_int(w, "listed", s_slot[s].seen.n);
        dbg_json_int(w, "unplayed", svc_messages_unplayed(thread));
        dbg_json_obj_end(w);
    }
}

void svc_post_init(void)
{
    svc_debug_register("post", describe, NULL);
    /* A boot's state (the host suite boots it more than once). */
    memset(s_slot, 0, sizeof s_slot);
    for (uint8_t i = 0; i < s_ndone; i++) free(s_done[i].body);
    s_queued = s_ndone = 0;
    memset(&s_tag, 0, sizeof s_tag);
    s_tag_moved = false;
    /* Stamps a reboot interrupted: their messages stay played here and go to the box first. */
    memset(&s_stamps, 0, sizeof s_stamps);
    if (post_port_load(&s_stamps)) post_stamps_clean(&s_stamps);
    else s_stamps.n = 0;
    s_stamp_out = s_stamps_dirty = false;
    s_stamp_fails = 0;
    s_stamp_due = 0;
}

void svc_post_note(char *out, size_t len)
{
    post_port_lock();
    char tag[POST_TAG_BYTES];
    memcpy(tag, s_tag.last, sizeof tag);
    post_port_unlock();
    snprintf(out, len, "post=rq:%lu,ok:%lu,fail:%lu,new:%lu,chimes:%lu,stamps:%u,retries:%lu,tag:%s,last:%d",
             (unsigned long)s_st.asked, (unsigned long)s_st.ok, (unsigned long)s_st.failed,
             (unsigned long)s_st.arrivals, (unsigned long)s_st.chimes, (unsigned)s_stamps.n,
             (unsigned long)s_st.stamp_retries, tag[0] ? tag : "-", s_st.last);
}
