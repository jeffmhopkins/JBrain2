#pragma once

#include <stdbool.h>
#include <stdint.h>

/* THE LINE ALONG THE BOTTOM: what the panel heard, scrolling right to left.
 *
 * The owner asked for this in as many words — "just try and listen to the microphone and put
 * text scrolling on the bottom as it recognizes it". What it can show is bounded by what is on
 * the board: MultiNet7 recognises a COMMAND LIST, not free speech (see `speech.h` and
 * ROOM_ENDPOINT_PLAN.md §10.4ar — dictation on this chip is two orders of magnitude out of
 * reach). So the ticker carries the phrases the model actually resolved, and a phrase it did
 * not resolve leaves the line empty rather than printing a guess. A toy that silently invents
 * what it heard is worse than one that misses, because the miss is legible and the invention
 * is not.
 *
 * THERE USED TO BE A PIP HERE — a small red dot at the left of this row, drawn whenever the
 * microphone was open and swelling when the recogniser heard speech. The owner had it
 * removed: *"the little LED on the bottom left ... I'd prefer just to remove that altogether.
 * The red dot on the top right that says listening I want to keep when I press it, but the
 * other one on the bottom left is unneeded."*
 *
 * Its comment claimed the ICO Children's Code required it. THAT WAS OVERSTATED and is
 * corrected here rather than quietly deleted with the code: the Code's explicit "obvious sign
 * while it is active" wording is its GEOLOCATION standard, the connected-toys standard asks
 * for conformance tools rather than a specific light, and the Code governs information
 * society services offered to the public — not a self-hosted panel a parent runs in their own
 * house for their own children. The honest version is that it was a good idea, not a legal
 * one, and it was argued as a legal one.
 *
 * What remains true and is worth knowing: the microphone IS always open, because the wake-word
 * recogniser needs it to be, and after this there is no always-on sign of that on the glass.
 * The press-and-hold indicator (`draw_listening`) still marks the recordings that LEAVE the
 * panel, which is the part that reaches the network.
 *
 * Pure C, no ESP dependencies: the host suite renders it exactly as the panel does.
 */

/* Characters held in flight. ~30 fit across the panel at scale 2, so this is a few phrases of
   backlog and a hard bound on the memory a talkative room can cost. */
#define CAPTION_MAX 128

typedef struct {
    char buf[CAPTION_MAX + 1];
    int len;
    float scrolled;  /* pixels the head of the buffer has travelled off the left edge */
    uint32_t last_ms;
} caption_t;

void caption_reset(caption_t *c);

/* The panel heard `text`. Appended to the ticker; a buffer with no room for it keeps what it
   already has, because dropping the front while it is still on screen would make the line
   jump. Returns false when the phrase was refused. */
bool caption_say(caption_t *c, const char *text);

/* Advance the scroll to `now_ms` across a panel `panel_w` wide. `live` is whether the
   microphone is open at all and `speech` whether the front end says someone is talking now. */
void caption_tick(caption_t *c, uint32_t now_ms, int panel_w);

/* Nothing left to show. The renderer draws no line at all in that case — an empty strip along
   the bottom of a 29 mm panel is a sixth of the robot. */
bool caption_idle(const caption_t *c);

/* Pixels per second at this backlog. Exposed because it is the one number here worth a test:
   a fixed rate makes a talkative minute take half a minute to drain. */
float caption_speed(const caption_t *c, int panel_w);

/* Draw the ticker and the indicator. `fb` is the panel's own byte-swapped RGB565, as
   `font_draw` and `face.c` use. */
void caption_draw(const caption_t *c, uint16_t *fb, int fbw, int fbh, uint16_t text);
