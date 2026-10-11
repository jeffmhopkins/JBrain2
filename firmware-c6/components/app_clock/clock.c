#include <stdint.h>
#include <stdio.h>

#include "app_clock.h"
#include "clock_model.h"
#include "svc_alarms.h"
#include "svc_rtc.h"
#include "svc_settings.h"
#include "ui_kit.h"

typedef enum { VIEW_FACE, VIEW_ALARMS, VIEW_RINGING } view_t;

typedef struct {
    lv_obj_t *root, *view;
    view_t shown, want;
} clock_t_;

static clock_t_ s_clock;
static const ui_icon_t ICON = {UI_GLYPH_CLOCK, 0};
static const char *const NIGHT_NAMES[] = {"Red", "Amber", "Grey"};

static void show(app_t *self, view_t v);

static uint16_t now_min(rtc_time_t *t)
{
    svc_rtc_now(t);
    return (uint16_t)(t->h * 60 + t->m);
}

static void rebuild(void *self)
{
    show(self, ((clock_t_ *)((app_t *)self)->state)->want);
}

/* From inside a widget's own event: the view about to be replaced holds that widget, so the
 * swap waits for the event to finish. */
static void later(app_t *self, view_t v)
{
    ((clock_t_ *)self->state)->want = v;
    lv_async_call(rebuild, self);
}

static void face_tapped(lv_event_t *e)
{
    later(lv_event_get_user_data(e), VIEW_ALARMS);
}

static void back(lv_event_t *e)
{
    later(lv_event_get_user_data(e), VIEW_FACE);
}

static void build_face(app_t *self, lv_obj_t *v)
{
    rtc_time_t t;
    uint16_t now = now_min(&t);
    clock_face_t f = clock_face(svc_settings_get(SET_NIGHT_ON), (uint16_t)svc_settings_get(SET_NIGHT_FROM),
                                (uint16_t)svc_settings_get(SET_NIGHT_TO),
                                (uint8_t)svc_settings_get(SET_NIGHT_COLOR), now, false);
    lv_obj_add_flag(v, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(v, face_tapped, LV_EVENT_CLICKED, self);
    char hm[6];
    clock_fmt_hm(t.h, t.m, hm);
    lv_obj_t *l = ui_label_big(v, hm, 124, f.color);
    lv_obj_set_style_text_opa(l, f.opa, 0);
    lv_obj_center(l);
}

static void alarm_toggled(lv_event_t *e)
{
    lv_obj_t *sw = lv_event_get_current_target(e);
    svc_alarms_set_on((uint8_t)(uintptr_t)lv_obj_get_user_data(sw), lv_obj_has_state(sw, LV_STATE_CHECKED));
    later(lv_event_get_user_data(e), VIEW_ALARMS);
}

static void night_toggled(lv_event_t *e)
{
    svc_settings_set(SET_NIGHT_ON, lv_obj_has_state(lv_event_get_current_target(e), LV_STATE_CHECKED));
    later(lv_event_get_user_data(e), VIEW_ALARMS);
}

static void night_picked(lv_event_t *e)
{
    svc_settings_set(SET_NIGHT_COLOR, (int32_t)(uintptr_t)lv_obj_get_user_data(lv_event_get_current_target(e)));
    later(lv_event_get_user_data(e), VIEW_ALARMS);
}

static void build_alarms(app_t *self, lv_obj_t *v)
{
    ui_header(v, &ICON, UI_ACCENT_NEUTRAL, "Alarms", back, self);
    lv_obj_t *page = ui_page(v, true, 0);
    rtc_time_t t;
    uint16_t now = now_min(&t);
    alarm_t a[ALARM_MAX];
    uint8_t n = 0;
    while (n < ALARM_MAX && svc_alarms_get(n, &a[n])) n++;
    uint8_t idx;
    uint32_t wait;
    if (clock_next_alarm(a, n, now, t.wday, &idx, &wait)) {
        char s[48];
        clock_fmt_next(&a[idx], wait, s, sizeof s);
        lv_obj_t *chip = ui_chip(page, s, UI_C_WARN, true);
        lv_obj_set_style_align(chip, LV_ALIGN_TOP_MID, 0);
    }
    lv_obj_t *list = ui_card(page, NULL);
    for (uint8_t i = 0; i < n; i++) {
        char hm[6], sub[48], label[64];
        clock_fmt_hm(a[i].h, a[i].m, hm);
        clock_fmt_days(a[i].days, a[i].label, sub, sizeof sub);
        snprintf(label, sizeof label, "%s\n%s", hm, sub);
        lv_obj_t *sw = ui_switch_row(list, label, a[i].on, true, alarm_toggled, self);
        lv_obj_set_user_data(sw, (void *)(uintptr_t)a[i].id);
    }
    lv_obj_t *night = ui_card(page, NULL);
    char window[64];
    int32_t from = svc_settings_get(SET_NIGHT_FROM), to = svc_settings_get(SET_NIGHT_TO);
    snprintf(window, sizeof window, "Night clock\n%02ld:%02ld - %02ld:%02ld", (long)from / 60,
             (long)from % 60, (long)to / 60, (long)to % 60);
    bool on = svc_settings_get(SET_NIGHT_ON) != 0;
    ui_switch_row(night, window, on, true, night_toggled, self);
    lv_obj_t *colors = ui_box(night, LV_FLEX_FLOW_ROW, 8);
    lv_obj_set_width(colors, LV_PCT(100));
    for (uint8_t c = 0; c < 3; c++) {
        clock_face_t f = clock_face(true, 0, 0, c, 0, true);
        bool picked = svc_settings_get(SET_NIGHT_COLOR) == c;
        lv_obj_t *b = ui_button(colors, NIGHT_NAMES[c], UI_BTN_SECONDARY, true, night_picked, self);
        lv_obj_set_user_data(b, (void *)(uintptr_t)c);
        lv_obj_set_width(b, LV_SIZE_CONTENT);
        lv_obj_set_flex_grow(b, 1);
        lv_obj_set_style_border_color(b, lv_color_hex(picked ? UI_C_STEEL : UI_C_LINE), 0);
        lv_obj_set_style_border_width(b, 2, 0);
        lv_obj_set_style_text_color(lv_obj_get_child(b, 0), lv_color_hex(f.color), 0);
        if (!on) lv_obj_set_style_opa(b, LV_OPA_50, 0);
    }
    ui_label_wrap(night, "Dim digits on black from bedtime to morning. Hours are set in the PWA.", 16,
                  UI_C_MUTED);
    ui_label_wrap(page, "Add or change alarms in the PWA: jpanel > Panels > Jeff's. Alarms ring from "
                        "the panel's own clock, even with Wi-Fi off.",
                  16, UI_C_MUTED);
}

static void snooze(app_t *self)
{
    uint16_t at = svc_alarms_snooze();
    char s[32];
    snprintf(s, sizeof s, "Snoozed until %02u:%02u", at / 60, at % 60);
    self->host->toast(s);
}

static void stop(app_t *self)
{
    svc_alarms_stop();
    self->host->toast("Alarm stopped");
}

static void snooze_clicked(lv_event_t *e)
{
    snooze(lv_event_get_user_data(e));
    later(lv_event_get_user_data(e), VIEW_FACE);
}

static void stop_clicked(lv_event_t *e)
{
    stop(lv_event_get_user_data(e));
    later(lv_event_get_user_data(e), VIEW_FACE);
}

static void build_ringing(app_t *self, lv_obj_t *v)
{
    alarm_t a;
    if (!svc_alarms_ringing(&a)) return;
    lv_obj_t *o = ui_overlay(v);
    lv_obj_set_style_pad_row(o, 10, 0);
    ui_label(o, LV_SYMBOL_BELL, 48, UI_C_WARN);
    char hm[6];
    clock_fmt_hm(a.h, a.m, hm);
    lv_obj_t *t = ui_label_big(o, hm, 92, UI_C_TEXT);
    lv_obj_set_style_margin_ver(t, 26, 0);
    ui_label(o, a.label[0] ? a.label : "Alarm", 24, UI_C_WARN);
    lv_obj_t *row = ui_box(o, LV_FLEX_FLOW_ROW, 14);
    lv_obj_set_style_margin_top(row, 22, 0);
    lv_obj_t *s = ui_button(row, "Snooze 9 min", UI_BTN_PRIMARY, true, snooze_clicked, self);
    lv_obj_set_size(s, 196, UI_BIG_BUTTON_H);
    lv_obj_t *x = ui_button(row, "Stop", UI_BTN_SECONDARY, true, stop_clicked, self);
    lv_obj_set_size(x, 150, UI_BIG_BUTTON_H);
    ui_label(o, "KEY snoozes - BOOT stops", 16, UI_C_MUTED);
}

static void show(app_t *self, view_t v)
{
    clock_t_ *c = self->state;
    if (c->view) lv_obj_delete(c->view);
    c->view = lv_obj_create(c->root);
    lv_obj_remove_style_all(c->view);
    lv_obj_set_size(c->view, UI_W, UI_H);
    lv_obj_remove_flag(c->view, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    c->shown = c->want = v;
    if (v == VIEW_FACE) build_face(self, c->view);
    if (v == VIEW_ALARMS) build_alarms(self, c->view);
    if (v == VIEW_RINGING) build_ringing(self, c->view);
}

static void clock_open(app_t *self, lv_obj_t *root)
{
    clock_t_ *c = self->state;
    *c = (clock_t_){.root = root};
    show(self, svc_alarms_ringing(NULL) ? VIEW_RINGING : VIEW_FACE);
}

static void clock_close(app_t *self)
{
    lv_async_call_cancel(rebuild, self);
    ((clock_t_ *)self->state)->view = NULL;
}

static bool on_event(app_t *self, const app_event_t *ev)
{
    clock_t_ *c = self->state;
    bool ringing = c->shown == VIEW_RINGING;
    switch (ev->type) {
    case APP_EV_ALARM_DUE: show(self, VIEW_RINGING); return true;
    case APP_EV_KEY_DOWN:
        if (!ringing) return false;
        snooze(self);
        show(self, VIEW_FACE);
        return true;
    case APP_EV_BOOT:
        if (ringing)
            stop(self);
        else if (c->shown != VIEW_ALARMS)
            return false;
        show(self, VIEW_FACE);
        return true;
    case APP_EV_TICK_MINUTE:
        if (c->shown == VIEW_FACE) show(self, VIEW_FACE);
        return false;
    default: return false;
    }
}

static void tile_state(app_t *self, ui_tile_state_t *out)
{
    (void)self;
    rtc_time_t t;
    svc_rtc_now(&t);
    clock_fmt_hm(t.h, t.m, out->live_label);
}

static void describe(app_t *self, dbg_json_t *w)
{
    static const char *const V[] = {"face", "alarms", "ringing"};
    dbg_json_str(w, "view", V[((clock_t_ *)self->state)->shown]);
}

app_t app_clock = {
    .id = "clock",
    .title = "Clock",
    .icon = &ICON,
    .accent = UI_ACCENT_NEUTRAL,
    .page = 1,
    .slot = 0,
    .open = clock_open,
    .close = clock_close,
    .on_event = on_event,
    .tile_state = tile_state,
    .describe = describe,
    .state = &s_clock,
};
