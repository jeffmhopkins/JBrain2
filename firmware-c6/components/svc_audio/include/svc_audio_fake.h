/* The audio service on the host: the device's talk state (talk_core.c) with no codec and no
 * box behind it. A hold to Jerv leaves the "Transcribing…" bubble the device leaves; the sim
 * answers it with svc_audio_fake_reply. A hold to a twin and a play of one of her messages are
 * answered by svc_audio_fake_posted and svc_audio_fake_listen (0.0.16). Time comes from svc_rtc's uptime, so the sim advances a
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
/* The box's transcript and a status line for the hold in flight, as JT2 streams them before the
 * answer: the sim's "transcript + status line" state. */
void svc_audio_fake_progress(const char *heard, const char *status);
/* A tool the agent started, and its end, as JT2's STEP records deliver them (0.0.14). */
void svc_audio_fake_step(uint8_t index, const char *label);
void svc_audio_fake_step_done(uint8_t index, bool ok);
/* The alarm sound "playing" (svc_audio_ring), -1 when silent. */
int svc_audio_fake_ring_sound(void);
/* The box's answer to a voice message to a twin: 201 with her transcript, 400 (nothing said),
 * or another status (a failure). */
void svc_audio_fake_posted(int status, const char *heard);
/* A play: the box answers with the audio (`arrives`), it plays `to_the_end`, and its digest
 * matched (`verified`). Not to the end leaves it playing, as a stream still arriving would. */
void svc_audio_fake_listen(bool arrives, bool to_the_end, bool verified);
/* New-message chimes the panel played. */
unsigned svc_audio_fake_chimes(void);
