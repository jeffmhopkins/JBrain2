#pragma once

/* The request and answer shapes `link.c` takes, split out of `link.h` so the engine core
 * (`jpanel_core.c`) and its host fakes can build them without ESP-IDF. `link.h` includes this.
 *
 * 0.4.0 adds absolute deadlines and progress (plan §7.5a). A request with `deadline_ms == 0` keeps
 * the old per-request `timeout_ms` meaning, so callers migrate one at a time. */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* The link's clock constants, here rather than in `link.c` because the engine builds its SEND
   bound (claim + LINK_UP_WAIT_MS + LINK_STALL_MS) and its UI margin from them (plan §7.5). */
/* Every wait is cut into steps this long: the most a deadline, a cancel or a TICK can lag. */
#define LINK_STEP_MS 250
/* A body that has stopped flowing for this long is a connection that has died under it. */
#define LINK_STALL_MS 20000
/* How long a request waits for a socket that is reconnecting (unused while the socket is off). */
#define LINK_UP_WAIT_MS 8000

/* Body bytes as they arrive, on the caller's task. Return nonzero to stop: the rest of the
   answer is abandoned (a finger stopped a message). May block — that IS the backpressure. */
typedef int (*link_sink_fn)(void *ctx, const uint8_t *data, size_t n);
/* Each response header the box passed through (`X-…` and `Content-Type`), before any body. */
typedef void (*link_header_fn)(void *ctx, const char *key, const char *value);
/* The status, once headers are in and before the first body byte. Nonzero abandons the body —
   which is how a caller says "not 200, I do not want it" or "the speaker is busy". */
typedef int (*link_head_fn)(void *ctx, int status);

typedef enum {
    LINK_PHASE_UPLOAD = 0, /* a request-body frame went out; deadline refreshed (PROGRESS mode) */
    LINK_PHASE_HEAD,       /* the upload ended; deadline = now + head_budget_ms */
    /* A wait loop's LINK_STEP_MS heartbeat. Carries the deadline in force: the request's
       until the head, then the body's stall bound (last byte + LINK_STALL_MS). */
    LINK_PHASE_TICK,
} link_phase_t;

typedef enum {
    /* `deadline_ms` bounds the request up to its head: every wait is clamped to what is left.
       The body is then bounded by LINK_STALL_MS per network gap (plan §7.5a). */
    LINK_DEADLINE_ABSOLUTE = 0,
    /* jpanel's SEND only: the deadline moves with the upload (LINK_STALL_MS per frame), then
       becomes now + head_budget_ms once the body is out. */
    LINK_DEADLINE_PROGRESS,
} link_upload_mode_t;

/* Called on the requesting task. The engine bumps its heartbeat and republishes deadline_ms. */
typedef void (*link_progress_fn)(void *ctx, link_phase_t phase, uint32_t deadline_ms);
/* Polled between steps (HTTPS) and every LINK_STEP_MS (socket waits). True abandons the request
   with `stopped`. NULL for a request that must never be cancelled (SEND, acks). */
typedef bool (*link_cancelled_fn)(void *ctx);

typedef struct {
    const char *method; /* "GET" or "POST" */
    const char *path;   /* below the api base, query included: "/jpanel/next?at=0" */
    const char *content_type;
    const char *hdr_name; /* one optional extra request header — the integrity digest */
    const char *hdr_value;
    const uint8_t *body;
    size_t body_len;
    int timeout_ms; /* legacy: for the answer to begin; used only when deadline_ms == 0 */
    /* The body goes to exactly one of these. `buf` takes up to `cap` bytes and the rest of a
       longer answer is drained and dropped; `sink` streams under a window. */
    uint8_t *buf;
    size_t cap;
    link_sink_fn sink;
    link_header_fn on_header;
    link_head_fn on_head;
    void *ctx;
    /* 0.4.0 (plan §7.5a). Absolute, in the same ms clock as esp_timer/1000; compared with a
       wrap-safe signed difference. */
    uint32_t deadline_ms;
    link_upload_mode_t upload_mode;
    uint32_t head_budget_ms; /* PROGRESS mode only */
    link_progress_fn on_progress;
    link_cancelled_fn cancelled;
} link_req_t;

typedef struct {
    int status;      /* the box's HTTP status; -1 when there was no answer at all */
    size_t got;      /* body bytes delivered to `buf` or `sink` */
    bool cut;        /* the body ended before its declared length — a lost connection */
    bool stopped;    /* the caller's sink, head callback or `cancelled` asked to stop */
    bool timed_out;  /* the deadline passed (0.4.0) */
    /* WHY THERE WAS NO ANSWER, never just "connect": the socket's last real error (esp-tls code,
       TLS stack code, errno, upgrade status) or the HTTPS path's `esp_err` name and errno.
       Static storage, valid until a later failure of the same transport replaces it — which is
       what `reach_fail`'s keep-the-pointer contract needs. "" when there was an answer. */
    const char *err;
} link_res_t;
