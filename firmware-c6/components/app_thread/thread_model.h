/* A thread's bubbles, apart from drawing them. Pure C, host-tested. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "svc_messages.h"

typedef struct {
    bool unplayed; /* the amber dot */
    bool playing;
    uint8_t prog;  /* 0-100 */
    char dur[8];   /* "0:05" */
} thread_bubble_t;

/* `m` as its bubble shows it while `playing_id` plays with `played_ms` of it heard: the bar fills
 * left to right against the message's own length, full once heard and not playing, empty before. */
void thread_bubble(const msg_t *m, uint32_t playing_id, uint32_t played_ms, thread_bubble_t *out);
void thread_fmt_dur(uint32_t secs, char out[8]);
/* 0-100: `played_ms` of `m`, against the box's milliseconds where it gave them, else its seconds. */
uint8_t thread_play_pct(const msg_t *m, uint32_t played_ms);

/* What a thread's list is built from, folded to one number: a different one is a rebuild, the
 * same one an update of the bubbles where they stand (0.0.17, owner 2026-10-13: "the screen kind
 * of jumps around depending on when I play"). Whether a message has been heard is not in it: that
 * is the dot, changed in place. Start from THREAD_SHAPE0, fold each message in order, then end. */
#define THREAD_SHAPE0 2166136261u
uint32_t thread_shape_msg(uint32_t h, const msg_t *m);
uint32_t thread_shape_end(uint32_t h, uint16_t n, bool loaded);
/* Load earlier's rows (0.0.21): a row appearing, loading, its count or the start changing is a
   rebuild like a message's. */
uint32_t thread_shape_hist(uint32_t h, const hist_view_t *v);
