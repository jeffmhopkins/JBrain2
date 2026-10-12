#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cue.h"

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

/* Turn the codec's own AGC on or off (ES8311 REG18 bit 7), and read back what happened.
 *
 * It has never been on: `es8311.c` never writes that register, so it has sat at the chip's
 * reset default since first bring-up, and telemetry has reported `00 already-off` on every
 * panel. A fixed PGA cannot serve two units whose last readings were `mic_peak` 32767 and 814
 * on the same gain, which is what this exists for.
 *
 * Idempotent: the box sends its answer on every settings fetch, and only a CHANGE costs a
 * register write. `audio_alc_state()` carries the outcome into telemetry either way — including
 * when the chip refuses, which must never read as a write that worked. */
void audio_set_agc(bool on);

/* The volume and mic gain the codec last ACCEPTED — "90/36", or "90!/36" when it refused the
   volume. Both setters used to be called with their return values dropped and a log line
   asserting success underneath, which is the one failure the owner cannot diagnose: they
   change a level from the box, the part says no, and nothing anywhere disagrees with them. */
const char *audio_levels_state(void);

/* Largest absolute sample of the most recent chunk, 0..32767 — the one number that says
   whether anything reached the ADC at all, and what the meter draws. */
int audio_level(void);

/* What the ES8311's ALC register actually held and what it holds now, as a short string for
   telemetry — "f8-78 off", "78 already-off", "REFUSED". The console line this mirrors cannot
   be read on a panel that is no longer on a cable, which is every panel in its real place. */
const char *audio_alc_state(void);

/* PRESS AND HOLD: THE RECORDING.
 *
 * `audio_capture_open()` starts filling a PSRAM buffer from the same chunks the recogniser
 * already sees — one microphone, one owner, one read (see the task rule above). It never
 * blocks the audio task and it never allocates while recording: the buffer is claimed once,
 * at start-up, because a heap request in the middle of a four-year-old talking is a failure
 * with no good outcome.
 *
 * REFUSED (false) WHILE EITHER CAPTURE LEASE IS HELD (`stream.h`): the buffer is being uploaded
 * — a SEND, or a pet turn — and reopening would overwrite it under the upload's hash (A5). Every
 * opener handles the refusal.
 *
 * `audio_capture_close()` stops and hands back what was captured. NULL with `*len` zero means
 * nothing was recorded, which is a normal answer — an accidental hold on a silent room is the
 * most common recording this thing will ever make, and it must cost nothing to discard. */
bool audio_capture_open(void);
const int16_t *audio_capture_close(size_t *len_bytes);

/* The panel's one speaker state (`stream.h`). Always valid: with no codec it is the all-zero
   instance, which refuses streams and writes no slots, so nothing waits on a speaker that is not
   there. */
struct stream_s;
struct stream_s *audio_stream(void);

/* HOLD A STREAM, BOUNDED. A paused stream is an HTTP response held open on the box, so the
   machine resumes it after this rather than aborting: a message that plays out to an empty room
   costs nothing, where discarding one a child had not finished hearing is the outcome the queue
   exists to prevent. */
#define AUDIO_PAUSE_MAX_MS 60000

/* How long the current recording is, in milliseconds — for the cap, and for the log line that
   says how much audio a hold actually produced. */
int audio_capture_ms(void);

/* The hard ceiling `audio_capture_open` records to, in ms. Public because the hands-free
   listen has to know when the buffer is full: a held turn ends when the finger lifts, but a
   turn started by name ends on silence, and silence that never comes must still send what it
   has rather than record into a buffer that stopped accepting samples. */
int audio_capture_cap_ms(void);

/* Largest absolute sample of a buffer. */
int audio_peak(const int16_t *buf, int samples);
