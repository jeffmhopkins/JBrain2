/* Speaker and microphones. The audio task is the only owner of the ES8311/ES7210 handles and
 * every call here is a request it performs (esp_codec_dev has no lock — firmware/README.md).
 * Audio streams both ways: without PSRAM a clip does not fit. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define TALK_MIN_MS 600u
#define TALK_MAX_MS 60000u

/* TALK_TO_AGENT: `principal` is the agent's thread, "jerv" or "dave" — the box's real agent. */
typedef enum { TALK_TO_AGENT, TALK_TO_PERSON } talk_kind_t;

/* TALK_FAILED: the box could not be reached mid-hold, so nothing was sent. */
typedef enum { TALK_SENT, TALK_TOO_SHORT, TALK_CANCELLED, TALK_CAPPED, TALK_FAILED } talk_outcome_t;

/* False when no hold starts; then svc_audio_talk_refused() says why, or is NULL when the press
 * did something else on purpose (it stopped Jerv speaking). */
bool svc_audio_talk_begin(talk_kind_t kind, const char *principal);
/* Clear chat on an agent's thread: its next talk asks the box for a new conversation. Held until
 * a talk is answered (a hold the box heard nothing in keeps it); the other agent is untouched. */
void svc_audio_fresh(const char *agent);
bool svc_audio_fresh_pending(const char *agent);
const char *svc_audio_talk_refused(void);
/* `send` false is a tap on the glass. The outcome is what the app tells the user. */
talk_outcome_t svc_audio_talk_end(bool send);
bool svc_audio_talking(void);
uint32_t svc_audio_talk_ms(void);
uint8_t svc_audio_level(void); /* 0-100, the ring's pulse */

/* Jerv is thinking about, or speaking, a reply. */
bool svc_audio_replying(void);
/* ... and speaking it: KEY or BOOT now stops the voice. While only thinking, KEY says the agent is
 * still working and BOOT leaves the thread — the turn goes on and its answer lands when it comes
 * (owner, 2026-10-12). */
bool svc_audio_speaking(void);
/* What KEY says then: no new talk starts (owner, 2026-10-12). */
#define TALK_STILL_WORKING "Still working on the last one"
/* The live status line under `thread`'s waiting question ("Searching the web…"): true, with the
 * words, while that thread's agent is working on an answer the box has started streaming. */
bool svc_audio_status(const char *thread, char *out, size_t cap);
/* Drops that reply (BOOT or KEY over it). False when there was none. */
bool svc_audio_stop_reply(void);

bool svc_audio_play(uint32_t msg_id);
void svc_audio_stop(void);
uint32_t svc_audio_playing(void); /* msg id, 0 when silent */
void svc_audio_speak(uint32_t msg_id);
/* Whether a reply can be listened to on demand: not until the box can speak a stored one. */
bool svc_audio_can_speak(void);

/* "ok", "off" (not started: a fault boot), "mic-only:<why>" or "failed:<step>": what Settings
 * and the snapshot say about the sound. */
const char *svc_audio_state(void);

/* Pure policy, host-tested: what ending a hold of `held_ms` means. */
talk_outcome_t talk_policy_end(uint32_t held_ms, bool send);
