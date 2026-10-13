/* The conversations the glass draws, in memory. They start empty on the device and in the sim
 * alike: the agents' threads are filled by svc_audio and svc_convo, the people's by svc_post from
 * the box (0.0.16); the host suite adds its own through svc_messages_fake_add, so no sample text
 * ships in the image. */
#pragma once

#include "svc_messages.h"

void svc_messages_init(void);
/* Appends, or replaces the message with the same id where it stands (a voice bubble filled in
 * with its transcript), and raises MSG_ARRIVED for that principal. The oldest goes when full. */
bool svc_messages_fake_add(const char *principal, const msg_t *m);
bool svc_messages_find(const char *principal, uint32_t id, msg_t *out);
/* An agent thread's whole newest answer, for message `id` (already added with its cut text):
 * it replaces the one kept before. False for a person's thread. Raises nothing — the caller
 * adds the message, which announces it. Takes no lock and calls nothing, so the audio service
 * may call it inside its critical section. */
bool svc_messages_fake_long(const char *principal, uint32_t id, const char *text);
void svc_messages_fake_clear(void);
/* A person's thread as the box lists it (`m`, oldest first): replaces what it held, except the
 * panel's own bubbles the box has not listed yet — a send on its way, or one that failed — which
 * stay at the end. Raises MSG_ARRIVED only when something changed. False for an agent's thread
 * and for no change. */
bool svc_messages_fake_set(const char *principal, const msg_t *m, uint16_t n);

/* ---- Load earlier (0.0.21): the window, and the live view's counts — svc_convo's to set ---- */
#include "hist_logic.h"
/* The thread's live messages copied into a new window (another thread's is let go first). False
 * with no memory: the thread stays live. */
bool svc_messages_page_begin(const char *principal);
/* Back to the live messages: the window is freed. */
void svc_messages_page_end(const char *principal);
bool svc_messages_paged(const char *principal);
/* A page from the box, oldest first, before (`front`) or after the window; `remaining` is how
 * many the box has beyond it on that side, `start_at` when the conversation began (0: not said). */
void svc_messages_page_add(const char *principal, bool front, const hist_in_t *in, uint8_t n,
                           uint32_t remaining, int64_t start_at);
/* A person's message's box id, for one the window holds: the play path and its "played". */
bool svc_messages_page_uuid(uint32_t local_id, char principal[16], char uuid[37]);
void svc_messages_page_uuid_set(const char *principal, uint32_t local_id, const char *uuid);
/* The first (or last) message the thread shows now. */
bool svc_messages_edge(const char *principal, bool oldest, msg_t *out);
/* The live view's "Load earlier · N more": `older` than message `counted` (its local id, 0 for an
 * empty thread), and when the conversation began. -1: unknown. */
void svc_messages_hist_counts(const char *principal, uint32_t counted, int32_t older,
                              int64_t start_at);
/* Whether the live view's count is for the oldest message it shows now. */
bool svc_messages_hist_counted(const char *principal);
/* 0: nothing loading; 1: the top row ("Loading earlier…"); 2: the bottom row. */
void svc_messages_hist_loading(const char *principal, uint8_t side);
/* The window's heap, 0 when no thread is paged: the snapshot's and the sim's. */
uint32_t svc_messages_window_bytes(void);
