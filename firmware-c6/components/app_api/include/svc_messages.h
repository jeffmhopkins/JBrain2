/* Conversations: Jerv's and each person's thread. The service owns the waiting count, which
 * is how a closed Thread's tile carries a badge. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    uint32_t id;
    bool mine;
    bool voice;
    bool played;  /* theirs: heard on this panel */
    bool pending; /* transcribing, or awaiting a reply */
    uint16_t dur_s;
    /* A transcript or Jerv's reply; longer is cut at a character with an ellipsis. */
    char text[256];
    /* mine: "Sending", "Delivered", "Played"; an agent's answer: how it was worked out, "2 steps ·
       58 s" (0.0.14), shown dim above it */
    char status[24];
} msg_t;

/* The threads: "jerv" and "dave" (the agents), and the two people's, by slot. A person's thread
 * is bound to a sibling panel by svc_siblings, which supplies the name it shows. */
#define SVC_MESSAGES_THREAD_1 "thread1"
#define SVC_MESSAGES_THREAD_2 "thread2"

/* An agent's newest answer, whole (0.0.12): the box sends up to 1000 bytes (backend
 * `panel_stream.TEXT_BYTES`), and each agent thread keeps that one answer at this size beside
 * its bubbles' 255. Older answers keep their bubble's 255, cut with an ellipsis. */
#define MSG_LONG_TEXT 1024

uint16_t svc_messages_count(const char *principal);
/* Moves whenever anything in `principal`'s thread does — a message added, filled in, played or
 * cleared — so a view can tell a redraw of its messages from one of only what sits under them. */
uint32_t svc_messages_rev(const char *principal);
/* What to show for `m` in `principal`'s thread: its whole text when it is the answer the thread
 * keeps long, else m->text. Valid until that thread next changes. */
const char *svc_messages_text(const char *principal, const msg_t *m);
/* Clear chat: empties that thread on this panel only — nothing on the box is deleted. False for
 * a thread that does not exist. Raises MSG_ARRIVED so an open view redraws. */
bool svc_messages_clear(const char *principal);
bool svc_messages_get(const char *principal, uint16_t idx, msg_t *out);
uint16_t svc_messages_unplayed(const char *principal);
/* Across every person's thread: the clock face's badge, and which thread a tap on it opens.
 * False, with `principal` untouched, when nothing waits. */
uint16_t svc_messages_unplayed_total(void);
bool svc_messages_first_unplayed(char principal[16]);
void svc_messages_mark_played(const char *principal, uint32_t id);
