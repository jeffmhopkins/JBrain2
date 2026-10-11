/* Jerv's conversation rules, apart from drawing them. Pure C, host-tested. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "svc_audio.h"
#include "svc_messages.h"

/* A Listen button is on a finished text reply only when replies are not read aloud and it is
 * not being spoken now (mock README "Settings: Jerv"). */
bool jerv_shows_listen(const msg_t *m, bool tts_on, uint32_t speaking_id);
/* What ending a hold tells the person, or NULL when the send itself is the answer. */
const char *jerv_talk_toast(talk_outcome_t o);
