/* The audio service's device side, beside the app-facing svc_audio.h: how the audio task and
 * the main task's box connection take part in a talk (svc_audio_talk.c holds the one state, the
 * esp/ file owns the codecs, main/talk_link.c the upload and the reply). Every call here takes
 * the talk's lock and is safe from any task. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "dbg_json.h"
#include "talk_core.h"

/* The talk's state and its describer, before anything can ask for a hold: the device's
 * svc_audio_start calls it, the sim calls it alone. */
void svc_audio_init(void);

/* Main task, after svc_power_start, on a boot that may start the display. `wake_net` notifies
 * the main task when a hold begins; it must only notify. A codec that does not answer leaves
 * the panel running without sound and says which step failed (svc_audio_state). */
void svc_audio_start(void (*wake_net)(void));

/* UI task, once per loop pass: turns what the box answered into bubbles, frees the ring once
 * nothing touches it. */
void svc_audio_service(void);

/* The debug `play_tone`: `ms` of a sine at `hz` through the speaker at the set volume, when no
 * talk is under way. False with `why` otherwise. UI task. */
bool svc_audio_tone(uint16_t hz, uint16_t ms, const char **why);

/* Whether a talk's or a play's ring holds its 16 KB of internal RAM now: what the heap's
 * low-water mark is attributed to (telemetry's `int_min_at`). Any task. */
bool talk_ring_held(void);

/* `audio=…` (tk_stats_note) plus the codec's own state. */
void svc_audio_note(char *out, size_t len);

/* ---- the main task's half of an exchange ---- */
/* True once per hold: the exchange is this task's now, and must end with talk_net_end. Who it is
 * with, and whether it asks the box for a new conversation. */
bool talk_net_claim(bool *voice, tk_agent_t *agent, bool *fresh);
/* Right after the claim: what the exchange is, and for a voice message or a play the box path
 * it goes to (svc_post's, built on the UI task when it began). */
tk_kind_t talk_net_kind(char *path, size_t cap);
/* A play's end: whether every byte arrived and matched the box's digest. The message was marked
 * heard when its audio began (talk_net_reply, 0.0.17); a mismatch is counted as a failed play. */
void talk_net_listened(bool verified);
/* A play's ring, once the box has answered its GET (0.0.17): after the connection's handshake,
 * never during it, so the two never hold internal RAM at once. False with no memory for it. */
bool talk_net_ring(void);
/* What to do next; `mic_off` once the microphone has stopped, so the last of it is in the ring. */
tk_net_t talk_net_next(bool *mic_off);
uint32_t talk_net_take(void *dst, uint32_t cap);
/* JT2, as each record lands: what the box heard, what the agent is doing, then the answer. */
void talk_net_heard(const char *heard);
void talk_net_status(const char *status);
/* STEP_START / STEP_DONE (0.0.14): a tool the agent started, and how it ended. */
void talk_net_step_start(uint8_t index, const char *label);
void talk_net_step_done(uint8_t index, bool ok);
void talk_net_reply(const char *reply, bool audio_follows);
void talk_net_nothing(void);
void talk_net_fail(const char *why);
/* The reply's speech into the ring: how much fit. Zero once the speech is no longer wanted. */
uint32_t talk_net_give(const void *src, uint32_t n);
bool talk_net_wants_audio(void);
void talk_net_end(uint32_t up_bytes, uint32_t down_bytes, uint32_t reply_ms);

/* ---- the audio task's half ---- */
tk_audio_t talk_audio_want(void);
void talk_audio_on(bool mic, bool spk);
/* The microphone's mono chunk into the ring; what did not fit is counted as an overrun. */
void talk_audio_put(const int16_t *pcm, uint32_t bytes, uint16_t peak_l, uint16_t peak_r);
/* Up to `bytes` of the reply's speech. `*more` false once no more is coming. */
uint32_t talk_audio_get(void *dst, uint32_t bytes, bool *more);
uint32_t talk_audio_queued(void);
/* More of the reply's speech may still come. */
bool talk_audio_more(void);
/* The speaker would not open: the words stay, the speech stops. */
void talk_audio_drop(void);
void talk_audio_underrun(void);
/* `bytes` of a play's real audio went to the speaker (not the pre-roll's or an underrun's zeros):
 * the bubble's progress (svc_audio_played_ms). */
void talk_audio_played(uint32_t bytes);
void talk_audio_drained(void);

/* ---- what each build supplies: the device's esp/ file, or svc_audio_fake.c ---- */
void talk_port_lock(void);
void talk_port_unlock(void);
/* The talk's state moved: the audio task looks again. */
void talk_port_kick(void);
/* A hold began: the main task takes the exchange (and only then, so it is never woken into a
 * pass of its own for nothing). */
void talk_port_net(void);
/* NULL when a hold may record; otherwise what the toast says. */
const char *talk_port_refusal(void);
/* The speaker works, so a reply's voice may be asked for. */
bool talk_port_speaker(void);
uint8_t talk_port_level(void);
const char *talk_port_state(void);
/* The codecs' own lines in the `audio` snapshot. */
void talk_port_describe(dbg_json_t *w);
