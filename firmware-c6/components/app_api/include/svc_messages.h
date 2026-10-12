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
    char text[96];
    char status[16]; /* mine: "Sending", "Delivered", "Played" */
} msg_t;

uint16_t svc_messages_count(const char *principal);
bool svc_messages_get(const char *principal, uint16_t idx, msg_t *out);
uint16_t svc_messages_unplayed(const char *principal);
/* Across every person's thread: the clock face's badge, and which thread a tap on it opens.
 * False, with `principal` untouched, when nothing waits. */
uint16_t svc_messages_unplayed_total(void);
bool svc_messages_first_unplayed(char principal[16]);
void svc_messages_mark_played(const char *principal, uint32_t id);
