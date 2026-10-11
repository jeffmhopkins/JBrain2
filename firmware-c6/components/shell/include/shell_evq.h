/* The one way into the UI task. Services run on their own tasks and must never call LVGL or
 * an app (one task owns the panel); they push an event here and the UI task drains it.
 * Overflow is counted and reported, never silent. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "app_event.h"

#define SH_EVQ_DEPTH 16

typedef struct {
    app_event_t ev[SH_EVQ_DEPTH];
    uint8_t head, tail, count;
    uint32_t dropped;
} sh_evq_t;

void sh_evq_init(sh_evq_t *q);
bool sh_evq_push(sh_evq_t *q, const app_event_t *ev);
bool sh_evq_pop(sh_evq_t *q, app_event_t *out);
