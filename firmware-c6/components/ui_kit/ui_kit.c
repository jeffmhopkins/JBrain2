#include "ui_kit.h"

#include <stdio.h>
#include <string.h>

#define C(hex) lv_color_hex(hex)

#include "ui_jerv_mark.h"

uint32_t ui_accent_color(ui_accent_t a)
{
    switch (a) {
    case UI_ACCENT_JERV: return UI_C_JERV;
    case UI_ACCENT_PERSON_A: return UI_C_PERSON_A;
    case UI_ACCENT_PERSON_B: return UI_C_PERSON_B;
    case UI_ACCENT_PERSON_C: return UI_C_PERSON_C;
    case UI_ACCENT_PERSON_D: return UI_C_PERSON_D;
    case UI_ACCENT_MINECRAFT: return UI_C_MINECRAFT;
    case UI_ACCENT_STEEL: return UI_C_STEEL;
    default: return UI_C_TEXT;
    }
}

LV_FONT_DECLARE(ui_font_clock_92)
LV_FONT_DECLARE(ui_font_clock_124)
LV_FONT_DECLARE(ui_glyphs_14)
LV_FONT_DECLARE(ui_glyphs_16)
LV_FONT_DECLARE(ui_glyphs_18)
LV_FONT_DECLARE(ui_glyphs_20)
LV_FONT_DECLARE(ui_glyphs_22)
LV_FONT_DECLARE(ui_glyphs_24)
LV_FONT_DECLARE(ui_glyphs_26)
LV_FONT_DECLARE(ui_glyphs_28)
LV_FONT_DECLARE(ui_glyphs_30)

#define N_FONTS 10

/* The built-in fonts are const, so each size is a copy of its descriptor (a few dozen bytes,
 * pointing at the same glyph data in flash) with the punctuation fallback hung on it. */
static lv_font_t s_fonts[N_FONTS];
static const struct {
    int px;
    const lv_font_t *base, *glyphs;
} FONTS[N_FONTS] = {
    {14, &lv_font_montserrat_14, &ui_glyphs_14}, {16, &lv_font_montserrat_16, &ui_glyphs_16},
    {18, &lv_font_montserrat_18, &ui_glyphs_18}, {20, &lv_font_montserrat_20, &ui_glyphs_20},
    {22, &lv_font_montserrat_22, &ui_glyphs_22}, {24, &lv_font_montserrat_24, &ui_glyphs_24},
    {26, &lv_font_montserrat_26, &ui_glyphs_26}, {28, &lv_font_montserrat_28, &ui_glyphs_28},
    {30, &lv_font_montserrat_30, &ui_glyphs_30}, {48, &lv_font_montserrat_48, &ui_glyphs_30},
};

const lv_font_t *ui_font(int px)
{
    if (!s_fonts[0].get_glyph_dsc) {
        for (int i = 0; i < N_FONTS; i++) {
            s_fonts[i] = *FONTS[i].base;
            s_fonts[i].fallback = FONTS[i].glyphs;
        }
    }
    int best = 0, dist = 1000;
    for (int i = 0; i < N_FONTS; i++) {
        int d = px > FONTS[i].px ? px - FONTS[i].px : FONTS[i].px - px;
        if (d < dist) {
            dist = d;
            best = i;
        }
    }
    return &s_fonts[best];
}

static lv_obj_t *bare(lv_obj_t *parent)
{
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_remove_style_all(o);
    /* Not clickable unless a builder says so: a press then reaches the nearest control that
     * is, rather than dying on a decoration inside it. */
    lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    return o;
}

static void fill(lv_obj_t *o, uint32_t color, lv_opa_t opa, int radius)
{
    lv_obj_set_style_bg_color(o, C(color), 0);
    lv_obj_set_style_bg_opa(o, opa, 0);
    lv_obj_set_style_radius(o, radius, 0);
}

static void border(lv_obj_t *o, uint32_t color, int w)
{
    lv_obj_set_style_border_color(o, C(color), 0);
    lv_obj_set_style_border_width(o, w, 0);
}

lv_obj_t *ui_label(lv_obj_t *parent, const char *text, int px, uint32_t color)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_label_set_text(l, text ? text : "");
    lv_obj_set_style_text_font(l, ui_font(px), 0);
    lv_obj_set_style_text_color(l, C(color), 0);
    return l;
}

lv_obj_t *ui_label_wrap(lv_obj_t *parent, const char *text, int px, uint32_t color)
{
    lv_obj_t *l = ui_label(parent, text, px, color);
    lv_label_set_long_mode(l, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(l, LV_PCT(100));
    lv_obj_set_style_text_line_space(l, px / 4, 0);
    return l;
}

lv_obj_t *ui_label_big(lv_obj_t *parent, const char *text, int px, uint32_t color)
{
    lv_obj_t *l = ui_label(parent, text, px, color);
    if (px == 124) lv_obj_set_style_text_font(l, &ui_font_clock_124, 0);
    if (px == 92) lv_obj_set_style_text_font(l, &ui_font_clock_92, 0);
    /* The mock's letter-spacing: -0.02em. */
    if (px >= 92) lv_obj_set_style_text_letter_space(l, -px / 50, 0);
    return l;
}

lv_obj_t *ui_box(lv_obj_t *parent, lv_flex_flow_t flow, int gap)
{
    lv_obj_t *b = bare(parent);
    lv_obj_set_size(b, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(b, flow);
    lv_obj_set_style_pad_row(b, gap, 0);
    lv_obj_set_style_pad_column(b, gap, 0);
    lv_obj_set_flex_align(b, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    return b;
}

/* ---- glyphs: the mock's SVG paths on a 48-unit grid, scaled to the disc ---- */

typedef struct {
    int n;
    int16_t p[10][2];
} path_t;

static const path_t CUBE[] = {{7, {{24, 5}, {41, 14}, {41, 33}, {24, 43}, {7, 33}, {7, 14}, {24, 5}}},
                              {3, {{7, 14}, {24, 24}, {41, 14}}},
                              {2, {{24, 24}, {24, 43}}}};
static const path_t HANDS[] = {{3, {{24, 13}, {24, 24}, {31, 28}}}};
/* A compass: the clock's ring, and a needle as a diamond tilted to the north-east. */
static const path_t NEEDLE[] = {{5, {{32, 12}, {27, 27}, {16, 36}, {21, 21}, {32, 12}}}};
static const path_t SPOKES[] = {{2, {{24, 5}, {24, 11}}},  {2, {{24, 37}, {24, 43}}},
                                {2, {{5, 24}, {11, 24}}},  {2, {{37, 24}, {43, 24}}},
                                {2, {{11, 11}, {15, 15}}}, {2, {{33, 33}, {37, 37}}},
                                {2, {{11, 37}, {15, 33}}}, {2, {{33, 15}, {37, 11}}}};

static void free_points(lv_event_t *e)
{
    lv_free(lv_event_get_user_data(e));
}

static void draw_path(lv_obj_t *box, const path_t *path, int size, uint32_t color, int stroke)
{
    lv_point_precise_t *pts = lv_malloc(sizeof *pts * (size_t)path->n);
    for (int i = 0; i < path->n; i++) {
        pts[i].x = path->p[i][0] * size / 48;
        pts[i].y = path->p[i][1] * size / 48;
    }
    lv_obj_t *l = lv_line_create(box);
    lv_line_set_points_mutable(l, pts, (uint32_t)path->n);
    lv_obj_add_event_cb(l, free_points, LV_EVENT_DELETE, pts);
    lv_obj_set_style_line_color(l, C(color), 0);
    lv_obj_set_style_line_width(l, stroke, 0);
    lv_obj_set_style_line_rounded(l, true, 0);
    lv_obj_set_pos(l, 0, 0);
}

static void draw_ring(lv_obj_t *box, int size, int r48, uint32_t color, int stroke)
{
    lv_obj_t *c = bare(box);
    int d = 2 * r48 * size / 48;
    lv_obj_set_size(c, d, d);
    lv_obj_set_pos(c, size / 2 - d / 2, size / 2 - d / 2);
    lv_obj_set_style_radius(c, LV_RADIUS_CIRCLE, 0);
    border(c, color, stroke);
}

lv_obj_t *ui_icon(lv_obj_t *parent, const ui_icon_t *icon, ui_accent_t accent, int disc_px,
                  bool dim)
{
    uint32_t color = accent == UI_ACCENT_NEUTRAL ? UI_C_TEXT_SOFT : ui_accent_color(accent);
    lv_obj_t *disc = bare(parent);
    lv_obj_set_size(disc, disc_px, disc_px);
    if (accent == UI_ACCENT_NEUTRAL)
        fill(disc, UI_C_RAISED, LV_OPA_COVER, LV_RADIUS_CIRCLE);
    else
        fill(disc, color, UI_OPA_TINT, LV_RADIUS_CIRCLE);
    if (dim) lv_obj_set_style_opa(disc, LV_OPA_50, 0);
    int g = disc_px * 52 / 96;
    int stroke = disc_px >= 64 ? 3 : 2;
    if (icon->glyph == UI_GLYPH_INITIAL) {
        char s[2] = {icon->initial, 0};
        lv_obj_t *l = ui_label(disc, s, disc_px * 46 / 96, color);
        lv_obj_center(l);
        return disc;
    }
    lv_obj_t *box = bare(disc);
    lv_obj_set_size(box, g, g);
    lv_obj_center(box);
    switch (icon->glyph) {
    case UI_GLYPH_JERV: {
        /* The PWA's mark, rasterised from its SVG for each size the panel draws (an alpha mask,
           recoloured here): the tile's, or the header's for anything smaller. */
        lv_obj_t *img = lv_image_create(box);
        lv_image_set_src(img, g >= 40 ? &ui_jerv_mark_52 : &ui_jerv_mark_23);
        lv_obj_set_style_image_recolor(img, C(color), 0);
        lv_obj_set_style_image_recolor_opa(img, LV_OPA_COVER, 0);
        lv_obj_center(img);
        break;
    }
    case UI_GLYPH_CUBE:
        for (size_t i = 0; i < sizeof CUBE / sizeof *CUBE; i++) draw_path(box, &CUBE[i], g, color, stroke);
        break;
    case UI_GLYPH_CLOCK:
        draw_ring(box, g, 18, color, stroke);
        draw_path(box, HANDS, g, color, stroke);
        break;
    case UI_GLYPH_COMPASS:
        draw_ring(box, g, 18, color, stroke);
        draw_path(box, NEEDLE, g, color, stroke);
        break;
    case UI_GLYPH_GEAR:
        draw_ring(box, g, 6, color, stroke);
        draw_ring(box, g, 13, color, stroke);
        for (size_t i = 0; i < sizeof SPOKES / sizeof *SPOKES; i++)
            draw_path(box, &SPOKES[i], g, color, stroke);
        break;
    default: break;
    }
    return disc;
}

/* Main.dc.html's mic on the 48-unit grid (its 24-unit SVG doubled): a capsule, the cradle's
 * half circle (r 14 about 24,22, every 30°) and the stem. */
static const path_t MIC_CRADLE[] = {{7, {{10, 22}, {12, 29}, {17, 34}, {24, 36}, {31, 34}, {36, 29}, {38, 22}}}};
static const path_t MIC_STEM[] = {{2, {{24, 36}, {24, 42}}}};

lv_obj_t *ui_mic(lv_obj_t *parent, int px, uint32_t color)
{
    lv_obj_t *box = bare(parent);
    lv_obj_set_size(box, px, px);
    const int sw = px * 5 / 48 < 2 ? 2 : px * 5 / 48;
    lv_obj_t *cap = bare(box);
    lv_obj_set_pos(cap, 18 * px / 48 - sw / 2, 6 * px / 48 - sw / 2);
    lv_obj_set_size(cap, 12 * px / 48 + sw, 22 * px / 48 + sw);
    lv_obj_set_style_radius(cap, LV_RADIUS_CIRCLE, 0);
    border(cap, color, sw);
    draw_path(box, MIC_CRADLE, px, color, sw);
    draw_path(box, MIC_STEM, px, color, sw);
    return box;
}

lv_obj_t *ui_talk_hint(lv_obj_t *root)
{
    lv_obj_t *row = ui_box(root, LV_FLEX_FLOW_ROW, 10);
    ui_mic(row, 20, UI_C_MUTED);
    ui_label(row, "Hold KEY to talk", 18, UI_C_MUTED);
    lv_obj_align(row, LV_ALIGN_BOTTOM_MID, 0, -20);
    return row;
}

lv_obj_t *ui_header(lv_obj_t *root, const ui_icon_t *icon, ui_accent_t accent, const char *title,
                    lv_event_cb_t on_back, void *user)
{
    lv_obj_t *h = bare(root);
    lv_obj_set_size(h, UI_W, UI_HEADER_H);
    lv_obj_set_style_border_side(h, LV_BORDER_SIDE_BOTTOM, 0);
    border(h, UI_C_SURFACE, 1);
    lv_obj_set_style_pad_hor(h, UI_PAD_PAGE, 0);
    lv_obj_set_flex_flow(h, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(h, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(h, 12, 0);

    lv_obj_t *back = bare(h);
    lv_obj_set_size(back, UI_TOUCH_MIN, UI_TOUCH_MIN);
    fill(back, UI_C_SURFACE, LV_OPA_COVER, LV_RADIUS_CIRCLE);
    lv_obj_add_flag(back, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_ext_click_area(back, 12);
    if (on_back) lv_obj_add_event_cb(back, on_back, LV_EVENT_CLICKED, user);
    lv_obj_t *chev = ui_label(back, LV_SYMBOL_LEFT, 20, UI_C_TEXT);
    lv_obj_center(chev);

    ui_icon(h, icon, accent, UI_TOUCH_MIN, false);
    lv_obj_t *t = ui_label(h, title, 26, UI_C_TEXT);
    lv_obj_set_flex_grow(t, 1);
    return h;
}

lv_obj_t *ui_page(lv_obj_t *root, bool under_header, int bottom_gap)
{
    int top = under_header ? UI_HEADER_H + 1 : 0;
    lv_obj_t *p = bare(root);
    lv_obj_add_flag(p, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_pos(p, 0, top);
    lv_obj_set_size(p, UI_W, UI_H - top - bottom_gap);
    lv_obj_set_scroll_dir(p, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(p, LV_SCROLLBAR_MODE_AUTO);
    lv_obj_set_style_pad_hor(p, UI_PAD_PAGE, 0);
    lv_obj_set_style_pad_top(p, 16, 0);
    lv_obj_set_style_pad_bottom(p, 28, 0);
    lv_obj_set_flex_flow(p, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(p, UI_GAP, 0);
    /* The mock's thumb: 4 px, 5 px in from the right edge, inset 8 px from the track ends. */
    lv_obj_set_style_width(p, 4, LV_PART_SCROLLBAR);
    lv_obj_set_style_pad_right(p, 5, LV_PART_SCROLLBAR);
    lv_obj_set_style_pad_ver(p, 8, LV_PART_SCROLLBAR);
    lv_obj_set_style_radius(p, 2, LV_PART_SCROLLBAR);
    lv_obj_set_style_bg_color(p, C(UI_C_THUMB), LV_PART_SCROLLBAR);
    lv_obj_set_style_bg_opa(p, LV_OPA_COVER, LV_PART_SCROLLBAR);
    return p;
}

void ui_page_pin_bottom(lv_obj_t *content)
{
    lv_obj_update_layout(content);
    lv_obj_scroll_to_y(content, LV_COORD_MAX, LV_ANIM_OFF);
}

lv_obj_t *ui_card(lv_obj_t *parent, const char *title)
{
    lv_obj_t *c = bare(parent);
    lv_obj_set_size(c, LV_PCT(100), LV_SIZE_CONTENT);
    fill(c, UI_C_SURFACE, LV_OPA_COVER, UI_R_CARD);
    border(c, UI_C_LINE, 1);
    lv_obj_set_style_pad_hor(c, UI_PAD_CARD, 0);
    lv_obj_set_style_pad_ver(c, 16, 0);
    lv_obj_set_flex_flow(c, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(c, 12, 0);
    if (title) ui_label(c, title, 22, UI_C_TEXT);
    return c;
}

lv_obj_t *ui_chip(lv_obj_t *parent, const char *text, uint32_t color, bool tinted)
{
    lv_obj_t *c = bare(parent);
    lv_obj_set_size(c, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    if (tinted)
        fill(c, color, UI_OPA_TINT, 14);
    else
        fill(c, UI_C_RAISED, LV_OPA_COVER, 14);
    lv_obj_set_style_pad_hor(c, 12, 0);
    lv_obj_set_style_pad_ver(c, 4, 0);
    ui_label(c, text, 16, color);
    return c;
}

static lv_obj_t *row(lv_obj_t *parent)
{
    lv_obj_t *r = bare(parent);
    lv_obj_set_size(r, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(r, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(r, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER,
                          LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(r, 16, 0);
    return r;
}

lv_obj_t *ui_switch_row(lv_obj_t *parent, const char *label, bool on, bool enabled,
                        lv_event_cb_t cb, void *user)
{
    lv_obj_t *r = row(parent);
    lv_obj_t *l = ui_label_wrap(r, label, 20, UI_C_TEXT);
    lv_obj_set_width(l, LV_SIZE_CONTENT);
    lv_obj_set_flex_grow(l, 1);
    lv_obj_t *sw = lv_switch_create(r);
    lv_obj_remove_style_all(sw);
    lv_obj_set_size(sw, UI_SWITCH_W, UI_SWITCH_H);
    lv_obj_set_ext_click_area(sw, 4);
    fill(sw, UI_C_RAISED, LV_OPA_COVER, LV_RADIUS_CIRCLE);
    border(sw, UI_C_LINE_STRONG, 1);
    lv_obj_set_style_bg_color(sw, C(UI_C_OK), LV_PART_MAIN | LV_STATE_CHECKED);
    lv_obj_set_style_bg_opa(sw, UI_OPA_TRACK_ON, LV_PART_MAIN | LV_STATE_CHECKED);
    lv_obj_set_style_border_width(sw, 0, LV_PART_MAIN | LV_STATE_CHECKED);
    lv_obj_set_style_bg_color(sw, C(UI_C_MUTED), LV_PART_KNOB);
    lv_obj_set_style_bg_opa(sw, LV_OPA_COVER, LV_PART_KNOB);
    lv_obj_set_style_radius(sw, LV_RADIUS_CIRCLE, LV_PART_KNOB);
    lv_obj_set_style_pad_all(sw, -5, LV_PART_KNOB);
    lv_obj_set_style_bg_color(sw, C(UI_C_OK), LV_PART_KNOB | LV_STATE_CHECKED);
    if (on) lv_obj_add_state(sw, LV_STATE_CHECKED);
    if (!enabled) {
        lv_obj_add_state(sw, LV_STATE_DISABLED);
        lv_obj_remove_flag(sw, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_style_opa(r, 115, 0);
    }
    if (cb) lv_obj_add_event_cb(sw, cb, LV_EVENT_VALUE_CHANGED, user);
    return sw;
}

lv_obj_t *ui_switch_row_sub(lv_obj_t *parent, const char *title, int px, const char *sub, bool on,
                            bool enabled, lv_event_cb_t cb, void *user)
{
    lv_obj_t *sw = ui_switch_row(parent, title, on, enabled, cb, user);
    lv_obj_t *r = lv_obj_get_parent(sw);
    lv_obj_t *l = lv_obj_get_child(r, 0);
    lv_obj_set_style_text_font(l, ui_font(px), 0);
    if (!sub) return sw;
    /* The title becomes the first line of a column, with the muted line under it. */
    lv_obj_t *col = bare(r);
    lv_obj_move_to_index(col, 0);
    lv_obj_set_height(col, LV_SIZE_CONTENT);
    lv_obj_set_flex_grow(col, 1);
    lv_obj_set_flex_flow(col, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(col, 2, 0);
    lv_obj_set_parent(l, col);
    lv_obj_set_width(l, LV_PCT(100));
    lv_obj_set_flex_grow(l, 0);
    ui_label_wrap(col, sub, 16, UI_C_MUTED);
    return sw;
}

lv_obj_t *ui_divider(lv_obj_t *parent)
{
    lv_obj_t *d = bare(parent);
    lv_obj_set_size(d, LV_PCT(100), 1);
    fill(d, UI_C_LINE, LV_OPA_COVER, 0);
    return d;
}

lv_obj_t *ui_choice(lv_obj_t *parent, const char *text, uint32_t color, bool picked, bool enabled,
                    lv_event_cb_t cb, void *user)
{
    lv_obj_t *b = bare(parent);
    lv_obj_set_height(b, UI_BUTTON_H);
    lv_obj_set_flex_grow(b, 1);
    fill(b, 0x0A0A0A, LV_OPA_COVER, 16);
    border(b, picked ? UI_C_STEEL : UI_C_LINE, 2);
    lv_obj_t *l = ui_label(b, text, 22, color);
    lv_obj_center(l);
    if (!enabled) lv_obj_set_style_opa(b, LV_OPA_50, 0);
    lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
    if (cb) lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, user);
    return b;
}

lv_obj_t *ui_corner_badge(lv_obj_t *parent, const char *count, uint32_t color, uint8_t opa,
                          lv_event_cb_t cb, void *user)
{
    lv_obj_t *b = bare(parent);
    lv_obj_set_size(b, LV_SIZE_CONTENT, UI_TOUCH_MIN);
    lv_obj_set_style_min_width(b, UI_TOUCH_MIN, 0);
    lv_obj_set_style_pad_hor(b, 12, 0);
    fill(b, 0x0A0A0A, LV_OPA_COVER, UI_TOUCH_MIN / 2);
    border(b, UI_C_LINE, 1);
    lv_obj_set_style_opa(b, opa, 0);
    lv_obj_set_flex_flow(b, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(b, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(b, 6, 0);
    ui_label(b, LV_SYMBOL_BELL, 16, color);
    ui_label(b, count, 18, color);
    lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_ext_click_area(b, 10);
    if (cb) lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, user);
    return b;
}

lv_obj_t *ui_slider_row(lv_obj_t *parent, int min, int max, int value, lv_event_cb_t cb,
                        void *user)
{
    lv_obj_t *r = row(parent);
    lv_obj_set_style_pad_column(r, 12, 0);
    ui_label(r, LV_SYMBOL_MINUS, 18, UI_C_MUTED);
    lv_obj_t *s = lv_slider_create(r);
    lv_obj_remove_style_all(s);
    lv_obj_set_height(s, 8);
    lv_obj_set_flex_grow(s, 1);
    lv_obj_set_ext_click_area(s, 18);
    fill(s, UI_C_TRACK, LV_OPA_COVER, 4);
    lv_obj_set_style_bg_color(s, C(UI_C_STEEL), LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(s, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_obj_set_style_radius(s, 4, LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(s, C(UI_C_STEEL), LV_PART_KNOB);
    lv_obj_set_style_bg_opa(s, LV_OPA_COVER, LV_PART_KNOB);
    lv_obj_set_style_radius(s, LV_RADIUS_CIRCLE, LV_PART_KNOB);
    lv_obj_set_style_pad_all(s, 8, LV_PART_KNOB);
    lv_slider_set_range(s, min, max);
    lv_slider_set_value(s, value, LV_ANIM_OFF);
    if (cb) lv_obj_add_event_cb(s, cb, LV_EVENT_VALUE_CHANGED, user);
    ui_label(r, LV_SYMBOL_PLUS, 18, UI_C_MUTED);
    return s;
}

lv_obj_t *ui_progress(lv_obj_t *parent, int pct, uint32_t color, int h)
{
    lv_obj_t *track = bare(parent);
    lv_obj_set_size(track, LV_PCT(100), h);
    fill(track, UI_C_TRACK, LV_OPA_COVER, h / 2);
    lv_obj_t *bar = bare(track);
    lv_obj_set_size(bar, LV_PCT(pct < 0 ? 0 : pct > 100 ? 100 : pct), h);
    fill(bar, color, LV_OPA_COVER, h / 2);
    return track;
}

lv_obj_t *ui_button(lv_obj_t *parent, const char *text, ui_btn_kind_t kind, bool enabled,
                    lv_event_cb_t cb, void *user)
{
    lv_obj_t *b = bare(parent);
    lv_obj_set_size(b, LV_PCT(100), UI_BUTTON_H);
    uint32_t color = kind == UI_BTN_PRIMARY ? UI_C_STEEL
                     : kind == UI_BTN_DESTRUCTIVE ? UI_C_ROSE
                                                  : UI_C_TEXT;
    if (!enabled) {
        fill(b, UI_C_RAISED, LV_OPA_COVER, UI_R_BUTTON);
        color = UI_C_FAINT;
    } else if (kind == UI_BTN_SECONDARY) {
        fill(b, UI_C_RAISED, LV_OPA_COVER, UI_R_BUTTON);
        border(b, UI_C_LINE, 1);
    } else {
        fill(b, color, UI_OPA_TINT, UI_R_BUTTON);
    }
    lv_obj_t *l = ui_label(b, text, 20, color);
    lv_obj_center(l);
    if (enabled) {
        lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
        if (cb) lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, user);
    }
    return b;
}

lv_obj_t *ui_tile(lv_obj_t *parent, int x, int y, const char *title, const ui_icon_t *icon,
                  ui_accent_t accent, const ui_tile_state_t *st, lv_event_cb_t cb, void *user)
{
    lv_obj_t *t = bare(parent);
    lv_obj_set_pos(t, x, y);
    lv_obj_set_size(t, UI_TILE_W, UI_TILE_H);
    fill(t, UI_C_SURFACE, LV_OPA_COVER, UI_R_TILE);
    border(t, UI_C_LINE, 1);
    /* Bubbling lets the home page see a drag that starts on a tile and turn it into a swipe. */
    lv_obj_add_flag(t, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_EVENT_BUBBLE);
    if (cb) lv_obj_add_event_cb(t, cb, LV_EVENT_CLICKED, user);
    lv_obj_t *col = ui_box(t, LV_FLEX_FLOW_COLUMN, 14);
    lv_obj_center(col);
    ui_icon(col, icon, accent, 96, st && st->dimmed);
    const char *label = st && st->live_label[0] ? st->live_label : title;
    ui_label(col, label, 24, st && st->dimmed ? UI_C_MUTED : UI_C_TEXT);
    if (st && st->soon) {
        lv_obj_t *chip = ui_chip(t, "Soon", UI_C_MUTED, false);
        lv_obj_align(chip, LV_ALIGN_TOP_RIGHT, -14, 14);
    }
    if (st && st->badge) {
        char n[8];
        snprintf(n, sizeof n, "%u", st->badge > 99 ? 99u : (unsigned)st->badge);
        lv_obj_t *b = bare(t);
        lv_obj_set_size(b, LV_SIZE_CONTENT, 36);
        lv_obj_set_style_min_width(b, 36, 0);
        lv_obj_set_style_pad_hor(b, 8, 0);
        fill(b, UI_C_WARN, LV_OPA_COVER, 18);
        lv_obj_t *l = ui_label(b, n, 20, UI_C_BG);
        lv_obj_center(l);
        lv_obj_align(b, LV_ALIGN_TOP_RIGHT, -12, 12);
    }
    return t;
}

lv_obj_t *ui_tile_empty(lv_obj_t *parent, int x, int y)
{
    lv_obj_t *t = bare(parent);
    lv_obj_set_pos(t, x, y);
    lv_obj_set_size(t, UI_TILE_W, UI_TILE_H);
    lv_obj_set_style_radius(t, UI_R_TILE, 0);
    border(t, UI_C_LINE, 2);
    lv_obj_t *l = ui_label(t, "Empty", 20, UI_C_FAINT);
    lv_obj_center(l);
    return t;
}

lv_obj_t *ui_bubble(lv_obj_t *parent, const ui_bubble_t *b, ui_accent_t accent, lv_event_cb_t cb,
                    void *user)
{
    /* Flex has no per-item alignment, so each bubble rides a full-width line of its own. */
    lv_obj_t *line = bare(parent);
    lv_obj_set_size(line, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(line, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(line, b->mine ? LV_FLEX_ALIGN_END : LV_FLEX_ALIGN_START,
                          LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_t *wrap = ui_box(line, LV_FLEX_FLOW_COLUMN, 4);
    lv_obj_set_flex_align(wrap, LV_FLEX_ALIGN_START,
                          b->mine ? LV_FLEX_ALIGN_END : LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_t *body = ui_box(wrap, LV_FLEX_FLOW_COLUMN, 8);
    lv_obj_set_style_max_width(body, 360, 0);
    lv_obj_set_style_pad_hor(body, 16, 0);
    lv_obj_set_style_pad_ver(body, 12, 0);
    fill(body, b->mine ? UI_C_MINE : UI_C_RAISED, LV_OPA_COVER, UI_R_BUBBLE);
    uint32_t accent_c = ui_accent_color(accent);
    if (b->voice && b->mine) {
        lv_obj_t *r = ui_box(body, LV_FLEX_FLOW_ROW, 8);
        ui_mic(r, 18, UI_C_STEEL);
        ui_label(r, b->dur, 18, UI_C_STEEL);
    } else if (b->voice) {
        lv_obj_t *r = ui_box(body, LV_FLEX_FLOW_ROW, 10);
        lv_obj_t *play = bare(r);
        lv_obj_set_size(play, UI_TOUCH_MIN, UI_TOUCH_MIN);
        fill(play, accent_c, UI_OPA_TINT, LV_RADIUS_CIRCLE);
        lv_obj_add_flag(play, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_user_data(play, (void *)b->tag);
        if (cb) lv_obj_add_event_cb(play, cb, LV_EVENT_CLICKED, user);
        lv_obj_t *g = ui_label(play, b->playing ? LV_SYMBOL_PAUSE : LV_SYMBOL_PLAY, 16, accent_c);
        lv_obj_center(g);
        lv_obj_t *track = ui_progress(r, b->prog, accent_c, 6);
        lv_obj_set_width(track, 150);
        ui_label(r, b->dur, 18, UI_C_MUTED);
        if (b->unplayed) {
            lv_obj_t *dot = bare(r);
            lv_obj_set_size(dot, 10, 10);
            fill(dot, UI_C_WARN, LV_OPA_COVER, LV_RADIUS_CIRCLE);
        }
    }
    lv_obj_t *t = ui_label(body, b->text, 22, b->pending ? UI_C_MUTED : UI_C_TEXT);
    lv_label_set_long_mode(t, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_max_width(t, 360 - 32, 0);
    if (b->listen) {
        lv_obj_t *l = bare(body);
        lv_obj_set_size(l, LV_SIZE_CONTENT, UI_TOUCH_MIN);
        lv_obj_set_style_pad_hor(l, 14, 0);
        fill(l, UI_C_JERV, UI_OPA_TINT, 22);
        lv_obj_add_flag(l, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_user_data(l, (void *)b->tag);
        if (cb) lv_obj_add_event_cb(l, cb, LV_EVENT_CLICKED, user);
        lv_obj_t *lt = ui_label(l, LV_SYMBOL_VOLUME_MAX " Listen", 16, UI_C_JERV);
        lv_obj_center(lt);
    }
    if (b->speaking) ui_label(wrap, LV_SYMBOL_VOLUME_MAX " Speaking…", 16, UI_C_JERV);
    if (b->status && b->status[0]) ui_label(wrap, b->status, 16, UI_C_MUTED);
    return line;
}

lv_obj_t *ui_hold_ring(lv_obj_t *parent, int x, int y, uint32_t color)
{
    enum { D = 92, W = 3 };
    lv_obj_t *a = lv_arc_create(parent);
    lv_obj_remove_style_all(a);
    lv_obj_remove_flag(a, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(a, D, D);
    int cx = x - D / 2, cy = y - D / 2;
    cx = cx < 0 ? 0 : cx > UI_W - D ? UI_W - D : cx;
    cy = cy < 0 ? 0 : cy > UI_H - D ? UI_H - D : cy;
    lv_obj_set_pos(a, cx, cy);
    lv_arc_set_rotation(a, 270);
    lv_arc_set_bg_angles(a, 0, 360);
    lv_arc_set_range(a, 0, 1000);
    lv_arc_set_value(a, 0);
    /* A faint track and a thin stroke: feedback, not a control. */
    lv_obj_set_style_arc_width(a, W, LV_PART_MAIN);
    lv_obj_set_style_arc_color(a, C(color), LV_PART_MAIN);
    lv_obj_set_style_arc_opa(a, LV_OPA_20, LV_PART_MAIN);
    lv_obj_set_style_arc_width(a, W, LV_PART_INDICATOR);
    lv_obj_set_style_arc_color(a, C(color), LV_PART_INDICATOR);
    lv_obj_set_style_arc_rounded(a, true, LV_PART_INDICATOR);
    return a;
}

void ui_hold_ring_set(lv_obj_t *ring, uint16_t permille)
{
    lv_arc_set_value(ring, permille > 1000 ? 1000 : permille);
}

/* A steady quarter arc, once round a second. LVGL's own spinner grows and shrinks its arc on an
 * eased sweep, so a third of the way round it seemed to snap back and start again (owner,
 * 2026-10-12: "I expected it to go all the way around but it's kind of like resetting"); and,
 * rebuilt with its row on every status change, it really did. The angle is read from the tick,
 * not from the animation's own clock, so any spinner — a new one included — is exactly where the
 * last one was: there is no phase to lose. */
#define UI_SPIN_PERIOD_MS 1000
#define UI_SPIN_ARC 90

static void spin_exec(void *obj, int32_t v)
{
    (void)v;
    lv_arc_set_rotation(obj, (int32_t)(lv_tick_get() % UI_SPIN_PERIOD_MS * 360 / UI_SPIN_PERIOD_MS));
}

lv_obj_t *ui_spinner(lv_obj_t *parent, uint32_t color)
{
    lv_obj_t *a = lv_arc_create(parent);
    lv_obj_remove_style_all(a);
    lv_obj_remove_flag(a, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(a, 18, 18);
    lv_arc_set_bg_angles(a, 0, 360);
    lv_arc_set_angles(a, 0, UI_SPIN_ARC);
    lv_obj_set_style_arc_width(a, 2, LV_PART_MAIN);
    lv_obj_set_style_arc_color(a, C(UI_C_LINE_STRONG), LV_PART_MAIN);
    lv_obj_set_style_arc_width(a, 2, LV_PART_INDICATOR);
    lv_obj_set_style_arc_color(a, C(color), LV_PART_INDICATOR);
    lv_obj_set_style_arc_rounded(a, true, LV_PART_INDICATOR);
    spin_exec(a, 0);
    lv_anim_t an;
    lv_anim_init(&an);
    lv_anim_set_var(&an, a);
    lv_anim_set_exec_cb(&an, spin_exec);
    lv_anim_set_values(&an, 0, 360);
    lv_anim_set_duration(&an, UI_SPIN_PERIOD_MS);
    lv_anim_set_repeat_count(&an, LV_ANIM_REPEAT_INFINITE);
    lv_anim_start(&an);
    return a;
}

bool ui_is_spinner(lv_obj_t *o)
{
    return o && lv_anim_get(o, spin_exec) != NULL;
}

lv_obj_t *ui_step_row(lv_obj_t *parent, lv_obj_t **mark, lv_obj_t **text)
{
    /* On its own line under the waiting question, where the answer will land. */
    lv_obj_t *line = bare(parent);
    lv_obj_set_size(line, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(line, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(line, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(line, 10, 0);
    lv_obj_set_style_pad_left(line, 6, 0);
    *mark = ui_label(line, "", 16, UI_C_MUTED);
    lv_obj_set_width(*mark, 18);
    lv_obj_set_style_text_align(*mark, LV_TEXT_ALIGN_CENTER, 0);
    /* The mock's italic status row: the image carries no italic face, so it is the dim colour and
       a smaller size that set it apart from a message. */
    *text = ui_label(line, "", 18, UI_C_MUTED);
    lv_label_set_long_mode(*text, LV_LABEL_LONG_DOT);
    lv_obj_set_width(*text, 360);
    return line;
}

lv_obj_t *ui_note_line(lv_obj_t *parent, const char *text)
{
    lv_obj_t *l = ui_label(parent, text, 16, UI_C_FAINT);
    lv_obj_set_style_pad_left(l, 6, 0);
    return l;
}

lv_obj_t *ui_overlay(lv_obj_t *parent)
{
    lv_obj_t *o = bare(parent);
    lv_obj_set_size(o, UI_W, UI_H);
    lv_obj_set_pos(o, 0, 0);
    fill(o, UI_C_BG, LV_OPA_COVER, 0);
    lv_obj_add_flag(o, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_flex_flow(o, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(o, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    return o;
}

lv_obj_t *ui_menu(lv_obj_t *parent, const char *title, lv_event_cb_t on_outside, void *user)
{
    lv_obj_t *dim = bare(parent);
    lv_obj_set_size(dim, UI_W, UI_H);
    lv_obj_set_pos(dim, 0, 0);
    fill(dim, UI_C_BG, LV_OPA_70, 0);
    lv_obj_add_flag(dim, LV_OBJ_FLAG_CLICKABLE);
    if (on_outside) lv_obj_add_event_cb(dim, on_outside, LV_EVENT_CLICKED, user);
    lv_obj_t *card = ui_card(dim, title);
    lv_obj_set_width(card, UI_W - 2 * 60);
    lv_obj_set_style_pad_ver(card, 22, 0);
    lv_obj_set_style_pad_row(card, UI_GAP, 0);
    /* A press on the card is the card's, not the backdrop's: it must not close the menu. */
    lv_obj_add_flag(card, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_center(card);
    return card;
}

static void talk_tick(lv_timer_t *tm)
{
    ui_talk_t *t = lv_timer_get_user_data(tm);
    if (t->ms && t->cap_ms && t->ms() >= t->cap_ms && t->on_cap) {
        t->on_cap(t->user);
        return;
    }
    ui_talk_refresh(t);
}

static void talk_tapped(lv_event_t *e)
{
    ui_talk_t *t = lv_event_get_user_data(e);
    if (t->on_tap) t->on_tap(t->user);
}

void ui_talk_open(ui_talk_t *t, lv_obj_t *parent, uint32_t color, const char *title)
{
    t->root = ui_overlay(parent);
    lv_obj_add_event_cb(t->root, talk_tapped, LV_EVENT_CLICKED, t);
    lv_obj_set_style_pad_row(t->root, 22, 0);
    lv_obj_t *head = ui_box(t->root, LV_FLEX_FLOW_ROW, 10);
    lv_obj_t *dot = bare(head);
    lv_obj_set_size(dot, 14, 14);
    fill(dot, color, LV_OPA_COVER, LV_RADIUS_CIRCLE);
    ui_label(head, title, 24, UI_C_TEXT);
    lv_obj_t *stage = bare(t->root);
    lv_obj_set_size(stage, 220, 220);
    t->ring = bare(stage);
    lv_obj_set_size(t->ring, 170, 170);
    lv_obj_center(t->ring);
    fill(t->ring, color, 26, LV_RADIUS_CIRCLE);
    border(t->ring, color, 6);
    t->time = ui_label(t->ring, "0:00", 48, UI_C_TEXT);
    lv_obj_center(t->time);
    ui_label(t->root, "Let go of KEY to send", 22, UI_C_TEXT);
    ui_label(t->root, "Tap the screen to cancel · 1:00 max", 18, UI_C_MUTED);
    t->timer = lv_timer_create(talk_tick, 100, t);
    ui_talk_refresh(t);
}

void ui_talk_refresh(ui_talk_t *t)
{
    if (!t->root) return;
    char s[16];
    unsigned secs = (unsigned)((t->ms ? t->ms() : 0) / 1000);
    snprintf(s, sizeof s, "%u:%02u", secs / 60, secs % 60);
    lv_label_set_text(t->time, s);
    uint8_t level = t->level ? t->level() : 0;
    int d = 170 + 50 * (level > 100 ? 100 : level) / 100;
    lv_obj_set_size(t->ring, d, d);
    lv_obj_center(t->ring);
}

void ui_talk_close(ui_talk_t *t)
{
    if (t->timer) lv_timer_delete(t->timer);
    if (t->root) lv_obj_delete(t->root);
    t->timer = NULL;
    t->root = NULL;
}

bool ui_talk_up(const ui_talk_t *t)
{
    return t->root != NULL;
}

static lv_obj_t *s_toast;
static lv_timer_t *s_toast_timer;

void ui_toast_clear(void)
{
    if (s_toast_timer) lv_timer_delete(s_toast_timer);
    if (s_toast) lv_obj_delete(s_toast);
    s_toast = NULL;
    s_toast_timer = NULL;
}

static void toast_expired(lv_timer_t *t)
{
    (void)t;
    s_toast_timer = NULL;
    if (s_toast) lv_obj_delete(s_toast);
    s_toast = NULL;
}

void ui_toast(const char *text, uint32_t ms)
{
    ui_toast_clear();
    s_toast = bare(lv_layer_top());
    lv_obj_set_size(s_toast, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_max_width(s_toast, UI_W - 80, 0);
    fill(s_toast, UI_C_RAISED, LV_OPA_COVER, 22);
    border(s_toast, UI_C_LINE, 1);
    lv_obj_set_style_pad_hor(s_toast, 20, 0);
    lv_obj_set_style_pad_ver(s_toast, 12, 0);
    lv_obj_t *l = ui_label(s_toast, text, 20, UI_C_TEXT);
    lv_label_set_long_mode(l, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_max_width(l, UI_W - 120, 0);
    lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(s_toast, LV_ALIGN_BOTTOM_MID, 0, -78);
    s_toast_timer = lv_timer_create(toast_expired, ms, NULL);
    lv_timer_set_repeat_count(s_toast_timer, 1);
}

/* ---- 0.0.15: the conversation menu, Recent and a switched thread (Main.dc.html) ---- */

lv_obj_t *ui_header_sub(lv_obj_t *root, const ui_icon_t *icon, ui_accent_t accent,
                        const char *title, const char *sub, lv_event_cb_t on_back, void *user,
                        lv_obj_t **sub_label)
{
    lv_obj_t *h = ui_header(root, icon, accent, title, on_back, user);
    /* The title the plain header made becomes the first of two lines: the name, and under it
       the conversation's title, one line cut with "…". */
    lv_obj_t *t = lv_obj_get_child(h, -1);
    lv_obj_t *col = ui_box(h, LV_FLEX_FLOW_COLUMN, 0);
    lv_obj_set_flex_grow(col, 1);
    lv_obj_set_flex_align(col, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_set_parent(t, col);
    lv_obj_set_style_text_font(t, ui_font(22), 0);
    lv_obj_set_flex_grow(t, 0);
    lv_obj_t *s = ui_label(col, sub, 18, UI_C_MUTED);
    lv_label_set_long_mode(s, LV_LABEL_LONG_DOT);
    lv_obj_set_width(s, UI_W - 2 * UI_PAD_PAGE - 2 * UI_TOUCH_MIN - 2 * 12);
    if (sub_label) *sub_label = s;
    return h;
}

/* The mock's phone on the 48-unit grid (its 24-unit SVG doubled): a rounded body and a short
 * line for its button. */
static const path_t PHONE_BUTTON[] = {{2, {{22, 36}, {26, 36}}}};

lv_obj_t *ui_phone(lv_obj_t *parent, int px, uint32_t color)
{
    lv_obj_t *box = bare(parent);
    lv_obj_set_size(box, px, px);
    const int sw = px * 4 / 48 < 2 ? 2 : px * 4 / 48;
    lv_obj_t *body = bare(box);
    lv_obj_set_pos(body, 14 * px / 48 - sw / 2, 5 * px / 48 - sw / 2);
    lv_obj_set_size(body, 20 * px / 48 + sw, 38 * px / 48 + sw);
    lv_obj_set_style_radius(body, 5 * px / 48, 0);
    border(body, color, sw);
    draw_path(box, PHONE_BUTTON, px, color, sw);
    return box;
}

void ui_menu_sub(lv_obj_t *card, const char *text)
{
    /* The card's title, then this one muted line under it, closer than the buttons below. */
    lv_obj_t *title = lv_obj_get_child(card, 0);
    lv_obj_t *head = ui_box(card, LV_FLEX_FLOW_COLUMN, 2);
    lv_obj_move_to_index(head, 0);
    lv_obj_set_width(head, LV_PCT(100));
    lv_obj_set_style_pad_hor(head, 4, 0);
    lv_obj_set_style_pad_bottom(head, 4, 0);
    lv_obj_set_flex_align(head, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_set_parent(title, head);
    lv_obj_set_style_text_font(title, ui_font(20), 0);
    lv_obj_t *s = ui_label(head, text, 18, UI_C_MUTED);
    lv_label_set_long_mode(s, LV_LABEL_LONG_DOT);
    lv_obj_set_width(s, LV_PCT(100));
}

lv_obj_t *ui_menu_row(lv_obj_t *parent, ui_row_glyph_t glyph, const char *text, uint32_t color,
                      bool tinted, bool chevron, lv_event_cb_t cb, void *user)
{
    lv_obj_t *b = bare(parent);
    lv_obj_set_size(b, LV_PCT(100), 60);
    if (tinted) {
        fill(b, color, UI_OPA_TINT, UI_R_BUTTON);
    } else {
        fill(b, UI_C_RAISED, LV_OPA_COVER, UI_R_BUTTON);
        border(b, UI_C_LINE_STRONG, 1);
    }
    lv_obj_set_style_pad_left(b, 18, 0);
    lv_obj_set_style_pad_right(b, 14, 0);
    lv_obj_set_flex_flow(b, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(b, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(b, 12, 0);
    if (glyph == UI_ROW_GLYPH_PLUS) {
        ui_label(b, LV_SYMBOL_PLUS, 20, color);
    } else if (glyph == UI_ROW_GLYPH_CLOCK) {
        lv_obj_t *g = bare(b);
        lv_obj_set_size(g, 22, 22);
        draw_ring(g, 22, 17, color, 2);
        draw_path(g, HANDS, 22, color, 2);
    }
    lv_obj_t *l = ui_label(b, text, 20, color);
    lv_obj_set_flex_grow(l, 1);
    if (chevron) ui_label(b, LV_SYMBOL_RIGHT, 18, UI_C_MUTED);
    lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
    if (cb) lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, user);
    return b;
}

lv_obj_t *ui_ghost_button(lv_obj_t *parent, const char *text, lv_event_cb_t cb, void *user)
{
    lv_obj_t *b = bare(parent);
    lv_obj_set_size(b, LV_PCT(100), UI_BUTTON_H);
    lv_obj_set_style_radius(b, UI_R_BUTTON, 0);
    border(b, UI_C_LINE, 1);
    lv_obj_t *l = ui_label(b, text, 20, UI_C_TEXT_SOFT);
    lv_obj_center(l);
    lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
    if (cb) lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, user);
    return b;
}

/* Recent's rows share their looks (ten rows of four widgets each would otherwise carry their own
 * copies in LVGL's 64 KB pool), and the row's glyph is drawn rather than built from widgets. */
static lv_style_t s_row, s_row_title, s_row_line, s_row_chip;
static bool s_row_styles;

static void row_styles(void)
{
    if (s_row_styles) return;
    s_row_styles = true;
    lv_style_init(&s_row);
    lv_style_set_radius(&s_row, 20);
    lv_style_set_bg_opa(&s_row, LV_OPA_COVER);
    lv_style_set_bg_color(&s_row, C(UI_C_SURFACE));
    lv_style_set_border_color(&s_row, C(UI_C_LINE));
    lv_style_set_border_width(&s_row, 1);
    lv_style_init(&s_row_title);
    lv_style_set_text_font(&s_row_title, ui_font(20));
    lv_style_set_text_color(&s_row_title, C(UI_C_TEXT));
    lv_style_init(&s_row_line);
    lv_style_set_text_font(&s_row_line, ui_font(18));
    lv_style_set_text_color(&s_row_line, C(UI_C_MUTED));
    lv_style_init(&s_row_chip);
    lv_style_set_text_font(&s_row_chip, ui_font(18));
    lv_style_set_text_color(&s_row_chip, C(UI_C_BG));
    lv_style_set_bg_opa(&s_row_chip, LV_OPA_COVER);
    lv_style_set_radius(&s_row_chip, 12);
    lv_style_set_pad_hor(&s_row_chip, 10);
    lv_style_set_pad_ver(&s_row_chip, 2);
}

#define ROW_H 84
#define ROW_PAD_L 22
#define ROW_PAD_R 16
#define ROW_GLYPH 18
#define ROW_TOP 12
#define ROW_LINE_Y 46

/* What a row's draw adds to its background: the glyph at its top left — the microphone (started
 * at the panel) or the phone (in the app), the mock's strokes on the 48-unit grid — and, on the
 * current row, the 6 px accent bar down its left edge inside its rounded corners. Packed into
 * the event's user data: bit 0 the phone, bit 1 the bar, the accent above them. */
#define ROW_APP 1u
#define ROW_BAR 2u

static void row_glyph(lv_event_t *e)
{
    lv_layer_t *layer = lv_event_get_layer(e);
    lv_obj_t *o = lv_event_get_current_target(e);
    const uintptr_t flags = (uintptr_t)lv_event_get_user_data(e);
    const bool app = flags & ROW_APP;
    lv_area_t a;
    lv_obj_get_coords(o, &a);
    if (flags & ROW_BAR) {
        /* The row's own rounded shape in the accent, clipped to its first 6 px: no layer, which
           clipping a child to the corners would cost. */
        lv_draw_rect_dsc_t bar;
        lv_draw_rect_dsc_init(&bar);
        bar.bg_color = C((uint32_t)(flags >> 8));
        bar.radius = 20;
        const lv_area_t keep = layer->_clip_area;
        lv_area_t *clip = &layer->_clip_area;
        clip->x1 = LV_MAX(keep.x1, a.x1);
        clip->x2 = LV_MIN(keep.x2, a.x1 + 5);
        clip->y1 = LV_MAX(keep.y1, a.y1);
        clip->y2 = LV_MIN(keep.y2, a.y2);
        if (clip->x1 <= clip->x2 && clip->y1 <= clip->y2) {
            const lv_area_t shape = {a.x1, a.y1, a.x1 + 60, a.y2};
            lv_draw_rect(layer, &bar, &shape);
        }
        layer->_clip_area = keep;
    }
    const int px = ROW_GLYPH, x0 = a.x1 + ROW_PAD_L, y0 = a.y1 + ROW_TOP + 3;
    const int sw = 2;
    lv_draw_rect_dsc_t r;
    lv_draw_rect_dsc_init(&r);
    r.bg_opa = LV_OPA_TRANSP;
    r.border_color = C(UI_C_MUTED);
    r.border_width = sw;
    lv_area_t body;
    lv_draw_line_dsc_t l;
    lv_draw_line_dsc_init(&l);
    l.color = C(UI_C_MUTED);
    l.width = sw;
    l.round_start = l.round_end = 1;
    if (app) {
        body = (lv_area_t){x0 + 14 * px / 48 - 1, y0 + 5 * px / 48 - 1, x0 + 34 * px / 48 + 1,
                           y0 + 43 * px / 48 + 1};
        r.radius = 3;
        lv_draw_rect(layer, &r, &body);
        l.p1 = (lv_point_precise_t){x0 + 22 * px / 48, y0 + 36 * px / 48};
        l.p2 = (lv_point_precise_t){x0 + 26 * px / 48, y0 + 36 * px / 48};
        lv_draw_line(layer, &l);
        return;
    }
    body = (lv_area_t){x0 + 18 * px / 48 - 1, y0 + 6 * px / 48 - 1, x0 + 30 * px / 48 + 1,
                       y0 + 28 * px / 48 + 1};
    r.radius = LV_RADIUS_CIRCLE;
    lv_draw_rect(layer, &r, &body);
    for (size_t i = 0; i + 1 < (size_t)MIC_CRADLE[0].n; i++) {
        l.p1 = (lv_point_precise_t){x0 + MIC_CRADLE[0].p[i][0] * px / 48, y0 + MIC_CRADLE[0].p[i][1] * px / 48};
        l.p2 = (lv_point_precise_t){x0 + MIC_CRADLE[0].p[i + 1][0] * px / 48,
                                    y0 + MIC_CRADLE[0].p[i + 1][1] * px / 48};
        lv_draw_line(layer, &l);
    }
    l.p1 = (lv_point_precise_t){x0 + 24 * px / 48, y0 + 36 * px / 48};
    l.p2 = (lv_point_precise_t){x0 + 24 * px / 48, y0 + 42 * px / 48};
    lv_draw_line(layer, &l);
}

lv_obj_t *ui_convo_row(lv_obj_t *parent, const ui_convo_row_t *r, ui_accent_t accent,
                       lv_event_cb_t cb, void *user)
{
    row_styles();
    const uint32_t color = ui_accent_color(accent);
    lv_obj_t *b = bare(parent);
    lv_obj_add_style(b, &s_row, 0);
    lv_obj_set_size(b, LV_PCT(100), ROW_H);
    const uintptr_t draw = (r->from_app ? ROW_APP : 0u) |
                           (r->current ? ROW_BAR | (uintptr_t)color << 8 : 0u);
    lv_obj_add_event_cb(b, row_glyph, LV_EVENT_DRAW_MAIN_END, (void *)draw);
    lv_obj_update_layout(parent);
    const int w = lv_obj_get_content_width(parent);
    const int text_x = ROW_PAD_L + ROW_GLYPH + 8;
    /* The time, or the Current chip, at the top right; the title takes what is left. */
    lv_obj_t *right;
    if (r->current) {
        lv_obj_set_style_bg_color(b, C(color), 0);
        lv_obj_set_style_bg_opa(b, UI_OPA_TINT, 0);
        lv_obj_set_style_border_width(b, 0, 0);
        right = lv_label_create(b);
        lv_obj_add_style(right, &s_row_chip, 0);
        lv_obj_set_style_bg_color(right, C(color), 0);
        lv_label_set_text(right, "Current");
    } else {
        right = lv_label_create(b);
        lv_obj_add_style(right, &s_row_line, 0);
        if (r->opening) lv_obj_set_style_text_color(right, C(color), 0);
        lv_label_set_text(right, r->when ? r->when : "");
    }
    lv_obj_align(right, LV_ALIGN_TOP_RIGHT, -ROW_PAD_R, r->current ? ROW_TOP - 1 : ROW_TOP + 2);
    lv_obj_update_layout(right);
    const int right_w = lv_obj_get_width(right);
    lv_obj_t *t = lv_label_create(b);
    lv_obj_add_style(t, &s_row_title, 0);
    lv_label_set_text(t, r->title ? r->title : "");
    lv_label_set_long_mode(t, LV_LABEL_LONG_DOT);
    lv_obj_set_pos(t, text_x, ROW_TOP);
    lv_obj_set_size(t, w - text_x - ROW_PAD_R - right_w - 12, lv_font_get_line_height(ui_font(20)));
    lv_obj_t *s = lv_label_create(b);
    lv_obj_add_style(s, &s_row_line, 0);
    lv_label_set_text(s, r->snippet ? r->snippet : "");
    lv_label_set_long_mode(s, LV_LABEL_LONG_DOT);
    lv_obj_set_pos(s, ROW_PAD_L, ROW_LINE_Y);
    lv_obj_set_size(s, w - ROW_PAD_L - ROW_PAD_R, lv_font_get_line_height(ui_font(18)));
    lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
    if (cb) lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, user);
    return b;
}

lv_obj_t *ui_skeleton_row(lv_obj_t *parent, int w1_pct, int w2_pct, lv_opa_t opa)
{
    lv_obj_t *b = bare(parent);
    lv_obj_set_size(b, LV_PCT(100), 84);
    fill(b, UI_C_SURFACE, LV_OPA_COVER, 20);
    border(b, UI_C_LINE, 1);
    lv_obj_set_style_opa(b, opa, 0);
    lv_obj_set_style_pad_hor(b, 20, 0);
    lv_obj_set_style_pad_ver(b, 18, 0);
    lv_obj_set_flex_flow(b, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(b, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_set_style_pad_row(b, 12, 0);
    lv_obj_t *top = bare(b);
    lv_obj_set_size(top, LV_PCT(100), 18);
    lv_obj_t *a = bare(top);
    lv_obj_set_size(a, LV_PCT(w1_pct), 18);
    fill(a, UI_C_LINE, LV_OPA_COVER, 9);
    lv_obj_t *when = bare(top);
    lv_obj_set_size(when, LV_PCT(22), 18);
    lv_obj_align(when, LV_ALIGN_RIGHT_MID, 0, 0);
    fill(when, UI_C_RAISED, LV_OPA_COVER, 9);
    lv_obj_t *bottom = bare(b);
    lv_obj_set_size(bottom, LV_PCT(w2_pct), 14);
    fill(bottom, UI_C_RAISED, LV_OPA_COVER, 7);
    return b;
}

/* The mock's speech bubble on the 48-unit grid (its 24-unit path doubled). */
static const path_t SPEECH[] = {
    {8, {{8, 10}, {40, 10}, {40, 32}, {20, 32}, {12, 40}, {12, 32}, {8, 32}, {8, 10}}}};

lv_obj_t *ui_empty_state(lv_obj_t *parent, const char *title, const char *body)
{
    lv_obj_t *col = ui_box(parent, LV_FLEX_FLOW_COLUMN, 14);
    lv_obj_set_width(col, LV_PCT(100));
    lv_obj_set_style_pad_hor(col, 28, 0);
    lv_obj_set_style_pad_top(col, 32, 0);
    lv_obj_set_flex_align(col, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_t *disc = bare(col);
    lv_obj_set_size(disc, 72, 72);
    fill(disc, UI_C_SURFACE, LV_OPA_COVER, LV_RADIUS_CIRCLE);
    border(disc, UI_C_LINE, 1);
    lv_obj_t *g = bare(disc);
    lv_obj_set_size(g, 34, 34);
    lv_obj_center(g);
    draw_path(g, SPEECH, 34, UI_C_FAINT, 2);
    ui_label(col, title, 22, UI_C_TEXT);
    lv_obj_t *l = ui_label_wrap(col, body, 18, UI_C_MUTED);
    lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
    return col;
}

lv_obj_t *ui_app_chip(lv_obj_t *parent, const char *text)
{
    lv_obj_t *c = bare(parent);
    lv_obj_set_size(c, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    fill(c, UI_C_SURFACE, LV_OPA_COVER, 20);
    border(c, UI_C_LINE, 1);
    lv_obj_set_style_pad_hor(c, 16, 0);
    lv_obj_set_style_pad_ver(c, 8, 0);
    lv_obj_set_flex_flow(c, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(c, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(c, 8, 0);
    ui_phone(c, 18, UI_C_MUTED);
    ui_label(c, text, 18, UI_C_MUTED);
    return c;
}

lv_obj_t *ui_fade_bottom(lv_obj_t *parent, int h)
{
    /* Four bands of black, each more opaque than the one above: plain blending, which every
       renderer does the same, rather than a gradient of opacity. */
    static const lv_opa_t BANDS[] = {LV_OPA_20, LV_OPA_40, LV_OPA_60, LV_OPA_80};
    const int n = (int)(sizeof BANDS / sizeof *BANDS), band = h / n;
    lv_obj_t *f = bare(parent);
    lv_obj_set_size(f, UI_W, band * n);
    lv_obj_align(f, LV_ALIGN_BOTTOM_MID, 0, 0);
    for (int i = 0; i < n; i++) {
        lv_obj_t *b = bare(f);
        lv_obj_set_size(b, UI_W, band);
        lv_obj_set_pos(b, 0, i * band);
        fill(b, UI_C_BG, BANDS[i], 0);
    }
    return f;
}
