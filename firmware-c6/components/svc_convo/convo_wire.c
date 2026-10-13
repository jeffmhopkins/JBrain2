#include "convo_wire.h"

#include <stdio.h>
#include <string.h>

static const char ELLIPSIS[] = "\xE2\x80\xA6";

bool convo_agent_ok(const char *agent)
{
    return agent && (!strcmp(agent, "jerv") || !strcmp(agent, "dave"));
}

void convo_path(convo_rq_t kind, const char *agent, bool messages, char *out, size_t cap)
{
    const char *tail = kind == CONVO_RQ_RECENT                ? "recent"
                       : kind == CONVO_RQ_SWITCH && !messages ? "select"
                       : kind == CONVO_RQ_TITLE               ? "messages?limit=0"
                                                              : "messages?limit=" CONVO_STR(CONVO_PAIRS);
    snprintf(out, cap, "/endpoint/convo/%s/%s", agent, tail);
}

bool convo_select_body(const char *id, char *out, size_t cap)
{
    if (!id || strlen(id) != CONVO_ID_BYTES - 1) return false;
    for (const char *p = id; *p; p++)
        if (!((*p >= '0' && *p <= '9') || (*p >= 'a' && *p <= 'f') || *p == '-')) return false;
    return snprintf(out, cap, "{\"session_id\":\"%s\"}", id) < (int)cap;
}

/* ---- a small JSON reader: the box's answers and nothing more general ---- */

typedef struct {
    const char *p, *end;
} cur_t;

static void ws(cur_t *c)
{
    while (c->p < c->end && (*c->p == ' ' || *c->p == '\n' || *c->p == '\r' || *c->p == '\t')) c->p++;
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

static int hex(char h)
{
    if (h >= '0' && h <= '9') return h - '0';
    if (h >= 'a' && h <= 'f') return h - 'a' + 10;
    if (h >= 'A' && h <= 'F') return h - 'A' + 10;
    return -1;
}

static bool u16(cur_t *c, uint32_t *out)
{
    if (c->end - c->p < 4) return false;
    uint32_t v = 0;
    for (int i = 0; i < 4; i++) {
        const int h = hex(c->p[i]);
        if (h < 0) return false;
        v = v << 4 | (uint32_t)h;
    }
    c->p += 4;
    *out = v;
    return true;
}

static size_t utf8(uint32_t cp, char *b)
{
    if (cp < 0x80) return b[0] = (char)cp, 1;
    if (cp < 0x800) {
        b[0] = (char)(0xC0 | cp >> 6);
        b[1] = (char)(0x80 | (cp & 0x3F));
        return 2;
    }
    if (cp < 0x10000) {
        b[0] = (char)(0xE0 | cp >> 12);
        b[1] = (char)(0x80 | (cp >> 6 & 0x3F));
        b[2] = (char)(0x80 | (cp & 0x3F));
        return 3;
    }
    b[0] = (char)(0xF0 | cp >> 18);
    b[1] = (char)(0x80 | (cp >> 12 & 0x3F));
    b[2] = (char)(0x80 | (cp >> 6 & 0x3F));
    b[3] = (char)(0x80 | (cp & 0x3F));
    return 4;
}

/* The bytes of one character starting at a raw lead byte (1 for a stray continuation byte, which
 * is then kept as it came: the box sends valid UTF-8). */
static size_t seq_len(unsigned char lead)
{
    return lead >= 0xF0 ? 4 : lead >= 0xE0 ? 3 : lead >= 0xC0 ? 2 : 1;
}

/* Cut back to a character boundary with room for "…", and add it. */
static void ellipsis(char *dst, size_t cap, size_t o)
{
    if (cap < sizeof ELLIPSIS) {
        if (cap) dst[0] = '\0';
        return;
    }
    const size_t written = o;
    while (o > cap - sizeof ELLIPSIS) o--;
    /* `written` is a boundary by construction; anything before it may be mid-character. */
    while (o > 0 && o < written && ((unsigned char)dst[o] & 0xC0) == 0x80) o--;
    memcpy(dst + o, ELLIPSIS, sizeof ELLIPSIS);
}

/* A string into `dst` (NULL: skipped), whole or cut with "…". The cursor always ends past it. */
static bool str(cur_t *c, char *dst, size_t cap)
{
    if (!eat(c, '"')) return false;
    size_t o = 0;
    bool cut = false;
    while (c->p < c->end && *c->p != '"') {
        char b[8];
        size_t n;
        if (*c->p == '\\') {
            c->p++;
            if (c->p >= c->end) return false;
            const char e = *c->p++;
            if (e == 'u') {
                uint32_t cp;
                if (!u16(c, &cp)) return false;
                if (cp >= 0xD800 && cp < 0xDC00) {
                    uint32_t lo;
                    if (c->end - c->p >= 6 && c->p[0] == '\\' && c->p[1] == 'u') {
                        c->p += 2;
                        if (!u16(c, &lo)) return false;
                        cp = lo >= 0xDC00 && lo < 0xE000 ? 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)
                                                         : '?';
                    } else {
                        cp = '?';
                    }
                } else if (cp >= 0xDC00 && cp < 0xE000) {
                    cp = '?';
                }
                /* A control character is a space on a one-line row or a bubble. */
                n = utf8(cp < 0x20 ? ' ' : cp, b);
            } else {
                b[0] = e == 'n' || e == 'r' || e == 't' || e == 'b' || e == 'f' ? ' ' : e;
                n = 1;
            }
        } else {
            n = seq_len((unsigned char)*c->p);
            if ((size_t)(c->end - c->p) < n) return false;
            memcpy(b, c->p, n);
            c->p += n;
        }
        if (!dst || cut) continue;
        if (o + n < cap) {
            memcpy(dst + o, b, n);
            o += n;
        } else {
            cut = true;
        }
    }
    if (!eat(c, '"')) return false;
    if (dst && cap) {
        dst[o < cap ? o : cap - 1] = '\0';
        if (cut) ellipsis(dst, cap, o);
    }
    return true;
}

void convo_fit(char *dst, size_t cap, const char *src, size_t n)
{
    if (!cap) return;
    if (n < cap) {
        memcpy(dst, src, n);
        dst[n] = '\0';
        return;
    }
    memcpy(dst, src, cap - 1);
    dst[cap - 1] = '\0';
    ellipsis(dst, cap, cap - 1);
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
        if (v < 100000000000000LL) v = v * 10 + (*c->p - '0');
        c->p++;
    }
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

/* `{ "key": value, … }`, each key handed to `field`, which reads its value or skips it. */
typedef bool (*field_fn)(cur_t *c, const char *key, void *user);

static bool object(cur_t *c, field_fn field, void *user)
{
    if (!eat(c, '{')) return false;
    if (eat(c, '}')) return true;
    for (;;) {
        char key[16];
        if (!str(c, key, sizeof key) || !eat(c, ':')) return false;
        if (!field(c, key, user)) return false;
        if (eat(c, '}')) return true;
        if (!eat(c, ',')) return false;
    }
}

/* `[ item, … ]`, each handed to `item`. */
typedef bool (*item_fn)(cur_t *c, void *user);

static bool array(cur_t *c, item_fn item, void *user)
{
    if (!eat(c, '[')) return false;
    if (eat(c, ']')) return true;
    for (;;) {
        if (!item(c, user)) return false;
        if (eat(c, ']')) return true;
        if (!eat(c, ',')) return false;
    }
}

/* ---- Recent ---- */

typedef struct {
    convo_row_t row;
    char origin[8];
    bool has_id;
} row_in_t;

static bool row_field(cur_t *c, const char *key, void *user)
{
    row_in_t *r = user;
    if (!strcmp(key, "id") && peek(c, '"')) {
        r->has_id = true;
        return str(c, r->row.id, sizeof r->row.id);
    }
    if (!strcmp(key, "title") && peek(c, '"')) return str(c, r->row.title, sizeof r->row.title);
    if (!strcmp(key, "snippet") && peek(c, '"')) return str(c, r->row.snippet, sizeof r->row.snippet);
    if (!strcmp(key, "origin") && peek(c, '"')) return str(c, r->origin, sizeof r->origin);
    if (!strcmp(key, "at") && !peek(c, 'n')) return num(c, &r->row.at);
    if (!strcmp(key, "current") && !peek(c, 'n')) return boolean(c, &r->row.current);
    return skip(c, 1);
}

static bool row_item(cur_t *c, void *user)
{
    convo_list_t *l = user;
    row_in_t r = {0};
    if (!object(c, row_field, &r)) return false;
    const bool panel = !strcmp(r.origin, "panel"), pwa = !strcmp(r.origin, "pwa");
    if (!r.has_id || !r.row.id[0] || (!panel && !pwa) || l->n >= CONVO_ROWS) return true;
    r.row.origin = panel ? CONVO_FROM_PANEL : CONVO_FROM_PWA;
    l->row[l->n++] = r.row;
    return true;
}

typedef struct {
    convo_list_t *list;
    bool found;
} recent_in_t;

static bool recent_field(cur_t *c, const char *key, void *user)
{
    recent_in_t *r = user;
    if (!strcmp(key, "items") && peek(c, '[')) {
        r->found = true;
        return array(c, row_item, r->list);
    }
    return skip(c, 1);
}

bool convo_parse_recent(const char *json, size_t len, convo_list_t *out)
{
    memset(out, 0, sizeof *out);
    cur_t c = {json, json + len};
    recent_in_t r = {out, false};
    if (!object(&c, recent_field, &r) || !r.found) {
        out->n = 0;
        return false;
    }
    return true;
}

/* ---- a thread ---- */

typedef struct {
    char *title;
    size_t title_cap;
    bool *more;
    char *scratch;
    size_t cap;
    convo_msg_fn on_msg;
    void *user;
    bool mine, has_text;
    int64_t id;
} thread_in_t;

static bool msg_field(cur_t *c, const char *key, void *user)
{
    thread_in_t *t = user;
    if (!strcmp(key, "mine") && !peek(c, 'n')) return boolean(c, &t->mine);
    if (!strcmp(key, "id") && !peek(c, 'n') && !peek(c, '"')) return num(c, &t->id);
    if (!strcmp(key, "text") && peek(c, '"')) {
        t->has_text = true;
        return str(c, t->scratch, t->cap);
    }
    return skip(c, 1);
}

static uint32_t id32(int64_t id)
{
    return id > 0 && id <= 0xFFFFFFFFLL ? (uint32_t)id : 0;
}

static bool msg_item(cur_t *c, void *user)
{
    thread_in_t *t = user;
    t->mine = t->has_text = false;
    t->id = 0;
    if (!object(c, msg_field, t)) return false;
    if (t->has_text && t->scratch[0]) t->on_msg(t->user, t->mine, t->scratch, id32(t->id));
    return true;
}

static bool thread_field(cur_t *c, const char *key, void *user)
{
    thread_in_t *t = user;
    if (!strcmp(key, "title") && peek(c, '"')) return str(c, t->title, t->title_cap);
    if (!strcmp(key, "more") && !peek(c, 'n')) return boolean(c, t->more);
    if (!strcmp(key, "messages") && peek(c, '[')) return array(c, msg_item, t);
    return skip(c, 1);
}

bool convo_parse_thread(const char *json, size_t len, char *title, size_t title_cap, bool *more,
                        char *scratch, size_t cap, convo_msg_fn on_msg, void *user)
{
    if (title_cap) title[0] = '\0';
    *more = false;
    if (!cap) return false;
    cur_t c = {json, json + len};
    thread_in_t t = {title, title_cap, more, scratch, cap, on_msg, user, false, false, 0};
    return object(&c, thread_field, &t);
}

/* ---- Load earlier (0.0.21) ---- */

static bool uuid_ok(const char *id)
{
    if (!id || strlen(id) != CONVO_ID_BYTES - 1) return false;
    for (const char *p = id; *p; p++)
        if (!((*p >= '0' && *p <= '9') || (*p >= 'a' && *p <= 'f') || *p == '-')) return false;
    return true;
}

bool convo_hist_path(const char *who, uint8_t side, uint32_t box_id, const char *uuid, char *out,
                     size_t cap)
{
    if (cap) out[0] = '\0';
    const bool agent = convo_agent_ok(who);
    if (!agent && !uuid_ok(who)) return false;
    char anchor[64] = "";
    const char *dir = side == 2 ? "after" : "before";
    if (agent && box_id)
        snprintf(anchor, sizeof anchor, "%s=%lu&", dir, (unsigned long)box_id);
    else if (!agent && uuid && uuid[0]) {
        if (!uuid_ok(uuid)) return false;
        snprintf(anchor, sizeof anchor, "%s=%s&", dir, uuid);
    } else if (side == 2) {
        return false; /* newer than nothing is not a page */
    }
    const int n = snprintf(out, cap, "/endpoint/%s/%s/history?%slimit=%d",
                           agent ? "convo" : "messages", who, anchor, side ? HIST_PAGE : 0);
    return n > 0 && (size_t)n < cap;
}

typedef struct {
    convo_page_t *page;
    char *scratch;
    size_t cap;
    convo_item_fn on_item;
    void *user;
    hist_in_t item;
    int64_t num_id, ms;
    bool has_text, found;
} page_in_t;

static bool item_field(cur_t *c, const char *key, void *user)
{
    page_in_t *p = user;
    if (!strcmp(key, "id")) {
        if (peek(c, '"')) return str(c, p->item.uuid, sizeof p->item.uuid);
        if (!peek(c, 'n')) return num(c, &p->num_id);
    }
    if (!strcmp(key, "mine") && !peek(c, 'n')) return boolean(c, &p->item.mine);
    if (!strcmp(key, "voice") && !peek(c, 'n')) return boolean(c, &p->item.voice);
    if (!strcmp(key, "played") && !peek(c, 'n')) return boolean(c, &p->item.played);
    if (!strcmp(key, "ms") && !peek(c, 'n')) return num(c, &p->ms);
    if (!strcmp(key, "text") && peek(c, '"')) {
        p->has_text = true;
        return str(c, p->scratch, p->cap);
    }
    return skip(c, 1);
}

static bool page_item(cur_t *c, void *user)
{
    page_in_t *p = user;
    p->item = (hist_in_t){.played = true};
    p->num_id = p->ms = 0;
    p->has_text = false;
    p->scratch[0] = '\0';
    if (!object(c, item_field, p)) return false;
    p->item.box_id = id32(p->num_id);
    if (p->item.uuid[0] && !uuid_ok(p->item.uuid)) p->item.uuid[0] = '\0';
    if ((!p->item.box_id && !p->item.uuid[0]) || p->page->n >= HIST_PAGE) return true;
    const int64_t ms = p->ms < 0 ? 0 : p->ms > 65000 ? 65000 : p->ms;
    p->item.dur_ms = (uint16_t)ms;
    p->item.dur_s = (uint16_t)((ms + 999) / 1000);
    p->item.text = p->scratch;
    p->page->n++;
    if (p->on_item) p->on_item(p->user, &p->item);
    return true;
}

static bool page_field(cur_t *c, const char *key, void *user)
{
    page_in_t *p = user;
    int64_t v;
    if (!strcmp(key, "messages") && peek(c, '[')) {
        p->found = true;
        return array(c, page_item, p);
    }
    if (!strcmp(key, "older") && !peek(c, 'n')) {
        if (!num(c, &v)) return false;
        p->page->older = v < 0 ? 0 : (uint32_t)v;
        return true;
    }
    if (!strcmp(key, "newer") && !peek(c, 'n')) {
        if (!num(c, &v)) return false;
        p->page->newer = v < 0 ? 0 : (uint32_t)v;
        return true;
    }
    if (!strcmp(key, "start_at") && !peek(c, 'n')) return num(c, &p->page->start_at);
    return skip(c, 1);
}

bool convo_parse_page(const char *json, size_t len, convo_page_t *out, char *scratch, size_t cap,
                      convo_item_fn on_item, void *user)
{
    memset(out, 0, sizeof *out);
    if (!cap) return false;
    cur_t c = {json, json + len};
    page_in_t p = {.page = out, .scratch = scratch, .cap = cap, .on_item = on_item, .user = user};
    return object(&c, page_field, &p) && p.found;
}
