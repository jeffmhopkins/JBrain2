#pragma once

/* ── THE ONE CONNECTION ──────────────────────────────────────────────────────────────────────
 *
 * Every request this panel makes to its box goes through here: the settings poll, telemetry,
 * the manifest, a talk turn, and all of voice post. Over ONE TLS WebSocket (`/endpoint/ws`)
 * when the box offers it, and over the old one-HTTPS-session-per-request path when it does not.
 *
 * WHY. Measured on Quorra's panel, 0.3.38, 2026-10-04: after about two talk turns every request
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
#include "link_types.h"

/* 0.4.0 (plan §6a): the persistent TLS WebSocket is OFF. Its ~48 KB of internal RAM is what
   made 0.3.39 abort at boot (the PHY's allocation failed after speech started). `link_start`
   comes up in the HTTPS fallback — one session at a time under one lock — and push reaches the
   panel through the nudge and the poll. The socket code stays, unreachable, behind this switch;
   turning it on is a budgeted wave of its own, not planned. */
#define LINK_PERSISTENT_SOCKET 0

/* Starts the owner task. Call once, after Wi-Fi is up. False means no link: every request then
   fails with "no-link" and the panel is still a pet. */
bool link_start(const cfg_t *cfg);

link_res_t link_request(const link_req_t *req);

/* KEEP ONE HTTPS CONNECTION BETWEEN REQUESTS (0.4.2). With the socket off, every request was a
   fresh TLS handshake, and the settings poll alone is one every 3 s: 1-2 s of mbedTLS each, taken
   from the render task and the producer filling the speaker. The owner saw the animation lose its
   smoothness on 0.4.0's first afternoon. Still one session at a time, under the same lock; the
   request after it reuses it, so the handshake is paid once, not twenty times a minute.

   OFF UNTIL `main.c` TURNS IT ON, after the recogniser has started: a session held through boot
   would be the ~32 KB of internal RAM `speech_start` needs, which is §6a's whole lesson. While on,
   the steady state is what a request in flight already cost on 0.4.0, so the low-water mark the
   first 0.4.0 panel reported (with a session open) is unchanged. */
void link_keep_alive(bool on);

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
   "down" (the socket is the transport and is reconnecting), or "off" (never started). Without the
   socket, "https-keep" is the kept connection and `connects` counts TLS handshakes since boot,
   `drops` the kept connections the box had closed by the time they were reused. */
const char *link_mode(void);
void link_stats(unsigned *connects, unsigned *drops, unsigned *events, const char **err);
