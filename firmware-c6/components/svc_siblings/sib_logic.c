#include "sib_logic.h"

#include <string.h>

/* ---- names ---- */

/* One code point of `s` (UTF-8, or a lone byte when malformed): how many bytes it took. */
static size_t cp_len(const unsigned char *s)
{
    if (s[0] < 0x80) return 1;
    size_t n = (s[0] & 0xE0) == 0xC0 ? 2 : (s[0] & 0xF0) == 0xE0 ? 3 : (s[0] & 0xF8) == 0xF0 ? 4 : 1;
    for (size_t i = 1; i < n; i++)
        if ((s[i] & 0xC0) != 0x80) return i;
    return n;
}

void sib_clean_name(const char *in, char *out, size_t cap)
{
    if (!cap) return;
    size_t o = 0;
    const size_t limit = cap - 1 < SIB_NAME_BYTES ? cap - 1 : SIB_NAME_BYTES;
    const unsigned char *s = (const unsigned char *)in;
    while (*s && o < limit) {
        const size_t n = cp_len(s);
        char c = n == 1 && *s >= 0x20 && *s < 0x7F ? (char)*s : '?';
        if (n == 1 && (*s == '\t' || *s == '\n' || *s == '\r')) c = ' ';
        s += n;
        if (c == ' ' && (o == 0 || out[o - 1] == ' ')) continue;
        out[o++] = c;
    }
    while (o && out[o - 1] == ' ') o--;
    out[o] = '\0';
}

char sib_initial(const char *name)
{
    for (; *name; name++) {
        const char c = *name;
        if (c >= 'a' && c <= 'z') return (char)(c - 'a' + 'A');
        if ((c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')) return c;
    }
    return '?';
}

uint8_t sib_accent_for(const char *name, uint8_t taken)
{
    uint32_t h = 2166136261u; /* FNV-1a: stable across builds and boots */
    for (const unsigned char *s = (const unsigned char *)name; *s; s++) h = (h ^ *s) * 16777619u;
    uint8_t a = (uint8_t)(h % SIB_MAX);
    for (int i = 0; i < SIB_MAX && (taken >> a & 1u); i++) a = (uint8_t)((a + 1) % SIB_MAX);
    return a;
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

static int hex(char h)
{
    if (h >= '0' && h <= '9') return h - '0';
    if (h >= 'a' && h <= 'f') return h - 'a' + 10;
    if (h >= 'A' && h <= 'F') return h - 'A' + 10;
    return -1;
}

/* A JSON string into `out` as UTF-8, cut to fit; false when it is not one. */
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
                    const int v = hex(*c->p++);
                    if (v < 0) return false;
                    cp = cp << 4 | (unsigned)v;
                }
            } else {
                cp = e == 'n' ? '\n' : e == 't' ? '\t' : e == 'r' ? '\r' : e == 'b' ? '\b'
                     : e == 'f' ? '\f' : (unsigned char)e;
            }
            /* An escaped code point past ASCII is drawn as '?' anyway: keep one byte for it. */
            if (cp >= 0x80) cp = 0x7F;
        }
        if (o + 1 < cap) out[o++] = (char)cp;
    }
    out[o] = '\0';
    return eat(c, '"');
}

/* Skips any JSON value; false on a malformed one. Depth-bounded: nothing here nests deeply. */
static bool skip(cur_t *c, int depth)
{
    ws(c);
    if (c->p >= c->end || depth > 4) return false;
    const char ch = *c->p;
    if (ch == '"') {
        char tmp[2];
        return str(c, tmp, sizeof tmp);
    }
    if (ch == '{' || ch == '[') {
        const char close = ch == '{' ? '}' : ']';
        c->p++;
        if (eat(c, close)) return true;
        for (;;) {
            if (ch == '{') {
                char k[2];
                if (!str(c, k, sizeof k) || !eat(c, ':')) return false;
            }
            if (!skip(c, depth + 1)) return false;
            if (eat(c, close)) return true;
            if (!eat(c, ',')) return false;
        }
    }
    while (c->p < c->end && *c->p != ',' && *c->p != '}' && *c->p != ']' && *c->p != ' ')
        c->p++;
    return true;
}

static bool entry(cur_t *c, sibling_t *s)
{
    char raw[SIB_NAME_BYTES * 4 + 1];
    memset(s, 0, sizeof *s);
    if (!eat(c, '{')) return false;
    if (eat(c, '}')) return true;
    for (;;) {
        char key[16];
        if (!str(c, key, sizeof key) || !eat(c, ':')) return false;
        ws(c);
        if (!strcmp(key, "device_id") && c->p < c->end && *c->p == '"') {
            if (!str(c, s->device_id, sizeof s->device_id)) return false;
        } else if (!strcmp(key, "name") && c->p < c->end && *c->p == '"') {
            if (!str(c, raw, sizeof raw)) return false;
            sib_clean_name(raw, s->name, sizeof s->name);
        } else if (!skip(c, 1)) {
            return false;
        }
        if (eat(c, '}')) return true;
        if (!eat(c, ',')) return false;
    }
}

bool sib_parse(const char *json, size_t len, sib_list_t *out)
{
    memset(out, 0, sizeof *out);
    cur_t c = {json, json + len};
    if (!eat(&c, '{')) return false;
    bool found = false;
    if (eat(&c, '}')) return false;
    for (;;) {
        char key[16];
        if (!str(&c, key, sizeof key) || !eat(&c, ':')) return false;
        if (!strcmp(key, "siblings")) {
            if (!eat(&c, '[')) return false;
            found = true;
            if (!eat(&c, ']')) {
                for (;;) {
                    sibling_t s;
                    if (!entry(&c, &s)) return false;
                    if (s.device_id[0] && out->n < SIB_MAX) out->s[out->n++] = s;
                    if (eat(&c, ']')) break;
                    if (!eat(&c, ',')) return false;
                }
            }
        } else if (!skip(&c, 1)) {
            return false;
        }
        if (eat(&c, '}')) return found;
        if (!eat(&c, ',')) return false;
    }
}

/* ---- slots ---- */

static int listed(const sib_list_t *l, const char *id)
{
    for (int i = 0; i < l->n; i++)
        if (!strcmp(l->s[i].device_id, id)) return i;
    return -1;
}

static int seated(const sib_slots_t *s, const char *id)
{
    for (int k = 0; k < SIB_MAX; k++)
        if (s->used[k] && !strcmp(s->s[k].device_id, id)) return k;
    return -1;
}

uint8_t sib_merge(sib_slots_t *slots, const sib_list_t *list, bool *renamed)
{
    uint8_t changed = 0;
    *renamed = false;
    for (int k = 0; k < SIB_MAX; k++) {
        if (!slots->used[k]) continue;
        const int i = listed(list, slots->s[k].device_id);
        if (i < 0) {
            slots->used[k] = false;
            memset(&slots->s[k], 0, sizeof slots->s[k]);
            changed |= (uint8_t)(1u << k);
        } else if (strcmp(slots->s[k].name, list->s[i].name)) {
            memcpy(slots->s[k].name, list->s[i].name, sizeof slots->s[k].name);
            *renamed = true;
        }
    }
    for (int i = 0; i < list->n; i++) {
        if (seated(slots, list->s[i].device_id) >= 0) continue;
        for (int k = 0; k < SIB_MAX; k++)
            if (!slots->used[k]) {
                slots->s[k] = list->s[i];
                slots->used[k] = true;
                changed |= (uint8_t)(1u << k);
                break;
            }
    }
    /* Colours last, in slot order, so each avoids the ones already handed out. */
    uint8_t taken = 0;
    for (int k = 0; k < SIB_MAX; k++) {
        if (!slots->used[k]) continue;
        const uint8_t a = sib_accent_for(slots->s[k].name, taken);
        if (a != slots->s[k].accent && !(changed >> k & 1u)) *renamed = true;
        slots->s[k].accent = a;
        taken |= (uint8_t)(1u << a);
    }
    return changed;
}
