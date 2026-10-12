/* The audio service until the ES8311/ES7210 owner task lands (C3): no microphone, so a hold is
 * timed but nothing is recorded. Time comes from svc_rtc's uptime, so the sim advances a
 * recording by advancing the clock. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "svc_audio.h"
#include "svc_messages.h"

void svc_audio_init(void);

/* What the conversation shows for a hold this panel could not send: a voice bubble of the
 * length held that says plainly nothing was recorded — never a made-up transcript. False for a
 * hold that is not a message at all (cancelled, or too short). Pure, host-tested. */
#define TALK_PLACEHOLDER "Not sent: this panel cannot record yet. Voice arrives in a later update."
bool talk_placeholder(talk_outcome_t o, uint32_t held_ms, uint32_t id, msg_t *out);
