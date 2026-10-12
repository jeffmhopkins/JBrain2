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
    char status[16]; /* mine: "Sending", "Delivered", "Played" */
} msg_t;

/* The threads: "jerv" and "dave" (the agents), and the two people's, by slot. A person's thread
 * is bound to a sibling panel by svc_siblings, which supplies the name it shows. */
#define SVC_MESSAGES_THREAD_1 "thread1"
#define SVC_MESSAGES_THREAD_2 "thread2"

uint16_t svc_messages_count(const char *principal);
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
