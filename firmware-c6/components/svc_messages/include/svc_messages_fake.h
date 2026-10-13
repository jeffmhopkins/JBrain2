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
