/* The notice service's device side, beside the app-facing svc_notice.h (plan §2 "The panel
 * side"). The MAIN task owns the box connection and the files: it reports the switches on the
 * pickup, fetches each notice as a JN1 stream straight into its file, posts the states, deletes
 * what is finished and plays a sound from its file (talk_link.c). The UI task owns the queue's
 * head: what is on the glass, its hide clock, the answer. They meet under the port's lock. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "jn1.h"
#include "notice_logic.h"

/* Main task, after the rollback gate and before the UI: mounts `storage` (formatting it the first
 * time), deletes what a reboot left half-written or finished, re-queues the whole notices a reboot
 * interrupted, and loads the state POSTs still owed. False when there is no storage: no notice
 * can be taken, and the pickup says nothing (the box answers `no_notice_surface`). */
bool svc_notice_start(void);
/* Notifies the main task when the glass has something for the box (it must only notify). */
void svc_notice_set_wake(void (*wake)(void));

/* ---- main task ---- */
/* The pickup's report, `&na=…&tz=…`, or "" when this panel cannot show a notice (no storage, or
 * no UI on a boot after a fault). */
void svc_notice_pickup_query(bool ui_up, char *out, size_t cap);
/* The pickup's `X-Notice` header ("" for none). */
void svc_notice_waiting(const char *header);
/* The pickup's `X-Notice-Withdraw` ("" for none, 0.0.26): each held notice it names leaves the
 * glass on the UI task's next pass exactly as a lapsed one does, and nothing is told back. */
void svc_notice_withdraw(const char *header);
/* The next stream to collect: a torn one again (`…/{id}/body`) when its retry is due, else
 * `/endpoint/notice/next` while the box says one waits and there is room. */
bool svc_notice_want_fetch(char *path, size_t cap);
/* The box answered `next` with 204: nothing waits after all. */
void svc_notice_none_waiting(void);

/* One stream, straight into its file: begin, then room/took as the bytes arrive (a reader may
 * put the network's bytes where room says), then end. ~4.7 KB of heap while it runs, freed by
 * end. `whole` is false when the body stopped short. */
typedef struct nfetch nfetch_t;
nfetch_t *svc_notice_fetch_begin(const char *path);
size_t svc_notice_fetch_room(nfetch_t *f, uint8_t **at);
/* False once the stream is refused or the file cannot take it: stop reading. */
bool svc_notice_fetch_took(nfetch_t *f, size_t n);
/* The box did not answer `path` with a stream (`status`, -1 for no answer at all). A notice's
 * body that is gone (404: its window closed) is given up; anything else is tried again later. */
void svc_notice_fetch_failed(const char *path, int status);
/* END is in: what follows must be the body's end. */
bool svc_notice_fetch_ended(const nfetch_t *f);
/* The body went on after END: the stream is refused (decode()'s "bytes after END"). */
void svc_notice_fetch_trailing(nfetch_t *f);
/* True when the stream was whole and the notice is queued. */
bool svc_notice_fetch_end(nfetch_t *f, bool whole, int status);
/* The internal heap around a fetch: before it, with its buffer taken, and the lowest while the
 * stream ran (the snapshot's `notice.fetch.heap_*`). */
void svc_notice_fetch_mem(uint32_t before, uint32_t ready, uint32_t low);

/* The oldest state POST owed, when its retry is due: its path and body. */
bool svc_notice_post_take(char *path, size_t pcap, char *json, size_t jcap);
void svc_notice_post_done(int status);
/* Deletes the files of finished notices and keeps the owed POSTs in NVS. */
void svc_notice_housekeep(void);

/* A sound from its file (`key` as svc_audio was handed it) into `sink`, through `buf`. */
bool svc_notice_pcm_stream(const char *key, uint8_t *buf, size_t cap,
                           bool (*sink)(const uint8_t *pcm, uint32_t n, void *ctx), void *ctx);

/* `notice=q:N,fs:<ok|off|failed>,got:N,torn:N,bad:N,lost:N,st:<ok>/<fail>,last:<status>`. */
void svc_notice_note(char *out, size_t len);

/* ---- UI task, once per loop pass ---- */
/* New arrivals raise NOTICE_ARRIVED; a shown notice past its `hide_s` expires; one past its
 * answer window lapses. */
void svc_notice_service(void);
