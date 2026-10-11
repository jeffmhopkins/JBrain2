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

void thread_bubble(const msg_t *m, uint32_t playing_id, uint8_t play_pct, thread_bubble_t *out);
void thread_fmt_dur(uint32_t secs, char out[8]);
