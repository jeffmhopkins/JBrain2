#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "face.h"

/* THE TWO WAYS OUT OF A RECORDING, AS SOMETHING A FOUR-YEAR-OLD CAN AIM AT.
 *
 * Until now a recording had three silent exits and one gesture: going quiet sent it, saying
 * nothing dropped it, the cap sent what there was — and a touch ANYWHERE cancelled. That
 * gesture was right when it was the only one available ("a finger is the one input that is
 * always available and never ambiguous"), and wrong the moment there is somewhere deliberate
 * to press: a stray palm, or a finger aimed at the pet during a message, should not be able
 * to throw away what a child just said.
 *
 * The owner: *"I want icons that are green check and red x that are kind of large in the
 * bottom third ... Green check will finish when pressed and send, red x cancel."*
 *
 * GOING QUIET STILL SENDS. The check is "I am done, do not wait it out", not the only way
 * through — a voice assistant that needs a button press every time is not hands-free, and the
 * reader here is four and did not ask to press anything.
 *
 * ANCHORED TO THE OVERLAY BAND, NOT THE FRAME, which is the same rule the caption and the
 * label were moved to obey. `over_h` is the frame on a portrait panel and the SQUARE on a
 * side-mounted one, so measuring up from its bottom puts these in the bottom third either way
 * rather than off the edge of a panel that has been turned a quarter. */

/* The drawn disc, and the target around it. The target is deliberately the larger of the two:
   the pop-up's comment already paid for this lesson — "a four-year-old aiming at a small
   target with an excited finger is a miss". */
#define CONFIRM_R 56
#define CONFIRM_HIT_R 82
/* Up from the bottom of the band. */
#define CONFIRM_MARGIN 24
/* Far enough apart that the two targets cannot overlap: 184 px between centres against a
   82 px reach leaves a 20 px dead band, so a finger that lands between them does NOTHING
   rather than picking whichever circle happened to win. Cancel left, send right — the
   convention every phone call in the house already uses. */
#define CONFIRM_CX_CANCEL 92
#define CONFIRM_CX_SEND (FACE_W - CONFIRM_CX_CANCEL)

typedef enum {
    CONFIRM_NONE = 0, /* not on either target: consumed, and nothing happens */
    CONFIRM_SEND,
    CONFIRM_CANCEL,
} confirm_hit_t;

/* The centre line of both icons, measured up from the bottom of the overlay band. */
int confirm_cy(int over_h);

/* Which target a finger landed on, in FACE coordinates. `CONFIRM_NONE` for everything else,
   including the gap between them and the whole of the rest of the glass. */
confirm_hit_t confirm_hit(int fx, int fy, int over_h);

/* RGB565 BYTE-SWAPPED FOR THE PANEL'S BUS, the same convention `display.c` writes in — these
   go straight into the framebuffer, so they have to already be in its order. */
#define CONFIRM_SWAP(x) ((uint16_t)((uint16_t)(x) >> 8 | (uint16_t)(x) << 8))
#define CONFIRM_RED CONFIRM_SWAP(0xF800)
#define CONFIRM_GREEN CONFIRM_SWAP(0x07E0)
#define CONFIRM_GLYPH 0xFFFF /* white, and a palindrome either way round */
/* Thick enough to read across a room at a glance, thin enough that the glyph is a symbol
   rather than a blob filling its disc. */
#define CONFIRM_STROKE 11

/* Draw both icons into a framebuffer. Pure: it takes the buffer and its size and touches
   nothing else, which is what lets the host suite RENDER this rather than only measure it —
   the drawing is the half of this feature a hit test cannot check. */
void confirm_draw(uint16_t *fb, int w, int h, int over_h);

/* --- THE SINGLE-CONTROL ICONS, same size and the same place ---------------------------------
 *
 * The owner, after the tick and the cross were working: *"when panel is playing back a message
 * from my pwa, show a big stop icon similar to the x when recording. And when we have the again
 * have it the same size icon but with like a repeat and big like that."*
 *
 * Both replace text controls that were sized for an adult reading them: a
 * `"N MORE  TAP TO STOP"` bar and an `"AGAIN"` word box. `draw_repeat`'s comment explained why
 * it was a WORD — *"a hand-plotted circular arrow at this size reads as a smudge"* — and that
 * objection was entirely about SIZE. At the tick's size an arrow is a symbol again, and the
 * readers are four and pre-literate, so a glyph beats a word they cannot read.
 *
 * CENTRED, because unlike the tick and the cross these are ALONE. Two targets need a dead band
 * between them; one target should be where the thumb already is. */
#define CONFIRM_CX_CENTRE (FACE_W / 2)
/* Blue, the colour the AGAIN box already used, so the one control that survives from the old
   design keeps the colour a child had learned. */
#define CONFIRM_BLUE CONFIRM_SWAP(0x001F)

/* A red disc with a white square: stop what is playing. */
void confirm_draw_stop(uint16_t *fb, int w, int h, int over_h);

/* A blue disc with a circular arrow: play it again. */
void confirm_draw_repeat(uint16_t *fb, int w, int h, int over_h);

/* Whether a finger landed on a centred single control. Same generous reach as the pair. */
bool confirm_hit_centre(int fx, int fy, int over_h);
