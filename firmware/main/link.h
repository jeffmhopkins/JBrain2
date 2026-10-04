#pragma once

/* ── THE ONE CONNECTION ──────────────────────────────────────────────────────────────────────
 *
 * Every request this panel makes to its box goes through here: the settings poll, telemetry,
 * the manifest, a talk turn, and all of voice post. Over ONE TLS WebSocket (`/endpoint/ws`)
 * when the box offers it, and over the old one-HTTPS-session-per-request path when it does not.
 *
 * WHY. Measured on Elora's panel, 0.3.38, 2026-10-04: after about two talk turns every request
 * failed with "connect" — talk, the settings poll, the jpanel poll, a send. Internal heap was
 * ~75 KB free with a 25.6 KB largest block, and each request was a fresh mbedTLS session
 * (~40 KB internal, 1-2 s of handshake). The main task's three-second settings poll, the jpanel
 * task's poll and a talk turn each opened their own; two handshakes in flight at once do not
 * fit. No per-turn leak was found. So the panel now holds one session and puts everything on it.
 *
 * THE INVARIANT IS "NEVER TWO TLS SESSIONS AT ONCE", and every rule below serves it:
 *   - While the socket is the transport and is down, a request does NOT open its own HTTPS
 *     session as a shortcut — that would be the second session. It waits a few seconds for the
 *     socket and otherwise fails with the socket's real error.
 *   - On the HTTPS fallback, requests take turns (one lock), and the socket's own retry takes
 *     the same lock.
 *   - The OTA download (`esp_https_ota`, its own session) runs with the socket closed and the
 *     lock held: `link_suspend` before, `link_resume` after.
 *
 * ONE TASK OWNS THE SOCKET (connect, heartbeat, liveness, reconnect with backoff, fallback);
 * callers block on their own task in `link_request`, exactly as they blocked in
 * `esp_http_client` before, so nothing about who-waits-where changed for them.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cfg.h"

/* Body bytes as they arrive, on the caller's task. Return nonzero to stop: the rest of the
   answer is abandoned (a finger stopped a message). May block — that IS the backpressure. */
typedef int (*link_sink_fn)(void *ctx, const uint8_t *data, size_t n);
/* Each response header the box passed through (`X-…` and `Content-Type`), before any body. */
typedef void (*link_header_fn)(void *ctx, const char *key, const char *value);
/* The status, once headers are in and before the first body byte. Nonzero abandons the body —
   which is how a caller says "not 200, I do not want it" or "the speaker is busy". */
typedef int (*link_head_fn)(void *ctx, int status);

typedef struct {
    const char *method; /* "GET" or "POST" */
    const char *path;   /* below the api base, query included: "/jpanel/next?at=0" */
    const char *content_type;
    const char *hdr_name; /* one optional extra request header — the integrity digest */
    const char *hdr_value;
    const uint8_t *body;
    size_t body_len;
    int timeout_ms; /* for the answer to begin; the body then flows as fast as it drains */
    /* The body goes to exactly one of these. `buf` takes up to `cap` bytes and the rest of a
       longer answer is drained and dropped; `sink` streams under a window. */
    uint8_t *buf;
    size_t cap;
    link_sink_fn sink;
    link_header_fn on_header;
    link_head_fn on_head;
    void *ctx;
} link_req_t;

typedef struct {
    int status;      /* the box's HTTP status; -1 when there was no answer at all */
    size_t got;      /* body bytes delivered to `buf` or `sink` */
    bool cut;        /* the body ended before its declared length — a lost connection */
    bool stopped;    /* the caller's sink or head callback asked to stop */
    /* WHY THERE WAS NO ANSWER, never just "connect": the socket's last real error (esp-tls code,
       TLS stack code, errno, upgrade status) or the HTTPS path's `esp_err` name and errno.
       Static storage, valid until a later failure of the same transport replaces it — which is
       what `reach_fail`'s keep-the-pointer contract needs. "" when there was an answer. */
    const char *err;
} link_res_t;

/* Starts the owner task. Call once, after Wi-Fi is up. False means no link: every request then
   fails with "no-link" and the panel is still a pet. */
bool link_start(const cfg_t *cfg);

link_res_t link_request(const link_req_t *req);

/* For the OTA: close the socket, wait for its session to be gone, and hold every other request
   off (they fail with "suspended") until `link_resume`. */
void link_suspend(void);
void link_resume(void);

/* "Come and ask" from the box (`{"t":"ev"}`): the push the SSE stream and the UDP nudge carry.
   Called on the socket's task, so it must only post — `nudge_wake_settings`, `jpanel_poll_soon`. */
void link_on_event(void (*fn)(const char *why));

/* Whether the socket is up: the box can speak first, so the settings poll can relax. */
bool link_live(void);

/* For telemetry. `mode` is "ws" (the socket is carrying everything), "http" (fell back),
   "down" (the socket is the transport and is reconnecting), or "off" (never started). */
const char *link_mode(void);
void link_stats(unsigned *connects, unsigned *drops, unsigned *events, const char **err);
