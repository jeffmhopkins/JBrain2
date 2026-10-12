/* The conversations service: what the glass asked of the box, and the box's answers applied on
 * the UI task. Requests and answers cross between the tasks under the port's lock; everything
 * else here is the UI task's alone. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_event.h"
#include "svc_audio.h"
#include "svc_convo.h"
#include "svc_convo_drv.h"
#include "svc_debug.h"
#include "svc_messages_fake.h"

/* Requests waiting for the main task, and answers waiting for the UI task. A new request of the
 * same kind for the same agent replaces the one still waiting, so a burst of asks is one fetch. */
#define QUEUE 4
/* A switched thread's bubbles take ids from here up: past any the talks will reach (they count
 * up by two from 1000), and below the people's placeholders (900000). */
#define SWITCH_ID0 500000u

enum { A_JERV, A_DAVE, A_N };

static struct {
    char title[CONVO_TITLE_BYTES];
    bool known;   /* the box has said what the title is */
    bool earlier; /* a switch left older messages in the app */
} s_a[A_N];
static uint32_t s_rev;

static void (*s_wake)(void);
static convo_req_t s_queue[QUEUE];
static uint8_t s_queued;
static uint32_t s_seq;
typedef struct {
    convo_req_t req;
    int status;
    char *body;
    size_t len;
} done_t;
static done_t s_done[QUEUE];
static uint8_t s_ndone;

/* The UI task's own. */
static convo_list_t *s_list;
static convo_state_t s_list_state;
static uint32_t s_list_seq;
static convo_state_t s_switch_state;
static uint32_t s_switch_seq;
static uint32_t s_next_id = SWITCH_ID0;
static struct {
    uint32_t asked, ok, failed, stale;
    int last;
} s_st;

static int agent_ix(const char *agent)
{
    if (agent && !strcmp(agent, "jerv")) return A_JERV;
    if (agent && !strcmp(agent, "dave")) return A_DAVE;
    return -1;
}

static const char *agent_id(int a)
{
    return a == A_JERV ? "jerv" : "dave";
}

/* Something the agent's conversation view draws has changed: it hears it as its thread's
 * MSG_ARRIVED, the event it already redraws on. */
static void changed(int a)
{
    s_rev++;
    app_event_t ev = {.type = APP_EV_MSG_ARRIVED};
    snprintf(ev.u.principal, sizeof ev.u.principal, "%s", agent_id(a));
    app_events_post(&ev);
}

void svc_convo_set_wake(void (*wake)(void))
{
    s_wake = wake;
}

static uint32_t ask(convo_rq_t kind, const char *agent, const char *id)
{
    convo_req_t r = {.kind = (uint8_t)kind};
    snprintf(r.agent, sizeof r.agent, "%s", agent);
    if (id) snprintf(r.id, sizeof r.id, "%s", id);
    convo_port_lock();
    r.seq = ++s_seq;
    uint8_t at = s_queued;
    for (uint8_t i = 0; i < s_queued; i++)
        if (s_queue[i].kind == kind && !strcmp(s_queue[i].agent, r.agent)) at = i;
    if (at == QUEUE) {
        /* Full: the oldest gives way (its view has long since moved on). */
        memmove(s_queue, s_queue + 1, sizeof s_queue[0] * (QUEUE - 1));
        at = QUEUE - 1;
    } else if (at == s_queued) {
        s_queued++;
    }
    s_queue[at] = r;
    convo_port_unlock();
    s_st.asked++;
    if (s_wake) s_wake();
    return r.seq;
}

bool svc_convo_take(convo_req_t *out)
{
    convo_port_lock();
    const bool any = s_queued > 0;
    if (any) {
        *out = s_queue[0];
        memmove(s_queue, s_queue + 1, sizeof s_queue[0] * (QUEUE - 1));
        s_queued--;
    }
    convo_port_unlock();
    return any;
}

void svc_convo_done(const convo_req_t *req, int status, char *body, size_t len)
{
    char *drop = NULL;
    convo_port_lock();
    if (s_ndone == QUEUE) {
        drop = s_done[0].body;
        memmove(s_done, s_done + 1, sizeof s_done[0] * (QUEUE - 1));
        s_ndone--;
    }
    s_done[s_ndone++] = (done_t){*req, status, body, len};
    convo_port_unlock();
    free(drop);
}

/* ---- the glass ---- */

bool svc_convo_recent_open(const char *agent)
{
    svc_convo_recent_close();
    if (agent_ix(agent) < 0) return false;
    s_list = calloc(1, sizeof *s_list);
    if (!s_list) return false;
    s_list_state = CONVO_WAITING;
    s_list_seq = ask(CONVO_RQ_RECENT, agent, NULL);
    return true;
}

convo_state_t svc_convo_recent(const convo_list_t **out)
{
    if (out) *out = s_list_state == CONVO_READY ? s_list : NULL;
    return s_list ? s_list_state : CONVO_IDLE;
}

void svc_convo_recent_close(void)
{
    free(s_list);
    s_list = NULL;
    s_list_state = CONVO_IDLE;
    s_list_seq = 0;
}

void svc_convo_switch(const char *agent, const char *id)
{
    if (agent_ix(agent) < 0) return;
    s_switch_state = CONVO_WAITING;
    s_switch_seq = ask(CONVO_RQ_SWITCH, agent, id);
    s_rev++;
}

convo_state_t svc_convo_switching(void)
{
    return s_switch_state;
}

const char *svc_convo_title(const char *agent)
{
    const int a = agent_ix(agent);
    return a >= 0 && s_a[a].known ? s_a[a].title : NULL;
}

void svc_convo_refresh_title(const char *agent)
{
    if (agent_ix(agent) >= 0) ask(CONVO_RQ_TITLE, agent, NULL);
}

bool svc_convo_earlier(const char *agent)
{
    const int a = agent_ix(agent);
    return a >= 0 && s_a[a].earlier;
}

static void forget(int a)
{
    if (!s_a[a].known && !s_a[a].earlier && !s_a[a].title[0]) return;
    s_a[a].known = s_a[a].earlier = false;
    s_a[a].title[0] = '\0';
    changed(a);
}

void svc_convo_new(const char *agent)
{
    const int a = agent_ix(agent);
    if (a >= 0) forget(a);
}

uint32_t svc_convo_rev(void)
{
    return s_rev;
}

/* ---- applying the box's answers ---- */

typedef struct {
    const char *agent;
} fill_t;

static void add_msg(void *user, bool mine, const char *text)
{
    const fill_t *f = user;
    msg_t m = {.id = s_next_id++, .mine = mine, .played = true};
    convo_fit(m.text, sizeof m.text, text, strlen(text));
    /* The newest answer is kept whole: each answer replaces the one before, so the last wins. */
    if (!mine) svc_messages_fake_long(f->agent, m.id, text);
    svc_messages_fake_add(f->agent, &m);
}

static void apply_switch(const convo_req_t *r, int status, const char *body, size_t len)
{
    const int a = agent_ix(r->agent);
    if (status != 200) {
        s_switch_state = CONVO_FAILED;
        changed(a);
        return;
    }
    /* The box has switched: from here the agent's next talk continues that conversation. */
    svc_audio_continue(r->agent);
    svc_messages_clear(r->agent);
    char *scratch = body ? malloc(MSG_LONG_TEXT) : NULL;
    char title[CONVO_TITLE_BYTES];
    bool more = true;
    fill_t f = {r->agent};
    const bool read = scratch && convo_parse_thread(body, len, title, sizeof title, &more, scratch,
                                                    MSG_LONG_TEXT, add_msg, &f);
    free(scratch);
    if (read) {
        snprintf(s_a[a].title, sizeof s_a[a].title, "%s", title);
        s_a[a].known = true;
    } else {
        /* Switched, but the messages did not come: the title on the next ask, the rest in the
           app. */
        s_a[a].known = false;
        s_a[a].title[0] = '\0';
        more = true;
        ask(CONVO_RQ_TITLE, r->agent, NULL);
    }
    s_a[a].earlier = more;
    s_switch_state = CONVO_READY;
    changed(a);
}

static void ignore_msg(void *user, bool mine, const char *text)
{
    (void)user;
    (void)mine;
    (void)text;
}

static void apply_title(const convo_req_t *r, int status, const char *body, size_t len)
{
    const int a = agent_ix(r->agent);
    char title[CONVO_TITLE_BYTES], scratch[4];
    bool more;
    if (status != 200 || !body ||
        !convo_parse_thread(body, len, title, sizeof title, &more, scratch, sizeof scratch,
                            ignore_msg, NULL))
        return;
    /* A new conversation asked for since: the old one's title is not this one's. */
    if (svc_audio_fresh_pending(r->agent)) return;
    if (s_a[a].known && !strcmp(s_a[a].title, title)) return;
    snprintf(s_a[a].title, sizeof s_a[a].title, "%s", title);
    s_a[a].known = true;
    changed(a);
}

static void apply(const convo_req_t *r, int status, const char *body, size_t len)
{
    s_st.last = status;
    if (status == 200) s_st.ok++;
    else s_st.failed++;
    switch (r->kind) {
    case CONVO_RQ_RECENT:
        if (r->seq != s_list_seq || !s_list) {
            s_st.stale++;
            return;
        }
        s_list_state = status == 200 && body && convo_parse_recent(body, len, s_list) ? CONVO_READY
                                                                                     : CONVO_FAILED;
        changed(agent_ix(r->agent));
        return;
    case CONVO_RQ_SWITCH:
        if (r->seq != s_switch_seq) {
            s_st.stale++;
            return;
        }
        apply_switch(r, status, body, len);
        return;
    case CONVO_RQ_TITLE: apply_title(r, status, body, len); return;
    default: return;
    }
}

void svc_convo_service(void)
{
    /* A new conversation asked for anywhere (the menu, or KEY outside the thread): what the old
       one was called, and that it had earlier messages, go with it. */
    for (int a = 0; a < A_N; a++)
        if (svc_audio_fresh_pending(agent_id(a))) forget(a);
    for (;;) {
        convo_port_lock();
        const bool any = s_ndone > 0;
        done_t d;
        if (any) {
            d = s_done[0];
            memmove(s_done, s_done + 1, sizeof s_done[0] * (QUEUE - 1));
            s_ndone--;
        }
        convo_port_unlock();
        if (!any) return;
        apply(&d.req, d.status, d.body, d.len);
        free(d.body);
    }
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    static const char *const STATE[] = {"idle", "waiting", "ready", "failed"};
    dbg_json_int(w, "queued", s_queued);
    dbg_json_str(w, "recent", STATE[s_list ? s_list_state : CONVO_IDLE]);
    dbg_json_int(w, "rows", s_list && s_list_state == CONVO_READY ? s_list->n : 0);
    dbg_json_str(w, "switch", STATE[s_switch_state]);
    for (int a = 0; a < A_N; a++) {
        dbg_json_obj_begin(w, agent_id(a));
        dbg_json_bool(w, "title_known", s_a[a].known);
        dbg_json_str(w, "title", s_a[a].title);
        dbg_json_bool(w, "earlier", s_a[a].earlier);
        dbg_json_obj_end(w);
    }
}

void svc_convo_init(void)
{
    svc_debug_register("convo", describe, NULL);
}

void svc_convo_note(char *out, size_t len)
{
    snprintf(out, len, "convo=rq:%lu,ok:%lu,fail:%lu,stale:%lu,last:%d", (unsigned long)s_st.asked,
             (unsigned long)s_st.ok, (unsigned long)s_st.failed, (unsigned long)s_st.stale, s_st.last);
}
