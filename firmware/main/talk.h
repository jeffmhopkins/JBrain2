#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cfg.h"

/* PRESS AND HOLD, UPLOAD, SPEAK — the panel's half of a conversation.
 *
 * ITS OWN TASK, AND THAT IS THE WHOLE REASON THIS FILE EXISTS. A turn is an HTTPS round trip
 * that can take seconds: whisper alone was measured at ~9.8 s with the large model
 * (ROOM_ENDPOINT_PLAN.md §10.4bf). Doing that on the render task would freeze the pet for the
 * entire wait, which is the one thing the thinking bubble is there to prevent — an animation
 * that stops animating is worse than no animation, because it reads as a crash.
 *
 * So the renderer hands over a recording and asks a question every frame: `talk_state()`. It
 * never blocks and never waits.
 */

typedef enum {
    TALK_NET_IDLE = 0,  /* nothing in flight */
    TALK_NET_BUSY,      /* uploading, or waiting on the box */
    TALK_NET_SPOKE,     /* a reply arrived and is playing or played */
    TALK_NET_FAILED,    /* no reply — say so, never fail silently */
} talk_net_t;

/* Starts the task. False means no task, and the caller simply never gets past BUSY. */
bool talk_start(const cfg_t *cfg);

/* Hand over a finished recording. The buffer must stay valid until the state leaves BUSY,
   which `audio.c` guarantees: it is not reused until the next `audio_capture_open()`.
   False when a turn is already in flight — a second hold before the first answers is a child
   being impatient, not a request to queue. */
bool talk_send(const int16_t *pcm, size_t bytes);

/* Where the current turn got to. Polled by the renderer once a frame. */
talk_net_t talk_state(void);

/* Back to idle once the renderer has shown the outcome, so the next hold starts clean. */
void talk_clear(void);
