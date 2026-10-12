#include "alarm_logic.h"

#include <stdio.h>
#include <string.h>

/* ---- labels and sounds ---- */

static const char *const SOUNDS[ALARM_SOUNDS] = {"chime", "beeps", "rise"};

uint8_t alarm_sound_from(const char *name)
{
    for (uint8_t i = 0; i < ALARM_SOUNDS; i++)
        if (!strcmp(name, SOUNDS[i])) return i;
    return ALARM_SOUND_CHIME;
}

const char *alarm_sound_name(uint8_t sound)
{
    return SOUNDS[sound < ALARM_SOUNDS ? sound : ALARM_SOUND_CHIME];
}

void alarm_clean_label(const char *in, char *out, size_t cap)
{
    if (!cap) return;
    size_t o = 0;
    const size_t limit = cap - 1 < ALARM_LABEL_BYTES ? cap - 1 : ALARM_LABEL_BYTES;
    for (const unsigned char *s = (const unsigned char *)in; *s && o < limit;) {
        if (*s >= 0x20 && *s < 0x7F) {
            out[o++] = (char)*s++;
            continue;
        }
        /* One code point, however many bytes, is one '?'. */
        s++;
        while ((*s & 0xC0) == 0x80) s++;
        out[o++] = '?';
    }
    out[o] = '\0';
}

/* ---- the box's JSON ---- */

typedef struct {
    const char *p, *end;
} cur_t;

static void ws(cur_t *c)
{
    while (c->p < c->end && (*c->p == ' ' || *c->p == '\n' || *c->p == '\r' || *c->p == '\t'))
        c->p++;
}

static bool eat(cur_t *c, char ch)
{
    ws(c);
    if (c->p < c->end && *c->p == ch) {
        c->p++;
        return true;
    }
    return false;
}

static bool peek(cur_t *c, char ch)
{
    ws(c);
    return c->p < c->end && *c->p == ch;
}

/* A JSON string as bytes, cut to fit. An escaped code point past ASCII becomes 0x7F, which the
 * label cleaner turns into '?': the panel draws ASCII only. */
static bool str(cur_t *c, char *out, size_t cap)
{
    if (!eat(c, '"')) return false;
    size_t o = 0;
    while (c->p < c->end && *c->p != '"') {
        unsigned cp = (unsigned char)*c->p++;
        if (cp == '\\') {
            if (c->p >= c->end) return false;
            const char e = *c->p++;
            if (e == 'u') {
                if (c->end - c->p < 4) return false;
                cp = 0;
                for (int i = 0; i < 4; i++) {
                    const char h = *c->p++;
                    const int v = h >= '0' && h <= '9'   ? h - '0'
                                  : h >= 'a' && h <= 'f' ? h - 'a' + 10
                                  : h >= 'A' && h <= 'F' ? h - 'A' + 10
                                                         : -1;
                    if (v < 0) return false;
                    cp = cp << 4 | (unsigned)v;
                }
                if (cp >= 0x80) cp = 0x7F;
            } else {
                cp = e == 'n' ? '\n' : e == 't' ? '\t' : e == 'r' ? '\r' : (unsigned char)e;
            }
        }
        if (o + 1 < cap) out[o++] = (char)cp;
    }
    if (cap) out[o] = '\0';
    return eat(c, '"');
}

static bool num(cur_t *c, int64_t *out)
{
    ws(c);
    bool neg = false;
    if (c->p < c->end && *c->p == '-') {
        neg = true;
        c->p++;
    }
    if (c->p >= c->end || *c->p < '0' || *c->p > '9') return false;
    int64_t v = 0;
    while (c->p < c->end && *c->p >= '0' && *c->p <= '9') {
        if (v < 1000000000000LL) v = v * 10 + (*c->p - '0');
        c->p++;
    }
    /* A fraction or exponent is not an id, a minute or a mask. */
    if (c->p < c->end && (*c->p == '.' || *c->p == 'e' || *c->p == 'E')) return false;
    *out = neg ? -v : v;
    return true;
}

static bool lit(cur_t *c, const char *word)
{
    ws(c);
    const size_t n = strlen(word);
    if ((size_t)(c->end - c->p) < n || memcmp(c->p, word, n)) return false;
    c->p += n;
    return true;
}

static bool boolean(cur_t *c, bool *out)
{
    if (lit(c, "true")) return *out = true, true;
    if (lit(c, "false")) return *out = false, true;
    return false;
}

static bool skip(cur_t *c, int depth)
{
    ws(c);
    if (c->p >= c->end || depth > 4) return false;
    const char ch = *c->p;
    if (ch == '"') return str(c, NULL, 0);
    if (ch == '{' || ch == '[') {
        const char close = ch == '{' ? '}' : ']';
        c->p++;
        if (eat(c, close)) return true;
        for (;;) {
            if (ch == '{' && (!str(c, NULL, 0) || !eat(c, ':'))) return false;
            if (!skip(c, depth + 1)) return false;
            if (eat(c, close)) return true;
            if (!eat(c, ',')) return false;
        }
    }
    int64_t n;
    bool b;
    return lit(c, "null") || boolean(c, &b) || num(c, &n);
}

/* One alarm. False on malformed JSON; `*keep` false for a well-formed entry the panel cannot
 * use (no id, a minute or mask out of range). */
static bool entry(cur_t *c, alarm_t *a, bool *keep)
{
    memset(a, 0, sizeof *a);
    int64_t id = -1, at = -1, days = 0;
    bool on = false;
    char label[ALARM_LABEL_BYTES * 4 + 1] = "", sound[12] = "";
    if (!eat(c, '{')) return false;
    if (!eat(c, '}')) {
        for (;;) {
            char key[12];
            if (!str(c, key, sizeof key) || !eat(c, ':')) return false;
            bool ok;
            if (!strcmp(key, "id")) ok = num(c, &id);
            else if (!strcmp(key, "at")) ok = num(c, &at);
            else if (!strcmp(key, "days")) ok = num(c, &days);
            else if (!strcmp(key, "on")) ok = boolean(c, &on);
            else if (!strcmp(key, "label") && peek(c, '"')) ok = str(c, label, sizeof label);
            else if (!strcmp(key, "sound") && peek(c, '"')) ok = str(c, sound, sizeof sound);
            else ok = skip(c, 1);
            if (!ok) return false;
            if (eat(c, '}')) break;
            if (!eat(c, ',')) return false;
        }
    }
    *keep = id > 0 && id <= (int64_t)UINT32_MAX && at >= 0 && at < 1440 && days >= 0 && days < 128;
    if (!*keep) return true;
    a->id = (uint32_t)id;
    a->h = (uint8_t)(at / 60);
    a->m = (uint8_t)(at % 60);
    a->days = (uint8_t)days;
    a->on = on;
    a->sound = alarm_sound_from(sound);
    alarm_clean_label(label, a->label, sizeof a->label);
    return true;
}

static int minute_of(const alarm_t *a)
{
    return a->h * 60 + a->m;
}

static void sort(alarm_list_t *l)
{
    for (uint8_t i = 1; i < l->n; i++)
        for (uint8_t j = i; j > 0; j--) {
            alarm_t *x = &l->a[j - 1], *y = &l->a[j];
            if (minute_of(x) < minute_of(y) || (minute_of(x) == minute_of(y) && x->id <= y->id))
                break;
            const alarm_t t = *x;
            *x = *y;
            *y = t;
        }
}

bool alarm_parse(const char *json, size_t len, alarm_list_t *out)
{
    memset(out, 0, sizeof *out);
    cur_t c = {json, json + len};
    bool found = false;
    if (!eat(&c, '{') || eat(&c, '}')) return false;
    for (;;) {
        char key[12];
        if (!str(&c, key, sizeof key) || !eat(&c, ':')) return false;
        if (!strcmp(key, "alarms")) {
            if (!eat(&c, '[')) return false;
            found = true;
            if (!eat(&c, ']')) {
                for (;;) {
                    alarm_t a;
                    bool keep;
                    if (!entry(&c, &a, &keep)) return false;
                    if (keep && out->n < ALARM_MAX) out->a[out->n++] = a;
                    if (eat(&c, ']')) break;
                    if (!eat(&c, ',')) return false;
                }
            }
        } else if (!strcmp(key, "etag") && peek(&c, '"')) {
            if (!str(&c, out->etag, sizeof out->etag)) return false;
        } else if (!skip(&c, 1)) {
            return false;
        }
        if (eat(&c, '}')) break;
        if (!eat(&c, ',')) return false;
    }
    sort(out);
    return found;
}

/* ---- when ---- */

int64_t alarm_next_at(const alarm_t *a, const tz_rule_t *z, int64_t after)
{
    if (!a->on) return -1;
    civil_t today;
    tz_local(z, after, &today);
    today.h = today.m = today.s = 0;
    const int64_t day0 = civil_to_epoch(&today) / 86400;
    /* Eight days: a weekly alarm whose day is today but whose time has passed is a week on. */
    for (int d = 0; d <= 8; d++) {
        const uint8_t wday = (uint8_t)((today.wday + d) % 7);
        if (a->days && !(a->days >> wday & 1u)) continue;
        const int64_t u = tz_wall_to_utc(z, (day0 + d) * 86400 + a->h * 3600 + a->m * 60);
        if (u > after) return u;
    }
    return -1;
}

/* ---- the schedule ---- */

void alarm_sched_init(alarm_sched_t *s)
{
    memset(s, 0, sizeof *s);
}

static const alarm_t *find(const alarm_list_t *l, uint32_t id)
{
    for (uint8_t i = 0; i < l->n; i++)
        if (l->a[i].id == id) return &l->a[i];
    return NULL;
}

static bool same_time(const alarm_t *x, const alarm_t *y)
{
    return x->h == y->h && x->m == y->m && x->days == y->days;
}

static void end_snooze(alarm_sched_t *s)
{
    if (!s->snoozed) return;
    s->snoozed = false;
    s->snooze_cancelled++;
}

bool alarm_sched_set_list(alarm_sched_t *s, const alarm_list_t *l)
{
    s->list = *l;
    if (!s->snoozed || s->snooze_of.id == 0) return false;
    const alarm_t *now = find(l, s->snooze_of.id);
    if (!now || !now->on || !same_time(now, &s->snooze_of)) {
        end_snooze(s);
        return true;
    }
    s->snooze_of = *now;
    return false;
}

bool alarm_sched_set_on(alarm_sched_t *s, uint32_t id, bool on)
{
    for (uint8_t i = 0; i < s->list.n; i++)
        if (s->list.a[i].id == id) {
            s->list.a[i].on = on;
            if (!on && s->snoozed && s->snooze_of.id == id) end_snooze(s);
            return true;
        }
    return false;
}

static void ring(alarm_sched_t *s, const alarm_t *a, int64_t now, uint32_t now_ms)
{
    if (s->ringing) s->taken_over++;
    s->ringing = true;
    s->ring = *a;
    s->ring_ms = now_ms;
    s->fired++;
    s->last_fired = now;
    s->last_fired_id = a->id;
}

bool alarm_sched_tick(alarm_sched_t *s, const tz_rule_t *z, int64_t now, uint32_t now_ms)
{
    /* The first check covers the grace window; a clock stepped back covers nothing behind it (an
       alarm is never rung twice for one occurrence); a long gap or a step forward is missed. */
    if (!s->checked) {
        s->checked = true;
        s->last = s->high = now - ALARM_GRACE_S;
    }
    if (now < s->last) {
        s->last = now;
        /* Further behind than a correction: the clock was wrong. Start over, as at boot. */
        if (s->high - now > ALARM_STEP_BACK_MAX_S) s->last = s->high = now - ALARM_GRACE_S;
    }
    int64_t from = s->last > now - ALARM_GRACE_S ? s->last : now - ALARM_GRACE_S;
    /* Everything up to the furthest instant already checked has had its chance to ring. */
    if (from < s->high) from = s->high;
    if (from > s->last && s->last >= s->high)
        for (uint8_t i = 0; i < s->list.n; i++) {
            const int64_t o = alarm_next_at(&s->list.a[i], z, s->last);
            if (o >= 0 && o <= from) s->missed++;
        }
    s->last = now;
    if (now > s->high) s->high = now;

    const alarm_t *due = NULL;
    int64_t due_at = 0;
    for (uint8_t i = 0; i < s->list.n; i++) {
        const int64_t o = alarm_next_at(&s->list.a[i], z, from);
        if (o >= 0 && o <= now && (!due || o < due_at)) {
            due = &s->list.a[i];
            due_at = o;
        }
    }
    /* One ring per check, the earliest; the check then covers only up to it, so a second alarm
       due in the same window rings on the next pass rather than being lost. */
    if (s->snoozed && s->snooze_until > from && s->snooze_until <= now &&
        (!due || s->snooze_until <= due_at)) {
        s->snoozed = false;
        s->last = s->high = s->snooze_until;
        const alarm_t again = s->snooze_of;
        ring(s, &again, now, now_ms);
        return true;
    }
    if (!due) return false;
    s->last = s->high = due_at;
    /* The alarm already ringing is not started again by its own next occurrence. */
    if (s->ringing && s->ring.id == due->id) return false;
    const alarm_t a = *due;
    ring(s, &a, now, now_ms);
    return true;
}

/* A one-off has done its job once it is answered: it switches itself off (mock README). */
static void finish(alarm_sched_t *s, int64_t now, alarm_end_t why, uint32_t *off_id)
{
    *off_id = 0;
    if (!s->ringing) return;
    const alarm_t a = s->ring;
    s->ringing = false;
    s->last_end = why;
    if (why == ALARM_END_STOPPED) {
        s->dismissed++;
        s->last_dismissed = now;
    }
    if (s->snoozed && s->snooze_of.id == a.id) s->snoozed = false;
    if (a.id && a.days == 0 && alarm_sched_set_on(s, a.id, false)) *off_id = a.id;
}

bool alarm_sched_cap(alarm_sched_t *s, int64_t now, uint32_t now_ms, uint32_t *off_id)
{
    *off_id = 0;
    if (!s->ringing || (uint32_t)(now_ms - s->ring_ms) < ALARM_RING_CAP_MS) return false;
    s->timed_out++;
    finish(s, now, ALARM_END_TIMED_OUT, off_id);
    return true;
}

int64_t alarm_sched_snooze(alarm_sched_t *s, int64_t now)
{
    if (!s->ringing) return -1;
    const alarm_t a = s->ring;
    s->ringing = false;
    s->last_end = ALARM_END_SNOOZED;
    s->snoozed = true;
    s->snooze_of = a;
    s->snooze_until = now + ALARM_SNOOZE_MIN * 60;
    s->snoozes++;
    s->last_snoozed = now;
    return s->snooze_until;
}

void alarm_sched_stop(alarm_sched_t *s, int64_t now, uint32_t *off_id)
{
    finish(s, now, ALARM_END_STOPPED, off_id);
}

bool alarm_sched_next(const alarm_sched_t *s, const tz_rule_t *z, int64_t now, alarm_t *a,
                      int64_t *at, bool *snooze)
{
    bool found = false;
    if (s->snoozed && s->snooze_until > now) {
        *a = s->snooze_of;
        *at = s->snooze_until;
        *snooze = found = true;
    }
    for (uint8_t i = 0; i < s->list.n; i++) {
        const int64_t o = alarm_next_at(&s->list.a[i], z, now);
        if (o < 0 || (found && o >= *at)) continue;
        *a = s->list.a[i];
        *at = o;
        *snooze = false;
        found = true;
    }
    return found;
}

void alarm_sched_test(alarm_sched_t *s, uint8_t sound, const tz_rule_t *z, int64_t now,
                      uint32_t now_ms)
{
    civil_t c;
    tz_local(z, now, &c);
    alarm_t a = {.id = 0, .h = c.h, .m = c.m, .on = true, .sound = sound < ALARM_SOUNDS ? sound : 0};
    snprintf(a.label, sizeof a.label, "Test alarm");
    ring(s, &a, now, now_ms);
}

/* ---- the switch queue ---- */

void alarm_queue_put(alarm_queue_t *q, uint32_t id, bool on)
{
    int slot = -1, oldest = 0;
    for (int i = 0; i < ALARM_MAX; i++) {
        if (q->q[i].used && q->q[i].id == id) {
            slot = i;
            break;
        }
        if (slot < 0 && !q->q[i].used) slot = i;
        if (q->q[i].seq < q->q[oldest].seq) oldest = i;
    }
    /* Full of other alarms' switches, which cannot happen with ALARM_MAX alarms unless the box
       deleted some meanwhile: the oldest gives way. */
    if (slot < 0) slot = oldest;
    q->q[slot] = (alarm_put_t){.id = id, .on = on, .used = true, .seq = ++q->seq};
}

bool alarm_queue_peek(const alarm_queue_t *q, alarm_put_t *out)
{
    const alarm_put_t *best = NULL;
    for (int i = 0; i < ALARM_MAX; i++)
        if (q->q[i].used && (!best || q->q[i].seq < best->seq)) best = &q->q[i];
    if (best) *out = *best;
    return best != NULL;
}

void alarm_queue_done(alarm_queue_t *q, const alarm_put_t *sent, int status)
{
    const bool settled = (status >= 200 && status < 300) || status == 404;
    if (!settled) return;
    for (int i = 0; i < ALARM_MAX; i++)
        if (q->q[i].used && q->q[i].id == sent->id && q->q[i].seq == sent->seq) q->q[i].used = false;
}

uint8_t alarm_queue_count(const alarm_queue_t *q)
{
    uint8_t n = 0;
    for (int i = 0; i < ALARM_MAX; i++) n += q->q[i].used;
    return n;
}

void alarm_queue_overlay(const alarm_queue_t *q, alarm_list_t *l)
{
    for (int i = 0; i < ALARM_MAX; i++) {
        if (!q->q[i].used) continue;
        for (uint8_t k = 0; k < l->n; k++)
            if (l->a[k].id == q->q[i].id) l->a[k].on = q->q[i].on;
    }
}
