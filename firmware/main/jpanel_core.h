#pragma once

/* The voice-post engine (plan §7.3): one serial loop that owns every network request and the
 * ring's producer side, and publishes one snapshot. Plain C behind `jpanel_io_t`, so the host
 * tests drive it against a scripted box, a fake clock and an arbiter built from `stream.h`
 * (`host/fake_stream.c`; the end-to-end suite runs the real `stream.c`); `jpanel.c` keeps the
 * FreeRTOS task, the queue, cJSON, mbedTLS and the real io.
 *
 * SINGLE WRITERS. The engine task is the only caller of `jpanel_core_step` and the only writer
 * of the snapshot. Render is the only caller of `jpanel_core_snapshot`. The poll flag may be
 * raised from any task. */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "jpanel_types.h"
#include "link_types.h"
#include "stream.h"

#define JP_SHA256_LEN 32

typedef struct {
    void *ctx;
    /* A request through `link.c` (device) or the scripted box (host). Blocking, on the engine
       task; every wait inside is bounded by req->deadline_ms and ticks req->on_progress. */
    link_res_t (*request)(void *ctx, const link_req_t *req);
    /* The command queue's consumer side. Waits up to `wait_ms`; false when nothing came. */
    bool (*take)(void *ctx, jp_cmd_t *out, uint32_t wait_ms);
    bool (*queue_empty)(void *ctx);
    uint32_t (*now_ms)(void *ctx);
    /* Blocks the engine task: the speaker wait before a stream begins, the ring-full wait in the
       sink, and the drain poll. The host fake advances its clock and ticks the arbiter. */
    void (*sleep_ms)(void *ctx, uint32_t ms);
    /* `/waiting` is JSON; cJSON stays on the device, so parsing is io's. False on a bad body. */
    bool (*parse_waiting)(void *ctx, const uint8_t *body, size_t n, jp_waiting_t *out);
    /* One running digest at a time (the engine verifies one stream at a time). */
    void (*sha_begin)(void *ctx);
    void (*sha_update)(void *ctx, const uint8_t *data, size_t n);
    void (*sha_finish)(void *ctx, uint8_t out[JP_SHA256_LEN]);
    /* Host-only interleave hooks; NULL on the device. Named points: "claimed", "head",
       "chunk", "classified", "acked", "publishing" (inside the seqlock's odd window) and
       "published". */
    void (*yield)(void *ctx, const char *point);
    stream_t *stream;
    /* The first `/waiting` poll waits this long, so two panels on one Wi-Fi never ask in
       lockstep (the device passes a random 0..30 s). */
    uint32_t poll_first_ms;
} jpanel_io_t;

/* Resets all engine state. One instance; host tests call it per test. */
void jpanel_core_init(const jpanel_io_t *io);

/* One pass of the loop, in order: finish the stream → acknowledge (one slot) → continue the run
   (polling first if flagged and the speaker is idle) → dequeue → poll when due or flagged and
   !speaker_busy. Bumps `hb`. On the device the task calls it forever; the queue wait inside
   (250 ms) is the idle heartbeat. */
void jpanel_core_step(void);

/* "Come and ask": an atomic flag, not a command. Any task. */
void jpanel_core_poll_soon(void);

/* Render's per-frame copy (seqlock, plan §7.2). False after four torn tries: `out` then keeps
   the previous copy, which the caller must already hold. */
bool jpanel_core_snapshot(jp_view_t *out);

/* What the engine watchdog (§7.5d) reads, from any task: the three words copied under the same
   seqlock into the caller's locals, because `jpanel_core_snapshot`'s scratch belongs to render
   and a second reader would tear it. False after four torn tries. */
typedef struct {
    uint32_t hb;
    uint32_t deadline_ms;
    bool idle;
} jp_health_t;
bool jpanel_core_health(jp_health_t *out);

void jpanel_core_stats(jp_stats_t *out);

/* The last failure's reason, for the report's `msg_err`. A static string; never NULL. */
const char *jpanel_core_err(void);
