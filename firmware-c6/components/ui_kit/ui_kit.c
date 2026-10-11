#include "ui_kit.h"

#include <stdio.h>
#include <string.h>

#define C(hex) lv_color_hex(hex)

uint32_t ui_accent_color(ui_accent_t a)
{
    switch (a) {
    case UI_ACCENT_JERV: return UI_C_JERV;
    case UI_ACCENT_QUORRA: return UI_C_QUORRA;
    case UI_ACCENT_TAMSIN: return UI_C_TAMSIN;
    case UI_ACCENT_MINECRAFT: return UI_C_MINECRAFT;
    case UI_ACCENT_STEEL: return UI_C_STEEL;
    default: return UI_C_TEXT;
    }
}

const lv_font_t *ui_font(int px)
{
    static const struct {
        int px;
        const lv_font_t *f;
    } fonts[] = {
        {14, &lv_font_montserrat_14}, {16, &lv_font_montserrat_16}, {18, &lv_font_montserrat_18},
        {20, &lv_font_montserrat_20}, {22, &lv_font_montserrat_22}, {24, &lv_font_montserrat_24},
        {26, &lv_font_montserrat_26}, {28, &lv_font_montserrat_28}, {30, &lv_font_montserrat_30},
        {48, &lv_font_montserrat_48},
    };
    const lv_font_t *best = fonts[0].f;
    int dist = 1000;
    for (size_t i = 0; i < sizeof fonts / sizeof *fonts; i++) {
        int d = px > fonts[i].px ? px - fonts[i].px : fonts[i].px - px;
        if (d < dist) {
            dist = d;
            best = fonts[i].f;
        }
    }
    return best;
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
    lv_obj_t *l = ui_label(parent, text, 48, color);
    if (px > 48) {
        lv_obj_set_style_transform_scale(l, 256 * px / 48, 0);
        lv_obj_set_style_transform_pivot_x(l, LV_PCT(50), 0);
        lv_obj_set_style_transform_pivot_y(l, LV_PCT(50), 0);
    }
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

static const path_t STAR[] = {{9, {{24, 5}, {28, 19}, {43, 24}, {28, 29}, {24, 43}, {19, 29},
                                   {5, 24}, {19, 19}, {24, 5}}}};
static const path_t CUBE[] = {{7, {{24, 5}, {41, 14}, {41, 33}, {24, 43}, {7, 33}, {7, 14}, {24, 5}}},
                              {3, {{7, 14}, {24, 24}, {41, 14}}},
                              {2, {{24, 24}, {24, 43}}}};
static const path_t HANDS[] = {{3, {{24, 13}, {24, 24}, {31, 28}}}};
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
    case UI_GLYPH_STAR: draw_path(box, STAR, g, color, stroke); break;
    case UI_GLYPH_CUBE:
        for (size_t i = 0; i < sizeof CUBE / sizeof *CUBE; i++) draw_path(box, &CUBE[i], g, color, stroke);
        break;
    case UI_GLYPH_CLOCK:
        draw_ring(box, g, 18, color, stroke);
        draw_path(box, HANDS, g, color, stroke);
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
        ui_label(r, LV_SYMBOL_AUDIO, 18, UI_C_STEEL);
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
    if (b->speaking) ui_label(wrap, LV_SYMBOL_VOLUME_MAX " Speaking...", 16, UI_C_JERV);
    if (b->status && b->status[0]) ui_label(wrap, b->status, 16, UI_C_MUTED);
    return line;
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
    ui_label(t->root, "Tap the screen to cancel - 1:00 max", 18, UI_C_MUTED);
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
