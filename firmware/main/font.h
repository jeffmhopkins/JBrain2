#pragma once

#include <stdint.h>

/* A 5x7 bitmap font, deliberately tiny in both senses.
 *
 * DIGITS, UPPERCASE A-Z, and `. - ' ? ! ,` plus space (and a lowercase 'v', for `v0.2.36`).
 * It began as digits alone, because hand-drawing twenty-six glyphs for words nothing rendered
 * would have been inventory rather than work; the caption line is what needed words, so the
 * alphabet arrived with it. `font_text_w` sizes what it can draw; an unknown character renders
 * as a blank of the right width, so a wrong string is visibly wrong instead of silently short.
 *
 * No ESP dependencies, so the host preview harness renders exactly what the panel does —
 * which is how the geometry in face.c was checked, and how this was.
 */

#define FONT_W 5
#define FONT_H 7

/* Pixel width of `s` at `scale`, including the one-column gaps between glyphs. */
int font_text_w(const char *s, int scale);

/* Draw `s` at (x, y), clipped to the framebuffer. `colour` is already byte-swapped for the
   panel, exactly as face.c's palette is. */
void font_draw(uint16_t *fb, int fbw, int fbh, int x, int y, int scale, const char *s,
               uint16_t colour);
