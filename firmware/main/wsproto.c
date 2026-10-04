/* The panel socket's protocol arithmetic. See wsproto.h; host-tested in firmware/host. */

#include "wsproto.h"

#include <stdio.h>
#include <string.h>

void wsp_put_id(uint8_t out[WSP_ID_BYTES], uint32_t id)
{
    out[0] = (uint8_t)(id & 0xFF);
    out[1] = (uint8_t)((id >> 8) & 0xFF);
    out[2] = (uint8_t)((id >> 16) & 0xFF);
    out[3] = (uint8_t)((id >> 24) & 0xFF);
}

uint32_t wsp_get_id(const uint8_t in[WSP_ID_BYTES])
{
    return (uint32_t)in[0] | ((uint32_t)in[1] << 8) | ((uint32_t)in[2] << 16) |
           ((uint32_t)in[3] << 24);
}

/* Nothing the firmware sends needs escaping; something that would is refused, not mangled. */
static bool plain(const char *s)
{
    if (s == NULL) return true;
    for (; *s != '\0'; s++) {
        const unsigned char c = (unsigned char)*s;
        if (c < 0x20 || c == '"' || c == '\\' || c >= 0x7F) return false;
    }
    return true;
}

static int fits(int n, size_t cap)
{
    return (n < 0 || (size_t)n >= cap) ? -1 : n;
}

int wsp_req(char *out, size_t cap, uint32_t id, const char *method, const char *path,
            const char *ctype, const char *hname, const char *hval, size_t len, size_t win)
{
    if (out == NULL || cap == 0 || method == NULL || path == NULL) return -1;
    if (!plain(method) || !plain(path) || !plain(ctype) || !plain(hname) || !plain(hval)) return -1;
    char h[160];
    int hn;
    const bool extra = hname != NULL && hval != NULL;
    if (ctype != NULL && extra) {
        hn = snprintf(h, sizeof(h), "{\"Content-Type\":\"%s\",\"%s\":\"%s\"}", ctype, hname, hval);
    } else if (ctype != NULL) {
        hn = snprintf(h, sizeof(h), "{\"Content-Type\":\"%s\"}", ctype);
    } else if (extra) {
        hn = snprintf(h, sizeof(h), "{\"%s\":\"%s\"}", hname, hval);
    } else {
        hn = snprintf(h, sizeof(h), "{}");
    }
    if (fits(hn, sizeof(h)) < 0) return -1;
    const int n = snprintf(out, cap,
                           "{\"t\":\"req\",\"id\":%lu,\"m\":\"%s\",\"p\":\"%s\",\"h\":%s,"
                           "\"len\":%lu,\"win\":%lu}",
                           (unsigned long)id, method, path, h, (unsigned long)len,
                           (unsigned long)win);
    return fits(n, cap);
}

int wsp_ack(char *out, size_t cap, uint32_t id, size_t n)
{
    return fits(snprintf(out, cap, "{\"t\":\"ack\",\"id\":%lu,\"n\":%lu}", (unsigned long)id,
                         (unsigned long)n),
                cap);
}

int wsp_cancel(char *out, size_t cap, uint32_t id)
{
    return fits(snprintf(out, cap, "{\"t\":\"cancel\",\"id\":%lu}", (unsigned long)id), cap);
}

int wsp_hb(char *out, size_t cap)
{
    return fits(snprintf(out, cap, "{\"t\":\"hb\"}"), cap);
}

int wsp_rx_feed(wsp_rx_t *rx, int offset, const uint8_t *data, int len, uint32_t *id,
                const uint8_t **payload)
{
    if (rx == NULL || data == NULL || len < 0 || offset < 0) return -1;
    if (offset == 0) {
        rx->idgot = 0;
        rx->started = true;
    } else if (!rx->started) {
        return -1;
    }
    int at = 0;
    /* The id may straddle two pieces; take what this one has of it. `offset` is checked against
       what has been gathered, so a piece that skips ahead cannot leave the id half-read. */
    if (rx->idgot < WSP_ID_BYTES) {
        if (offset != rx->idgot) return -1;
        while (rx->idgot < WSP_ID_BYTES && at < len) rx->idbuf[rx->idgot++] = data[at++];
        if (rx->idgot < WSP_ID_BYTES) return 0;
    }
    *id = wsp_get_id(rx->idbuf);
    *payload = data + at;
    return len - at;
}

size_t wsp_credit(size_t consumed, size_t *acked, size_t win)
{
    if (acked == NULL || win == 0 || consumed <= *acked) return 0;
    const size_t owed = consumed - *acked;
    if (owed < win / 2) return 0;
    *acked = consumed;
    return owed;
}

void wsp_policy_init(wsp_policy_t *p)
{
    p->fails = 0;
    p->fallback = false;
    p->since_ms = 0;
}

void wsp_policy_up(wsp_policy_t *p)
{
    wsp_policy_init(p);
}

void wsp_policy_failed(wsp_policy_t *p, int hs_status, uint32_t now_ms)
{
    p->fails++;
    /* NO ROUTE IS AN ANSWER, NOT A FAULT. A box that predates the socket answers the upgrade
       with 403 — Starlette refuses a WebSocket to a path it has no route for that way — and a
       proxy in front of it may say 404 or 405; three attempts would only spend three handshakes
       learning it. A box WITH the route refuses a bad key with 403 too, and falling back is right
       for that as well: the HTTPS path reports the 401 plainly, and the socket is retried. */
    const bool absent = hs_status == 403 || hs_status == 404 || hs_status == 405;
    if (absent || p->fails >= WSP_FALLBACK_AFTER) {
        p->fallback = true;
        p->since_ms = now_ms;
    }
}

bool wsp_policy_try_ws(const wsp_policy_t *p, uint32_t now_ms)
{
    if (!p->fallback) return true;
    /* Unsigned subtraction, so the comparison survives the millisecond clock wrapping. */
    return (uint32_t)(now_ms - p->since_ms) >= WSP_FALLBACK_RETRY_MS;
}

bool wsp_retry_quiet(uint32_t now_ms, uint32_t last_http_ms, int http_waiting)
{
    if (http_waiting > 0) return false;
    return (uint32_t)(now_ms - last_http_ms) >= WSP_RETRY_QUIET_MS;
}

uint32_t wsp_backoff_ms(int fails)
{
    uint32_t ms = WSP_BACKOFF_MIN_MS;
    for (int i = 1; i < fails && ms < WSP_BACKOFF_MAX_MS; i++) ms *= 2;
    return ms > WSP_BACKOFF_MAX_MS ? WSP_BACKOFF_MAX_MS : ms;
}

int wsp_ws_uri(char *out, size_t cap, const char *api, const char *route)
{
    if (out == NULL || api == NULL || route == NULL) return -1;
    const char *scheme;
    const char *rest;
    if (strncmp(api, "https://", 8) == 0) {
        scheme = "wss://";
        rest = api + 8;
    } else if (strncmp(api, "http://", 7) == 0) {
        scheme = "ws://";
        rest = api + 7;
    } else {
        return -1;
    }
    return fits(snprintf(out, cap, "%s%s%s", scheme, rest, route), cap);
}

int wsp_err_str(char *out, size_t cap, const char *what, int tls_err, int stack_err,
                int sock_errno, int hs_status)
{
    if (out == NULL || cap == 0) return -1;
    int n = snprintf(out, cap, "%s", what != NULL ? what : "ws");
    if (tls_err != 0 && n >= 0 && (size_t)n < cap)
        n += snprintf(out + n, cap - (size_t)n, " tls=0x%x", (unsigned)tls_err);
    if (stack_err != 0 && n >= 0 && (size_t)n < cap)
        n += snprintf(out + n, cap - (size_t)n, " mbed=-0x%x",
                      (unsigned)(stack_err < 0 ? -stack_err : stack_err));
    if (sock_errno != 0 && n >= 0 && (size_t)n < cap)
        n += snprintf(out + n, cap - (size_t)n, " sock=%d", sock_errno);
    if (hs_status != 0 && n >= 0 && (size_t)n < cap)
        n += snprintf(out + n, cap - (size_t)n, " hs=%d", hs_status);
    return fits(n, cap);
}
