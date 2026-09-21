#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Capture and playback share one rate: the ES8311 is one part, opened once, and this is the
   vendor BSP's own duplex default. */
#define AUDIO_RATE 22050

/* Bring up the ES8311. False means no codec answered; the caller carries on in silence. */
bool audio_start(void);

/* RECORD the box's settings — it does NOT touch the codec, and the task that calls it is
   the main task, which must not. `esp_codec_dev` has no locking at all on its device API
   (the only mutex in the component guards the I2S data path), so a control write racing the
   render task's capture is two owners on one chip. Out of range values are ignored rather
   than clamped here: the box already clamps, and a panel silently re-interpreting a number
   would hide a bad one. */
void audio_set_levels(int volume, int mic_gain_db);

/* Apply whatever `audio_set_levels` last recorded. Called from the render task, between two
   captures, because that task owns the codec. A no-op when nothing is pending. */
void audio_apply_levels(void);

/* A short tone, played synchronously. Cheap enough to call from a touch handler. */
void audio_beep(void);

/* Capture exactly `samples` frames of mono int16, blocking until they arrive — roughly
   `samples`/AUDIO_RATE seconds. False means the read failed.

   BLOCKING IS THE POINT, not a limitation: sized to one render frame, the read paces the
   loop and guarantees the microphone is drained as fast as it fills. Consuming slower than
   the I2S DMA produces would show a meter lagging further behind the room every second. */
bool audio_record(int16_t *buf, int samples);

/* Largest absolute sample, 0..32767 — the one number that says whether anything reached the
   ADC at all. */
int audio_peak(const int16_t *buf, int samples);
