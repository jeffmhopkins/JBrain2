#include "settings_sync.h"

#include <stdio.h>
#include <string.h>

typedef struct {
    setting_t key;
    const char *wire;
} sync_key_t;

/* What the pickup reports beyond the notice switches (backend `panel_settings.CORE_WIRES`). */
static const sync_key_t REPORT[] = {
    {SET_NIGHT_ON, "no"}, {SET_NIGHT_COLOR, "nk"}, {SET_SHOW_DATE, "sd"}, {SET_BRIGHTNESS, "br"},
    {SET_VOLUME, "vo"}, {SET_TTS, "ra"}, {SET_AUTO_ROTATE, "ar"},
};

/* What the PWA may ask for (backend `panel_settings.SETTINGS`): never the microphone's gain or
   the tilt sensor's axes, which stay in the debug console. */
static const sync_key_t SYNC_KEYS[SYNC_MAX] = {
    {SET_NIGHT_ON, "no"},     {SET_NIGHT_FROM, "nf"},   {SET_NIGHT_TO, "nt"},
    {SET_NIGHT_COLOR, "nk"},  {SET_SHOW_DATE, "sd"},    {SET_TZ, "tz"},
    {SET_NOTIF_ACCEPT, "na"}, {SET_NOTIF_QUIET, "nq"},  {SET_NOTIF_CHIME, "nc"},
    {SET_BRIGHTNESS, "br"},   {SET_VOLUME, "vo"},       {SET_TTS, "ra"},
    {SET_AUTO_ROTATE, "ar"},
};

void settings_sync_query(int32_t (*get)(setting_t), char *out, size_t cap)
{
    size_t o = 0;
    if (cap) out[0] = '\0';
    for (size_t i = 0; i < sizeof REPORT / sizeof *REPORT && o < cap; i++) {
        const int n = snprintf(out + o, cap - o, "&%s=%ld", REPORT[i].wire, (long)get(REPORT[i].key));
        if (n < 0 || (size_t)n >= cap - o) {
            /* All of it or none: a half report is no report at the box. */
            out[0] = '\0';
            return;
        }
        o += (size_t)n;
    }
}

uint32_t settings_sync_header(const char *header)
{
    if (!header || !header[0]) return 0;
    uint32_t v = 0;
    for (const char *c = header; *c; c++) {
        if (*c < '0' || *c > '9' || v > 100000000u) return 0;
        v = v * 10u + (uint32_t)(*c - '0');
    }
    return v;
}

bool settings_sync_due(uint32_t signalled, uint32_t fetched, uint32_t since_fetch_ms)
{
    if (!signalled) return false;
    return signalled != fetched || since_fetch_ms >= SYNC_REFETCH_MS;
}

/* ---- the panel-settings answer: a reader for exactly its shape ---- */

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

static bool key(cur_t *c, char *out, size_t cap)
{
    if (!eat(c, '"')) return false;
    size_t n = 0;
    while (c->p < c->end && *c->p != '"') {
        if (*c->p == '\\' || n + 1 >= cap) return false;
        out[n++] = *c->p++;
    }
    out[n] = '\0';
    return eat(c, '"');
}

static bool num(cur_t *c, int32_t *out)
{
    ws(c);
    bool neg = false;
    if (c->p < c->end && *c->p == '-') {
        neg = true;
        c->p++;
    }
    if (c->p >= c->end || *c->p < '0' || *c->p > '9') return false;
    int32_t v = 0;
    while (c->p < c->end && *c->p >= '0' && *c->p <= '9') {
        if (v > 100000000) return false;
        v = v * 10 + (*c->p++ - '0');
    }
    *out = neg ? -v : v;
    return true;
}

static int key_of(const char *wire)
{
    for (int i = 0; i < SYNC_MAX; i++)
        if (!strcmp(SYNC_KEYS[i].wire, wire)) return SYNC_KEYS[i].key;
    return -1;
}

static bool changes(cur_t *c, sync_pending_t *out)
{
    if (!eat(c, '{')) return false;
    if (eat(c, '}')) return true;
    for (;;) {
        char wire[8];
        int32_t value, base;
        if (!key(c, wire, sizeof wire) || !eat(c, ':') || !eat(c, '[') || !num(c, &value) ||
            !eat(c, ',') || !num(c, &base) || !eat(c, ']'))
            return false;
        const int k = key_of(wire);
        if (k >= 0 && out->n < SYNC_MAX) out->c[out->n++] = (sync_change_t){(uint8_t)k, value, base};
        if (eat(c, '}')) return true;
        if (!eat(c, ',')) return false;
    }
}

bool settings_sync_parse(const char *json, size_t len, sync_pending_t *out)
{
    memset(out, 0, sizeof *out);
    cur_t c = {json, json + len};
    if (!eat(&c, '{')) return false;
    bool v = false, s = false;
    if (eat(&c, '}')) return false;
    for (;;) {
        char k[8];
        if (!key(&c, k, sizeof k) || !eat(&c, ':')) return false;
        if (!strcmp(k, "v")) {
            int32_t n;
            if (!num(&c, &n) || n < 0) return false;
            out->version = (uint32_t)n;
            v = true;
        } else if (!strcmp(k, "s")) {
            if (!changes(&c, out)) return false;
            s = true;
        } else {
            return false;
        }
        if (eat(&c, '}')) break;
        if (!eat(&c, ',')) return false;
    }
    return v && s;
}

uint32_t settings_sync_apply(const sync_pending_t *p, int32_t (*get)(setting_t),
                             void (*set)(setting_t, int32_t))
{
    uint32_t done = 0;
    for (uint8_t i = 0; i < p->n; i++) {
        const setting_t k = (setting_t)p->c[i].key;
        /* Changed at the glass since the owner asked: the panel's word stands. */
        if (get(k) != p->c[i].base) continue;
        set(k, p->c[i].value);
        if (get(k) != p->c[i].base) done |= 1u << k;
    }
    return done;
}
