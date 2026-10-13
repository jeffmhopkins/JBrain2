#pragma once
/* What the text layer does with a code point that may be emoji (README "Emoji"). Pure: no LVGL,
 * so the host suite tests it; ui_emoji.c turns the answer into a glyph. */

#include <stdint.h>

typedef enum {
    EMOJI_NOT,       /* plain text: left to Montserrat and the marks font, untouched */
    EMOJI_IN_SET,    /* one of emoji_set.txt: drawn in colour */
    EMOJI_INVISIBLE, /* part of a sequence that draws nothing of its own: zero width */
    EMOJI_UNKNOWN,   /* an emoji the panel does not carry: the dim placeholder, never a box */
} emoji_kind_t;

/* Sorted ascending, generated from emoji_set.txt (emoji/emoji_set.c). */
extern const uint16_t EMOJI_SET_N;
extern const uint32_t EMOJI_SET[];

/* The kind of `cp`; for EMOJI_IN_SET, `*index` (if given) is its place in EMOJI_SET, which is
 * also its image's place in each tier. */
emoji_kind_t emoji_kind(uint32_t cp, uint16_t *index);
