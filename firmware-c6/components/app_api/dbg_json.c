#include "dbg_json.h"

#include <ctype.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

/* `ca` and `pass`/`token` are the provisioning NVS keys the box's flasher writes (README "How
 * it talks to the box"). Matched whole or as a suffix, so `wifi_pass` and `box_token` are
 * caught too; a new secret named anything else must be added here, which the host suite pins. */
static const char *const DENY_EXACT[] = {"pass", "password", "psk", "token", "key", "device_key",
                                         "ca", "cert", "secret", "auth", "authorization",
                                         "bearer", "cookie"};
static const char *const DENY_SUFFIX[] = {"_pass", "_password", "_token", "_key", "_secret",
                                          "_cert"};

#define SECRETS_MAX 8
static const char *s_secrets[SECRETS_MAX];
static unsigned s_secret_n;

static bool ieq(const char *a, const char *b)
{
    for (; *a && *b; a++, b++)
        if (tolower((unsigned char)*a) != tolower((unsigned char)*b)) return false;
    return *a == *b;
}

bool dbg_json_key_is_secret(const char *key)
{
    if (!key) return false;
    for (size_t i = 0; i < sizeof DENY_EXACT / sizeof *DENY_EXACT; i++)
        if (ieq(key, DENY_EXACT[i])) return true;
    size_t kl = strlen(key);
    for (size_t i = 0; i < sizeof DENY_SUFFIX / sizeof *DENY_SUFFIX; i++) {
        size_t sl = strlen(DENY_SUFFIX[i]);
        if (kl >= sl && ieq(key + kl - sl, DENY_SUFFIX[i])) return true;
    }
    return false;
}

void dbg_secret_add(const char *value)
{
    if (!value || strlen(value) < 4 || s_secret_n >= SECRETS_MAX) return;
    for (unsigned i = 0; i < s_secret_n; i++)
        if (s_secrets[i] == value) return;
    s_secrets[s_secret_n++] = value;
}

void dbg_secret_clear(void)
{
    s_secret_n = 0;
}

bool dbg_secret_in(const char *text)
{
    if (!text) return false;
    for (unsigned i = 0; i < s_secret_n; i++)
        if (strstr(text, s_secrets[i])) return true;
    return false;
}

static void put(dbg_json_t *w, const char *s, size_t n)
{
    if (w->overflow) return;
    if (w->len + n + 1 > w->cap) {
        w->overflow = true;
        return;
    }
    memcpy(w->buf + w->len, s, n);
    w->len += n;
    w->buf[w->len] = '\0';
}

static void puts_(dbg_json_t *w, const char *s)
{
    put(w, s, strlen(s));
}

static void quoted(dbg_json_t *w, const char *s)
{
    put(w, "\"", 1);
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') {
            char e[2] = {'\\', (char)c};
            put(w, e, 2);
        } else if (c < 0x20) {
            char e[8];
            snprintf(e, sizeof e, "\\u%04x", c);
            puts_(w, e);
        } else {
            put(w, (const char *)&c, 1);
        }
    }
    put(w, "\"", 1);
}

static void sep_key(dbg_json_t *w, const char *key)
{
    if (w->depth > 0) {
        if (!w->first[w->depth - 1]) put(w, ",", 1);
        w->first[w->depth - 1] = false;
    }
    if (key) {
        quoted(w, key);
        put(w, ":", 1);
    }
}

void dbg_json_init(dbg_json_t *w, char *buf, size_t cap)
{
    memset(w, 0, sizeof *w);
    w->buf = buf;
    w->cap = cap;
    if (cap) buf[0] = '\0';
}

static void open_(dbg_json_t *w, const char *key, const char *brace)
{
    sep_key(w, key);
    puts_(w, brace);
    if (w->depth >= DBG_JSON_DEPTH) {
        w->overflow = true;
        return;
    }
    w->first[w->depth++] = true;
}

static void close_(dbg_json_t *w, const char *brace)
{
    if (w->depth) w->depth--;
    puts_(w, brace);
}

void dbg_json_obj_begin(dbg_json_t *w, const char *key) { open_(w, key, "{"); }
void dbg_json_obj_end(dbg_json_t *w) { close_(w, "}"); }
void dbg_json_arr_begin(dbg_json_t *w, const char *key) { open_(w, key, "["); }
void dbg_json_arr_end(dbg_json_t *w) { close_(w, "]"); }

void dbg_json_str(dbg_json_t *w, const char *key, const char *val)
{
    sep_key(w, key);
    if (dbg_json_key_is_secret(key) || dbg_secret_in(val)) {
        quoted(w, DBG_REDACTED);
        return;
    }
    if (!val) {
        puts_(w, "null");
        return;
    }
    quoted(w, val);
}

void dbg_json_int(dbg_json_t *w, const char *key, int64_t val)
{
    sep_key(w, key);
    if (dbg_json_key_is_secret(key)) {
        quoted(w, DBG_REDACTED);
        return;
    }
    char n[24];
    snprintf(n, sizeof n, "%" PRId64, val);
    puts_(w, n);
}

void dbg_json_bool(dbg_json_t *w, const char *key, bool val)
{
    sep_key(w, key);
    puts_(w, val ? "true" : "false");
}

const char *dbg_json_text(const dbg_json_t *w)
{
    return w->overflow || w->depth ? "" : w->buf;
}
