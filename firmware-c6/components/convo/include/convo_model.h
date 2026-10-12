/* A conversation's rules, apart from drawing them. Pure C, host-tested. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "svc_audio.h"
#include "svc_messages.h"

/* A Listen button is on a finished text reply only when replies are not read aloud and it is
 * not being spoken now (mock README "Settings: Jerv"). */
bool convo_shows_listen(const msg_t *m, bool tts_on, uint32_t speaking_id);
/* What ending a hold tells the person, or NULL when the send itself is the answer. */
const char *convo_talk_toast(talk_outcome_t o);

/* What a conversation's drawing depends on. The messages and how they are drawn rebuild the
 * list; the working view under the question (a step, the phase, its clock) changes in place —
 * so a status line moving on never recreates the widgets above it, nor the spinner (owner,
 * 2026-10-12: the thinking animation "kind of like resetting"). */
typedef struct {
    uint32_t rev;      /* svc_messages_rev */
    uint32_t speaking; /* svc_audio_playing */
    bool tts, can_speak;
    bool working;      /* the working view is up */
    uint32_t work_seq; /* svc_work_t.seq */
} convo_sig_t;
typedef enum {
    CONVO_SAME,    /* nothing to draw */
    CONVO_WORK,    /* only the working view: update it where it stands */
    CONVO_REBUILD, /* the messages, or whether there is a working view at all */
} convo_redraw_t;
convo_redraw_t convo_redraw(const convo_sig_t *drawn, const convo_sig_t *now);
