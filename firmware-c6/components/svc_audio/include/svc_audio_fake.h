/* The audio service on the host: the device's talk state (talk_core.c) with no codec and no
 * box behind it. A hold to Jerv leaves the "Transcribing…" bubble the device leaves; the sim
 * answers it with svc_audio_fake_reply. A hold to a person is still not sent: the box client
 * for messages is a later wave. Time comes from svc_rtc's uptime, so the sim advances a
 * recording by advancing the clock. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "svc_audio.h"
#include "svc_audio_drv.h"
#include "svc_messages.h"

/* The box's answer to the hold in flight, as the device's network side would deliver it:
 * `speaking` keeps the reply's "Speaking…" up as though its voice were playing. */
void svc_audio_fake_reply(const char *heard, const char *reply, bool speaking);
/* The converse path the device would have POSTed for the last claimed talk (tk_converse_path):
 * who it was with, voice or text, and whether Clear chat asked for a new conversation. */
const char *svc_audio_fake_last_path(void);
