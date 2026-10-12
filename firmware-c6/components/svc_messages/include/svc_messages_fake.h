/* In-memory conversations seeded with the mock's, until the box client lands (C3). The device
 * starts them empty (svc_messages_fake_clear): a sample message on the glass would read as a
 * real one. */
#pragma once

#include "svc_messages.h"

void svc_messages_init(void);
/* Appends, or replaces the message with the same id where it stands (a voice bubble filled in
 * with its transcript), and raises MSG_ARRIVED for that principal. The oldest goes when full. */
bool svc_messages_fake_add(const char *principal, const msg_t *m);
bool svc_messages_find(const char *principal, uint32_t id, msg_t *out);
void svc_messages_fake_clear(void);
