/* The notice overlay as the owner approved it on 2026-10-13 (mock boards NotifyInfo, NotifyQuestion,
 * NotifyQuestionSound, NotifyPicture, NotifyNight, NotifyBanner, NotifyAudio): the sender, the
 * title and the body over black, a speaker row for a sound, the answers, the hint, and a thin line
 * along the bottom edge that drains to the left over `hide_s`. A tap anywhere but a button hides
 * it, a question too (`dismissed`, no answer). A picture fills the glass or sits across the top,
 * drawn from its file a few rows at a time through an LVGL image decoder — never a frame in RAM;
 * at night every notice takes the night clock's colour and a picture waits behind Show picture.
 * What it looks like is notice_view.c's; this file only draws it, and the flush turns it with the
 * rest of the screen (auto-rotate). */
#include "notice_overlay.h"

#include <stdio.h>
#include <string.h>

#include "notice_view.h"
/* The decoder and draw-buffer structs: a custom image decoder fills them in (LVGL 9.2 keeps them
   out of lvgl.h). */
#include "src/draw/lv_draw_buf_private.h"
#include "src/draw/lv_image_decoder_private.h"
#include "svc_rtc.h"
#include "svc_settings.h"
#include "ui_kit.h"

#define C(hex) lv_color_hex(hex)
/* Rows a decode hands the renderer at a time: 480 × 8 × 2 = 7.5 KB of LVGL's pool for the length
   of one image draw, never a stripe's 38 KB. */
#define PIC_ROWS 8
#define REFRESH_MS 200

static struct {
    lv_obj_t *root, *hint, *chip_hint, *timer_fill;
    lv_obj_t *bar, *bar_fill, *again, *time; /* the speaker row's parts */
    lv_timer_t *tick;
    uint32_t id, revealed;
    uint8_t queued;
    /* The head's words stay in svc_notice (svc_notice_head): only what a tap needs is here. */
    uint8_t n_choices;
    uint16_t audio_ms;
    const char *kind;
} s_ov = {.kind = "none"};

/* ---- the picture: an image decoder that reads its rows from the notice's file ---- */

static lv_image_dsc_t s_pic_src;
static uint32_t s_pic_id;

typedef struct {
    notice_pic_t pic;
    lv_draw_buf_t buf;
    uint16_t *rows;
} pic_ctx_t;

static lv_result_t pic_info(lv_image_decoder_t *d, lv_image_decoder_dsc_t *dsc, lv_image_header_t *h)
{
    (void)d;
    if (dsc->src != &s_pic_src) return LV_RESULT_INVALID;
    *h = s_pic_src.header;
    return LV_RESULT_OK;
}

static lv_result_t pic_open(lv_image_decoder_t *d, lv_image_decoder_dsc_t *dsc)
{
    (void)d;
    pic_ctx_t *c = lv_malloc_zeroed(sizeof *c);
    if (!c) return LV_RESULT_INVALID;
    c->rows = lv_malloc((size_t)s_pic_src.header.w * PIC_ROWS * 2);
    if (!c->rows || !svc_notice_pic_open(s_pic_id, &c->pic)) {
        if (c->rows) lv_free(c->rows);
        lv_free(c);
        return LV_RESULT_INVALID;
    }
    dsc->user_data = c;
    /* `decoded` stays NULL until the first area: the renderer then asks for it a piece at a time. */
    return LV_RESULT_OK;
}

static lv_result_t pic_area(lv_image_decoder_t *d, lv_image_decoder_dsc_t *dsc, const lv_area_t *full,
                            lv_area_t *got)
{
    (void)d;
    pic_ctx_t *c = dsc->user_data;
    const int32_t y = got->y1 == LV_COORD_MIN ? full->y1 : got->y2 + 1;
    if (y > full->y2 || y >= c->pic.h) return LV_RESULT_INVALID;
    int32_t rows = full->y2 - y + 1;
    if (rows > PIC_ROWS) rows = PIC_ROWS;
    if (y + rows > c->pic.h) rows = c->pic.h - y;
    if (!svc_notice_pic_rows(&c->pic, (uint16_t)y, (uint16_t)rows, c->rows)) return LV_RESULT_INVALID;
    got->x1 = 0;
    got->x2 = c->pic.w - 1;
    got->y1 = y;
    got->y2 = y + rows - 1;
    c->buf.header = dsc->header;
    c->buf.header.h = (uint32_t)rows;
    c->buf.header.stride = (uint32_t)c->pic.w * 2;
    c->buf.data = (uint8_t *)c->rows;
    c->buf.data_size = c->buf.header.stride * (uint32_t)rows;
    dsc->decoded = &c->buf;
    return LV_RESULT_OK;
}

static void pic_close(lv_image_decoder_t *d, lv_image_decoder_dsc_t *dsc)
{
    (void)d;
    pic_ctx_t *c = dsc->user_data;
    if (!c) return;
    svc_notice_pic_close(&c->pic);
    lv_free(c->rows);
    lv_free(c);
    dsc->user_data = NULL;
    dsc->decoded = NULL;
}

void notice_overlay_init(void)
{
    static bool done;
    if (done) return;
    done = true;
    lv_image_decoder_t *d = lv_image_decoder_create();
    lv_image_decoder_set_info_cb(d, pic_info);
    lv_image_decoder_set_open_cb(d, pic_open);
    lv_image_decoder_set_get_area_cb(d, pic_area);
    lv_image_decoder_set_close_cb(d, pic_close);
    d->name = "notice";
}

static lv_obj_t *picture(lv_obj_t *parent, const notice_t *n)
{
    s_pic_id = n->id;
    memset(&s_pic_src, 0, sizeof s_pic_src);
    s_pic_src.header.magic = LV_IMAGE_HEADER_MAGIC;
    s_pic_src.header.cf = LV_COLOR_FORMAT_RGB565;
    s_pic_src.header.w = n->img_w;
    s_pic_src.header.h = n->img_h;
    s_pic_src.header.stride = (uint32_t)n->img_w * 2;
    /* Never read: the decoder knows this source by its address and reads the file instead. */
    s_pic_src.data = (const uint8_t *)&s_pic_src;
    s_pic_src.data_size = 1;
    lv_obj_t *img = lv_image_create(parent);
    lv_image_set_src(img, &s_pic_src);
    lv_obj_remove_flag(img, LV_OBJ_FLAG_CLICKABLE);
    return img;
}

/* ---- the card ---- */

static lv_obj_t *box(lv_obj_t *parent)
{
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_remove_style_all(o);
    lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    return o;
}

static lv_obj_t *text(lv_obj_t *parent, const char *t, int px, uint32_t color, uint8_t opa)
{
    lv_obj_t *l = ui_label(parent, t, px, color);
    if (opa != 255) lv_obj_set_style_text_opa(l, opa, 0);
    return l;
}

static void tapped(lv_event_t *e)
{
    (void)e;
    /* Hidden, unanswered: the sender reads `dismissed` (owner, 2026-10-13). */
    const bool question = s_ov.n_choices != 0;
    svc_notice_dismiss();
    if (question) ui_toast("Hidden — no answer sent", 2200);
}

static void answered(lv_event_t *e)
{
    const uint8_t choice = (uint8_t)(uintptr_t)lv_event_get_user_data(e);
    char t[64] = "Sent";
    notice_t n;
    if (svc_notice_head(&n) && n.id == s_ov.id && choice < n.n_choices)
        snprintf(t, sizeof t, "Sent to %s: %s", n.source, n.choices[choice]);
    svc_notice_answer(choice);
    ui_toast(t, 2200);
}

static void replay(lv_event_t *e)
{
    (void)e;
    if (!svc_notice_replay(s_ov.id)) ui_toast("The speaker is busy", 1800);
}

/* After the click is done with the button: the rebuild deletes it. */
static void rebuild(void *arg)
{
    (void)arg;
    notice_t n;
    if (!s_ov.root || !svc_notice_head(&n) || n.id != s_ov.id) return;
    notice_overlay_build(lv_obj_get_parent(s_ov.root), &n, s_ov.queued);
}

static void reveal(lv_event_t *e)
{
    (void)e;
    s_ov.revealed = s_ov.id;
    lv_async_call(rebuild, NULL);
}

/* Only when it changed: a label set is a redraw, and over a picture a redraw reads the file. */
static void set_text(lv_obj_t *l, const char *t)
{
    if (l && strcmp(lv_label_get_text(l), t)) lv_label_set_text(l, t);
}

static void look_now(const notice_t *n, notice_look_t *l)
{
    rtc_time_t t;
    svc_rtc_now(&t);
    const bool night = notice_night_now(svc_settings_get(SET_NIGHT_ON) != 0,
                                        (uint16_t)svc_settings_get(SET_NIGHT_FROM),
                                        (uint16_t)svc_settings_get(SET_NIGHT_TO), t.valid,
                                        (uint16_t)(t.h * 60 + t.m));
    uint32_t shown_for = 0;
    svc_notice_shown_for(s_ov.id, &shown_for);
    notice_look(n, s_ov.queued, night, (uint8_t)svc_settings_get(SET_NIGHT_COLOR),
                s_ov.revealed == s_ov.id, shown_for, l);
}

static void refresh(lv_timer_t *tm)
{
    (void)tm;
    notice_t n;
    if (!s_ov.root || !svc_notice_head(&n) || n.id != s_ov.id) return;
    notice_look_t l;
    look_now(&n, &l);
    set_text(s_ov.hint, l.hint);
    set_text(s_ov.chip_hint, l.chip_hint);
    if (s_ov.timer_fill) lv_obj_set_width(s_ov.timer_fill, (int32_t)UI_W * l.timer_permille / 1000);
    bool playing;
    uint32_t played;
    if (s_ov.time && svc_notice_sound(s_ov.id, &playing, &played)) {
        notice_sound_line_t sl;
        notice_sound_line(playing, played, s_ov.audio_ms, &sl);
        set_text(s_ov.time, sl.time);
        if (playing) {
            lv_obj_remove_flag(s_ov.bar, LV_OBJ_FLAG_HIDDEN);
            lv_obj_add_flag(s_ov.again, LV_OBJ_FLAG_HIDDEN);
            lv_obj_set_width(s_ov.bar_fill, lv_pct((int32_t)sl.permille / 10));
        } else {
            lv_obj_add_flag(s_ov.bar, LV_OBJ_FLAG_HIDDEN);
            lv_obj_remove_flag(s_ov.again, LV_OBJ_FLAG_HIDDEN);
        }
    }
}

static void speaker_row(lv_obj_t *card, const notice_look_t *l)
{
    lv_obj_t *row = box(card);
    lv_obj_set_size(row, LV_PCT(100), UI_BUTTON_H);
    lv_obj_set_style_bg_color(row, C(l->accent), 0);
    lv_obj_set_style_bg_opa(row, l->night ? 20 : 26, 0);
    lv_obj_set_style_radius(row, UI_R_BUTTON, 0);
    lv_obj_set_style_pad_hor(row, 16, 0);
    lv_obj_set_style_pad_column(row, 12, 0);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    /* Its own tap: it plays the sound again rather than hiding the notice. */
    lv_obj_add_flag(row, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(row, replay, LV_EVENT_CLICKED, NULL);
    text(row, LV_SYMBOL_VOLUME_MAX, 22, l->accent, l->ink_opa);
    s_ov.bar = box(row);
    lv_obj_set_height(s_ov.bar, 6);
    lv_obj_set_flex_grow(s_ov.bar, 1);
    lv_obj_set_style_bg_color(s_ov.bar, C(UI_C_TRACK), 0);
    lv_obj_set_style_bg_opa(s_ov.bar, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(s_ov.bar, 3, 0);
    s_ov.bar_fill = box(s_ov.bar);
    lv_obj_set_size(s_ov.bar_fill, 0, 6);
    lv_obj_set_style_bg_color(s_ov.bar_fill, C(l->accent), 0);
    lv_obj_set_style_bg_opa(s_ov.bar_fill, l->ink_opa, 0);
    lv_obj_set_style_radius(s_ov.bar_fill, 3, 0);
    s_ov.again = text(row, "Play again", 20, l->accent, l->ink_opa);
    lv_obj_set_flex_grow(s_ov.again, 1);
    s_ov.time = text(row, "", 18, l->night ? l->body : UI_C_TEXT_SOFT, l->ink_opa);
}

static void choices(lv_obj_t *card, const notice_t *n, const notice_look_t *l)
{
    lv_obj_t *row = box(card);
    lv_obj_set_size(row, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_set_style_pad_column(row, 10, 0);
    for (uint8_t i = 0; i < n->n_choices; i++) {
        lv_obj_t *b = ui_button(row, n->choices[i], i == 0 ? UI_BTN_PRIMARY : UI_BTN_SECONDARY, true,
                                answered, (void *)(uintptr_t)i);
        lv_obj_set_height(b, UI_CHOICE_H);
        lv_obj_set_width(b, LV_SIZE_CONTENT);
        lv_obj_set_flex_grow(b, 1);
        lv_obj_set_style_radius(b, UI_R_CHOICE, 0);
        if (l->night) {
            /* The night colour on black, a hairline round each: nothing bright at 3 a.m. */
            lv_obj_set_style_bg_color(b, C(l->accent), 0);
            lv_obj_set_style_bg_opa(b, i == 0 ? 31 : 0, 0);
            lv_obj_set_style_border_color(b, C(l->accent), 0);
            lv_obj_set_style_border_width(b, 1, 0);
            lv_obj_set_style_border_opa(b, l->ink_opa, 0);
            lv_obj_t *label = lv_obj_get_child(b, 0);
            lv_obj_set_style_text_color(label, C(l->accent), 0);
            lv_obj_set_style_text_opa(label, l->ink_opa, 0);
        }
    }
}

static void show_picture(lv_obj_t *card, const notice_look_t *l)
{
    lv_obj_t *b = box(card);
    lv_obj_set_size(b, LV_PCT(100), UI_CHOICE_H);
    lv_obj_set_style_radius(b, UI_R_CHOICE, 0);
    lv_obj_set_style_bg_color(b, C(l->accent), 0);
    lv_obj_set_style_bg_opa(b, 31, 0);
    lv_obj_set_style_border_color(b, C(l->accent), 0);
    lv_obj_set_style_border_width(b, 1, 0);
    lv_obj_set_style_border_opa(b, l->ink_opa, 0);
    lv_obj_set_flex_flow(b, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(b, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(b, 12, 0);
    lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(b, reveal, LV_EVENT_CLICKED, NULL);
    text(b, LV_SYMBOL_IMAGE, 22, l->accent, l->ink_opa);
    text(b, "Show picture", 22, l->accent, l->ink_opa);
}

static lv_obj_t *chip(lv_obj_t *parent, const char *t, bool bold)
{
    lv_obj_t *c = box(parent);
    lv_obj_set_size(c, LV_SIZE_CONTENT, 40);
    lv_obj_set_style_pad_hor(c, 14, 0);
    lv_obj_set_style_radius(c, 20, 0);
    lv_obj_set_style_bg_color(c, C(0x000000), 0);
    lv_obj_set_style_bg_opa(c, 140, 0);
    lv_obj_set_flex_flow(c, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(c, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(c, 8, 0);
    if (bold) text(c, LV_SYMBOL_BELL, 16, UI_C_TEXT, 255);
    lv_obj_t *l = text(c, t, 18, bold ? UI_C_TEXT : 0xFFFFFF, bold ? 255 : 217);
    if (!bold) s_ov.chip_hint = l;
    return c;
}

static void timer_line(const notice_look_t *l)
{
    if (!l->timer) return;
    lv_obj_t *track = box(s_ov.root);
    lv_obj_set_size(track, UI_W, 4);
    lv_obj_align(track, LV_ALIGN_BOTTOM_LEFT, 0, 0);
    lv_obj_add_flag(track, LV_OBJ_FLAG_IGNORE_LAYOUT);
    lv_obj_set_style_bg_color(track, C(0xFFFFFF), 0);
    lv_obj_set_style_bg_opa(track, 26, 0);
    s_ov.timer_fill = box(track);
    lv_obj_set_size(s_ov.timer_fill, (int32_t)UI_W * l->timer_permille / 1000, 4);
    lv_obj_set_style_bg_color(s_ov.timer_fill, C(l->timer_color), 0);
    lv_obj_set_style_bg_opa(s_ov.timer_fill, l->timer_opa, 0);
}

void notice_overlay_drop(void)
{
    if (s_ov.tick) lv_timer_delete(s_ov.tick);
    if (s_ov.root) lv_obj_delete(s_ov.root);
    const uint32_t revealed = s_ov.revealed;
    memset(&s_ov, 0, sizeof s_ov);
    s_ov.revealed = revealed;
    s_ov.kind = "none";
}

void notice_overlay_build(lv_obj_t *parent, const notice_t *n, uint8_t queued)
{
    notice_overlay_drop();
    s_ov.n_choices = n->n_choices;
    s_ov.audio_ms = n->audio_ms;
    s_ov.id = n->id;
    s_ov.queued = queued;
    notice_look_t l;
    look_now(n, &l);

    s_ov.root = box(parent);
    lv_obj_set_size(s_ov.root, UI_W, UI_H);
    lv_obj_set_pos(s_ov.root, 0, 0);
    lv_obj_set_style_bg_color(s_ov.root, C(UI_C_BG), 0);
    lv_obj_set_style_bg_opa(s_ov.root, LV_OPA_COVER, 0);
    /* A tap anywhere that is not a button hides it (owner, 2026-10-13). */
    lv_obj_add_flag(s_ov.root, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(s_ov.root, tapped, LV_EVENT_CLICKED, NULL);

    if (l.full_picture) {
        s_ov.kind = "picture";
        picture(s_ov.root, n);
        lv_obj_t *from = chip(s_ov.root, n->source, true);
        lv_obj_set_pos(from, 20, 20);
        lv_obj_t *hint = chip(s_ov.root, l.chip_hint, false);
        lv_obj_align(hint, LV_ALIGN_TOP_RIGHT, -20, 20);
    } else {
        s_ov.kind = l.ask_picture ? "night-picture" : l.banner ? "banner" : n->n_choices ? "question" : "plain";
        int32_t top = 0;
        if (l.banner) {
            lv_obj_t *img = picture(s_ov.root, n);
            lv_obj_set_pos(img, 0, 0);
            top = n->img_h;
        }
        lv_obj_t *card = box(s_ov.root);
        lv_obj_set_pos(card, 0, top);
        lv_obj_set_size(card, UI_W, UI_H - top);
        lv_obj_set_flex_flow(card, LV_FLEX_FLOW_COLUMN);
        lv_obj_set_style_pad_top(card, l.banner ? 14 : 40, 0);
        lv_obj_set_style_pad_hor(card, 32, 0);
        lv_obj_set_style_pad_bottom(card, l.banner ? 26 : 30, 0);
        lv_obj_set_style_pad_row(card, 14, 0);

        lv_obj_t *head = box(card);
        lv_obj_set_size(head, LV_PCT(100), LV_SIZE_CONTENT);
        lv_obj_set_flex_flow(head, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(head, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        lv_obj_set_style_pad_column(head, 10, 0);
        text(head, LV_SYMBOL_BELL, 18, l.accent, l.ink_opa);
        char from[56];
        snprintf(from, sizeof from, "%s%s", n->source, n->urgent ? " · urgent" : "");
        lv_obj_t *who = text(head, from, 18, l.accent, l.ink_opa);
        lv_obj_set_flex_grow(who, 1);
        if (l.count[0]) text(head, l.count, 18, l.muted, l.ink_opa);

        lv_obj_t *title = ui_label_wrap(card, n->title, l.banner ? 26 : 30, l.title);
        if (l.ink_opa != 255) lv_obj_set_style_text_opa(title, l.ink_opa, 0);
        lv_obj_t *body = ui_label_wrap(card, n->body, 22, l.body);
        if (l.ink_opa != 255) lv_obj_set_style_text_opa(body, l.ink_opa, 0);
        lv_obj_set_flex_grow(body, 1);
        if (n->audio_ms) speaker_row(card, &l);
        if (n->n_choices) choices(card, n, &l);
        if (l.ask_picture) show_picture(card, &l);
        s_ov.hint = text(card, l.hint, 18, l.muted, l.ink_opa);
        lv_obj_set_width(s_ov.hint, LV_PCT(100));
        lv_obj_set_style_text_align(s_ov.hint, LV_TEXT_ALIGN_CENTER, 0);
    }
    timer_line(&l);
    s_ov.tick = lv_timer_create(refresh, REFRESH_MS, NULL);
    refresh(NULL);
}

uint32_t notice_overlay_id(void)
{
    return s_ov.id;
}

uint8_t notice_overlay_count(void)
{
    return s_ov.queued;
}

const char *notice_overlay_kind(void)
{
    return s_ov.kind;
}
