/* Load earlier (0.0.21, owner-approved mock 2026-10-13): a thread paged back past what the panel
 * keeps live. Pure C, no allocation, host-tested.
 *
 * ONE WINDOW, ONLY WHILE PAGED. A thread keeps its live messages as before (an agent's eight, a
 * person's four). Tapping "Load earlier" copies them into a window and puts the box's ten older
 * ones in front; each further page does the same. The window holds at most HIST_MAX messages
 * (the owner's 20, answer 3 of 2026-10-13) and HIST_TEXT_BYTES of their words: paging back past
 * either drops the newest, which the view then offers as "Newer messages · N more"; paging
 * forward drops the oldest. Only the open thread is ever paged, so there is one window, on the
 * heap, from the first "Load earlier" until the thread closes or a new message from KEY sends it
 * back to the newest. Each entry is ~36 bytes and the words share one arena, rather than a
 * msg_t's fixed 256: twenty messages are ~4.3 KB, not the ~6 KB twenty msg_t would be. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "svc_messages.h"

/* Words of twenty typical bubbles (the box cuts each to 255 bytes; most are far shorter), and at
 * least fourteen full ones: past it the window holds fewer than twenty, never cuts a bubble. */
#define HIST_TEXT_BYTES 3584

typedef struct {
    uint32_t local_id; /* what the view and svc_audio name it by */
    uint32_t box_id;   /* an agent's turn `seq`; 0 for a person's message or none known */
    uint8_t uuid[16];  /* a person's message id, raw; all zero when it has none */
    uint16_t off;      /* its words in the arena, with their terminator */
    uint16_t len;      /* bytes, the terminator not counted */
    uint16_t dur_s, dur_ms;
    bool mine, voice, played, pending;
} hist_ent_t;

typedef struct {
    uint8_t n;
    hist_ent_t e[HIST_MAX]; /* oldest first */
    uint16_t used;          /* arena bytes in use */
    uint32_t older, newer;  /* messages on the box beyond the window either side */
    int64_t start_at;       /* when the conversation began (UTC seconds), 0 until the box said */
    char text[HIST_TEXT_BYTES];
} hist_win_t;

/* One message going into the window: from the live thread or from the box's page. */
typedef struct {
    uint32_t local_id, box_id;
    char uuid[37]; /* "" for none */
    bool mine, voice, played, pending;
    uint16_t dur_s, dur_ms;
    const char *text;
} hist_in_t;

void hist_init(hist_win_t *w);
/* `n` messages (oldest first) before the window's first (`front`) or after its last, then the far
 * side trimmed until the window holds at most HIST_MAX messages and HIST_TEXT_BYTES of words;
 * each trimmed message is counted on that side (`newer` when adding in front, `older` behind).
 * Returns how many were trimmed. A message whose words alone overrun the arena is cut to fit. */
uint8_t hist_add(hist_win_t *w, bool front, const hist_in_t *in, uint8_t n);
const char *hist_text(const hist_win_t *w, uint8_t i);
/* Entry `i` as a bubble: a person's own message says "Played" or "Sent". */
void hist_to_msg(const hist_win_t *w, uint8_t i, msg_t *out);
int hist_find(const hist_win_t *w, uint32_t local_id);
/* A box id's text and raw forms. False for anything that is not 36 of [0-9a-f-]. */
bool hist_uuid_parse(const char *text, uint8_t out[16]);
void hist_uuid_text(const uint8_t raw[16], char out[37]);
bool hist_uuid_set(const uint8_t raw[16]);

/* The rows above and below the messages, as the mock draws them. */
hist_top_t hist_top_row(bool loading, int32_t older, int64_t start_at);
hist_bottom_t hist_bottom_row(bool loading, int32_t newer);
