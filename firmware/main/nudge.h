#pragma once

/* ── THE NUDGE: HOW THE BOX SAYS "COME AND ASK" ──────────────────────────────────────────
 *
 * A contentless UDP datagram on the LAN, and the panel answers it by doing exactly the
 * authenticated HTTPS poll it would have done anyway — just now instead of in a minute.
 *
 * THE OWNER ASKED FOR PUSH, AND THIS IS NOT QUITE PUSH, WHICH IS WORTH BEING HONEST ABOUT:
 * *"I want to get rid of polling altogether... when a message is sent it should be
 * instantaneously available on the next device."* The latency goal is met — a message reaches
 * the other twin in tens of milliseconds rather than at the next poll. What is NOT met is the
 * literal reading: a slow poll stays as the safety net, because UDP may be dropped and a
 * dropped datagram must not mean a message that never arrives. The nudge owns LATENCY; the
 * poll owns CORRECTNESS, and it can be slow precisely because it no longer owns latency.
 *
 * WHY A DATAGRAM AND NOT A REAL PUSH SOCKET — AND THE FIRST ANSWER HERE WAS WRONG, so it is
 * worth having the corrected one written down rather than quietly replaced.
 *
 * THE ARGUMENT THAT BUILT THIS FILE: `int_largest` reads exactly 31744 bytes on both panels and
 * never moves, a default mbedTLS session wants ~32 KB, therefore a second concurrent session
 * does not fit. Every step of that is either wrong or does not follow.
 *
 *   - 31744 = 32768 - 1024, and it never moves because it is a FLOOR, not a ceiling. Two 32 KB
 *     regions exist that the allocator only reaches at priority 1 — the DMA reserve this
 *     firmware itself asks for (`SPIRAM_MALLOC_RESERVE_INTERNAL=32768`) and the leftover from a
 *     32 KB data cache. Nothing takes from them at priority 0, so they sit pristine forever.
 *     `largest_free_block` maxes over every heap, so 31744 tells you the reserves are untouched
 *     and says NOTHING about the main heap, whose own largest block is somewhere at or below it.
 *   - "~32 KB of buffers" conflates a sum with a single allocation. mbedTLS makes two separate
 *     calloc()s: 16384+333 = 16717 for the RX record and 4096+333 = 4429 for TX (ESP-IDF
 *     already ships the asymmetric 16384/4096 default, so that saving is banked, not available).
 *     The largest CONTIGUOUS demand is ~16.6 KB, which 31744 houses with room to spare.
 *   - TLS asks for `MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT`, never `MALLOC_CAP_DMA`. On this chip
 *     those two internal pools nearly coincide, so the number was not far off — but the
 *     conclusion drawn from it was about the wrong thing.
 *   - And the refutation that needed no arithmetic at all: THIS FILE ALREADY DOES IT. Every
 *     accepted datagram wakes the jpanel task and the main task in the same instant, so a nudge
 *     fires two TLS handshakes concurrently, several times a day, and the panels are fine. The
 *     claim that two concurrent sessions are unaffordable was refuted by its own implementation.
 *
 * SO THE DATAGRAM IS A CHOICE, NOT A FORCED MOVE, and it is still a good one: it costs no TLS
 * session, no handshake, no certificate and about a kilobyte, and it degrades to the poll
 * underneath it. What it is NOT is the only thing this board can afford. ESPHome holds five
 * concurrent persistent push connections on a plain ESP32 — and four on an ESP8266 with 40 KB
 * free — by using a pre-shared-key Noise handshake over plain TCP instead of X.509 TLS, at
 * ~500-1000 bytes per connection and 32 bytes of static RAM for the crypto. That is the shape a
 * real push socket should take here if one is built, and it would replace this file.
 *
 * WHAT WAS ACTUALLY MISSING WAS A MEASUREMENT, and it is in telemetry now: `int_free`, the
 * total free INTERNAL heap. `free_heap` is `MALLOC_CAP_DEFAULT`, which on this build silently
 * includes PSRAM and so reads in the megabytes — which is how a budget nobody had measured came
 * to be argued about with such confidence.
 *
 * IT CARRIES NO DATA AND NO AUTHORITY, and that is the security design rather than an
 * omission. The datagram says only "something changed"; every actual fact still arrives over
 * the authenticated, TLS-protected poll that follows. So the worst an attacker with a foothold
 * on the LAN can do is make the panel ask the box a question it is entitled to ask — which is
 * why the rate limit below is the real defence and the magic word is not. Nothing here may
 * ever grow a payload the panel ACTS on; the moment it does, this becomes an unauthenticated
 * control channel into a child's bedroom.
 */

/* Both ends spell this, so a test pins that they agree — the same class of contract as the
   integrity header, which has already been got wrong twice by being written down twice. */
#define NUDGE_PORT 8267
/* Not security — a filter, so stray broadcast traffic on a home network cannot spin the poll.
   Anything that does not start with these bytes is dropped without a thought. */
#define NUDGE_MAGIC "JBN1"
#define NUDGE_MAGIC_LEN 4

/* THE RATE LIMIT IS THE DEFENCE. Without it a flood of datagrams is a flood of TLS handshakes
   against the box, from a device whose whole memory problem is TLS handshakes. One poll per
   window at most; everything else in that window is absorbed silently, because a nudge is a
   hint that state changed and two hints do not mean two questions. */
#define NUDGE_MIN_GAP_MS 250

/* WHOSE SLEEP THE SETTINGS HALF CUTS SHORT. The message half needs no such thing — the poll
   task waits on a queue and `jpanel_poll_soon()` posts to it — but the main task is sitting in
   a timed wait with nothing to signal, so it hands its own handle over here. Passed in rather
   than looked up, because "the main task" is a fact only `app_main` actually knows. */
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
void nudge_wake_settings_from(TaskHandle_t t);

/* Cut the main task's sleep short. Public because the socket's events (`link.h`, routed in
   `main.c`) wake exactly the same two halves a datagram does — two ways of hearing one thing. */
void nudge_wake_settings(void);

/* Bind the socket and start listening. Safe to call before Wi-Fi is up — the socket is bound
   to INADDR_ANY and simply receives nothing until there is a network. */
void nudge_start(void);

/* How many datagrams have been accepted and how many the rate limit absorbed, for telemetry:
   a push path that silently stopped working looks exactly like a quiet house, and this panel
   has been burned by that distinction before (see the render heartbeat in `display.c`). */
unsigned nudge_count(void);
unsigned nudge_dropped(void);
