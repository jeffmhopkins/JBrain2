/* The shared widgets: thin LVGL wrappers that put the mock's tokens on a widget once, so no
 * app styles anything itself. No app knowledge lives here — a widget takes text and a colour,
 * never a principal or an app id. Every builder creates under `parent` and owns nothing
 * outside it, so deleting an app's root frees all of it. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "lvgl.h"
#include "ui_theme.h"

/* LVGL ships Montserrat in even sizes up to 48, without · – — … →; each size carries those as a
 * generated fallback. A mock size maps to the nearest one. */
const lv_font_t *ui_font(int px);
lv_obj_t *ui_label(lv_obj_t *parent, const char *text, int px, uint32_t color);
lv_obj_t *ui_label_wrap(lv_obj_t *parent, const char *text, int px, uint32_t color);
/* The clock's digits: 124 and 92 px are generated bitmap fonts holding only 0-9, ':' and '-'
 * (components/ui_kit/fonts/); any other size is the nearest built-in. */
lv_obj_t *ui_label_big(lv_obj_t *parent, const char *text, int px, uint32_t color);
/* A bare container: no style, no scroll, sized to content. */
lv_obj_t *ui_box(lv_obj_t *parent, lv_flex_flow_t flow, int gap);

lv_obj_t *ui_icon(lv_obj_t *parent, const ui_icon_t *icon, ui_accent_t accent, int disc_px,
                  bool dim);
/* The mock's microphone, `px` square, drawn as strokes: LVGL's symbol font has no microphone,
 * and LV_SYMBOL_AUDIO, the nearest, is a music note. */
lv_obj_t *ui_mic(lv_obj_t *parent, int px, uint32_t color);
/* A conversation's footer: the microphone and "Hold KEY to talk", centred along the bottom. */
lv_obj_t *ui_talk_hint(lv_obj_t *root);

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
/* A switch row with a title at `px` and, when `sub` is not NULL, a muted line under it. */
lv_obj_t *ui_switch_row_sub(lv_obj_t *parent, const char *title, int px, const char *sub, bool on,
                            bool enabled, lv_event_cb_t cb, void *user);
lv_obj_t *ui_slider_row(lv_obj_t *parent, int min, int max, int value, lv_event_cb_t cb,
                        void *user);
/* A hairline across a card, between rows. */
lv_obj_t *ui_divider(lv_obj_t *parent);
/* One of a row of choices: its own colour for the text, a steel ring when picked. */
lv_obj_t *ui_choice(lv_obj_t *parent, const char *text, uint32_t color, bool picked, bool enabled,
                    lv_event_cb_t cb, void *user);
/* The clock face's corner badge: a bell and a count on a dark pill, in `color` at `opa`. */
lv_obj_t *ui_corner_badge(lv_obj_t *parent, const char *count, uint32_t color, uint8_t opa,
                          lv_event_cb_t cb, void *user);
lv_obj_t *ui_progress(lv_obj_t *parent, int pct, uint32_t color, int h);

typedef enum { UI_BTN_PRIMARY, UI_BTN_SECONDARY, UI_BTN_DESTRUCTIVE } ui_btn_kind_t;
lv_obj_t *ui_button(lv_obj_t *parent, const char *text, ui_btn_kind_t kind, bool enabled,
                    lv_event_cb_t cb, void *user);

lv_obj_t *ui_tile(lv_obj_t *parent, int x, int y, const char *title, const ui_icon_t *icon,
                  ui_accent_t accent, const ui_tile_state_t *st, lv_event_cb_t cb, void *user);
lv_obj_t *ui_tile_empty(lv_obj_t *parent, int x, int y);

/* A voice bubble's moving parts, for a view that changes it where it stands rather than
 * rebuilding its list (0.0.17: a rebuild lost the reader's scroll on every play). */
typedef struct {
    lv_obj_t *glyph; /* the play button's ▶ or ⏸ */
    lv_obj_t *bar;   /* the progress bar's fill */
    lv_obj_t *dot;   /* the amber "not heard yet" dot */
} ui_bubble_parts_t;

/* Its moving parts, changed where they stand while it plays (a rebuild would lose the scroll). */
typedef struct {
    lv_obj_t *button, *arc, *icon, *stop, *caption;
} ui_listen_parts_t;

typedef struct {
    bool mine;
    bool voice;
    bool pending;
    bool playing;
    bool unplayed;
    uint8_t listen;           /* ui_listen_t: the speaker button beside a reply */
    uint16_t listen_permille; /* PLAYING: the ring */
    const char *listen_caption; /* under the bubble: "Starting… · tap to stop", "0:14 · tap to stop" */
    ui_listen_parts_t *listen_parts;
    uint8_t prog;  /* playback 0-100 */
    const char *text;
    const char *dur;
    const char *status;
    uintptr_t tag; /* the control's user data: which message a press was on */
    /* Filled for one of theirs with play when set: then the dot is made clear rather than
       left out, so ui_bubble_set can show it again without the row changing width. */
    ui_bubble_parts_t *parts;
} ui_bubble_t;
/* `accent` colours their play button; LV_EVENT_CLICKED on Play or Listen reaches `cb` with
 * `user`, and the control carries `tag` (lv_obj_get_user_data of the event's target). */
lv_obj_t *ui_bubble(lv_obj_t *parent, const ui_bubble_t *b, ui_accent_t accent, lv_event_cb_t cb,
                    void *user);
/* The parts' new state: nothing moves but the glyph, the fill's width and the dot. */
void ui_bubble_set(const ui_bubble_parts_t *p, bool playing, uint8_t prog, bool unplayed);
/* Listen's button and caption, in place: STARTING turns a short arc, PLAYING fills the ring. */
void ui_listen_set(const ui_listen_parts_t *p, ui_listen_t state, uint16_t permille,
                   const char *caption, uint32_t accent);

/* Load earlier (0.0.21): the full-width row above a thread's messages ("↑ Load earlier · 44
 * more", or a spinner and "Loading earlier…"), the same below them ("↓ Newer messages · 20
 * more"), and "Start of conversation / Yesterday 18:20" at the true start. */
lv_obj_t *ui_hist_row(lv_obj_t *parent, bool up, bool loading, uint32_t more, lv_event_cb_t cb,
                      void *user);
lv_obj_t *ui_hist_start(lv_obj_t *parent, const char *when);

/* Full-screen black base for everything modal: the talk ring, the push pop-up, the alarm. It
 * takes every press inside it, so nothing under it can be reached while it is drawn. */
lv_obj_t *ui_overlay(lv_obj_t *parent);
/* A small menu over a dimmed screen: a card titled `title`, centred, that the caller fills with
 * buttons. A tap on the dim around it reaches `on_outside`; nothing under it can be reached.
 * Returns the card; its parent (the dim) is what to delete. */
lv_obj_t *ui_menu(lv_obj_t *parent, const char *title, lv_event_cb_t on_outside, void *user);

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

/* A long press's feedback (ui_hold): a thin ring centred where the finger is, filling clockwise
 * from the top, in `color`. ui_hold_ring_set takes ui_hold_permille's 0-1000. Not clickable: the
 * finger's stroke stays with the widget it started on. */
lv_obj_t *ui_hold_ring(lv_obj_t *parent, int x, int y, uint32_t color);
void ui_hold_ring_set(lv_obj_t *ring, uint16_t permille);

/* The working spinner: a fixed quarter arc in `color` turning steadily once a second on a faint
 * ring. Its angle comes from the tick, so a spinner made later is in step with the one it
 * replaces. ui_is_spinner says whether `o` is one (the sim's check). */
lv_obj_t *ui_spinner(lv_obj_t *parent, uint32_t color);
bool ui_is_spinner(lv_obj_t *o);
/* One line of a conversation's working list under the waiting question: an 18 px slot for a mark
 * (✓, ✗, or a spinner moved in) and dim words, so it reads as the box working, not a message. */
lv_obj_t *ui_step_row(lv_obj_t *parent, lv_obj_t **mark, lv_obj_t **text);
/* A dim line of its own in a conversation: an answer's "2 steps · 58 s". */
lv_obj_t *ui_note_line(lv_obj_t *parent, const char *text);

/* ---- 0.0.15: an agent's conversation menu, Recent, a switched thread (Main.dc.html) ---- */

/* The header with a second, muted line under the title (the conversation's title, cut with "…").
 * `*sub_label` is that line, for the caller to change in place. */
lv_obj_t *ui_header_sub(lv_obj_t *root, const ui_icon_t *icon, ui_accent_t accent,
                        const char *title, const char *sub, lv_event_cb_t on_back, void *user,
                        lv_obj_t **sub_label);
/* The mock's phone, `px` square, in strokes: where a conversation started in the app. */
lv_obj_t *ui_phone(lv_obj_t *parent, int px, uint32_t color);
/* A muted line under a ui_menu card's title: "Jerv · Shopping list". */
void ui_menu_sub(lv_obj_t *card, const char *text);
typedef enum { UI_ROW_GLYPH_NONE, UI_ROW_GLYPH_PLUS, UI_ROW_GLYPH_CLOCK } ui_row_glyph_t;
/* A 60 px action: tinted in `color` (New conversation) or raised with a strong line (Recent
 * conversations), a glyph before the words, a chevron after when it opens a page. */
lv_obj_t *ui_menu_row(lv_obj_t *parent, ui_row_glyph_t glyph, const char *text, uint32_t color,
                      bool tinted, bool chevron, lv_event_cb_t cb, void *user);
/* A button drawn only as its outline: a menu's Cancel. */
lv_obj_t *ui_ghost_button(lv_obj_t *parent, const char *text, lv_event_cb_t cb, void *user);
typedef struct {
    const char *title, *when, *snippet;
    bool from_app; /* the phone; else the microphone */
    bool current;  /* the accent bar, the tint and the "Current" chip, in place of the time */
    bool opening;  /* `when` ("Opening…") in the accent */
} ui_convo_row_t;
/* One of Recent's rows, 84 px; LV_EVENT_CLICKED reaches `cb` with `user`. */
lv_obj_t *ui_convo_row(lv_obj_t *parent, const ui_convo_row_t *r, ui_accent_t accent,
                       lv_event_cb_t cb, void *user);
/* A row still loading: grey bars where its words will be. */
lv_obj_t *ui_skeleton_row(lv_obj_t *parent, int w1_pct, int w2_pct, lv_opa_t opa);
/* An empty list: a speech bubble on a disc, a title and a line under it. */
lv_obj_t *ui_empty_state(lv_obj_t *parent, const char *title, const char *body);
/* A chip with the phone before its words: "Earlier messages are in the app". */
lv_obj_t *ui_app_chip(lv_obj_t *parent, const char *text);
/* Black fading in over the bottom `h` px of `parent`: there is more below. */
lv_obj_t *ui_fade_bottom(lv_obj_t *parent, int h);

/* One toast at a time, on the top layer, above every overlay. */
void ui_toast(const char *text, uint32_t ms);
void ui_toast_clear(void);
