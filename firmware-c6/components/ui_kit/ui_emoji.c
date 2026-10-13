#include "ui_emoji.h"

#include "emoji_logic.h"

/* The gap either side of an emoji, so a run of them does not touch. */
#define SIDE 1

LV_FONT_DECLARE(ui_glyphs_14)
LV_FONT_DECLARE(ui_glyphs_16)
LV_FONT_DECLARE(ui_glyphs_18)
LV_FONT_DECLARE(ui_glyphs_20)
LV_FONT_DECLARE(ui_glyphs_22)
LV_FONT_DECLARE(ui_glyphs_24)
LV_FONT_DECLARE(ui_glyphs_26)
LV_FONT_DECLARE(ui_glyphs_28)
LV_FONT_DECLARE(ui_glyphs_30)

/* Where the line is. LVGL places a glyph against the label's own font (the Montserrat in front
 * of this one), so each emoji font carries that font's metrics to centre its cell on the line;
 * ui_emoji_metrics_ok holds them to the built-ins. */
typedef struct {
    const ui_emoji_tier_t *tier;
    int16_t line_h, base_line;
} emoji_metrics_t;

static bool emoji_dsc(const lv_font_t *f, lv_font_glyph_dsc_t *g, uint32_t cp, uint32_t next)
{
    const emoji_metrics_t *m = f->dsc;
    uint16_t i = 0;
    switch (emoji_kind(cp, &i)) {
    case EMOJI_NOT: return false;
    case EMOJI_INVISIBLE:
        g->adv_w = g->box_w = g->box_h = 0;
        g->ofs_x = g->ofs_y = 0;
        g->format = LV_FONT_GLYPH_FORMAT_NONE;
        g->is_placeholder = 0;
        g->gid.src = NULL;
        return true;
    case EMOJI_UNKNOWN:
        /* Plain text the marks font draws stays its glyph, should the two ever meet. */
        if (f->fallback && f->fallback->get_glyph_dsc(f->fallback, g, cp, next)) return false;
        i = EMOJI_SET_N;
        break;
    case EMOJI_IN_SET: break;
    }
    const ui_emoji_tier_t *t = m->tier;
    const lv_image_dsc_t *img = &t->img[i];
    const ui_emoji_at_t at = t->at[i];
    /* The cell's top sits (line - cell) / 2 down the line; LVGL measures ofs_y up from the
       baseline to the box's bottom. */
    const int top = (m->line_h - t->px) / 2 + at.y;
    g->adv_w = (uint16_t)(t->px + 2 * SIDE);
    g->box_w = (uint16_t)img->header.w;
    g->box_h = (uint16_t)img->header.h;
    g->ofs_x = (int16_t)(SIDE + at.x);
    g->ofs_y = (int16_t)(m->line_h - m->base_line - top - (int)img->header.h);
    g->format = LV_FONT_GLYPH_FORMAT_IMAGE;
    g->is_placeholder = 0;
    g->gid.src = img;
    return true;
}

/* The image straight from flash: nothing is decoded or copied to draw it. */
static const void *emoji_bitmap(lv_font_glyph_dsc_t *g, lv_draw_buf_t *buf)
{
    LV_UNUSED(buf);
    return g->gid.src;
}

/* 18 px for text up to 20 px, 24 px above it (README "Emoji"). The line height and baseline
 * are each Montserrat size's own. */
#define METRICS(px, lh, bl, tier)                                                                  \
    static const emoji_metrics_t M_##px = {&UI_EMOJI_TIERS[tier], lh, bl};                         \
    enum { LH_##px = lh, BL_##px = bl }
METRICS(14, 16, 3, 0);
METRICS(16, 18, 3, 0);
METRICS(18, 21, 4, 0);
METRICS(20, 22, 4, 0);
METRICS(22, 24, 4, 1);
METRICS(24, 27, 5, 1);
METRICS(26, 29, 5, 1);
METRICS(28, 30, 5, 1);
METRICS(30, 33, 6, 1);
METRICS(48, 52, 9, 1);

#define FONT(px, glyphs)                                                                           \
    {                                                                                              \
        .get_glyph_dsc = emoji_dsc, .get_glyph_bitmap = emoji_bitmap, .line_height = LH_##px,      \
        .base_line = BL_##px, .subpx = LV_FONT_SUBPX_NONE, .dsc = &M_##px, .fallback = &glyphs,     \
    }

static const struct {
    int px;
    const lv_font_t *base;
    lv_font_t font;
} FONTS[] = {
    {14, &lv_font_montserrat_14, FONT(14, ui_glyphs_14)},
    {16, &lv_font_montserrat_16, FONT(16, ui_glyphs_16)},
    {18, &lv_font_montserrat_18, FONT(18, ui_glyphs_18)},
    {20, &lv_font_montserrat_20, FONT(20, ui_glyphs_20)},
    {22, &lv_font_montserrat_22, FONT(22, ui_glyphs_22)},
    {24, &lv_font_montserrat_24, FONT(24, ui_glyphs_24)},
    {26, &lv_font_montserrat_26, FONT(26, ui_glyphs_26)},
    {28, &lv_font_montserrat_28, FONT(28, ui_glyphs_28)},
    {30, &lv_font_montserrat_30, FONT(30, ui_glyphs_30)},
    {48, &lv_font_montserrat_48, FONT(48, ui_glyphs_30)},
};

const lv_font_t *ui_emoji_font(int px)
{
    for (size_t i = 0; i < sizeof FONTS / sizeof *FONTS; i++)
        if (FONTS[i].px == px) return &FONTS[i].font;
    return NULL;
}

bool ui_emoji_metrics_ok(void)
{
    for (size_t i = 0; i < sizeof FONTS / sizeof *FONTS; i++)
        if (FONTS[i].font.line_height != FONTS[i].base->line_height ||
            FONTS[i].font.base_line != FONTS[i].base->base_line)
            return false;
    return true;
}
