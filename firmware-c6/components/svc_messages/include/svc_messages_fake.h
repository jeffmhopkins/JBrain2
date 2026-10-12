/* In-memory conversations, until the box client for messages lands (C3). They start empty on
 * the device and in the sim alike; the sim and the host suite add their own sample messages
 * through svc_messages_fake_add, so no sample text ships in the image. */
#pragma once

#include "svc_messages.h"

void svc_messages_init(void);
/* Appends, or replaces the message with the same id where it stands (a voice bubble filled in
 * with its transcript), and raises MSG_ARRIVED for that principal. The oldest goes when full. */
bool svc_messages_fake_add(const char *principal, const msg_t *m);
bool svc_messages_find(const char *principal, uint32_t id, msg_t *out);
void svc_messages_fake_clear(void);
