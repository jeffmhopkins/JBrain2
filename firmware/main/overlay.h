#pragma once

/* EVERYTHING `sm_view_t` PUTS ON THE GLASS OVER THE PET: the notice and its badge, the run and the
 * again/reply pair, the microphone's dot, the thinking box, the who-grid, and the send's
 * placeholder outcome. `panel_sm.c` decides; this draws; `display.c` hands one to the other.
 *
 * Plain C, no ESP-IDF, for the reason `face.c` is: the host suites and the browser mock
 * (`firmware/mock/`) draw these exact pixels, so a control that is drawn somewhere its target is
 * not shows up on a laptop rather than on the wall. */

#include <stdbool.h>
#include <stdint.h>

#include "cue.h"
#include "panel_sm.h"

/* The panel takes RGB565 big-endian; the framebuffer is written in the order the QSPI sends. */
#define SWAP16(x) ((uint16_t)((uint16_t)(x) >> 8 | (uint16_t)(x) << 8))

typedef struct {
    int over_y0, over_h;     /* the overlay band for this orientation */
    uint32_t now;            /* the pulses and the dots animate off it */
    const char *sibling;     /* the twin's name from GET /waiting; NULL or "" when unknown */
    const char *result_word; /* a send's placeholder outcome (F4), NULL when there is none */
    uint16_t result_colour;
} overlay_ctx_t;

void overlay_draw(uint16_t *fb, const sm_view_t *view, const overlay_ctx_t *c);

/* PLACEHOLDER (F4): what a send's outcome looks like, read off the cue the machine chose for it. */
void overlay_result(cue_t c, bool nobody, const char **word, uint16_t *colour);
