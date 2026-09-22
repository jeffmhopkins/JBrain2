#pragma once

#include <stdint.h>

/* THE PANEL'S VOICE WHEN IT IS NOT TALKING — playful arcade bleeps, one per event.
 *
 * Every acknowledgement used to be the SAME 880 Hz tone: the label toggling, a tap on the
 * pet, a voice command landing, a calibration sample, the microphone opening. Five different
 * things and one sound, so the sound told a four-year-old only that *something* registered.
 * And the one event that most needed a sound — a turn that failed — had none at all, so a
 * pet that could not reach the box was silent in exactly the way a broken one is.
 *
 * WHY THESE ARE SYNTHESISED AND NOT SAMPLED: a WAV of a coin is a licence question, a
 * download and 100 KB of flash to answer something an oscillator answers in a few hundred
 * bytes of table.
 *
 * WHY ADDITIVE (a sum of sines) AND NOT A SQUARE WAVE: output here is 16 kHz, so Nyquist is
 * 8 kHz, and a hard-edged square at arcade pitches aliases badly — a 2 kHz square puts
 * harmonics at 10, 14, 18 kHz which fold back to 6, 2, 2 kHz, landing ON TOP of the real
 * partials and sliding the wrong way as the pitch sweeps. That is the gritty shimmer you hear
 * on a cheap retro sweep. Summing sines is EXACTLY band-limited instead of approximately: a
 * partial above Nyquist is simply never generated, so there is nothing to fold. The CPU cost
 * is irrelevant because every cue is rendered once into a buffer, not streamed.
 *
 * Pure C with no ESP-IDF in it, deliberately: `audio.c` cannot be built on a host, which is
 * why `audio_rude()` shipped with no test. This can, and `firmware/host/tests.c` asserts the
 * things that actually go wrong — a cue that clicks, clips, sits off centre, or reads as the
 * wrong emotion because its pitch contour points the wrong way.
 */

typedef enum {
    /* A tap landed. The shortest, flattest thing in the set: a blip that does not move,
       because any pitch contour at all imports a mood, and a tap has none. */
    CUE_BLIP = 0,
    /* The label flipped between the panel's name and its version. Same family as the blip,
       two semitones up, so the two touch targets do not sound identical. */
    CUE_TOGGLE,
    /* A voice command was understood. Ascending perfect fourth — the coin's interval, short
       note into long note, which is the shape the ear reads as arriving somewhere. */
    CUE_HEARD,
    /* The microphone just opened and the pet is listening. A rising sweep: rising is the
       prosody of a question, which is exactly what an open microphone is. */
    CUE_LISTEN,
    /* The conversation was ended on purpose ("fish stop"). The same gesture falling. */
    CUE_STOP,
    /* A turn failed — the box was unreachable, or took too long. Low, descending, and rough
       on purpose. Gentle enough for a bedroom: this tells a child to try again, it does not
       tell them off. */
    CUE_OOPS,
    /* A calibration sample was captured. Fires sixteen times in a row, so it is the one cue
       that has to be pleasant to hear repeatedly: quiet, brief, and a touch higher each time
       would be nice but a fixed neutral tick is what sixteen of them can bear. */
    CUE_TICK,
    CUE_COUNT,
} cue_t;

/* How many samples `cue_render` will write for `c` at `rate` Hz. Never more than
   CUE_MAX_SAMPLES at 16 kHz, so a caller can size one buffer and stop thinking about it. */
int cue_samples(cue_t c, int rate);

/* Longest cue at 16 kHz, for a statically sized buffer. */
#define CUE_MAX_SAMPLES 5600

/* Render `c` into `out` (at least `cue_samples(c, rate)` int16s) and return how many samples
   were written. `gain` is 0..100 on the same scale as the speaker volume — the cues are
   normalised to a fixed headroom internally, so this is the only loudness control and one
   number keeps the whole set consistent with each other. 0 samples for an unknown cue. */
int cue_render(cue_t c, int16_t *out, int rate, int gain);
