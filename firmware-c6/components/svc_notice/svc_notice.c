/* The notice service (plan §2 "The panel side"): the box's notices collected into files on
 * `storage`, queued, put on the glass by the shell's overlay, and told back to the box.
 *
 * Two tasks, one lock. The main task writes files and adds to the queue, takes the state POSTs
 * owed and deletes finished files; the UI task reads the head, answers it, hides it, and adds
 * the POSTs. Everything they share is below, touched only under notice_port_lock; files are
 * never opened under it. Nothing here holds a notice's picture or sound: they stay in its file,
 * read a stripe or a slice at a time. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "app_event.h"
#include "minutes.h"
#include "notice_port.h"
#include "notice_store.h"
#include "svc_audio_drv.h"
#include "svc_debug.h"
#include "svc_notice_drv.h"
#include "svc_notice_fake.h"
#include "svc_rtc.h"
#include "svc_settings.h"

/* The owed POSTs go to flash at most this often: an answer and its `shown` are one write. */
#define POSTS_SAVE_MS 2000
/* Finished notices whose files the main task has yet to delete: twice the open ones, since four
 * can finish and four more arrive before the main task's next pass. */
#define TRASH 8

/* One notice in the queue. Its words stay in its file; only the head's are read, once. */
typedef struct {
    uint32_t serial; /* notice_t.id */
    uint32_t seq;    /* its file's order */
    uint8_t id[16];
    uint8_t flags, img_mode;
    uint16_t hide_s, img_w, img_h, audio_ms, stripes;
    uint32_t ttl_s, stripe_at, pcm_at;
    uint32_t arrived_ms, shown_ms;
    bool shown, announced;
} entry_t;

typedef struct {
    uint32_t seq;
    uint8_t id[16];
} trash_t;

typedef struct {
    uint8_t id[16];
    uint8_t tries;
    uint32_t due_ms;
} refetch_t;

/* ---- shared, under the lock ---- */
static entry_t s_q[NOTICE_OPEN_MAX];
static uint8_t s_n;
static trash_t s_trash[TRASH];
static uint8_t s_ntrash;
static refetch_t s_refetch[NOTICE_OPEN_MAX];
static uint8_t s_nrefetch;
static bool s_want_next;
static notice_posts_t s_posts;
static bool s_post_out;
static uint8_t s_post_fails;
static uint32_t s_post_due;
static bool s_posts_dirty;
static uint32_t s_posts_changed;
static uint32_t s_serial, s_seq;
typedef struct {
    uint32_t got, torn, bad, fs_err, lost, dup, full, refetched, bytes;
    int last_status;
    char last_err[40];
    uint32_t heap_before, heap_ready, heap_low;
} fetch_stats_t;
static fetch_stats_t s_fetch;
typedef struct {
    uint32_t ok, failed, retries, dropped;
    int last_status;
} post_stats_t;
static post_stats_t s_post;
static struct {
    uint32_t arrived, shown, answered, dismissed, expired, lapsed, sounds, sound_refused, chimes;
    uint32_t restored, orphans, unreadable;
} s_st;

/* ---- the main task's ---- */
static bool s_mounted, s_mount_failed, s_started;
static uint32_t s_mount_cost, s_fs_total, s_fs_used;
static void (*s_wake)(void);

/* ---- the UI task's: the head's words, read from its file once ---- */
static notice_t s_view;
static uint32_t s_view_serial;
static uint32_t s_sound_serial; /* the notice whose sound was asked for, 0 none */

struct nfetch {
    jn1_reader_t r;
    jn1_notice_t n;
    int fd;
    bool fs_err;
    char path[NOTICE_PATH_BYTES];
    uint8_t buf[JN1_BUF_BYTES];
};

static void retry_later(const uint8_t id[16]);
static void refetch_done(const uint8_t id[16]);

static uint32_t now_ms(void)
{
    return svc_rtc_uptime_ms();
}

static void name_of(const entry_t *e, char *out)
{
    notice_file_name(e->seq, e->id, out, NOTICE_FILE_NAME);
}

static void wake(void)
{
    if (s_wake) s_wake();
}

void svc_notice_set_wake(void (*w)(void))
{
    s_wake = w;
}

/* ---- the report ---- */

void svc_notice_report(notice_report_t *out)
{
    out->accept = svc_settings_get(SET_NOTIF_ACCEPT) != 0;
    out->quiet = svc_settings_get(SET_NOTIF_QUIET) != 0;
    out->from = (uint16_t)svc_settings_get(SET_NIGHT_FROM);
    out->to = (uint16_t)svc_settings_get(SET_NIGHT_TO);
}

void svc_notice_pickup_query(bool ui_up, char *out, size_t cap)
{
    out[0] = '\0';
    if (!ui_up || !s_mounted) return;
    notice_report_t r;
    svc_notice_report(&r);
    const notice_prefs_t p = {
        .accept = r.accept,
        .quiet = r.quiet,
        .chime = svc_settings_get(SET_NOTIF_CHIME) != 0,
        .night_from = r.from,
        .night_to = r.to,
        .tz = (uint8_t)svc_settings_get(SET_TZ),
    };
    notice_pickup_query(&p, out, cap);
}

/* ---- the queue ---- */

static int find_id(const uint8_t id[16])
{
    for (uint8_t i = 0; i < s_n; i++)
        if (!memcmp(s_q[i].id, id, 16)) return i;
    return -1;
}

/* Under the lock: the entry goes, its file to the trash for the main task. */
static void drop_at(uint8_t i)
{
    if (s_ntrash < TRASH) {
        s_trash[s_ntrash].seq = s_q[i].seq;
        memcpy(s_trash[s_ntrash].id, s_q[i].id, 16);
        s_ntrash++;
    }
    memmove(s_q + i, s_q + i + 1, sizeof s_q[0] * (size_t)(s_n - i - 1));
    s_n--;
}

/* Under the lock. */
static void owe(const uint8_t id[16], notice_state_t st, uint8_t choice)
{
    char text[NOTICE_ID_TEXT];
    jn1_uuid_text(id, text);
    if (!notice_posts_add(&s_posts, text, st, choice)) {
        s_post.dropped++;
        /* The POST in flight may have been the one that gave way: it is sent again from the head. */
        s_post_out = false;
    }
    s_posts_dirty = true;
    s_posts_changed = now_ms();
}

static bool add_entry(const jn1_notice_t *n, uint32_t seq)
{
    entry_t e = {
        .seq = seq,
        .flags = n->flags,
        .img_mode = n->img_mode,
        .hide_s = n->hide_s,
        .img_w = n->img_w,
        .img_h = n->img_h,
        .audio_ms = n->audio_ms,
        .stripes = n->stripes,
        .ttl_s = n->ttl_s,
        .stripe_at = n->stripe_at,
        .pcm_at = n->pcm_at,
        .arrived_ms = now_ms(),
    };
    memcpy(e.id, n->id, 16);
    bool ok = false;
    notice_port_lock();
    if (s_n < NOTICE_OPEN_MAX && find_id(n->id) < 0) {
        e.serial = ++s_serial;
        s_q[s_n++] = e;
        ok = true;
    }
    notice_port_unlock();
    return ok;
}

uint8_t svc_notice_count(void)
{
    notice_port_lock();
    const uint8_t n = s_n;
    notice_port_unlock();
    return n;
}

bool svc_notice_urgent_waiting(void)
{
    /* Nothing on the wire says urgent (JN1 has no such flag), so no notice from the box is. */
    return false;
}

/* The head's entry, copied out. */
static bool head(entry_t *out)
{
    notice_port_lock();
    const bool any = s_n > 0;
    if (any) *out = s_q[0];
    notice_port_unlock();
    return any;
}

bool svc_notice_head(notice_t *out)
{
    entry_t e;
    while (head(&e)) {
        if (s_view_serial != e.serial) {
            char name[NOTICE_FILE_NAME];
            name_of(&e, name);
            memset(&s_view, 0, sizeof s_view);
            if (!nstore_load_text(name, &s_view)) {
                /* A file that cannot be read cannot be shown: it goes, unanswered and unshown,
                   rather than stand in front of the ones that can. */
                notice_port_lock();
                const int i = find_id(e.id);
                if (i >= 0) drop_at((uint8_t)i);
                s_st.unreadable++;
                notice_port_unlock();
                wake();
                continue;
            }
            s_view.id = e.serial;
            s_view.urgent = false;
            s_view.chime = (e.flags & JN1_FLAG_CHIME) != 0;
            s_view.hide_s = e.hide_s;
            s_view.img_mode = e.img_mode;
            s_view.img_w = e.img_w;
            s_view.img_h = e.img_h;
            s_view.stripes = e.stripes;
            s_view.audio_ms = e.audio_ms;
            s_view_serial = e.serial;
        }
        *out = s_view;
        return true;
    }
    return false;
}

/* The head leaves the glass as `st`: its sound stops, the box is told, its file goes. */
static void finish(notice_state_t st, uint8_t choice)
{
    notice_port_lock();
    if (!s_n) {
        notice_port_unlock();
        return;
    }
    const uint32_t serial = s_q[0].serial;
    owe(s_q[0].id, st, choice);
    drop_at(0);
    notice_port_unlock();
    if (s_sound_serial == serial) {
        svc_audio_notice_stop();
        s_sound_serial = 0;
    }
    if (st == NOTICE_ANSWERED) s_st.answered++;
    else if (st == NOTICE_DISMISSED) s_st.dismissed++;
    else if (st == NOTICE_EXPIRED) s_st.expired++;
    svc_debug_log("notice", "%lu %s", (unsigned long)serial, notice_state_name(st));
    wake();
}

void svc_notice_answer(uint8_t choice)
{
    notice_t n;
    if (!svc_notice_head(&n)) return;
    /* A plain notice's only button is OK: the box hears a dismissal, not an answer it never asked. */
    if (!n.n_choices) finish(NOTICE_DISMISSED, 0);
    else finish(NOTICE_ANSWERED, choice < n.n_choices ? choice : 0);
}

void svc_notice_dismiss(void)
{
    finish(NOTICE_DISMISSED, 0);
}

void svc_notice_on_glass(uint32_t id)
{
    notice_port_lock();
    const bool first = s_n && s_q[0].serial == id && !s_q[0].shown;
    entry_t e = {0};
    if (first) {
        s_q[0].shown = true;
        s_q[0].shown_ms = now_ms();
        owe(s_q[0].id, NOTICE_SHOWN, 0);
        e = s_q[0];
    }
    notice_port_unlock();
    if (!first) return;
    s_st.shown++;
    wake();
    /* Once, as it appears; never over an alarm (svc_audio refuses then), and stopped when it
       hides (finish). */
    if (e.flags & JN1_FLAG_SOUND) {
        char name[NOTICE_FILE_NAME];
        name_of(&e, name);
        if (!svc_audio_ringing() && svc_audio_notice_play(name)) {
            s_sound_serial = e.serial;
            s_st.sounds++;
        } else {
            s_st.sound_refused++;
        }
    }
}

bool svc_notice_stripe(uint32_t id, uint16_t index, uint16_t *px, uint32_t cap_px, uint16_t *y0,
                       uint16_t *rows)
{
    notice_port_lock();
    entry_t e = {0};
    bool have = false;
    for (uint8_t i = 0; i < s_n && !have; i++)
        if (s_q[i].serial == id) {
            e = s_q[i];
            have = true;
        }
    notice_port_unlock();
    if (!have || index >= e.stripes) return false;
    char name[NOTICE_FILE_NAME];
    name_of(&e, name);
    return nstore_stripe(name, e.stripe_at, index, e.img_w, px, cap_px, y0, rows);
}

/* ---- the UI task's pass ---- */

static bool night_now(void)
{
    rtc_time_t t;
    svc_rtc_now(&t);
    return t.valid && min_window_contains((uint16_t)(t.h * 60 + t.m),
                                          (uint16_t)svc_settings_get(SET_NIGHT_FROM),
                                          (uint16_t)svc_settings_get(SET_NIGHT_TO));
}

void svc_notice_service(void)
{
    const uint32_t now = now_ms();
    /* A notice the box has stopped taking answers for goes quietly: anything said now 404s. */
    bool stop_sound = false;
    notice_port_lock();
    for (uint8_t i = 0; i < s_n;) {
        if (notice_lapsed(s_q[i].arrived_ms, s_q[i].ttl_s, now)) {
            if (s_sound_serial == s_q[i].serial) stop_sound = true;
            drop_at(i);
            s_st.lapsed++;
        } else {
            i++;
        }
    }
    const bool expire = s_n && s_q[0].shown && notice_hide_due(s_q[0].shown_ms, s_q[0].hide_s, now);
    uint32_t arrived[NOTICE_OPEN_MAX];
    uint8_t n_arrived = 0;
    bool chime = false;
    for (uint8_t i = 0; i < s_n; i++)
        if (!s_q[i].announced) {
            s_q[i].announced = true;
            arrived[n_arrived++] = s_q[i].serial;
            /* A sound of its own plays as it is shown; the chime is for the rest. */
            if ((s_q[i].flags & JN1_FLAG_CHIME) && !(s_q[i].flags & JN1_FLAG_SOUND)) chime = true;
        }
    notice_port_unlock();
    if (stop_sound) {
        svc_audio_notice_stop();
        s_sound_serial = 0;
    }
    if (expire) finish(NOTICE_EXPIRED, 0);
    for (uint8_t i = 0; i < n_arrived; i++) {
        s_st.arrived++;
        app_event_t ev = {.type = APP_EV_NOTICE_ARRIVED, .at_ms = now, .u.notice_id = arrived[i]};
        app_events_post(&ev);
    }
    /* The plan's rule (notice_chime): the switch, never in the night window, and never over
       anything else using the speaker. */
    if (chime && notice_chime(svc_settings_get(SET_NOTIF_CHIME) != 0, false, night_now()) &&
        !svc_audio_talking() && !svc_audio_replying() && !svc_audio_playing() &&
        !svc_audio_ringing() && svc_audio_chime())
        s_st.chimes++;
}

/* ---- the main task: fetching ---- */

void svc_notice_waiting(const char *header)
{
    const uint8_t n = notice_waiting_count(header);
    notice_port_lock();
    s_want_next = n > 0;
    notice_port_unlock();
}

void svc_notice_none_waiting(void)
{
    notice_port_lock();
    s_want_next = false;
    notice_port_unlock();
}

bool svc_notice_want_fetch(char *path, size_t cap)
{
    if (!s_mounted) return false;
    const uint32_t now = now_ms();
    bool want = false;
    notice_port_lock();
    for (uint8_t i = 0; i < s_nrefetch && !want; i++)
        if ((int32_t)(now - s_refetch[i].due_ms) >= 0) {
            char id[NOTICE_ID_TEXT];
            jn1_uuid_text(s_refetch[i].id, id);
            notice_body_path(id, path, cap);
            want = true;
        }
    /* Room for one more: the box holds a fifth back anyway (429 busy), but a panel holding
       stale ones must not take a notice it has nowhere to keep. */
    if (!want && s_want_next && s_n + s_nrefetch < NOTICE_OPEN_MAX) {
        snprintf(path, cap, "%s", NOTICE_NEXT_PATH);
        want = true;
    }
    notice_port_unlock();
    return want;
}

static void fetch_err(const char *why)
{
    notice_port_lock();
    snprintf(s_fetch.last_err, sizeof s_fetch.last_err, "%s", why);
    notice_port_unlock();
}

nfetch_t *svc_notice_fetch_begin(const char *path)
{
    if (!s_mounted) return NULL;
    nfetch_t *f = malloc(sizeof *f);
    if (!f) {
        fetch_err("no memory");
        return NULL;
    }
    f->fd = nstore_part_open();
    if (f->fd < 0) {
        free(f);
        fetch_err("storage would not open a file");
        return NULL;
    }
    f->fs_err = false;
    snprintf(f->path, sizeof f->path, "%s", path);
    jn1_init(&f->r, f->buf, &f->n);
    return f;
}

size_t svc_notice_fetch_room(nfetch_t *f, uint8_t **at)
{
    return f->fs_err ? 0 : jn1_room(&f->r, at);
}

bool svc_notice_fetch_ended(const nfetch_t *f)
{
    return jn1_ended(&f->r);
}

static int hexval(char c)
{
    return c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : -1;
}

/* The id in `/endpoint/notice/<id>/body`, or false for any other path. */
static bool body_id(const char *path, uint8_t id[16])
{
    static const char PRE[] = "/endpoint/notice/";
    if (strncmp(path, PRE, sizeof PRE - 1)) return false;
    const char *p = path + sizeof PRE - 1;
    for (int i = 0; i < 16; i++) {
        if (*p == '-') p++;
        const int hi = hexval(p[0]), lo = hi < 0 ? -1 : hexval(p[1]);
        if (lo < 0) return false;
        id[i] = (uint8_t)(hi << 4 | lo);
        p += 2;
    }
    return !strcmp(p, "/body");
}

void svc_notice_fetch_failed(const char *path, int status)
{
    uint8_t id[16];
    notice_port_lock();
    s_fetch.last_status = status;
    snprintf(s_fetch.last_err, sizeof s_fetch.last_err, "%s", status < 0 ? "no answer" : "not a stream");
    if (body_id(path, id)) {
        if (status == 404) {
            refetch_done(id);
            s_fetch.lost++;
        } else if (status < 0) {
            /* No box at all is an outage, not this notice's fault: it waits without spending a
               try, so a radio that drops for a minute does not use up all three. */
            for (uint8_t i = 0; i < s_nrefetch; i++)
                if (!memcmp(s_refetch[i].id, id, 16)) s_refetch[i].due_ms = now_ms() + 30000;
        } else {
            retry_later(id);
        }
    }
    notice_port_unlock();
}

void svc_notice_fetch_trailing(nfetch_t *f)
{
    jn1_ev_t ev;
    const uint8_t one = 0;
    jn1_feed(&f->r, &one, 1, &ev);
}

bool svc_notice_fetch_took(nfetch_t *f, size_t n)
{
    const jn1_ev_t ev = jn1_took(&f->r, n);
    if (ev == JN1_EV_BAD) return false;
    if (ev == JN1_EV_RECORD || ev == JN1_EV_END) {
        if (!nstore_part_write(f->fd, f->r.rec, f->r.rec_n)) {
            f->fs_err = true;
            return false;
        }
    }
    return true;
}

/* Under the lock: a torn or refused stream is fetched again from its body, a bounded number of
   times, then given up (the box expires it at its deadline). */
static void retry_later(const uint8_t id[16])
{
    int at = -1;
    for (uint8_t i = 0; i < s_nrefetch; i++)
        if (!memcmp(s_refetch[i].id, id, 16)) at = i;
    if (at < 0) {
        if (s_nrefetch == NOTICE_OPEN_MAX) {
            s_fetch.lost++;
            return;
        }
        at = s_nrefetch++;
        memcpy(s_refetch[at].id, id, 16);
        s_refetch[at].tries = 0;
    }
    refetch_t *r = &s_refetch[at];
    if (r->tries >= NOTICE_REFETCH_TRIES) {
        memmove(s_refetch + at, s_refetch + at + 1, sizeof s_refetch[0] * (size_t)(s_nrefetch - at - 1));
        s_nrefetch--;
        s_fetch.lost++;
        return;
    }
    r->tries++;
    r->due_ms = now_ms() + notice_refetch_ms(r->tries);
    s_fetch.refetched++;
}

static void refetch_done(const uint8_t id[16])
{
    for (uint8_t i = 0; i < s_nrefetch; i++)
        if (!memcmp(s_refetch[i].id, id, 16)) {
            memmove(s_refetch + i, s_refetch + i + 1, sizeof s_refetch[0] * (size_t)(s_nrefetch - i - 1));
            s_nrefetch--;
            return;
        }
}

static void fs_refresh(void)
{
    uint32_t total = 0, used = 0;
    if (!notice_port_fs_info(&total, &used)) return;
    notice_port_lock();
    s_fs_total = total;
    s_fs_used = used;
    notice_port_unlock();
}

bool svc_notice_fetch_end(nfetch_t *f, bool whole, int status)
{
    if (!f) return false;
    /* END checked is the whole notice: a connection that drops on the chunked terminator after
       it lost nothing. `whole` only names how a stream without END stopped. */
    (void)whole;
    const bool ended = jn1_ended(&f->r);
    const bool ok = ended && !f->fs_err && !f->r.bad;
    const bool knows_id = f->r.meta;
    char name[NOTICE_FILE_NAME];
    bool queued = false, committed = false;
    if (ok) {
        notice_port_lock();
        const uint32_t seq = ++s_seq;
        notice_port_unlock();
        notice_file_name(seq, f->n.id, name, sizeof name);
        committed = nstore_part_commit(f->fd, name);
        if (committed) queued = add_entry(&f->n, seq);
        if (committed && !queued) nstore_remove(name);
    } else {
        nstore_part_discard(f->fd);
    }
    notice_port_lock();
    s_fetch.last_status = status;
    s_fetch.bytes += f->r.at + f->r.rec_n;
    if (queued) {
        s_fetch.got++;
        refetch_done(f->n.id);
    } else if (ok && committed) {
        /* Already here, or no room: the copy on the glass (or the four ahead of it) stand. */
        if (find_id(f->n.id) >= 0) s_fetch.dup++;
        else s_fetch.full++;
        refetch_done(f->n.id);
    } else {
        const char *why = f->fs_err || (ok && !committed) ? "storage refused the write"
                          : f->r.bad                       ? f->r.why
                                                           : "the stream stopped short";
        snprintf(s_fetch.last_err, sizeof s_fetch.last_err, "%s", why);
        if (f->fs_err || (ok && !committed)) s_fetch.fs_err++;
        else if (f->r.bad) s_fetch.bad++;
        else s_fetch.torn++;
        if (knows_id) retry_later(f->n.id);
        else s_fetch.lost++;
    }
    notice_port_unlock();
    free(f);
    fs_refresh();
    return queued;
}

void svc_notice_fetch_mem(uint32_t before, uint32_t ready, uint32_t low)
{
    notice_port_lock();
    s_fetch.heap_before = before;
    s_fetch.heap_ready = ready;
    s_fetch.heap_low = low;
    notice_port_unlock();
}

/* ---- the main task: what the box is owed, and the files ---- */

bool svc_notice_post_take(char *path, size_t pcap, char *json, size_t jcap)
{
    bool take = false;
    notice_port_lock();
    if (s_posts.n && !s_post_out && (int32_t)(now_ms() - s_post_due) >= 0) {
        const notice_post_t p = s_posts.p[0];
        notice_state_path(p.id, path, pcap);
        notice_state_json((notice_state_t)p.state, p.choice, json, jcap);
        s_post_out = true;
        take = true;
    }
    notice_port_unlock();
    return take;
}

void svc_notice_post_done(int status)
{
    notice_port_lock();
    s_post.last_status = status;
    if (s_post_out) {
        s_post_out = false;
        if (notice_post_done(status)) {
            notice_posts_pop(&s_posts);
            s_post_fails = 0;
            s_post_due = 0;
            s_posts_dirty = true;
            s_posts_changed = now_ms() - POSTS_SAVE_MS;
            if (status >= 200 && status < 300) s_post.ok++;
            else s_post.failed++;
        } else {
            if (s_post_fails < 255) s_post_fails++;
            s_post.retries++;
            s_post_due = now_ms() + notice_backoff_ms(s_post_fails);
        }
    }
    notice_port_unlock();
}

void svc_notice_housekeep(void)
{
    bool removed = false;
    for (;;) {
        notice_port_lock();
        const bool any = s_ntrash > 0;
        trash_t t = {0};
        if (any) {
            t = s_trash[0];
            memmove(s_trash, s_trash + 1, sizeof s_trash[0] * (size_t)(s_ntrash - 1));
            s_ntrash--;
        }
        notice_port_unlock();
        if (!any) break;
        char name[NOTICE_FILE_NAME];
        notice_file_name(t.seq, t.id, name, sizeof name);
        nstore_remove(name);
        removed = true;
    }
    if (removed) fs_refresh();
    notice_port_lock();
    const bool save = s_posts_dirty && now_ms() - s_posts_changed >= POSTS_SAVE_MS;
    notice_posts_t copy = s_posts;
    if (save) s_posts_dirty = false;
    notice_port_unlock();
    if (save) notice_port_save(&copy);
}

typedef struct {
    bool (*sink)(const uint8_t *, uint32_t, void *);
    void *ctx;
} sink_t;

bool svc_notice_pcm_stream(const char *key, uint8_t *buf, size_t cap,
                           bool (*sink)(const uint8_t *pcm, uint32_t n, void *ctx), void *ctx)
{
    uint32_t seq;
    uint8_t id[16];
    if (!notice_file_parse(key, &seq, id)) return false;
    notice_port_lock();
    const int i = find_id(id);
    const uint32_t at = i >= 0 ? s_q[i].pcm_at : 0;
    notice_port_unlock();
    return at && nstore_pcm(key, at, buf, cap, sink, ctx);
}

/* ---- start: mount, sweep, restore ---- */

/* A whole file, checked again the way it was checked arriving. */
static bool check_file(const char *name, uint8_t *buf, jn1_notice_t *n)
{
    const int fd = nstore_open(name);
    if (fd < 0) return false;
    jn1_reader_t r;
    jn1_init(&r, buf, n);
    bool ok = true;
    while (ok && !jn1_ended(&r)) {
        uint8_t *at;
        const size_t room = jn1_room(&r, &at);
        const ssize_t k = room ? read(fd, at, room) : 0;
        ok = k > 0 && jn1_took(&r, (size_t)k) != JN1_EV_BAD;
    }
    uint8_t more;
    ok = ok && read(fd, &more, 1) == 0;
    close(fd);
    return ok;
}

typedef struct {
    char name[NOTICE_FILE_NAME];
    uint32_t seq;
} found_t;

/* Whether `name` is a whole notice this panel should show again after a reboot: one it wrote,
   whole, and not one that ended before the reboot with only its POST still owed. */
static bool worth_keeping(const char *name, uint8_t *buf, jn1_notice_t *n, uint32_t *seq)
{
    uint8_t id[16];
    char text[NOTICE_ID_TEXT];
    if (strlen(name) >= NOTICE_FILE_NAME || !notice_file_parse(name, seq, id)) return false;
    if (!check_file(name, buf, n) || memcmp(n->id, id, 16)) return false;
    jn1_uuid_text(id, text);
    return !notice_posts_final(&s_posts, text);
}

/* What a reboot left: anything half-written, finished or not ours is deleted; the whole notices
   it interrupted are queued again in the order they came, the oldest four. */
static void restore(void)
{
    uint8_t *buf = malloc(JN1_BUF_BYTES + sizeof(jn1_notice_t));
    if (!buf) return;
    jn1_notice_t *n = (jn1_notice_t *)(buf + JN1_BUF_BYTES);
    char name[NOTICE_FILE_NAME + 16];
    uint32_t seq;
    /* Nothing is deleted while the directory is being listed: one name at a time. */
    for (unsigned at = 0; nstore_entry(at, name, sizeof name);) {
        if (worth_keeping(name, buf, n, &seq)) {
            at++;
        } else {
            s_st.orphans++;
            if (!nstore_remove(name)) at++;
        }
    }
    found_t keep[NOTICE_OPEN_MAX];
    uint8_t nkeep = 0;
    for (unsigned at = 0; nstore_entry(at, name, sizeof name); at++) {
        if (!notice_file_parse(name, &seq, n->id)) continue;
        if (seq > s_seq) s_seq = seq;
        uint8_t i = nkeep;
        while (i && keep[i - 1].seq > seq) i--;
        if (i == NOTICE_OPEN_MAX) continue;
        if (nkeep < NOTICE_OPEN_MAX) nkeep++;
        memmove(keep + i + 1, keep + i, sizeof keep[0] * (size_t)(nkeep - 1 - i));
        /* A name notice_file_parse took is exactly 45 characters. */
        memcpy(keep[i].name, name, strlen(name) + 1);
        keep[i].seq = seq;
    }
    for (uint8_t i = 0; i < nkeep; i++) {
        if (check_file(keep[i].name, buf, n) && add_entry(n, keep[i].seq)) s_st.restored++;
    }
    /* Past the four kept: more than the box ever lets open, so the newest go. */
    for (unsigned at = 0; nstore_entry(at, name, sizeof name);) {
        bool kept = false;
        for (uint8_t i = 0; i < nkeep && !kept; i++) kept = !strcmp(keep[i].name, name);
        if (kept) {
            at++;
            continue;
        }
        s_st.orphans++;
        if (!nstore_remove(name)) at++;
    }
    free(buf);
}

bool svc_notice_start(void)
{
    /* A boot's state (the host suite boots it more than once). */
    notice_port_lock();
    s_n = s_ntrash = s_nrefetch = 0;
    s_want_next = s_post_out = s_posts_dirty = false;
    s_post_fails = 0;
    s_post_due = 0;
    memset(&s_posts, 0, sizeof s_posts);
    notice_port_unlock();
    s_view_serial = s_sound_serial = 0;
    s_seq = 0;
    memset(&s_st, 0, sizeof s_st);
    if (notice_port_load(&s_posts)) notice_posts_clean(&s_posts);
    else s_posts.n = 0;
    s_started = true;
    s_mounted = notice_port_mount(&s_mount_cost);
    s_mount_failed = !s_mounted;
    if (!s_mounted) return false;
    restore();
    fs_refresh();
    return true;
}

/* ---- the sim's and the debug path's notices ---- */

static bool put(int fd, uint8_t kind, const void *p, size_t n, uint32_t *at)
{
    const uint8_t h[3] = {kind, (uint8_t)n, (uint8_t)(n >> 8)};
    *at += 3 + (uint32_t)n;
    return nstore_part_write(fd, h, 3) && (!n || nstore_part_write(fd, p, n));
}

bool svc_notice_fake_push(const notice_t *src)
{
    if (!s_mounted || svc_notice_count() >= NOTICE_OPEN_MAX) return false;
    /* Written as the box would send it and read back the same way, so the sim draws through the
       panel's own file path. Its id is its number, which no box uuid can be. */
    jn1_notice_t n = {0};
    n.id[12] = (uint8_t)(src->id >> 24);
    n.id[13] = (uint8_t)(src->id >> 16);
    n.id[14] = (uint8_t)(src->id >> 8);
    n.id[15] = (uint8_t)src->id;
    n.n_choices = src->n_choices;
    n.flags = (uint8_t)((src->chime ? JN1_FLAG_CHIME : 0) | (src->n_choices ? JN1_FLAG_QUESTION : 0));
    n.hide_s = src->hide_s;
    n.ttl_s = 15 * 60;
    uint8_t meta[JN1_META_FIXED + JN1_SENDER_BYTES];
    memset(meta, 0, sizeof meta);
    memcpy(meta, n.id, 16);
    meta[16] = n.flags;
    meta[17] = (uint8_t)n.hide_s;
    meta[18] = (uint8_t)(n.hide_s >> 8);
    meta[19] = (uint8_t)n.ttl_s;
    meta[20] = (uint8_t)(n.ttl_s >> 8);
    const size_t sl = strlen(src->source);
    memcpy(meta + JN1_META_FIXED, src->source, sl);
    const int fd = nstore_part_open();
    if (fd < 0) return false;
    uint32_t at = 3;
    bool ok = nstore_part_write(fd, "JN1", 3) && put(fd, JN1_META, meta, JN1_META_FIXED + sl, &at) &&
              put(fd, JN1_TITLE, src->title, strlen(src->title), &at);
    if (ok && src->body[0]) ok = put(fd, JN1_BODY, src->body, strlen(src->body), &at);
    for (uint8_t i = 0; ok && i < src->n_choices && i < NOTICE_CHOICES_MAX; i++)
        ok = put(fd, JN1_CHOICE, src->choices[i], strlen(src->choices[i]), &at);
    const uint8_t end[5] = {0, (uint8_t)at, (uint8_t)(at >> 8), (uint8_t)(at >> 16), (uint8_t)(at >> 24)};
    uint32_t ignore = at;
    ok = ok && put(fd, JN1_END, end, sizeof end, &ignore);
    if (!ok) {
        nstore_part_discard(fd);
        return false;
    }
    notice_port_lock();
    const uint32_t seq = ++s_seq;
    notice_port_unlock();
    char name[NOTICE_FILE_NAME];
    notice_file_name(seq, n.id, name, sizeof name);
    if (!nstore_part_commit(fd, name) || !add_entry(&n, seq)) {
        nstore_remove(name);
        return false;
    }
    /* Raised now, as before: the sim steps the shell straight after. */
    notice_port_lock();
    s_q[s_n - 1].announced = true;
    const uint32_t serial = s_q[s_n - 1].serial;
    notice_port_unlock();
    s_st.arrived++;
    app_event_t ev = {.type = APP_EV_NOTICE_ARRIVED, .u.notice_id = serial};
    app_events_post(&ev);
    return true;
}

/* ---- what the box can read ---- */

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    notice_report_t r;
    svc_notice_report(&r);
    notice_port_lock();
    const uint8_t n = s_n;
    entry_t q[NOTICE_OPEN_MAX];
    memcpy(q, s_q, sizeof q);
    const fetch_stats_t fetch = s_fetch;
    const post_stats_t post = s_post;
    const uint8_t owed = s_posts.n, fails = s_post_fails, refetch = s_nrefetch, trash = s_ntrash;
    const bool want = s_want_next;
    const uint32_t total = s_fs_total, used = s_fs_used;
    notice_port_unlock();
    const uint32_t now = now_ms();
    dbg_json_int(w, "queued", n);
    dbg_json_arr_begin(w, "queue");
    for (uint8_t i = 0; i < n; i++) {
        char id[NOTICE_ID_TEXT];
        jn1_uuid_text(q[i].id, id);
        dbg_json_obj_begin(w, NULL);
        dbg_json_int(w, "n", q[i].serial);
        dbg_json_str(w, "id", id);
        dbg_json_int(w, "flags", q[i].flags);
        dbg_json_bool(w, "shown", q[i].shown);
        dbg_json_int(w, "age_s", (now - q[i].arrived_ms) / 1000);
        if (q[i].hide_s) dbg_json_int(w, "hide_s", q[i].hide_s);
        if (q[i].stripes) dbg_json_int(w, "stripes", q[i].stripes);
        if (q[i].audio_ms) dbg_json_int(w, "audio_ms", q[i].audio_ms);
        dbg_json_obj_end(w);
    }
    dbg_json_arr_end(w);
    if (n && s_view_serial == q[0].serial) {
        char id[NOTICE_ID_TEXT];
        jn1_uuid_text(q[0].id, id);
        dbg_json_obj_begin(w, "head");
        dbg_json_int(w, "n", q[0].serial);
        dbg_json_str(w, "id", id);
        dbg_json_str(w, "from", s_view.source);
        dbg_json_str(w, "title", s_view.title);
        dbg_json_int(w, "choices", s_view.n_choices);
        dbg_json_int(w, "flags", q[0].flags);
        dbg_json_obj_end(w);
    }
    dbg_json_obj_begin(w, "fs");
    dbg_json_bool(w, "mounted", s_mounted);
    if (s_mount_failed) dbg_json_bool(w, "mount_failed", true);
    dbg_json_int(w, "total_kb", total / 1024);
    dbg_json_int(w, "used_kb", used / 1024);
    dbg_json_int(w, "free_kb", (total - used) / 1024);
    dbg_json_int(w, "mount_heap", s_mount_cost);
    dbg_json_obj_end(w);
    dbg_json_obj_begin(w, "fetch");
    dbg_json_bool(w, "waiting", want);
    dbg_json_int(w, "got", fetch.got);
    dbg_json_int(w, "torn", fetch.torn);
    dbg_json_int(w, "refused", fetch.bad);
    dbg_json_int(w, "fs_errors", fetch.fs_err);
    dbg_json_int(w, "retrying", refetch);
    dbg_json_int(w, "refetched", fetch.refetched);
    dbg_json_int(w, "lost", fetch.lost);
    dbg_json_int(w, "dup", fetch.dup);
    dbg_json_int(w, "full", fetch.full);
    dbg_json_int(w, "bytes", fetch.bytes);
    dbg_json_int(w, "last_status", fetch.last_status);
    if (fetch.last_err[0]) dbg_json_str(w, "last_err", fetch.last_err);
    dbg_json_int(w, "heap_before", fetch.heap_before);
    dbg_json_int(w, "heap_ready", fetch.heap_ready);
    dbg_json_int(w, "heap_low", fetch.heap_low);
    dbg_json_obj_end(w);
    dbg_json_obj_begin(w, "state_posts");
    dbg_json_int(w, "owed", owed);
    dbg_json_int(w, "fails", fails);
    dbg_json_int(w, "ok", post.ok);
    dbg_json_int(w, "refused", post.failed);
    dbg_json_int(w, "retries", post.retries);
    dbg_json_int(w, "dropped", post.dropped);
    dbg_json_int(w, "last_status", post.last_status);
    dbg_json_obj_end(w);
    dbg_json_int(w, "trash", trash);
    dbg_json_int(w, "arrived", s_st.arrived);
    dbg_json_int(w, "shown", s_st.shown);
    dbg_json_int(w, "answered", s_st.answered);
    dbg_json_int(w, "dismissed", s_st.dismissed);
    dbg_json_int(w, "expired", s_st.expired);
    dbg_json_int(w, "lapsed", s_st.lapsed);
    dbg_json_int(w, "sounds", s_st.sounds);
    dbg_json_int(w, "sound_refused", s_st.sound_refused);
    dbg_json_int(w, "chimes", s_st.chimes);
    dbg_json_int(w, "restored", s_st.restored);
    dbg_json_int(w, "orphans", s_st.orphans);
    dbg_json_int(w, "unreadable", s_st.unreadable);
    dbg_json_obj_begin(w, "report");
    dbg_json_bool(w, "accept", r.accept);
    dbg_json_bool(w, "quiet", r.quiet);
    dbg_json_int(w, "from", r.from);
    dbg_json_int(w, "to", r.to);
    dbg_json_obj_end(w);
}

void svc_notice_init(void)
{
    svc_debug_register("notice", describe, NULL);
    /* The device starts the files on the main task before the UI; the sim and the host here. */
    if (!s_started) svc_notice_start();
}

void svc_notice_note(char *out, size_t len)
{
    notice_port_lock();
    const uint8_t n = s_n;
    const fetch_stats_t f = s_fetch;
    const post_stats_t p = s_post;
    const uint8_t owed = s_posts.n;
    notice_port_unlock();
    snprintf(out, len, "notice=q:%u,fs:%s,got:%lu,torn:%lu,bad:%lu,lost:%lu,st:%lu/%lu,owed:%u,last:%d",
             (unsigned)n, s_mounted ? "ok" : s_mount_failed ? "failed" : "off",
             (unsigned long)f.got, (unsigned long)f.torn, (unsigned long)f.bad,
             (unsigned long)f.lost, (unsigned long)p.ok, (unsigned long)(p.failed + p.retries),
             (unsigned)owed, f.last_status);
}
