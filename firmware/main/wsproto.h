#pragma once

/* THE PANEL SOCKET'S PROTOCOL, the pure half — no ESP-IDF, built and asserted on the host.
 *
 * `link.c` owns the connection; this file owns the arithmetic and the spelling, so the parts a
 * mistake in would only show up on a panel in a bedroom (a frame id read off the wrong bytes,
 * a window acked too late, a fallback that never comes back) are tested in CI instead.
 *
 * The box's half is `backend/src/jbrain/api/panel_ws.py`, and its docstring is the protocol's
 * reference. In short:
 *
 *   panel -> box   {"t":"req","id":N,"m":"GET","p":"/endpoint/settings","h":{..},"len":B,"win":W}
 *                  then B body bytes as binary frames `[id u32 LE][bytes]`
 *                  {"t":"ack","id":N,"n":K}   {"t":"cancel","id":N}   {"t":"hb"}
 *   box -> panel   {"t":"hello","v":1,...}   {"t":"res","id":N,"s":200,"h":{..},"len":M}
 *                  then M bytes as `[id][chunk]` frames   {"t":"ev","why":".."}   {"t":"hb"}
 *
 * Constants both ends spell are pinned from the box's test suite
 * (`test_the_firmware_spells_the_same_route_and_frame`), like the nudge port. */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define WSP_VERSION 1
#define WSP_ID_BYTES 4
/* Both ends beat this often; the box drops a socket that has been silent for 75 s, so three
   missed beats from the panel still fit. */
#define WSP_HEARTBEAT_MS 20000
/* And the panel's own cut: a box that has said nothing — not a heartbeat, not an answer — for
   this long is gone, whatever the radio thinks. */
#define WSP_DEAD_MS 60000

/* HOW MANY TIMES THE SOCKET MAY FAIL TO COME UP BEFORE THE PANEL STOPS INSISTING ON IT.
   After this it falls back to one HTTPS request per connection — the 0.3.38 behaviour, which
   is exactly right for a box that predates the socket route — and tries the socket again every
   `WSP_FALLBACK_RETRY_MS`. A box that answers the upgrade with 404 or 405 has no such route at
   all, which is not a transient fault worth three attempts: that falls back at once. */
#define WSP_FALLBACK_AFTER 3
#define WSP_FALLBACK_RETRY_MS (10 * 60 * 1000)
#define WSP_BACKOFF_MIN_MS 2000
#define WSP_BACKOFF_MAX_MS 60000

/* The id in front of every binary frame, little-endian. */
void wsp_put_id(uint8_t out[WSP_ID_BYTES], uint32_t id);
uint32_t wsp_get_id(const uint8_t in[WSP_ID_BYTES]);

/* The control frames, written into `out`. Return the length, or -1 when it does not fit or a
   string would need escaping — every string here is the firmware's own (a route, a MIME type,
   a hex digest), so one that needs escaping is a bug to refuse rather than a case to handle.
   `hname`/`hval` and `ctype` may be NULL. `win` 0 means "send the whole answer". */
int wsp_req(char *out, size_t cap, uint32_t id, const char *method, const char *path,
            const char *ctype, const char *hname, const char *hval, size_t len, size_t win);
int wsp_ack(char *out, size_t cap, uint32_t id, size_t n);
int wsp_cancel(char *out, size_t cap, uint32_t id);
int wsp_hb(char *out, size_t cap);

/* ONE BINARY FRAME CAN ARRIVE IN SEVERAL PIECES. `esp_websocket_client` hands a frame larger
 * than its buffer over as consecutive events, each saying where in the frame it starts, and
 * only the first carries the id — possibly not all of it, if a piece is shorter than four
 * bytes. This keeps the id across the pieces of one frame.
 *
 * `wsp_rx_feed` takes one piece (`offset` is where it starts within its frame) and returns how
 * many PAYLOAD bytes it carries, pointing `*payload` at them and setting `*id`; 0 when the piece
 * held only (part of) the id; -1 when the stream is malformed (a continuation with no start). */
typedef struct {
    uint8_t idbuf[WSP_ID_BYTES];
    int idgot;
    bool started;
} wsp_rx_t;

int wsp_rx_feed(wsp_rx_t *rx, int offset, const uint8_t *data, int len, uint32_t *id,
                const uint8_t **payload);

/* THE WINDOW. The panel tells the box how many answer bytes it can hold (`win`) and acks as it
   drains them. Acking every chunk would be a frame per 2 KB; acking only when all is drained
   would stall the stream for a round trip each window. Half is the usual answer: returns how
   much to ack now (0 for nothing yet) and advances `*acked`. */
size_t wsp_credit(size_t consumed, size_t *acked, size_t win);

/* Whether the socket is the transport right now, and what to do when it fails. */
typedef struct {
    int fails;          /* consecutive failures to come up */
    bool fallback;      /* on HTTPS-per-request until `since_ms + WSP_FALLBACK_RETRY_MS` */
    uint32_t since_ms;
} wsp_policy_t;

void wsp_policy_init(wsp_policy_t *p);
void wsp_policy_up(wsp_policy_t *p);
/* `hs_status` is the HTTP status the upgrade was answered with, 0 when it never got that far. */
void wsp_policy_failed(wsp_policy_t *p, int hs_status, uint32_t now_ms);
bool wsp_policy_try_ws(const wsp_policy_t *p, uint32_t now_ms);
/* How long to wait before the next attempt, doubling with consecutive failures. */
uint32_t wsp_backoff_ms(int fails);

/* `https://host/api` -> `wss://host/api<route>`, `http://` -> `ws://`. -1 for anything else or
   when it does not fit. */
int wsp_ws_uri(char *out, size_t cap, const char *api, const char *route);

/* THE REAL REASON A CONNECTION FAILED, as one short string — what "connect" used to stand for.
   esp-tls's own code, the TLS stack's, the socket errno and the upgrade's HTTP status; zero
   fields are left out, so a refused upgrade reads "hs=403" and a dead route "tls=0x8006 sock=113". */
int wsp_err_str(char *out, size_t cap, const char *what, int tls_err, int stack_err,
                int sock_errno, int hs_status);
