#pragma once

#include <stdbool.h>

#include "cfg.h"
#include "jpanel_core.h"
#include "jpanel_types.h"
#include "stream.h"

/* VOICE POST — the panel's half of `docs/plans/JPANEL_PLAN.md`.
 *
 * A message is recorded, filed on the box, and waits until the recipient chooses to play it.
 * Nothing rings and nothing is missed by not being there, which is the only shape that works
 * for a four-year-old: an intercom needing both ends present would be a toy that mostly fails.
 *
 * ITS OWN TASK, for the same reason `talk.c` has one: every request is a round trip to the box
 * measured in seconds, and the render task must never block. The engine itself is
 * `jpanel_core.c` (plain C, host-tested against a scripted box); this file is its FreeRTOS task,
 * its command queue, cJSON, mbedTLS and the real io.
 *
 * TWO ARROWS AND NOTHING ELSE (plan §7.1). Render posts commands (`jpanel_post`) and copies one
 * snapshot per frame (`jpanel_snapshot`). It never reads the engine's private state, and the
 * engine never writes render's. */

/* Starts the task. False means no task: every post then fails, and the panel is still a pet.
   `stream` is the speaker's one owner (stream.h); the engine is its producer. */
bool jpanel_start(const cfg_t *cfg, stream_t *stream);

/* Render only. Never blocks: a full queue (depth 4) or no task is a failed post, which the
   machine reports back to itself as `post_failed`. For a SEND, render has already taken lease 1,
   and releases it itself when this fails. */
bool jpanel_post(const jp_cmd_t *cmd);

/* Render only, once per frame. False after four torn tries; `out` then keeps the caller's
   previous copy. */
bool jpanel_snapshot(jp_view_t *out);

/* The engine watchdog's view (§7.5d), safe from any task. False with no task, or torn. */
bool jpanel_health(jp_health_t *out);

/* "Come and ask": the next `/waiting` poll happens now rather than up to 30 s from now. Any
   task: the settings poll's waiting count, the nudge, the socket's event. */
void jpanel_poll_soon(void);

/* For `main.c`'s report: the engine's counters plus this file's own (`post_failed`,
   `jpanel_stack_free`), and the last failure's reason for `msg_err`. */
void jpanel_stats(jp_stats_t *out);
const char *jpanel_msg_err(void);
