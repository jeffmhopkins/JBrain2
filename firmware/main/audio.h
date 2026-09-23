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




/* ASK for a CUE — one of the arcade bleeps in `cue.h`, chosen per event.
   Every acknowledgement used to be this file's single 880 Hz tone, which told a four-year-old
   that SOMETHING registered and nothing about what. Refused while the panel is speaking, for
   the same reason a rude noise is: the sentence outranks the acknowledgement. */
void audio_cue(cue_t c);

/* The rude noises moved into `cue.h` with everything else. `audio_rude()` hand-rolled its own
   sawtooth, envelope and clipping here — the one generator in the firmware that could not be
   built on a host and so shipped with no test at all, and the one that stayed at a fixed
   level while the rest were peak-normalised. CUE_FART and CUE_BURP are the same sound through
   the tested path, and the DC blocker they inherit is not something the old loop had. */

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

/* ── PLAYING SOMETHING LONGER THAN MEMORY ───────────────────────────────────────────────
 *
 * `audio_play()` takes the whole clip at once, which caps a message at whatever buffer the
 * panel can afford to keep — thirty seconds of 16 kHz mono is 960 KB, and the inbound fetch
 * buffer that fed it was another 960 KB. These four calls replace both with a ring a few
 * seconds deep: the network writes into it as the bytes arrive and the codec drains it, so a
 * message is bounded by the box's storage rather than by this board's PSRAM.
 *
 * IT ALSO MAKES PLAYBACK START SOONER, which is why the prefetch this replaces could go. A
 * tap used to wait for the WHOLE message to arrive (the owner: "there's a couple of seconds
 * between me acknowledging the message and it starting to play"), and the panel worked around
 * it by fetching ahead and holding the bytes. Streaming needs only the preroll before the
 * first sound — about twenty times less data — so the tap answers faster with nothing held.
 *
 * `audio_playing()` COVERS A STREAM, and that is the contract everything else reads. The
 * render loop's `speaking`, the pop-up, the message queue and `POST /played` all ask that one
 * question, and a ring that momentarily runs dry must not answer it the way a finished message
 * does — a panel that marked a message played mid-sentence would drop it from the queue and
 * nobody would ever hear the rest. So a stream counts as playing until `audio_stream_end()`
 * AND the ring has drained. */

/* Claim the speaker for a stream. False when anything else is sounding. */
bool audio_stream_begin(void);

/* Hand over more bytes. Returns how many were ACCEPTED, which is less than `bytes` when the
   ring is full — the caller pauses and offers the rest, which is what paces a fast network to
   the speed of the speaker and keeps the memory bounded. */
size_t audio_stream_write(const void *pcm, size_t bytes);

/* No more is coming. What is already in the ring still plays out. */
void audio_stream_end(void);

/* Stop now and throw away what has not been played — a finger on the screen, not an end. */
void audio_stream_abort(void);

/* IS THIS STREAM STILL WANTED. False once `audio_stream_end` or `audio_stream_abort` has run,
   and the producer MUST check it: a full ring and a stopped stream both refuse bytes, and a
   writer that could not tell them apart would offer the same bytes forever to a speaker that
   has stopped listening — wedging the task that feeds it, which on this panel is also the task
   that polls, sends and acknowledges. A child tapping to stop a long message is exactly how
   that would be found. */
bool audio_stream_live(void);

/* Stop whatever is sounding, at the next chunk. For a child getting out of a run of messages
   (`jpanel.h`) — a queue has to end on the finger, not only on the last message. */
void audio_stop(void);

/* How long the current recording is, in milliseconds — for the cap, and for the log line that
   says how much audio a hold actually produced. */
int audio_capture_ms(void);

/* The hard ceiling `audio_capture_open` records to, in ms. Public because the hands-free
   listen has to know when the buffer is full: a held turn ends when the finger lifts, but a
   turn started by name ends on silence, and silence that never comes must still send what it
   has rather than record into a buffer that stopped accepting samples. */
int audio_capture_cap_ms(void);

int audio_peak(const int16_t *buf, int samples);
