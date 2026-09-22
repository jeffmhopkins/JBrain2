#pragma once

#include <stdbool.h>
#include <stddef.h>
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

/* ASK for a rude noise — the twins' request, and the one action in the table that was mute.
   `burp` and `fart` have been in the vocabulary since bring-up but only ever moved the face:
   `rig.c` gives them a bewildered look and a wobble, and nothing came out of the speaker, so
   what a four-year-old asked for did not happen. Synthesised rather than sampled, because a
   WAV of a burp is a licence question, a download and 100 KB of flash to answer something an
   oscillator answers in twenty lines.

   Built straight into the reply buffer and played by the same path a reply takes, so it is
   chunked, interruptible, and deafens the microphone while it sounds — a burp the recogniser
   hears is a false trigger with a loudspeaker behind it. Refused, like a reply, while the
   panel is already speaking. Safe from any task. */
void audio_rude(bool wet);

/* The volume and mic gain the codec last ACCEPTED — "90/36", or "90!/36" when it refused the
   volume. Both setters used to be called with their return values dropped and a log line
   asserting success underneath, which is the one failure the owner cannot diagnose: they
   change a level from the box, the part says no, and nothing anywhere disagrees with them. */
const char *audio_levels_state(void);

/* Largest absolute sample of the most recent chunk, 0..32767 — the one number that says
   whether anything reached the ADC at all, and what the meter draws. */
int audio_level(void);

/* Largest absolute sample of a buffer. Pure, and exposed for the host tests. */
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
 * `audio_capture_close()` stops and hands back what was captured. NULL with `*len` zero means
 * nothing was recorded, which is a normal answer — an accidental hold on a silent room is the
 * most common recording this thing will ever make, and it must cost nothing to discard.
 *
 * The buffer stays valid until the next `audio_capture_open()`, so the caller uploads it
 * before starting another turn. */
void audio_capture_open(void);
const int16_t *audio_capture_close(size_t *len_bytes);

/* PLAY A REPLY. The bytes are COPIED into a buffer this file owns, because the caller's
   buffer is the HTTP response and that is freed the moment the request is. Returns false when
   playback is already running or the clip does not fit — a reply arriving on top of one still
   speaking is a request to interrupt, and this panel has no reference channel to do that
   safely (see `PANEL_CONVERSATION_PLAN.md` on barge-in).

   16 kHz mono s16, which is what the box sends BECAUSE the panel cannot convert anything. */
bool audio_play(const int16_t *pcm, size_t bytes);

/* True while a reply is still coming out of the speaker. The renderer holds its "speaking"
   state on this rather than on a timer, or a long reply ends on screen mid-sentence. */
bool audio_playing(void);

/* How long the current recording is, in milliseconds — for the cap, and for the log line that
   says how much audio a hold actually produced. */
int audio_capture_ms(void);

/* The hard ceiling `audio_capture_open` records to, in ms. Public because the hands-free
   listen has to know when the buffer is full: a held turn ends when the finger lifts, but a
   turn started by name ends on silence, and silence that never comes must still send what it
   has rather than record into a buffer that stopped accepting samples. */
int audio_capture_cap_ms(void);

int audio_peak(const int16_t *buf, int samples);
