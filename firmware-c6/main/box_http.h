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

/* The reply's Date header, caught as the headers arrive. */
typedef struct {
    char date[32];
} box_reply_t;

void box_http_init(const cfg_t *cfg);

/* One request: GET when `body` is NULL, else POST of `len` bytes as `ctype`. Up to `cap` bytes
 * of the reply land in `out`. Returns the HTTP status, or -1 when no response arrived. A kept
 * session the box has since closed is retried once on a fresh one, so a stale socket costs a
 * handshake, not a failed request. */
int box_http_call(const char *path, const char *ctype, const char *body, size_t len, char *out,
                  size_t cap, size_t *got, box_reply_t *reply, int timeout_ms);

/* A chunked POST whose length is not known up front (the screenshot). Always a fresh session:
 * a stream cannot be replayed onto a second one if the kept one turns out to be stale. */
bool box_http_stream_open(const char *path, const char *ctype, int timeout_ms);
bool box_http_stream_write(const void *data, size_t n);
/* Sends the last chunk and returns the status (-1: none). */
int box_http_stream_finish(void);
/* Drops the connection without the last chunk, so the box sees a cut-off body and records
 * nothing — the request stays open for the JSON failure that follows. */
void box_http_stream_abort(void);

void box_http_close(void);

/* `tls=hs:N,reuse:N,hs_ms:N,max_ms:N` — handshakes, requests that rode a kept session, the last
 * and the slowest connect (TCP + TLS + the request's headers). */
void box_http_note(char *out, size_t len);
