/* The panel's one HTTPS session to the box, kept open between requests.
 *
 * 0.0.9 asks the box for debug requests every three seconds. A TLS handshake for each (the
 * pattern until 0.0.8: one session per request, closed after) would be an ECDHE + certificate
 * check every three seconds on a 160 MHz core at the UI's priority, so the manifest, telemetry
 * and the pickup now share one kept-alive session instead. It is still ONE session (the S3's
 * hardest-won rule, harder without PSRAM): every call here is on the main task, one at a time,
 * and box_http_close() runs before anything opens a second (the OTA download, a Wi-Fi re-join). */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "cfg.h"

/* The reply's headers this panel reads, caught as they arrive: the Date, the pickup's
 * new-message tag (`X-Jpanel-Post`, 0.0.16), a played message's digest (`X-Jpanel-Sha256`) and the
 * pickup's count of notices waiting (`X-Notice`, 0.0.19). */
typedef struct {
    char date[32];
    char post[12];
    char sha[65];
    char notice[8];
} box_reply_t;

void box_http_init(const cfg_t *cfg);

/* One request: GET when `body` is NULL, else POST of `len` bytes as `ctype`. Up to `cap` bytes
 * of the reply land in `out`. Returns the HTTP status, or -1 when no response arrived. A kept
 * session the box has since closed is retried once on a fresh one, so a stale socket costs a
 * handshake, not a failed request. */
int box_http_call(const char *path, const char *ctype, const char *body, size_t len, char *out,
                  size_t cap, size_t *got, box_reply_t *reply, int timeout_ms);
/* A PUT of `len` bytes (the alarm switch, `{on}`); the reply's body is read and dropped. */
int box_http_put(const char *path, const char *ctype, const char *body, size_t len,
                 int timeout_ms);

/* A chunked POST whose length is not known up front (the screenshot, a talk to Jerv). A stream
 * cannot be replayed onto a second session if the kept one turns out to be stale, so it opens a
 * fresh one — unless `reuse` and the kept session carried a request in the last
 * BOX_REUSE_FRESH_MS: a talk cannot wait out a handshake while the microphone fills its ring,
 * and the 3 s pickups keep that session warm. Opening on it is retried fresh if it fails before
 * a byte of the body is sent. */
#define BOX_REUSE_FRESH_MS 20000
/* `integrity` is the request's `X-Jpanel-Sha256` (a voice message's "trailer"), NULL for none. */
bool box_http_stream_open(const char *path, const char *ctype, int timeout_ms, bool reuse,
                          const char *integrity);
bool box_http_stream_write(const void *data, size_t n);
/* Sends the last chunk and returns the status (-1: none). */
int box_http_stream_finish(void);

/* A stream's reply, read in slices so the caller can give up between them (a talk dropped on
 * the glass). After box_http_stream_end: wait returns the status once the headers are in, 0
 * while they are not after `slice_ms`, -1 on a failed connection. read returns the body's bytes,
 * 0 at its end, BOX_HTTP_AGAIN when none came in `slice_ms`, -1 on a failure. reply_done keeps
 * the session when the body was read to its end; box_http_stream_abort drops it otherwise. */
#define BOX_HTTP_AGAIN (-2)
bool box_http_stream_end(void);
int box_http_reply_wait(int slice_ms);
int box_http_reply_read(void *buf, size_t cap, int slice_ms);
void box_http_reply_done(void);
/* Drops the connection without the last chunk, so the box sees a cut-off body and records
 * nothing — the request stays open for the JSON failure that follows. */
void box_http_stream_abort(void);

/* A GET whose body is read in slices as it arrives (a message played, 0.0.16): returns the status
 * once the headers are in (-1: none), `reply` holding them. Then box_http_reply_read, and
 * box_http_reply_done or box_http_stream_abort, exactly as after a stream. Retried once on a
 * fresh session when the kept one turns out stale, which is safe: nothing was sent. */
int box_http_get_open(const char *path, box_reply_t *reply, int timeout_ms);

void box_http_close(void);

/* `tls=hs:N,reuse:N,hs_ms:N,max_ms:N` — handshakes, requests that rode a kept session, the last
 * and the slowest connect (TCP + TLS + the request's headers). */
void box_http_note(char *out, size_t len);
