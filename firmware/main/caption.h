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
 * THE PIP IS A COMPLIANCE REQUIREMENT, not decoration. The ICO Children's Code requires a
 * recording indicator, so it is drawn whenever the microphone is live and is absent whenever
 * it is not — "muted is a promise", and this is the only thing on the glass that keeps it.
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
    float lit;       /* 0..1 microphone-live indicator, eased so it does not strobe */
    bool live;       /* is the microphone actually open? */
} caption_t;

void caption_reset(caption_t *c);

/* The panel heard `text`. Appended to the ticker; a buffer with no room for it keeps what it
   already has, because dropping the front while it is still on screen would make the line
   jump. Returns false when the phrase was refused. */
bool caption_say(caption_t *c, const char *text);

/* Advance the scroll to `now_ms` across a panel `panel_w` wide. `live` is whether the
   microphone is open at all and `speech` whether the front end says someone is talking now. */
void caption_tick(caption_t *c, uint32_t now_ms, int panel_w, bool live, bool speech);

/* Nothing left to show. The renderer draws no line at all in that case — an empty strip along
   the bottom of a 29 mm panel is a sixth of the robot. */
bool caption_idle(const caption_t *c);

/* Pixels per second at this backlog. Exposed because it is the one number here worth a test:
   a fixed rate makes a talkative minute take half a minute to drain. */
float caption_speed(const caption_t *c, int panel_w);

/* Draw the ticker and the indicator. `fb` is the panel's own byte-swapped RGB565, as
   `font_draw` and `face.c` use. */
void caption_draw(const caption_t *c, uint16_t *fb, int fbw, int fbh, uint16_t text,
                  uint16_t pip);
