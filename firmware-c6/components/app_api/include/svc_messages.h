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
    /* dur_s to the millisecond, where the box said it (a twin's message, 0.0.17): what a play's
       progress bar fills against; 0 when unknown. 16 bits hold 65 s (a message is 30 s, a talk
       60), and last, it sits in what was the struct's padding: no message grew. */
    uint16_t dur_ms;
    /* The box's id for an agent's message, its turn `seq` (0.0.21): what Listen speaks and Load
       earlier pages from. 0 for a person's message (svc_post keeps her box ids) and for one the
       box has not named — a question still on its way, a talk before this image. */
    uint32_t box_id;
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

/* ---- Load earlier (0.0.21, owner-approved 2026-10-13) ----
 * A thread opens on its live messages with "Load earlier · N more" above them while the box has
 * older ones, and "Start of conversation · <when>" at the true start. Ten at a time come in front;
 * past HIST_MAX the newest go and "Newer messages · N more" sits below. While paged the thread's
 * count, get and text are the window's (hist_logic.h). Requests are svc_convo.h's. */
#define HIST_MAX 20
#define HIST_PAGE 10
typedef enum { HIST_TOP_NONE, HIST_TOP_LOAD, HIST_TOP_LOADING, HIST_TOP_START } hist_top_t;
typedef enum { HIST_BOTTOM_NONE, HIST_BOTTOM_NEWER, HIST_BOTTOM_LOADING } hist_bottom_t;
typedef struct {
    uint8_t top;      /* hist_top_t */
    uint32_t older;   /* LOAD: how many */
    int64_t start_at; /* START: when it began, UTC seconds */
    uint8_t bottom;   /* hist_bottom_t */
    uint32_t newer;   /* NEWER: how many */
    bool paged;       /* the window is what is shown */
} hist_view_t;
void svc_messages_hist(const char *principal, hist_view_t *out);
