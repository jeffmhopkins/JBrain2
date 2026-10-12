#include "post_logic.h"

#include <stdio.h>
#include <string.h>

static const char ELLIPSIS[] = "\xE2\x80\xA6";

bool post_id_ok(const char *id)
{
    if (!id || strlen(id) != POST_ID_BYTES - 1) return false;
    for (const char *p = id; *p; p++)
        if (!((*p >= '0' && *p <= '9') || (*p >= 'a' && *p <= 'f') || *p == '-')) return false;
    return true;
}

static bool path(char *out, size_t cap, int n)
{
    if (n < 0 || (size_t)n >= cap) {
        if (cap) out[0] = '\0';
        return false;
    }
    return true;
}

bool post_thread_path(const char *device, char *out, size_t cap)
{
    if (!post_id_ok(device)) return path(out, cap, -1);
    return path(out, cap, snprintf(out, cap, "/endpoint/messages/%s?limit=%d", device, POST_KEEP));
}

bool post_send_path(const char *device, char *out, size_t cap)
{
    if (!post_id_ok(device)) return path(out, cap, -1);
    return path(out, cap, snprintf(out, cap, "/endpoint/messages/%s/send", device));
}

static bool msg_path(const char *device, const char *id, const char *tail, char *out, size_t cap)
{
    if (!post_id_ok(device) || !post_id_ok(id)) return path(out, cap, -1);
    return path(out, cap, snprintf(out, cap, "/endpoint/messages/%s/%s/%s", device, id, tail));
}

bool post_pcm_path(const char *device, const char *id, char *out, size_t cap)
{
    return msg_path(device, id, "pcm", out, cap);
}

bool post_played_path(const char *device, const char *id, char *out, size_t cap)
{
    return msg_path(device, id, "played", out, cap);
}

uint32_t post_upload_room(uint32_t sent, uint32_t n)
{
    if (sent >= POST_MAX_BYTES) return 0;
    return n < POST_MAX_BYTES - sent ? n : POST_MAX_BYTES - sent;
}

/* ---- a small JSON reader: the box's two answers and nothing more general ---- */

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

static size_t utf8(uint32_t cp, char *b)
{
    if (cp < 0x80) return b[0] = (char)cp, 1;
    if (cp < 0x800) {
        b[0] = (char)(0xC0 | cp >> 6);
        b[1] = (char)(0x80 | (cp & 0x3F));
        return 2;
    }
    b[0] = (char)(0xE0 | cp >> 12);
    b[1] = (char)(0x80 | (cp >> 6 & 0x3F));
    b[2] = (char)(0x80 | (cp & 0x3F));
    return 3;
}

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
    while (o > 0 && o < written && ((unsigned char)dst[o] & 0xC0) == 0x80) o--;
    memcpy(dst + o, ELLIPSIS, sizeof ELLIPSIS);
}

/* A string into `dst` (NULL: skipped), whole or cut with "…". A \u escape outside the basic
 * plane, or a control character, is drawn as "?" or a space: a bubble is one font's glyphs. */
static bool str(cur_t *c, char *dst, size_t cap)
{
    if (!eat(c, '"')) return false;
    size_t o = 0;
    bool cut = false;
    while (c->p < c->end && *c->p != '"') {
        char b[4];
        size_t n;
        if (*c->p == '\\') {
            c->p++;
            if (c->p >= c->end) return false;
            const char e = *c->p++;
            if (e == 'u') {
                if (c->end - c->p < 4) return false;
                uint32_t cp = 0;
                for (int i = 0; i < 4; i++) {
                    const int h = hex(c->p[i]);
                    if (h < 0) return false;
                    cp = cp << 4 | (uint32_t)h;
                }
                c->p += 4;
                if (cp >= 0xD800 && cp < 0xE000) cp = '?';
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
        if (v < 100000000000LL) v = v * 10 + (*c->p - '0');
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

static uint32_t clamp_ms(int64_t v)
{
    return v < 0 ? 0 : v > 3600000 ? 3600000 : (uint32_t)v;
}

/* ---- a thread ---- */

typedef struct {
    post_line_t line;
    char id[40];
} line_in_t;

static bool line_field(cur_t *c, const char *key, void *user)
{
    line_in_t *l = user;
    int64_t v;
    if (!strcmp(key, "id") && peek(c, '"')) return str(c, l->id, sizeof l->id);
    if (!strcmp(key, "text") && peek(c, '"')) return str(c, l->line.text, sizeof l->line.text);
    if (!strcmp(key, "mine") && !peek(c, 'n')) return boolean(c, &l->line.mine);
    if (!strcmp(key, "played") && !peek(c, 'n')) return boolean(c, &l->line.played);
    if (!strcmp(key, "voice") && !peek(c, 'n')) return boolean(c, &l->line.voice);
    if (!strcmp(key, "ms") && !peek(c, 'n')) {
        if (!num(c, &v)) return false;
        l->line.ms = clamp_ms(v);
        return true;
    }
    return skip(c, 1);
}

static bool line_item(cur_t *c, void *user)
{
    post_thread_t *t = user;
    line_in_t l = {0};
    l.line.voice = true;
    if (!object(c, line_field, &l)) return false;
    if (!post_id_ok(l.id)) return true;
    memcpy(l.line.id, l.id, POST_ID_BYTES);
    /* Oldest first from the box: past the keep, the oldest kept goes so the newest stay. */
    if (t->n == POST_KEEP) {
        memmove(t->line, t->line + 1, sizeof t->line[0] * (POST_KEEP - 1));
        t->n--;
    }
    t->line[t->n++] = l.line;
    return true;
}

typedef struct {
    post_thread_t *t;
    bool found;
} thread_in_t;

static bool thread_field(cur_t *c, const char *key, void *user)
{
    thread_in_t *in = user;
    if (!strcmp(key, "tag") && peek(c, '"')) return str(c, in->t->tag, sizeof in->t->tag);
    if (!strcmp(key, "messages") && peek(c, '[')) {
        in->found = true;
        return array(c, line_item, in->t);
    }
    return skip(c, 1);
}

bool post_parse_thread(const char *json, size_t len, post_thread_t *out)
{
    memset(out, 0, sizeof *out);
    cur_t c = {json, json + len};
    thread_in_t in = {out, false};
    if (!object(&c, thread_field, &in) || !in.found) {
        memset(out, 0, sizeof *out);
        return false;
    }
    return true;
}

/* ---- the send's answer ---- */

typedef struct {
    char id[40];
    char *text;
    size_t cap;
    int64_t ms;
} sent_in_t;

static bool sent_field(cur_t *c, const char *key, void *user)
{
    sent_in_t *s = user;
    if (!strcmp(key, "id") && peek(c, '"')) return str(c, s->id, sizeof s->id);
    if (!strcmp(key, "text") && peek(c, '"')) return str(c, s->text, s->cap);
    if (!strcmp(key, "ms") && !peek(c, 'n')) return num(c, &s->ms);
    return skip(c, 1);
}

bool post_parse_sent(const char *json, size_t len, char id[POST_ID_BYTES], char *text, size_t cap,
                     uint32_t *ms)
{
    if (cap) text[0] = '\0';
    id[0] = '\0';
    *ms = 0;
    cur_t c = {json, json + len};
    sent_in_t s = {.text = text, .cap = cap};
    if (!object(&c, sent_field, &s) || !post_id_ok(s.id)) return false;
    memcpy(id, s.id, POST_ID_BYTES);
    *ms = clamp_ms(s.ms);
    return true;
}

void post_to_msg(const post_line_t *l, uint32_t local_id, msg_t *out)
{
    memset(out, 0, sizeof *out);
    out->id = local_id;
    out->mine = l->mine;
    out->voice = l->voice;
    out->played = l->played;
    out->dur_s = (uint16_t)((l->ms + 500) / 1000);
    snprintf(out->text, sizeof out->text, "%s", l->text[0] ? l->text : POST_NO_TEXT);
    if (l->mine) snprintf(out->status, sizeof out->status, "%s", l->played ? "Played" : "Sent");
}

/* ---- new, and the chime ---- */

static bool seen_has(const post_seen_t *s, const char *id)
{
    for (uint8_t i = 0; i < s->n; i++)
        if (!strcmp(s->id[i], id)) return true;
    return false;
}

uint8_t post_arrivals(post_seen_t *seen, const post_thread_t *t)
{
    uint8_t n = 0;
    for (uint8_t i = 0; i < t->n; i++) {
        const post_line_t *l = &t->line[i];
        if (seen->loaded && !l->mine && !l->played && !seen_has(seen, l->id)) n++;
    }
    seen->loaded = true;
    seen->n = t->n;
    for (uint8_t i = 0; i < t->n; i++) memcpy(seen->id[i], t->line[i].id, POST_ID_BYTES);
    return n;
}

bool post_chime(uint8_t arrivals, bool chime_on, bool night_now, bool busy)
{
    return arrivals && chime_on && !night_now && !busy;
}

bool post_tag_feed(post_tag_t *t, const char *tag)
{
    if (!tag || !tag[0] || !strncmp(t->last, tag, sizeof t->last - 1)) return false;
    snprintf(t->last, sizeof t->last, "%s", tag);
    return true;
}
