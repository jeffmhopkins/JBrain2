#include "box_http.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "box";

static const cfg_t *s_cfg;
static esp_http_client_handle_t s_c;
static char *s_auth;
/* A connection is open and the last reply on it was read to its end, so the next request may
   ride it. Anything less and it is closed: a half-read reply would be read as the next one's. */
static bool s_live;
static uint32_t s_hs, s_reuse, s_hs_ms, s_hs_max;
/* When the kept session last finished a request: a stream rides it only while this is recent. */
static int64_t s_used_us;

void box_http_init(const cfg_t *cfg)
{
    s_cfg = cfg;
}

static uint32_t ms_since(int64_t t0_us)
{
    return (uint32_t)((esp_timer_get_time() - t0_us) / 1000);
}

static esp_err_t on_http(esp_http_client_event_t *e)
{
    box_reply_t *r = e->user_data;
    if (e->event_id == HTTP_EVENT_ON_HEADER && r && e->header_key && e->header_value &&
        strcasecmp(e->header_key, "Date") == 0)
        snprintf(r->date, sizeof r->date, "%s", e->header_value);
    return ESP_OK;
}

/* Exactly one of two trust answers, decided at flash time. A LAN name is served by Caddy's
   internal CA, which no public bundle contains, so the box's own root is the only thing
   trusted; a public hostname has an ordinary certificate and only the bundle can validate it.
   Never both: pinning one root is the stronger guarantee and is free on the LAN path. */
static void trust(esp_http_client_config_t *hc)
{
    if (s_cfg->ca != NULL && s_cfg->ca[0] != '\0') {
        hc->cert_pem = s_cfg->ca;
        return;
    }
    hc->crt_bundle_attach = esp_crt_bundle_attach;
}

static bool client(void)
{
    if (s_c) return true;
    if (!s_cfg) return false;
    if (!s_auth) {
        const size_t n = strlen(s_cfg->token) + 8;
        s_auth = malloc(n);
        if (!s_auth) return false;
        snprintf(s_auth, n, "Bearer %s", s_cfg->token);
    }
    esp_http_client_config_t hc = {.url = s_cfg->api, .event_handler = on_http};
    trust(&hc);
    s_c = esp_http_client_init(&hc);
    if (!s_c) return false;
    esp_http_client_set_header(s_c, "Authorization", s_auth);
    return true;
}

static void drop(void)
{
    if (s_c) esp_http_client_close(s_c);
    s_live = false;
}

void box_http_close(void)
{
    drop();
    if (s_c) esp_http_client_cleanup(s_c);
    s_c = NULL;
}

/* Points the kept client at the next request. A changed host would close the connection inside
   esp_http_client_set_url; the host never changes here, so the session survives. */
static bool aim(const char *path, esp_http_client_method_t method, const char *ctype,
                box_reply_t *reply, int timeout_ms)
{
    char url[320];
    if (!client() || snprintf(url, sizeof url, "%s%s", s_cfg->api, path) >= (int)sizeof url)
        return false;
    if (esp_http_client_set_url(s_c, url) != ESP_OK) return false;
    esp_http_client_set_method(s_c, method);
    esp_http_client_set_timeout_ms(s_c, timeout_ms);
    esp_http_client_set_user_data(s_c, reply);
    if (ctype)
        esp_http_client_set_header(s_c, "Content-Type", ctype);
    else
        esp_http_client_delete_header(s_c, "Content-Type");
    return true;
}

/* esp_http_client_open, counted: a request on a closed connection pays the handshake. */
static esp_err_t open_counted(int len, bool *reused)
{
    *reused = s_live;
    const int64_t t0 = esp_timer_get_time();
    const esp_err_t err = esp_http_client_open(s_c, len);
    if (*reused) {
        s_reuse++;
    } else if (err == ESP_OK) {
        s_hs++;
        s_hs_ms = ms_since(t0);
        if (s_hs_ms > s_hs_max) s_hs_max = s_hs_ms;
    }
    return err;
}

/* The reply is in; keep the connection only if it was read to the end and the box keeps it. */
static void settle(void)
{
    const bool whole = esp_http_client_flush_response(s_c, NULL) == ESP_OK &&
                       esp_http_client_is_complete_data_received(s_c);
    esp_http_client_clear_response_buffer(s_c);
    if (whole && esp_http_client_is_persistent_connection(s_c)) {
        s_live = true;
        s_used_us = esp_timer_get_time();
    } else {
        drop();
    }
}

static int once(const char *path, const char *ctype, const char *body, size_t len, char *out,
                size_t cap, size_t *got, box_reply_t *reply, int timeout_ms, bool *reused)
{
    *reused = false;
    if (!aim(path, body ? HTTP_METHOD_POST : HTTP_METHOD_GET, body ? ctype : NULL, reply,
             timeout_ms))
        return -1;
    if (open_counted(body ? (int)len : 0, reused) != ESP_OK) {
        ESP_LOGW(TAG, "%s: no connection", path);
        drop();
        return -1;
    }
    if (body && len && esp_http_client_write(s_c, body, (int)len) != (int)len) {
        ESP_LOGW(TAG, "%s: short write", path);
        drop();
        return -1;
    }
    if (esp_http_client_fetch_headers(s_c) < 0) {
        ESP_LOGW(TAG, "%s: no response headers", path);
        drop();
        return -1;
    }
    const int status = esp_http_client_get_status_code(s_c);
    if (out && cap) {
        const int n = esp_http_client_read_response(s_c, out, (int)cap);
        if (n > 0 && got) *got = (size_t)n;
    }
    settle();
    return status;
}

int box_http_call(const char *path, const char *ctype, const char *body, size_t len, char *out,
                  size_t cap, size_t *got, box_reply_t *reply, int timeout_ms)
{
    if (got) *got = 0;
    bool reused;
    int status = once(path, ctype, body, len, out, cap, got, reply, timeout_ms, &reused);
    /* The box (or Caddy) closes an idle connection on its own schedule, and that is only seen
       when the next request on it fails. One retry on a fresh session, never more. */
    if (status < 0 && reused) status = once(path, ctype, body, len, out, cap, got, reply, timeout_ms,
                                            &reused);
    return status;
}

bool box_http_stream_open(const char *path, const char *ctype, int timeout_ms, bool reuse)
{
    if (!reuse || ms_since(s_used_us) > BOX_REUSE_FRESH_MS) drop();
    bool reused;
    if (!aim(path, HTTP_METHOD_POST, ctype, NULL, timeout_ms)) return false;
    esp_err_t err = open_counted(-1, &reused);
    if (err != ESP_OK && reused) {
        drop();
        err = open_counted(-1, &reused);
    }
    if (err != ESP_OK) {
        drop();
        return false;
    }
    return true;
}

static bool put(const void *data, size_t n)
{
    return esp_http_client_write(s_c, data, (int)n) == (int)n;
}

bool box_http_stream_write(const void *data, size_t n)
{
    if (!n) return true;
    char head[12];
    const int k = snprintf(head, sizeof head, "%x\r\n", (unsigned)n);
    return put(head, (size_t)k) && put(data, n) && put("\r\n", 2);
}

bool box_http_stream_end(void)
{
    if (put("0\r\n\r\n", 5)) return true;
    drop();
    return false;
}

int box_http_stream_finish(void)
{
    if (!box_http_stream_end() || esp_http_client_fetch_headers(s_c) < 0) {
        drop();
        return -1;
    }
    const int status = esp_http_client_get_status_code(s_c);
    settle();
    return status;
}

int box_http_reply_wait(int slice_ms)
{
    esp_http_client_set_timeout_ms(s_c, slice_ms);
    const int64_t r = esp_http_client_fetch_headers(s_c);
    if (r == -ESP_ERR_HTTP_EAGAIN) return 0;
    if (r < 0) {
        drop();
        return -1;
    }
    return esp_http_client_get_status_code(s_c);
}

int box_http_reply_read(void *buf, size_t cap, int slice_ms)
{
    esp_http_client_set_timeout_ms(s_c, slice_ms);
    const int n = esp_http_client_read(s_c, buf, (int)cap);
    if (n == -ESP_ERR_HTTP_EAGAIN) return BOX_HTTP_AGAIN;
    return n < 0 ? -1 : n;
}

void box_http_reply_done(void)
{
    settle();
}

void box_http_stream_abort(void)
{
    drop();
}

void box_http_note(char *out, size_t len)
{
    snprintf(out, len, "tls=hs:%lu,reuse:%lu,hs_ms:%lu,max_ms:%lu", (unsigned long)s_hs,
             (unsigned long)s_reuse, (unsigned long)s_hs_ms, (unsigned long)s_hs_max);
}
