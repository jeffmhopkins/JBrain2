/* Speaker and microphones. The audio task is the only owner of the ES8311/ES7210 handles and
 * every call here is a request it performs (esp_codec_dev has no lock — firmware/README.md).
 * Audio streams both ways: without PSRAM a clip does not fit. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#define TALK_MIN_MS 600u
#define TALK_MAX_MS 60000u

typedef enum { TALK_TO_JERV, TALK_TO_PERSON } talk_kind_t;

typedef enum { TALK_SENT, TALK_TOO_SHORT, TALK_CANCELLED, TALK_CAPPED } talk_outcome_t;

bool svc_audio_talk_begin(talk_kind_t kind, const char *principal);
/* `send` false is a tap on the glass. The outcome is what the app tells the user. */
talk_outcome_t svc_audio_talk_end(bool send);
bool svc_audio_talking(void);
uint32_t svc_audio_talk_ms(void);
uint8_t svc_audio_level(void); /* 0-100, the ring's pulse */

bool svc_audio_play(uint32_t msg_id);
void svc_audio_stop(void);
uint32_t svc_audio_playing(void); /* msg id, 0 when silent */
void svc_audio_speak(uint32_t msg_id);

/* Pure policy, host-tested: what ending a hold of `held_ms` means. */
talk_outcome_t talk_policy_end(uint32_t held_ms, bool send);
