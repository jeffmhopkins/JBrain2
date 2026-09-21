#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Capture and playback share one rate: the ES8311 is one part, opened once.
   16 kHz, not the vendor BSP's 22050, because ESP-SR's audio front end takes "16-bit signed,
   16 kHz" and nothing else — and the models are already on the board (`firmware/README.md`).
   Nothing here is worse for it: this rate carries a beep and a level meter perfectly well. */
#define AUDIO_RATE 16000

/* One capture chunk, and therefore the audio task's period. */
#define AUDIO_CHUNK_MS 40
#define AUDIO_CHUNK (AUDIO_RATE * AUDIO_CHUNK_MS / 1000)

/* ONE TASK OWNS THE CODEC, AND IT IS NOT THE CALLER OF ANY FUNCTION HERE.
 *
 * `esp_codec_dev.c` contains no lock of any kind — read, write, `set_out_vol` and
 * `set_in_gain` all walk straight into the device struct and the chip's I2C registers, and the
 * component's only mutex guards the I2S data path (§10.4al, which cost a panic to find). So
 * this file runs its own task, that task performs every codec operation, and everything below
 * is a REQUEST rather than an action. It is the same rule `display.c` states for the panel,
 * arrived at the same way.
 *
 * It also takes the blocking capture out of the render loop, which is where that loop spent
 * most of its wall clock — the reason `crash_phase` kept reporting stage 10 whatever failed.
 *
 * Brings up the ES8311 and starts the task. False means no codec answered; the caller carries
 * on in silence. */
bool audio_start(void);

/* RECORD the box's settings — it does NOT touch the codec, and the task that calls it is
   the main task, which must not. `esp_codec_dev` has no locking at all on its device API
   (the only mutex in the component guards the I2S data path), so a control write racing the
   render task's capture is two owners on one chip. Out of range values are ignored rather
   than clamped here: the box already clamps, and a panel silently re-interpreting a number
   would hide a bad one. */
void audio_set_levels(int volume, int mic_gain_db);



/* ASK for a tone. Returns immediately; the audio task plays it between two captures, within
   one chunk. Safe from any task, which a direct `esp_codec_dev_write` from the render loop
   was not. */
void audio_beep(void);

/* Largest absolute sample of the most recent chunk, 0..32767 — the one number that says
   whether anything reached the ADC at all, and what the meter draws. */
int audio_level(void);

/* Largest absolute sample of a buffer. Pure, and exposed for the host tests. */
int audio_peak(const int16_t *buf, int samples);
