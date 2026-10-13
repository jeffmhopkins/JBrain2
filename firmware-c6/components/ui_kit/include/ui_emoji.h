#pragma once
/* Colour emoji as an LVGL font (README "Emoji"). Each Montserrat size ui_font hands out falls
 * back to the emoji font of its size, which falls back to the marks font (ui_glyphs_N): one
 * chain, so every label on the panel draws emoji without its builder doing anything. */

#include <stdbool.h>
#include <stdint.h>

#include "lvgl.h"

/* Where an image sits in its cell: the columns and rows trimmed off its top-left. */
typedef struct {
    uint8_t x, y;
} ui_emoji_at_t;

/* One cut size: every emoji of emoji_set.txt in EMOJI_SET order, then the unknown mark. Images
 * and positions are const, in flash; a glyph is drawn straight from them. */
typedef struct {
    uint8_t px;
    const lv_image_dsc_t *img;
    const ui_emoji_at_t *at;
} ui_emoji_tier_t;

extern const ui_emoji_tier_t UI_EMOJI_TIERS[2];

/* The emoji font for Montserrat at `px` (one of ui_font's sizes), or NULL for another size. */
const lv_font_t *ui_emoji_font(int px);

/* True when the line metrics each emoji font centres on are the built-in fonts' own (the sim's
 * --lifecycle checks it, so an LVGL that moves them fails CI rather than drawing emoji off-line). */
bool ui_emoji_metrics_ok(void);
