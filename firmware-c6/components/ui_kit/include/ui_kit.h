/* The shared widgets: thin LVGL wrappers that put the mock's tokens on a widget once, so no
 * app styles anything itself. No app knowledge lives here — a widget takes text and a colour,
 * never a principal or an app id. Every builder creates under `parent` and owns nothing
 * outside it, so deleting an app's root frees all of it. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "lvgl.h"
#include "ui_theme.h"

/* LVGL ships Montserrat in even sizes up to 48. A mock size maps to the nearest one; the
 * clock's 92/124 px digits are 48 scaled (see ui_label_big) until C3 generates digit fonts. */
const lv_font_t *ui_font(int px);
lv_obj_t *ui_label(lv_obj_t *parent, const char *text, int px, uint32_t color);
lv_obj_t *ui_label_wrap(lv_obj_t *parent, const char *text, int px, uint32_t color);
lv_obj_t *ui_label_big(lv_obj_t *parent, const char *text, int px, uint32_t color);
/* A bare container: no style, no scroll, sized to content. */
lv_obj_t *ui_box(lv_obj_t *parent, lv_flex_flow_t flow, int gap);

lv_obj_t *ui_icon(lv_obj_t *parent, const ui_icon_t *icon, ui_accent_t accent, int disc_px,
                  bool dim);

/* Back + icon + title, 76 px, the top of every page but the clock face. */
lv_obj_t *ui_header(lv_obj_t *root, const ui_icon_t *icon, ui_accent_t accent, const char *title,
                    lv_event_cb_t on_back, void *user);

/* The one scroll page. A thin thumb on the right edge appears only when there is more than
 * the glass holds. `bottom_gap` keeps a fixed footer (a conversation's "Hold KEY to talk")
 * clear. Returns the content column to build into. */
lv_obj_t *ui_page(lv_obj_t *root, bool under_header, int bottom_gap);
/* A conversation pins to its newest message only when one arrives; callers decide when
 * (thread_model), this does the scrolling. */
void ui_page_pin_bottom(lv_obj_t *content);

lv_obj_t *ui_card(lv_obj_t *parent, const char *title);
lv_obj_t *ui_chip(lv_obj_t *parent, const char *text, uint32_t color, bool tinted);

/* Returns the switch; LV_EVENT_VALUE_CHANGED reaches `cb`. A disabled row is dimmed and its
 * switch is not clickable: the picture and the target are one decision. */
lv_obj_t *ui_switch_row(lv_obj_t *parent, const char *label, bool on, bool enabled,
                        lv_event_cb_t cb, void *user);
lv_obj_t *ui_slider_row(lv_obj_t *parent, int min, int max, int value, lv_event_cb_t cb,
                        void *user);
lv_obj_t *ui_progress(lv_obj_t *parent, int pct, uint32_t color, int h);

typedef enum { UI_BTN_PRIMARY, UI_BTN_SECONDARY, UI_BTN_DESTRUCTIVE } ui_btn_kind_t;
lv_obj_t *ui_button(lv_obj_t *parent, const char *text, ui_btn_kind_t kind, bool enabled,
                    lv_event_cb_t cb, void *user);

lv_obj_t *ui_tile(lv_obj_t *parent, int x, int y, const char *title, const ui_icon_t *icon,
                  ui_accent_t accent, const ui_tile_state_t *st, lv_event_cb_t cb, void *user);
lv_obj_t *ui_tile_empty(lv_obj_t *parent, int x, int y);

typedef struct {
    bool mine;
    bool voice;
    bool pending;
    bool playing;
    bool unplayed;
    bool listen;   /* a Listen button: replies are text-only */
    bool speaking; /* "Speaking…" under it */
    uint8_t prog;  /* playback 0-100 */
    const char *text;
    const char *dur;
    const char *status;
    uintptr_t tag; /* the control's user data: which message a press was on */
} ui_bubble_t;
/* `accent` colours their play button; LV_EVENT_CLICKED on Play or Listen reaches `cb` with
 * `user`, and the control carries `tag` (lv_obj_get_user_data of the event's target). */
lv_obj_t *ui_bubble(lv_obj_t *parent, const ui_bubble_t *b, ui_accent_t accent, lv_event_cb_t cb,
                    void *user);

/* Full-screen black base for everything modal: the talk ring, the push pop-up, the alarm. It
 * takes every press inside it, so nothing under it can be reached while it is drawn. */
lv_obj_t *ui_overlay(lv_obj_t *parent);

/* The hold-to-talk ring, shared by every app that talks. The app supplies the clock and the
 * level (from svc_audio) and what a tap and the cap mean; the ring owns its timer, and
 * ui_talk_close frees both, so closing an app mid-hold leaves nothing running. */
typedef struct {
    lv_obj_t *root, *ring, *time;
    lv_timer_t *timer;
    uint32_t cap_ms;
    uint32_t (*ms)(void);
    uint8_t (*level)(void);
    void (*on_tap)(void *user); /* a tap on the glass cancels */
    void (*on_cap)(void *user); /* the 1:00 cap sends */
    void *user;
} ui_talk_t;
void ui_talk_open(ui_talk_t *t, lv_obj_t *parent, uint32_t color, const char *title);
void ui_talk_close(ui_talk_t *t);
bool ui_talk_up(const ui_talk_t *t);
void ui_talk_refresh(ui_talk_t *t);

/* One toast at a time, on the top layer, above every overlay. */
void ui_toast(const char *text, uint32_t ms);
void ui_toast_clear(void);
