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
 * WHY NOT A REAL PUSH SOCKET, MEASURED RATHER THAN ASSUMED. Both panels report `int_largest`
 * — the largest free INTERNAL DMA block — at exactly 31744 bytes, and it does not move: same
 * number at 7 seconds of uptime and at 47 minutes, across two firmware versions. That is a
 * structural ceiling in the memory map rather than fragmentation drifting. A default mbedTLS
 * session wants about 32 KB of buffers, so a persistent WebSocket or MQTT connection HELD OPEN
 * while a message is also streaming means two concurrent TLS sessions against a 31 KB budget —
 * the ESP-SR-versus-radio out-of-memory fault (`ROOM_ENDPOINT_PLAN.md` §10) in new clothes.
 *
 * A UDP socket needs no TLS, no handshake and no second session. That is the whole reason this
 * design wins here: it buys the latency without spending the one resource this board has none
 * of. If `int_largest` ever lifts — moving mbedTLS buffers to PSRAM is the obvious lever, and
 * there are megabytes of it free — a real bidirectional socket becomes affordable and this file
 * is what it would replace.
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

/* Bind the socket and start listening. Safe to call before Wi-Fi is up — the socket is bound
   to INADDR_ANY and simply receives nothing until there is a network. */
void nudge_start(void);

/* How many datagrams have been accepted and how many the rate limit absorbed, for telemetry:
   a push path that silently stopped working looks exactly like a quiet house, and this panel
   has been burned by that distinction before (see the render heartbeat in `display.c`). */
unsigned nudge_count(void);
unsigned nudge_dropped(void);
