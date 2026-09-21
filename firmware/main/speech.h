#pragma once

#include <stdbool.h>
#include <stdint.h>

/* ON-BOARD SPEECH, AND AN HONEST ACCOUNT OF WHAT THAT MEANS ON THIS CHIP.
 *
 * The owner asked for the microphone to be understood ON THE PANEL rather than over Wi-Fi,
 * "for faster processing", and then for what it hears to scroll along the bottom of the
 * screen. Both are built. What is NOT built, because it cannot be, is dictation:
 *
 *   * ESP-SR is a WAKE WORD engine (WakeNet) plus a COMMAND engine (MultiNet). MultiNet7
 *     resolves up to ~200 short phrases you give it in advance, in under half a second, and
 *     returns WHICH ONE it heard. It does not transcribe.
 *   * The smallest genuinely open-vocabulary model anyone runs (Whisper tiny, int8) is about
 *     75 MB against 8 MB of PSRAM. That is two orders of magnitude, not a tuning problem.
 *     ROOM_ENDPOINT_PLAN.md §10.4ar has the measurement.
 *
 * So the ticker shows the phrase the model RESOLVED, and shows nothing when it resolved
 * nothing. A toy that prints a guess is worse than one that misses: the miss is legible to a
 * four-year-old and the invention is not, and a hallucinated command the robot then ACTS on
 * reads as the toy being broken (the PWA plan's finding 3, same reasoning).
 *
 * NO WAKE WORD. The owner asked it to "just try and listen", so WakeNet is disabled and
 * MultiNet runs on every frame the front end says contains speech. That is the configuration
 * this file exists to hold, and it is the reason the command list is short and phrases are
 * two or three words: a one-word always-on vocabulary fires at the television.
 *
 * ONE OWNER, AGAIN. `audio.c`'s task owns the codec and is the only thing that reads the
 * microphone, so it is the only thing that calls `speech_feed`. This file's own task does the
 * fetching and the recognising, so a 200 ms MultiNet pass cannot stall the capture that feeds
 * it or the render loop that draws it.
 */

/* Bring up the front end and the command model. False means the models are not on the board
 * or would not fit, and the panel simply has no ticker — never a reason not to be a robot. */
bool speech_start(void);

/* Is the microphone being listened to? Drives the recording indicator, which the ICO
 * Children's Code requires, so it must answer for the REAL state and not for intent. */
bool speech_live(void);

/* Does the front end think someone is talking right now? Brightens the indicator. */
bool speech_hearing(void);

/* PCM from the codec, 16 kHz mono, called by the audio task and no one else. Buffers to the
 * front end's own chunk size, which is not the capture chunk size. */
void speech_feed(const int16_t *pcm, int samples);

/* Pop the next resolved phrase into `out`, uppercased for the 5x7 font, and its command id
 * (an index into `vocab.c`'s table) into `id`. False when there is nothing new. Called by the
 * render task, once a frame, so a phrase cannot land between two frames and be lost. */
bool speech_take(char *out, int cap, int *id);

/* How many phrases the model accepted, and how many it refused. The refusals matter: a phrase
 * MultiNet cannot tokenise is silently absent from the vocabulary, and the panel would look
 * deaf to exactly that one thing. Reported to the debug API rather than discovered by ear. */
void speech_vocab(int *accepted, int *rejected);
