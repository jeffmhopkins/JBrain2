/* The one connection. See link.h for why, and wsproto.h for the protocol and the deadline
 * arithmetic.
 *
 * 0.4.0 (plan §6a, §7.5a, §7.6): the persistent socket is compiled out (`LINK_PERSISTENT_SOCKET`
 * 0), so every request rides the HTTPS fallback, one session at a time under `s_http_lock`.
 * Every wait in both transports is cut into LINK_STEP_MS steps, each clamped to the request's
 * absolute deadline, ticking the caller's `on_progress` and polling its `cancelled`. */

#include "link.h"

#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>

#include "esp_attr.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#if LINK_PERSISTENT_SOCKET
#include "cJSON.h"
#include "esp_websocket_client.h"
#include "freertos/event_groups.h"
#include "freertos/stream_buffer.h"
#endif

#include "wsproto.h"

static const char *TAG = "link";

/* One HTTPS read or upload write. The read buffer is PSRAM, so its size costs no internal RAM. */
#define LINK_IO_BUF 2048
#define LINK_WRITE_CHUNK 4096

static const cfg_t *s_cfg;
static SemaphoreHandle_t s_http_lock;
static volatile bool s_started;
static volatile bool s_fallback;
static volatile bool s_suspended;
/* The HTTPS path's last real failure. Written by whoever holds `s_http_lock`, read by telemetry;
   a torn read costs a garbled string in one report, never a crash, because it stays
   NUL-terminated at its last byte. */
static char s_http_err[96];
static uint8_t *s_http_tmp; /* one HTTPS read, PSRAM; only the holder of `s_http_lock` uses it */
static bool s_suspend_holds_http;
/* The fallback's traffic, for a socket retry to find a quiet moment in (`wsp_retry_quiet`). */
static volatile uint32_t s_last_http_ms;
static int s_http_waiting; /* touched only through __atomic builtins */
static void (*s_event_fn)(const char *why);

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* A wait of `ms` as ticks, rounded UP: at 100 Hz a 5 ms remainder is 0 ticks, which would turn
   the last step before a deadline into a spin. 0 stays 0 (a poll). */
static TickType_t ticks(uint32_t ms)
{
    if (ms == 0) return 0;
    const TickType_t t = pdMS_TO_TICKS(ms);
    return t > 0 ? t : 1;
}

static int as_timeout(uint32_t ms)
{
    return ms > (uint32_t)INT_MAX ? INT_MAX : (int)ms;
}

/* ── one request's clock ────────────────────────────────────────────────────────────────── */

typedef struct {
    const link_req_t *r;
    uint32_t deadline;  /* in force: the caller's, moved by PROGRESS mode */
    uint32_t last_beat; /* the last `on_progress` call, for a TICK every LINK_STEP_MS */
} run_t;

static void beat(run_t *x, link_phase_t phase, uint32_t shown)
{
    x->last_beat = now_ms();
    if (x->r->on_progress != NULL) x->r->on_progress(x->r->ctx, phase, shown);
}

/* `shown` is the deadline in force: the request's until its head, the stall bound after. */
static void tick(run_t *x, uint32_t shown)
{
    if (link_tick_due(now_ms(), x->last_beat)) beat(x, LINK_PHASE_TICK, shown);
}

static bool cancel_asked(const run_t *x)
{
    return x->r->cancelled != NULL && x->r->cancelled(x->r->ctx);
}

static uint32_t left(const run_t *x)
{
    return link_left(now_ms(), x->deadline);
}

/* A legacy request (`deadline_ms == 0`) keeps 0.3.39's HTTPS meaning: `timeout_ms` for each
   phase — the lock, the open, each write, the head — rather than in total, so a caller not yet
   migrated behaves as it did. A migrated request's deadline is never touched here. */
static void phase(run_t *x)
{
    if (x->r->deadline_ms == 0) x->deadline = link_req_deadline(now_ms(), 0, x->r->timeout_ms);
}

static void frame_sent(run_t *x)
{
    x->deadline = link_after_frame(x->r->upload_mode, now_ms(), x->deadline);
    if (x->r->upload_mode == LINK_DEADLINE_PROGRESS) {
        beat(x, LINK_PHASE_UPLOAD, x->deadline);
    } else {
        tick(x, x->deadline);
    }
}

static void upload_done(run_t *x)
{
    x->deadline =
        link_after_upload(x->r->upload_mode, now_ms(), x->deadline, x->r->head_budget_ms);
    if (x->r->upload_mode == LINK_DEADLINE_PROGRESS) beat(x, LINK_PHASE_HEAD, x->deadline);
}

#if LINK_PERSISTENT_SOCKET

/* ── the persistent socket: compiled out since 0.4.0 (plan §6a) ─────────────────────────────
 *
 * Kept, with the same deadline rules as the HTTPS path, for the budgeted wave that would bring
 * it back; nothing here is built or allocated while the switch is 0. */

/* Requests in flight at once. Three tasks ask (main, talk, jpanel); the fourth is slack. The box
   allows the same number (`MAX_INFLIGHT`). */
#define LINK_SLOTS 4
/* THE WINDOW, and the size of the buffer it fills. A streamed answer (a voice-post message) is
   never more than this far ahead of what the caller has taken, so the socket's task can always
   hand a frame over without waiting — it never blocks on a slow speaker, which would stall every
   other request on the socket behind one message. Half a second of 16 kHz audio. */
#define LINK_WIN 16384
/* `esp_websocket_client`'s receive and send buffers, each, in INTERNAL RAM (they are malloc'd
   below the PSRAM threshold). A larger frame is delivered in pieces, which `wsp_rx_feed`
   reassembles; an upload chunk is sized to fit one frame exactly. */
#define LINK_WS_BUF 2048
#define LINK_TX_CHUNK (LINK_WS_BUF - WSP_ID_BYTES)
/* The socket task does the TLS handshake. 0.3.22 crash-looped on a 4096-byte stack doing exactly
   that (jpanel.c's push task), and every task here that handshakes has had at least 6144 since. */
#define LINK_WS_STACK 7168
#define LINK_OWNER_STACK 4096
#define LINK_CONNECT_MS 15000
/* A retry from the fallback holds the HTTPS lock while it handshakes, and a request that arrives
   meanwhile waits for it; so it gives up sooner than a first connect. A box that is reachable
   answers in one or two seconds. */
#define LINK_RETRY_CONNECT_MS 6000
/* How long an OTA's suspend waits for the socket to be gone: a TLS connect already under way
   cannot be interrupted short of its own network timeout (10 s), then the close (2 s). */
#define LINK_SUSPEND_MS 30000
#define LINK_SEND_MS 10000
#define LINK_TEXT_MAX 1024
/* The whole `res` frame is kept, so it is as large as any frame this reads. */
#define LINK_HEAD_MAX LINK_TEXT_MAX
/* A socket counts as having come up only once it has STAYED up this long. One the box accepts and
   then drops at once (a key revoked mid-flight, a proxy that kills upgrades) would otherwise reset
   the failure count every time and reconnect every two seconds forever — a handshake loop on the
   one board where handshakes are the expensive thing. */
#define LINK_PROVEN_MS 30000

#define EV_UP BIT0
#define EV_DOWN BIT1
#define EV_WAKE BIT2
#define EV_PARKED BIT3

typedef struct {
    bool used;
    uint32_t id;
    SemaphoreHandle_t sig;
    StreamBufferHandle_t sb;
    uint8_t *tx; /* one upload frame, PSRAM */
    uint8_t *rx; /* one stream read, PSRAM */
    bool streaming;
    uint8_t *buf;
    size_t cap;
    volatile bool head;
    volatile bool dead;
    volatile bool overflow;
    volatile int status;
    volatile size_t total;
    volatile size_t recvd;
    char headjson[LINK_HEAD_MAX];
} slot_t;

static EXT_RAM_BSS_ATTR char s_uri[300];
static EXT_RAM_BSS_ATTR char s_auth_hdr[300];
static esp_websocket_client_handle_t s_ws;
static SemaphoreHandle_t s_ws_lock;
static SemaphoreHandle_t s_slot_lock;
static EventGroupHandle_t s_ev;
/* In PSRAM (`EXT_RAM_BSS_ATTR`): about 5 KB of bookkeeping that has no business in the
   internal RAM the TLS session it serves needs. */
static EXT_RAM_BSS_ATTR slot_t s_slots[LINK_SLOTS];
static uint32_t s_next_id;
static wsp_policy_t s_policy;
static wsp_rx_t s_rx;
static EXT_RAM_BSS_ATTR char s_text[LINK_TEXT_MAX];
static uint8_t s_cur_op;

static volatile bool s_up;
static volatile uint32_t s_last_rx_ms;
static volatile int s_hs_status;
static unsigned s_connects;
static unsigned s_drops;
static unsigned s_events;
/* The socket's last real failure; written by its own task only. */
static char s_err[96];

/* ── the socket task's side: events and data ── */

static slot_t *find(uint32_t id)
{
    for (int i = 0; i < LINK_SLOTS; i++) {
        if (s_slots[i].used && s_slots[i].id == id) return &s_slots[i];
    }
    return NULL;
}

static void on_text(const char *text)
{
    cJSON *root = cJSON_Parse(text);
    if (root == NULL) return;
    const cJSON *t = cJSON_GetObjectItemCaseSensitive(root, "t");
    if (cJSON_IsString(t) && strcmp(t->valuestring, "res") == 0) {
        const cJSON *id = cJSON_GetObjectItemCaseSensitive(root, "id");
        const cJSON *st = cJSON_GetObjectItemCaseSensitive(root, "s");
        const cJSON *len = cJSON_GetObjectItemCaseSensitive(root, "len");
        if (cJSON_IsNumber(id) && cJSON_IsNumber(st) && cJSON_IsNumber(len)) {
            xSemaphoreTake(s_slot_lock, portMAX_DELAY);
            slot_t *s = find((uint32_t)id->valuedouble);
            if (s != NULL && !s->head) {
                s->status = st->valueint;
                s->total = (size_t)len->valuedouble;
                /* The raw frame, for the caller to walk on its own task: headers are a caller's
                   business and its callbacks should not run on the socket's stack. */
                strlcpy(s->headjson, text, sizeof(s->headjson));
                s->head = true;
                xSemaphoreGive(s->sig);
            }
            xSemaphoreGive(s_slot_lock);
        }
    } else if (cJSON_IsString(t) && strcmp(t->valuestring, "ev") == 0) {
        s_events++;
        const cJSON *why = cJSON_GetObjectItemCaseSensitive(root, "why");
        if (s_event_fn != NULL) {
            s_event_fn(cJSON_IsString(why) && why->valuestring != NULL ? why->valuestring : "");
        }
    }
    /* "hello" and "hb" carry nothing to act on: arriving at all is their whole message, and
       `s_last_rx_ms` has already recorded that. */
    cJSON_Delete(root);
}

static void on_body(uint32_t id, const uint8_t *p, size_t n)
{
    xSemaphoreTake(s_slot_lock, portMAX_DELAY);
    slot_t *s = find(id);
    if (s != NULL && s->head && !s->dead) {
        if (s->streaming) {
            /* NEVER BLOCKS: the box sends no more than the window, and the buffer IS the window.
               A box that oversends has broken the protocol, and the request fails loudly rather
               than this task waiting on a speaker. */
            const size_t put = xStreamBufferSend(s->sb, p, n, 0);
            if (put < n) {
                s->overflow = true;
                s->dead = true;
            }
        } else if (s->buf != NULL && s->recvd < s->cap) {
            const size_t room = s->cap - s->recvd;
            memcpy(s->buf + s->recvd, p, n < room ? n : room);
        }
        s->recvd += n;
        if (s->recvd >= s->total || s->dead) xSemaphoreGive(s->sig);
    }
    xSemaphoreGive(s_slot_lock);
}

static void on_data(const esp_websocket_event_data_t *d)
{
    s_last_rx_ms = now_ms();
    uint8_t op = d->op_code & 0x0F;
    if (op == 0x0) op = s_cur_op; /* a continuation of the message before it */
    if (op == 0x1 || op == 0x2) s_cur_op = op;
    if (op == 0x1) {
        if (d->payload_len >= (int)sizeof(s_text)) return; /* not a control frame this speaks */
        if (d->payload_offset + d->data_len > d->payload_len) return;
        memcpy(s_text + d->payload_offset, d->data_ptr, (size_t)d->data_len);
        if (d->payload_offset + d->data_len == d->payload_len) {
            s_text[d->payload_len] = '\0';
            on_text(s_text);
        }
    } else if (op == 0x2) {
        uint32_t id = 0;
        const uint8_t *p = NULL;
        const int n = wsp_rx_feed(&s_rx, d->payload_offset, (const uint8_t *)d->data_ptr,
                                  d->data_len, &id, &p);
        if (n > 0) on_body(id, p, (size_t)n);
    }
}

static void on_ws(void *arg, esp_event_base_t base, int32_t event, void *data)
{
    (void)arg;
    (void)base;
    const esp_websocket_event_data_t *d = data;
    switch (event) {
    case WEBSOCKET_EVENT_CONNECTED:
        s_last_rx_ms = now_ms();
        xEventGroupSetBits(s_ev, EV_UP);
        break;
    case WEBSOCKET_EVENT_ERROR: {
        /* THE ANSWER TO "connect". esp-tls's code, the TLS stack's, the socket errno and the
           upgrade's HTTP status: a starved handshake, a refused certificate, a box that is down
           and a box without the route are four different numbers here and were one word before. */
        const esp_websocket_error_codes_t *e = &d->error_handle;
        const char *what = "ws";
        if (e->error_type == WEBSOCKET_ERROR_TYPE_PONG_TIMEOUT) what = "pong-timeout";
        if (e->error_type == WEBSOCKET_ERROR_TYPE_HANDSHAKE) what = "upgrade";
        if (e->error_type == WEBSOCKET_ERROR_TYPE_TCP_TRANSPORT) what = "connect";
        if (e->error_type == WEBSOCKET_ERROR_TYPE_SERVER_CLOSE) what = "closed-by-box";
        s_hs_status = e->esp_ws_handshake_status_code;
        wsp_err_str(s_err, sizeof(s_err), what, (int)e->esp_tls_last_esp_err,
                    e->esp_tls_stack_err, e->esp_transport_sock_errno,
                    e->esp_ws_handshake_status_code);
        ESP_LOGW(TAG, "%s (internal free %u, largest %u)", s_err,
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
        break;
    }
    case WEBSOCKET_EVENT_CLOSED:
    case WEBSOCKET_EVENT_DISCONNECTED:
        if (d != NULL && d->close_status_code != 0) {
            /* 4000 replaced, 4403 revoked, 4408 idle, 4400 protocol — the box's own account of
               why it hung up, which is worth more than any guess from this end. (A refused key
               never gets here: the box closes before accepting, which arrives as `upgrade
               hs=403`.) */
            snprintf(s_err, sizeof(s_err), "closed=%d", d->close_status_code);
        }
        xEventGroupSetBits(s_ev, EV_DOWN);
        break;
    case WEBSOCKET_EVENT_DATA:
        on_data(d);
        break;
    default:
        break;
    }
}

/* ── the owner task ── */

/* `x` NULL is the owner task's own traffic (heartbeats, acks, cancels), which keeps the fixed
   LINK_SEND_MS; a request's lock wait and send are clamped to its deadline and tick. */
static int ws_send(run_t *x, bool binary, const void *data, int n)
{
    uint32_t budget = LINK_SEND_MS;
    if (x != NULL) {
        while (true) {
            const uint32_t l = left(x);
            if (l == 0) return -1;
            if (xSemaphoreTake(s_ws_lock, ticks(wsp_wait_step(l, LINK_STEP_MS))) == pdTRUE) break;
            tick(x, x->deadline);
        }
        const uint32_t l = left(x);
        if (l < budget) budget = l;
    } else if (xSemaphoreTake(s_ws_lock, pdMS_TO_TICKS(LINK_SEND_MS)) != pdTRUE) {
        return -1;
    }
    int sent = -1;
    if (s_ws != NULL && s_up && budget > 0) {
        sent = binary ? esp_websocket_client_send_bin(s_ws, data, n, ticks(budget))
                      : esp_websocket_client_send_text(s_ws, data, n, ticks(budget));
    }
    xSemaphoreGive(s_ws_lock);
    return sent;
}

static void fail_all(void)
{
    xSemaphoreTake(s_slot_lock, portMAX_DELAY);
    for (int i = 0; i < LINK_SLOTS; i++) {
        if (s_slots[i].used) {
            s_slots[i].dead = true;
            xSemaphoreGive(s_slots[i].sig);
        }
    }
    xSemaphoreGive(s_slot_lock);
}

static void teardown(bool graceful)
{
    /* A dead socket's "up" must not be read as the next one's. */
    xEventGroupClearBits(s_ev, EV_UP);
    xSemaphoreTake(s_ws_lock, portMAX_DELAY);
    esp_websocket_client_handle_t h = s_ws;
    s_ws = NULL;
    xSemaphoreGive(s_ws_lock);
    if (h == NULL) return;
    if (graceful && esp_websocket_client_is_connected(h)) {
        esp_websocket_client_close(h, pdMS_TO_TICKS(2000));
    }
    /* DESTROYED, NOT MERELY STOPPED, every time: that is what returns the TLS session's ~40 KB
       to the heap, and the OTA that follows a suspend is counting on having it. */
    esp_websocket_client_destroy(h);
}

static bool connect_once(uint32_t timeout_ms)
{
    esp_websocket_client_config_t wc = {
        .uri = s_uri,
        .headers = s_auth_hdr,
        .user_agent = "jbrain-panel",
        .disable_auto_reconnect = true,
        .buffer_size = LINK_WS_BUF,
        .task_stack = LINK_WS_STACK,
        .task_prio = 5,
        .task_name = "ws",
        .task_core_id_set = true,
        .task_core_id = 0, /* off the render core, like talk and jpanel */
        .network_timeout_ms = 10000,
        .ping_interval_sec = WSP_HEARTBEAT_MS / 1000,
        .pingpong_timeout_sec = WSP_DEAD_MS / 1000,
    };
    /* The same trust rule as every other request (`ota.c`'s `trust`): the box's own pinned root
       on the LAN, the public bundle otherwise, never both. */
    if (s_cfg->ca != NULL && s_cfg->ca[0] != '\0') {
        wc.cert_pem = s_cfg->ca;
    } else {
        wc.crt_bundle_attach = esp_crt_bundle_attach;
    }
    esp_websocket_client_handle_t h = esp_websocket_client_init(&wc);
    if (h == NULL) {
        snprintf(s_err, sizeof(s_err), "ws-init (internal free %u)",
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
        return false;
    }
    esp_websocket_register_events(h, WEBSOCKET_EVENT_ANY, on_ws, NULL);
    xEventGroupClearBits(s_ev, EV_UP | EV_DOWN);
    s_hs_status = 0;
    memset(&s_rx, 0, sizeof(s_rx));
    xSemaphoreTake(s_ws_lock, portMAX_DELAY);
    s_ws = h;
    xSemaphoreGive(s_ws_lock);
    if (esp_websocket_client_start(h) != ESP_OK) {
        snprintf(s_err, sizeof(s_err), "ws-start");
        return false;
    }
    /* A suspend cuts the wait short: the OTA waiting on it should not also wait out a handshake
       to a box that is not answering. */
    const uint32_t began = now_ms();
    EventBits_t bits = 0;
    while (true) {
        const uint32_t spent = now_ms() - began;
        if (spent >= timeout_ms) break;
        bits = xEventGroupWaitBits(s_ev, EV_UP | EV_DOWN | EV_WAKE, pdFALSE, pdFALSE,
                                   pdMS_TO_TICKS(timeout_ms - spent));
        if (bits & (EV_UP | EV_DOWN)) break;
        if (s_suspended) return false;
        xEventGroupClearBits(s_ev, EV_WAKE);
    }
    if ((bits & EV_UP) && !(bits & EV_DOWN)) return true;
    if (!(bits & (EV_UP | EV_DOWN))) snprintf(s_err, sizeof(s_err), "connect-timeout");
    return false;
}

/* Sleep that a suspend or resume cuts short. */
static void nap(uint32_t ms)
{
    xEventGroupWaitBits(s_ev, EV_WAKE, pdTRUE, pdFALSE, pdMS_TO_TICKS(ms));
}

static void nap_until_woken(void)
{
    xEventGroupWaitBits(s_ev, EV_WAKE, pdTRUE, pdFALSE, portMAX_DELAY);
}

/* Returns whether the socket stayed up long enough to count as having worked. */
static bool serve(void)
{
    const uint32_t began = now_ms();
    uint32_t last_hb = began;
    bool proven = false;
    while (true) {
        const EventBits_t bits =
            xEventGroupWaitBits(s_ev, EV_DOWN | EV_WAKE, pdFALSE, pdFALSE, pdMS_TO_TICKS(1000));
        if (bits & EV_DOWN) return proven;
        if (s_suspended) return true;
        xEventGroupClearBits(s_ev, EV_WAKE);
        const uint32_t now = now_ms();
        if (!proven && now - began >= LINK_PROVEN_MS) {
            proven = true;
            wsp_policy_up(&s_policy);
        }
        if (now - last_hb >= WSP_HEARTBEAT_MS) {
            char hb[16];
            const int n = wsp_hb(hb, sizeof(hb));
            if (n > 0) ws_send(NULL, false, hb, n);
            last_hb = now;
        }
        /* THE BOX IS THE LIVENESS TEST, as it is for the radio in `main.c`: it beats every twenty
           seconds, so a minute of nothing is a socket that only exists in this panel's memory —
           a router that rebooted, a box that restarted — whatever TCP still believes. */
        if (now - s_last_rx_ms > WSP_DEAD_MS) {
            snprintf(s_err, sizeof(s_err), "silent %u s", (unsigned)((now - s_last_rx_ms) / 1000));
            return proven;
        }
    }
}

static void owner_task(void *arg)
{
    (void)arg;
    while (true) {
        if (s_suspended) {
            xEventGroupSetBits(s_ev, EV_PARKED);
            nap_until_woken();
            continue;
        }
        xEventGroupClearBits(s_ev, EV_PARKED);
        const uint32_t now = now_ms();
        if (!wsp_policy_try_ws(&s_policy, now)) {
            s_fallback = true;
            nap(30 * 1000);
            continue;
        }
        /* THE HANDSHAKE TAKES THE SAME LOCK AS AN HTTPS REQUEST. In fallback a request may be
           mid-flight on its own session; retrying the socket on top of it would be the two
           concurrent handshakes this whole module exists to prevent. And a retry from the
           fallback never WAITS for that lock — it waits for a quiet moment, so a talk turn is
           never queued behind it except for the second or two a handshake takes. */
        const bool retrying = s_fallback;
        if (retrying) {
            const int waiting = __atomic_load_n(&s_http_waiting, __ATOMIC_SEQ_CST);
            if (!wsp_retry_quiet(now, s_last_http_ms, waiting) ||
                xSemaphoreTake(s_http_lock, 0) != pdTRUE) {
                nap(1000);
                continue;
            }
        } else {
            xSemaphoreTake(s_http_lock, portMAX_DELAY);
        }
        const bool up =
            !s_suspended && connect_once(retrying ? LINK_RETRY_CONNECT_MS : LINK_CONNECT_MS);
        if (up) {
            /* FLIPPED BEFORE THE LOCK IS GIVEN: a request queued on it then finds the socket up
               and takes it (`http_request` re-checks), instead of opening a second session
               beside it. */
            s_fallback = false;
            s_up = true;
        }
        xSemaphoreGive(s_http_lock);
        if (!up) {
            teardown(false);
            if (s_suspended) continue;
            wsp_policy_failed(&s_policy, s_hs_status, now_ms());
            s_fallback = s_policy.fallback;
            ESP_LOGW(TAG, "socket did not come up (%s), %d in a row%s", s_err, s_policy.fails,
                     s_fallback ? " — falling back to a request per connection" : "");
            if (!s_fallback) nap(wsp_backoff_ms(s_policy.fails));
            continue;
        }
        s_connects++;
        ESP_LOGI(TAG, "socket up (%u) — internal free %u, low-water %u", s_connects,
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                 (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL));
        const bool proven = serve();
        s_up = false;
        const bool asked = s_suspended;
        if (!asked) s_drops++;
        fail_all();
        teardown(asked);
        if (!asked) {
            if (!proven) {
                wsp_policy_failed(&s_policy, 0, now_ms());
                s_fallback = s_policy.fallback;
            }
            ESP_LOGW(TAG, "socket lost (%s)%s", s_err, s_fallback ? " — falling back" : "");
            if (!s_fallback) nap(wsp_backoff_ms(s_policy.fails));
        }
    }
}

static bool socket_start(const cfg_t *cfg)
{
    if (wsp_ws_uri(s_uri, sizeof(s_uri), cfg->api, "/endpoint/ws") < 0) {
        ESP_LOGE(TAG, "cannot build a socket address from %s", cfg->api);
        return false;
    }
    /* CHECKED, for `talk.c`'s reason: a truncated credential is a 401 that sends the next person
       looking at the server. */
    const int an = snprintf(s_auth_hdr, sizeof(s_auth_hdr), "Authorization: Bearer %s\r\n",
                            cfg->token);
    if (an < 0 || an >= (int)sizeof(s_auth_hdr)) {
        ESP_LOGE(TAG, "token too long for the socket header");
        return false;
    }
    s_ws_lock = xSemaphoreCreateMutex();
    s_slot_lock = xSemaphoreCreateMutex();
    s_ev = xEventGroupCreate();
    if (s_ws_lock == NULL || s_slot_lock == NULL || s_ev == NULL) return false;
    for (int i = 0; i < LINK_SLOTS; i++) {
        slot_t *s = &s_slots[i];
        s->sig = xSemaphoreCreateBinary();
        /* PSRAM: these are the panel's byte buckets, and internal RAM is what TLS needs. */
        s->sb = xStreamBufferCreateWithCaps(LINK_WIN, 1, MALLOC_CAP_SPIRAM);
        s->tx = heap_caps_malloc(LINK_WS_BUF, MALLOC_CAP_SPIRAM);
        s->rx = heap_caps_malloc(LINK_WS_BUF, MALLOC_CAP_SPIRAM);
        if (s->sig == NULL || s->sb == NULL || s->tx == NULL || s->rx == NULL) {
            ESP_LOGE(TAG, "no memory for request slot %d", i);
            return false;
        }
    }
    wsp_policy_init(&s_policy);
    if (xTaskCreatePinnedToCore(owner_task, "link", LINK_OWNER_STACK, NULL, 4, NULL, 0) != pdPASS) {
        return false;
    }
    ESP_LOGI(TAG, "one socket: %s", s_uri);
    return true;
}

/* ── the caller's side ── */

static slot_t *claim(const link_req_t *r)
{
    slot_t *got = NULL;
    xSemaphoreTake(s_slot_lock, portMAX_DELAY);
    for (int i = 0; i < LINK_SLOTS && got == NULL; i++) {
        slot_t *s = &s_slots[i];
        if (s->used) continue;
        s->used = true;
        if (++s_next_id == 0) s_next_id = 1;
        s->id = s_next_id;
        s->streaming = r->sink != NULL;
        s->buf = r->buf;
        s->cap = r->buf != NULL ? r->cap : 0;
        s->head = false;
        s->dead = false;
        s->overflow = false;
        s->status = -1;
        s->total = 0;
        s->recvd = 0;
        s->headjson[0] = '\0';
        xStreamBufferReset(s->sb);
        xSemaphoreTake(s->sig, 0);
        got = s;
    }
    xSemaphoreGive(s_slot_lock);
    return got;
}

static void release(slot_t *s)
{
    xSemaphoreTake(s_slot_lock, portMAX_DELAY);
    s->used = false;
    xSemaphoreGive(s_slot_lock);
}

static void cancel(uint32_t id)
{
    char c[48];
    const int n = wsp_cancel(c, sizeof(c), id);
    if (n > 0) ws_send(NULL, false, c, n);
}

static void headers_out(const slot_t *s, const link_req_t *r)
{
    if (r->on_header == NULL) return;
    cJSON *root = cJSON_Parse(s->headjson);
    if (root == NULL) return;
    const cJSON *h = cJSON_GetObjectItemCaseSensitive(root, "h");
    const cJSON *one = NULL;
    cJSON_ArrayForEach(one, h)
    {
        if (cJSON_IsString(one) && one->string != NULL && one->valuestring != NULL) {
            r->on_header(r->ctx, one->string, one->valuestring);
        }
    }
    cJSON_Delete(root);
}

static link_res_t ws_request(run_t *x)
{
    const link_req_t *r = x->r;
    link_res_t res = {.status = -1, .err = ""};
    slot_t *s = claim(r);
    if (s == NULL) {
        res.err = "ws-busy";
        return res;
    }
    char ctl[400];
    const int cn = wsp_req(ctl, sizeof(ctl), s->id, r->method, r->path, r->content_type,
                           r->hdr_name, r->hdr_value, r->body_len, r->sink != NULL ? LINK_WIN : 0);
    if (cn < 0) {
        res.err = "req-too-long";
        release(s);
        return res;
    }
    if (ws_send(x, false, ctl, cn) < 0) {
        res.timed_out = left(x) == 0;
        res.err = res.timed_out ? "send-timeout" : (s_err[0] ? s_err : "ws-send");
        release(s);
        return res;
    }
    /* The body in frames of one socket buffer each, so a 1.1 MB recording interleaves with the
       other tasks' small requests instead of holding the socket for its whole length. */
    for (size_t off = 0; off < r->body_len;) {
        const size_t n = r->body_len - off > LINK_TX_CHUNK ? LINK_TX_CHUNK : r->body_len - off;
        wsp_put_id(s->tx, s->id);
        memcpy(s->tx + WSP_ID_BYTES, r->body + off, n);
        if (ws_send(x, true, s->tx, (int)(n + WSP_ID_BYTES)) < 0) {
            res.timed_out = left(x) == 0;
            res.err = "upload-stall";
            /* Told, so the box frees the request's slot now rather than when it ages out. */
            cancel(s->id);
            release(s);
            return res;
        }
        off += n;
        frame_sent(x);
    }
    upload_done(x);

    /* The answer's head, in steps so a dropped socket, a deadline or a cancel ends it at once.
       A legacy request's one `timeout_ms` covers the upload too, as it did on the socket. */
    while (!s->head && !s->dead) {
        if (cancel_asked(x)) {
            res.stopped = true;
            break;
        }
        const uint32_t l = left(x);
        if (l == 0) {
            res.timed_out = true;
            break;
        }
        xSemaphoreTake(s->sig, ticks(wsp_wait_step(l, LINK_STEP_MS)));
        tick(x, x->deadline);
    }
    if (!s->head) {
        res.err = res.stopped     ? "cancelled"
                  : s->dead       ? (s_err[0] ? s_err : "ws-lost")
                  : res.timed_out ? "no-answer"
                                  : "ws-lost";
        if (!s->dead) cancel(s->id);
        release(s);
        return res;
    }
    res.status = s->status;
    headers_out(s, r);
    if (r->on_head != NULL && r->on_head(r->ctx, res.status) != 0) {
        res.stopped = true;
        if (s->recvd < s->total) cancel(s->id);
        release(s);
        return res;
    }

    /* The body: LINK_STALL_MS per network gap. Time inside the sink does not count — the clock
       restarts when it returns, because a sink blocked on a full ring is backpressure. */
    uint32_t last_byte = now_ms();
    if (s->streaming) {
        size_t consumed = 0, acked = 0;
        while (consumed < s->total) {
            if (cancel_asked(x)) {
                res.stopped = true;
                cancel(s->id);
                break;
            }
            const size_t n = xStreamBufferReceive(s->sb, s->rx, LINK_WS_BUF, ticks(LINK_STEP_MS));
            if (n > 0) {
                consumed += n;
                res.got = consumed;
                if (r->sink(r->ctx, s->rx, n) != 0) {
                    res.stopped = true;
                    if (consumed < s->total) cancel(s->id);
                    break;
                }
                last_byte = now_ms();
                const size_t credit = wsp_credit(consumed, &acked, LINK_WIN);
                if (credit > 0) {
                    char a[64];
                    const int an = wsp_ack(a, sizeof(a), s->id, credit);
                    if (an > 0) ws_send(NULL, false, a, an);
                }
                tick(x, link_deadline_in(last_byte, LINK_STALL_MS));
                continue;
            }
            if (s->dead && xStreamBufferIsEmpty(s->sb)) {
                res.cut = true;
                break;
            }
            if (link_left(now_ms(), last_byte + LINK_STALL_MS) == 0) {
                res.cut = true;
                res.err = "body-stall";
                cancel(s->id);
                break;
            }
            tick(x, link_deadline_in(last_byte, LINK_STALL_MS));
        }
        if (s->overflow) res.err = "window-overrun";
    } else {
        size_t seen = s->recvd;
        while (s->recvd < s->total && !s->dead) {
            if (cancel_asked(x)) {
                res.stopped = true;
                cancel(s->id);
                break;
            }
            xSemaphoreTake(s->sig, ticks(LINK_STEP_MS));
            if (s->recvd != seen) {
                seen = s->recvd;
                last_byte = now_ms();
            } else if (link_left(now_ms(), last_byte + LINK_STALL_MS) == 0) {
                res.err = "body-stall";
                cancel(s->id);
                break;
            }
            tick(x, link_deadline_in(last_byte, LINK_STALL_MS));
        }
        res.cut = !res.stopped && s->recvd < s->total;
        res.got = s->recvd < s->cap ? s->recvd : s->cap;
    }
    release(s);
    return res;
}

#endif /* LINK_PERSISTENT_SOCKET */

/* ── the HTTPS path: one session per request, one at a time ─────────────────────────────── */

static esp_err_t http_event(esp_http_client_event_t *e)
{
    const link_req_t *r = e->user_data;
    if (e->event_id == HTTP_EVENT_ON_HEADER && r != NULL && r->on_header != NULL &&
        e->header_key != NULL && e->header_value != NULL) {
        r->on_header(r->ctx, e->header_key, e->header_value);
    }
    return ESP_OK;
}

static void http_err(esp_http_client_handle_t c, esp_err_t err)
{
    int tls = 0, flags = 0;
    esp_http_client_get_and_clear_last_tls_error(c, &tls, &flags);
    snprintf(s_http_err, sizeof(s_http_err), "%s tls=0x%x sock=%d", esp_err_to_name(err),
             (unsigned)tls, esp_http_client_get_errno(c));
}

/* The lock in steps: a suspend, a cancel or the deadline ends the wait within one. */
static bool http_lock(run_t *x, link_res_t *res)
{
    phase(x);
    __atomic_add_fetch(&s_http_waiting, 1, __ATOMIC_SEQ_CST);
    bool got = false;
    while (true) {
        if (s_suspended) {
            res->err = "suspended";
            break;
        }
        if (cancel_asked(x)) {
            res->stopped = true;
            res->err = "cancelled";
            break;
        }
        const uint32_t l = left(x);
        if (l == 0) {
            res->timed_out = true;
            res->err = "http-busy";
            break;
        }
        if (xSemaphoreTake(s_http_lock, ticks(wsp_wait_step(l, LINK_STEP_MS))) == pdTRUE) {
            got = true;
            break;
        }
        tick(x, x->deadline);
    }
    __atomic_sub_fetch(&s_http_waiting, 1, __ATOMIC_SEQ_CST);
    return got;
}

static link_res_t http_request(run_t *x)
{
    const link_req_t *r = x->r;
    link_res_t res = {.status = -1, .err = ""};
    if (!http_lock(x, &res)) return res;
#if LINK_PERSISTENT_SOCKET
    /* The lock may have been held by a retry that brought the socket back: then this request
       is the socket's, and opening an HTTPS session now would be a second one beside it. */
    if (!s_fallback && s_up) {
        xSemaphoreGive(s_http_lock);
        return ws_request(x);
    }
#endif
    char url[288];
    char auth[256];
    const int un = snprintf(url, sizeof(url), "%s%s", s_cfg->api, r->path);
    const int an = snprintf(auth, sizeof(auth), "Bearer %s", s_cfg->token);
    if (un < 0 || un >= (int)sizeof(url) || an < 0 || an >= (int)sizeof(auth)) {
        res.err = "url-too-long";
        xSemaphoreGive(s_http_lock);
        return res;
    }
    /* The open's own timeout is a third of what is left; it applies per phase (connect, then
       each handshake read), so three phases still fit. */
    phase(x);
    const uint32_t open_left = left(x);
    if (open_left == 0) {
        res.timed_out = true;
        res.err = "open-timeout";
        xSemaphoreGive(s_http_lock);
        return res;
    }
    esp_http_client_config_t hc = {
        .url = url,
        .timeout_ms = r->deadline_ms == 0 ? as_timeout(open_left) : link_open_timeout_ms(open_left),
        .method = strcmp(r->method, "POST") == 0 ? HTTP_METHOD_POST : HTTP_METHOD_GET,
        .event_handler = http_event,
        .user_data = (void *)r,
    };
    if (s_cfg->ca != NULL && s_cfg->ca[0] != '\0') {
        hc.cert_pem = s_cfg->ca;
    } else {
        hc.crt_bundle_attach = esp_crt_bundle_attach;
    }
    esp_http_client_handle_t c = esp_http_client_init(&hc);
    if (c == NULL) {
        res.err = "client-init";
        xSemaphoreGive(s_http_lock);
        return res;
    }
    esp_http_client_set_header(c, "Authorization", auth);
    if (r->content_type != NULL) esp_http_client_set_header(c, "Content-Type", r->content_type);
    if (r->hdr_name != NULL && r->hdr_value != NULL) {
        esp_http_client_set_header(c, r->hdr_name, r->hdr_value);
    }
    esp_err_t err = esp_http_client_open(c, (int)r->body_len);
    if (err != ESP_OK) {
        http_err(c, err);
        res.timed_out = left(x) == 0;
        res.err = s_http_err;
        goto done;
    }
    /* Each write is one blocking call, bounded by what is left; PROGRESS mode then moves the
       deadline to LINK_STALL_MS past it and tells the engine. */
    for (size_t off = 0; off < r->body_len;) {
        if (cancel_asked(x)) {
            res.stopped = true;
            res.err = "cancelled";
            goto done;
        }
        phase(x);
        const uint32_t l = left(x);
        if (l == 0) {
            res.timed_out = true;
            res.err = "upload-timeout";
            goto done;
        }
        esp_http_client_set_timeout_ms(c, as_timeout(l));
        const size_t rest = r->body_len - off;
        const int n = esp_http_client_write(c, (const char *)r->body + off,
                                            rest > LINK_WRITE_CHUNK ? LINK_WRITE_CHUNK : (int)rest);
        if (n <= 0) {
            res.timed_out = left(x) == 0;
            res.err = "upload-stall";
            goto done;
        }
        off += (size_t)n;
        frame_sent(x);
    }
    upload_done(x);
    phase(x);
    /* THE HEAD IN STEPS. `esp_http_client_fetch_headers` with a LINK_STEP_MS timeout returns
       EAGAIN with its parser state kept, so a 60 s transcription ticks the engine and can be
       cancelled, instead of one blind read the length of the whole budget. */
    while (true) {
        if (cancel_asked(x)) {
            res.stopped = true;
            res.err = "cancelled";
            goto done;
        }
        const uint32_t l = left(x);
        if (l == 0) {
            res.timed_out = true;
            res.err = "head-timeout";
            goto done;
        }
        esp_http_client_set_timeout_ms(c, as_timeout(wsp_wait_step(l, LINK_STEP_MS)));
        const int64_t h = esp_http_client_fetch_headers(c);
        if (h >= 0) break;
        if (h != -ESP_ERR_HTTP_EAGAIN) {
            res.err = "no-headers";
            goto done;
        }
        tick(x, x->deadline);
    }
    res.status = esp_http_client_get_status_code(c);
    if (r->on_head != NULL && r->on_head(r->ctx, res.status) != 0) {
        res.stopped = true;
        goto done;
    }
    /* THE BODY: LINK_STALL_MS per network gap, read in LINK_STEP_MS steps so a cancel and the
       engine's heartbeat are never further apart than one. Time inside the sink does not count:
       the clock restarts when it returns, since a sink blocked on a full ring is backpressure. */
    esp_http_client_set_timeout_ms(c, LINK_STEP_MS);
    uint8_t *tmp = s_http_tmp;
    uint32_t last_byte = now_ms();
    bool stalled = false;
    while (true) {
        if (cancel_asked(x)) {
            res.stopped = true;
            break;
        }
        int n;
        const bool into_buf = r->sink == NULL && r->buf != NULL && res.got < r->cap;
        if (into_buf) {
            n = esp_http_client_read(c, (char *)r->buf + res.got, (int)(r->cap - res.got));
        } else {
            /* A sink's chunk, or past the caller's buffer (or no buffer at all): drained so the
               session ends cleanly, and dropped. */
            n = esp_http_client_read(c, (char *)tmp, LINK_IO_BUF);
        }
        if (n == -ESP_ERR_HTTP_EAGAIN) {
            if (link_left(now_ms(), last_byte + LINK_STALL_MS) == 0) {
                stalled = true;
                break;
            }
            tick(x, link_deadline_in(last_byte, LINK_STALL_MS));
            continue;
        }
        if (n <= 0) break;
        if (r->sink != NULL) {
            res.got += (size_t)n;
            if (r->sink(r->ctx, tmp, (size_t)n) != 0) {
                res.stopped = true;
                break;
            }
        } else if (into_buf) {
            res.got += (size_t)n;
        }
        last_byte = now_ms();
        tick(x, link_deadline_in(last_byte, LINK_STALL_MS));
    }
    const int64_t declared = esp_http_client_get_content_length(c);
    if (stalled) {
        res.cut = true;
        res.err = "body-stall";
    } else if (!res.stopped && declared > 0 && !esp_http_client_is_complete_data_received(c)) {
        res.cut = true;
    }

done:
    esp_http_client_cleanup(c);
    s_last_http_ms = now_ms();
    xSemaphoreGive(s_http_lock);
    return res;
}

/* ── the API ────────────────────────────────────────────────────────────────────────────── */

bool link_start(const cfg_t *cfg)
{
    if (cfg == NULL || s_started) return s_started;
    s_cfg = cfg;
    /* Guarded, because `main.c` calls this again after a failed start. */
    if (s_http_lock == NULL) s_http_lock = xSemaphoreCreateMutex();
    if (s_http_tmp == NULL) s_http_tmp = heap_caps_malloc(LINK_IO_BUF, MALLOC_CAP_SPIRAM);
    if (s_http_lock == NULL || s_http_tmp == NULL) return false;
    /* The head is fetched in LINK_STEP_MS steps, and esp_http_client warns "timed out before
       data was ready" on every one: 240 lines per 60 s transcription would drown the console.
       Its errors still print; every failure that matters is in `res.err` anyway. */
    esp_log_level_set("HTTP_CLIENT", ESP_LOG_ERROR);
#if LINK_PERSISTENT_SOCKET
    if (!socket_start(cfg)) return false;
#else
    /* NO SOCKET, NO OWNER TASK: nothing here holds a TLS session between requests, which is
       the ~48 KB that made 0.3.39 abort at boot. */
    s_fallback = true;
    ESP_LOGI(TAG, "https, one session at a time");
#endif
    s_started = true;
    return true;
}

link_res_t link_request(const link_req_t *req)
{
    link_res_t res = {.status = -1, .err = "no-link"};
    if (!s_started || req == NULL || req->method == NULL || req->path == NULL) return res;
    if (s_suspended) {
        res.err = "suspended";
        return res;
    }
    const uint32_t t0 = now_ms();
    run_t x = {.r = req,
               .deadline = link_req_deadline(t0, req->deadline_ms, req->timeout_ms),
               .last_beat = t0};
    if (s_fallback) return http_request(&x);
#if LINK_PERSISTENT_SOCKET
    /* THE SOCKET IS THE TRANSPORT, so a request does not open a session of its own while it is
       reconnecting — that would be the second concurrent handshake. It waits for one. */
    while (!s_up && !s_fallback && !s_suspended && now_ms() - t0 < LINK_UP_WAIT_MS) {
        if (cancel_asked(&x)) {
            res.stopped = true;
            res.err = "cancelled";
            return res;
        }
        const uint32_t l = left(&x);
        if (l == 0) {
            res.timed_out = true;
            res.err = s_err[0] ? s_err : "ws-down";
            return res;
        }
        vTaskDelay(ticks(wsp_wait_step(l, 50)));
        tick(&x, x.deadline);
    }
    if (s_up) return ws_request(&x);
    /* It fell back while this request waited — a box without the route says so on the first
       attempt — and the fallback is exactly what this request should now use. */
    if (s_fallback) return http_request(&x);
    res.err = s_err[0] ? s_err : "ws-down";
#endif
    return res;
}

void link_suspend(void)
{
    if (!s_started) return;
    s_suspended = true;
#if LINK_PERSISTENT_SOCKET
    xEventGroupSetBits(s_ev, EV_WAKE);
    /* Parked means the socket is destroyed and its session's memory is back. Bounded: a stuck
       teardown must not stop an update that is the one thing that could fix it. */
    const EventBits_t parked = xEventGroupWaitBits(s_ev, EV_PARKED, pdFALSE, pdFALSE,
                                                   pdMS_TO_TICKS(LINK_SUSPEND_MS));
    if (!(parked & EV_PARKED)) ESP_LOGW(TAG, "socket still closing after %d ms", LINK_SUSPEND_MS);
#endif
    /* And no request is mid-session either. Held until `link_resume`; a request already queued
       on the lock sees `s_suspended` within one step and leaves. */
    s_suspend_holds_http = xSemaphoreTake(s_http_lock, pdMS_TO_TICKS(60000)) == pdTRUE;
    ESP_LOGI(TAG, "suspended — internal free %u", (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
}

void link_resume(void)
{
    if (!s_started) return;
    s_suspended = false;
    if (s_suspend_holds_http) xSemaphoreGive(s_http_lock);
    s_suspend_holds_http = false;
#if LINK_PERSISTENT_SOCKET
    xEventGroupSetBits(s_ev, EV_WAKE);
#endif
}

void link_on_event(void (*fn)(const char *why))
{
    s_event_fn = fn;
}

#if LINK_PERSISTENT_SOCKET

bool link_live(void)
{
    return s_up;
}

const char *link_mode(void)
{
    if (!s_started) return "off";
    if (s_up) return "ws";
    if (s_fallback) return "http";
    return "down";
}

void link_stats(unsigned *connects, unsigned *drops, unsigned *events, const char **err)
{
    if (connects != NULL) *connects = s_connects;
    if (drops != NULL) *drops = s_drops;
    if (events != NULL) *events = s_events;
    if (err != NULL) *err = s_err;
}

#else

/* No socket: the box never speaks first, so the settings poll keeps its own pace. */
bool link_live(void)
{
    return false;
}

const char *link_mode(void)
{
    return s_started ? "http" : "off";
}

/* The report's `ws_err` carries the HTTPS path's last failure instead, the only transport there
   is: an empty field would hide the one reason the owner can read through the debug API. */
void link_stats(unsigned *connects, unsigned *drops, unsigned *events, const char **err)
{
    if (connects != NULL) *connects = 0;
    if (drops != NULL) *drops = 0;
    if (events != NULL) *events = 0;
    if (err != NULL) *err = s_http_err;
}

#endif
