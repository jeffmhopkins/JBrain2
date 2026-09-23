#pragma once

#include <stdbool.h>
#include <stdint.h>

/* THE PANEL'S VOICE WHEN IT IS NOT TALKING — one sound per thing that happens.
 *
 * The owner, after the first cut shipped with seven: *"Sound effects are too repetitious.
 * Same with the poke. They should all be unique or kind of change variations... if we poke
 * head or whatever we could have some cooing. Happy sounds. You can't all just be the same
 * little coin sound effect."*
 *
 * THE PANEL ALREADY VARIED WHAT IT DID; ONLY THE SOUND STAYED PUT. A poke resolves through
 * `variants.c` to a weighted random action from the zone's pool — the head can blush, giggle,
 * nod, wiggle or fall asleep — and every one of those played the same 880 Hz blip. So the fix
 * is not to randomise a noise, it is to let the sound follow the ACTION, which is the thing
 * that was already interesting. Poking the head coos because the head's most likely action is
 * a blush; no special case is needed for it, and the two stay in step for free when the pools
 * are next re-weighted.
 *
 * AND EACH ONE VARIES BETWEEN PLAYS. `cue_render` takes a `variant`: the same cue at a
 * different variant is transposed a little, timed a little differently, and for the
 * multi-note shapes uses a different set of intervals. Identity survives — a giggle is always
 * a giggle — but the fourth one in a row is not the sample of the first. The generator stays
 * PURE for a given (cue, variant), which is what lets `cue_render` find its own peak by
 * running twice and storing nothing.
 *
 * WHY SYNTHESISED, NOT SAMPLED: twenty-six WAVs is a licence question, a download and most of
 * a megabyte of flash to answer what a table of numbers and eight oscillator shapes answer.
 *
 * WHY ADDITIVE: output is 16 kHz, so Nyquist is 8 kHz, and a hard-edged square at these
 * pitches aliases badly — a 2 kHz square folds its 10, 14 and 18 kHz harmonics back to 6, 2
 * and 2 kHz, on top of the real partials and sliding the wrong way as the pitch sweeps.
 * Summing sines is exactly band-limited: a partial above Nyquist is never generated.
 *
 * Pure C with no ESP-IDF in it, deliberately — `audio.c` cannot be built on a host, which is
 * why `audio_rude()` shipped untested. The host suite asserts what actually goes wrong with
 * generated audio: clicks at the edges, clipping, DC offset, two cues that sound alike, and a
 * contour that contradicts the meaning.
 */

typedef enum {
    /* --- the nineteen actions, in the order `rig.h` declares them ---------------------- */
    CUE_WIGGLE = 0, /* a wobble — pitch vibrato, no journey */
    CUE_GIGGLE,     /* four quick rising blips */
    CUE_BOING,      /* up fast, down slow, with a wobble on the way back */
    CUE_BLUSH,      /* THE COO. Warm, soft, rises and settles. The head's happy sound. */
    CUE_SNEEZE,     /* a catch of breath, then a noise burst falling away */
    CUE_HICCUP,     /* two tiny blips, the second higher, and nothing else */
    CUE_NOD,        /* two low soft notes, agreeing */
    CUE_JUMP,       /* the rising sweep, flat plateau first */
    CUE_WAVE,       /* a friendly two-note hello */
    CUE_DANCE,      /* a little major arpeggio */
    CUE_BOP,        /* low rhythmic pulses */
    CUE_SHIMMY,     /* fast vibrato, buzzier than the wiggle */
    CUE_SLEEP,      /* a slow descent that fades — a yawn */
    CUE_HIDE,       /* a quick drop to nothing */
    CUE_FART,       /* FIVE of them — see FARTS[] in cue.c. Rumbler, squeaker, sputterer,
                       wet one, pfft; 170-700 ms and nearly two octaves apart, because a fart
                       transposed a semitone is the same fart and these are the twins'
                       favourite thing the panel does. */
    CUE_BURP,       /* the dry one, and still a single character */
    CUE_EAT,        /* two soft noise bites */
    CUE_KICK,       /* a thud: noise plus a pitch drop */
    CUE_SPIN,       /* three rising sweeps, each starting higher */

    /* --- the interface, which is not an action ----------------------------------------- */
    CUE_BLIP,   /* a tap that resolved to nothing to do */
    CUE_TOGGLE, /* the label flipped between the panel's name and its version */
    CUE_TICK,   /* a calibration sample — fires sixteen times, so the lightest thing here */
    CUE_HEARD,  /* a voice command understood: the coin's interval and its short-into-long */
    CUE_LISTEN, /* the microphone opened — rising, because that is the prosody of a question */
    CUE_STOP,   /* the conversation ended on purpose — the same gesture falling */
    CUE_OOPS,   /* the turn failed. Low, falling, rough. Try again, not told off. */
    /* --- voice post, and they are two EVENTS rather than two versions of "something
       happened". The message leaving and a message arriving are opposite directions and the
       sounds say so; reusing the label's toggle for "sent" was the 26-cue problem coming
       back, one event at a time. */
    CUE_SENT,   /* the message went — three notes climbing away */
    CUE_MESSAGE,/* one arrived — a soft falling pair, because this rings in a bedroom */
    CUE_COUNT,
} cue_t;

/* The MOST samples `cue_render` can write for `c` at `rate` Hz, across every variant — not
   the length of any particular one. A caller sizes a buffer from this, so it has to be the
   worst case: variants stretch the run by up to 7%, and returning the nominal length would
   under-allocate for exactly the variants that are longer than nominal. 0 for an unknown
   cue. */
int cue_samples(cue_t c, int rate);

/* 800 ms at 16 kHz. The longest cue is the rumbling fart at 700 ms, which stretches to 749.
   The first cut of this constant said 600 against a 620 ms fart, so the fart rendered NOTHING
   and the length guard in `cue_render` swallowed it silently. A ceiling that excludes a real
   cue is worse than no ceiling, so there is deliberate room above the longest one here, and
   the host suite checks every cue at every variant against it rather than trusting the
   arithmetic in this comment. */
#define CUE_MAX_SAMPLES 12800

/* Render `c` into `out` and return the samples written.
   `gain` is 0..100 on the speaker's own scale; the cues are peak-normalised internally so
   this is the only loudness control and one number keeps the whole set consistent.
   `variant` picks among the small perturbations described above — any value is valid, and
   the same value always renders the same samples. */
int cue_render(cue_t c, int16_t *out, int rate, int gain, unsigned variant);

/* The cue for an action, so the sound and the animation cannot drift apart. `rig.h`'s
   `action_t` is the argument; an action with no sound of its own returns CUE_BLIP. */
cue_t cue_for_action(int action);
