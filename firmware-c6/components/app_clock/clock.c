#include <stdint.h>
#include <stdio.h>

#include "app_clock.h"
#include "clock_model.h"
#include "svc_alarms.h"
#include "svc_messages.h"
#include "svc_notice.h"
#include "svc_rtc.h"
#include "svc_settings.h"
#include "ui_kit.h"

typedef enum { VIEW_FACE, VIEW_ALARMS, VIEW_RINGING } view_t;

/* Everything the face draws from. The face is rebuilt only when this changes, so a second
 * passing costs a comparison, and a badge appears within a second of what it counts. */
typedef struct {
    bool valid, date, night;
    uint16_t minute;
    uint8_t mday, color;
    uint16_t badge;
    bool urgent;
} face_sig_t;

typedef struct {
    lv_obj_t *root, *view;
    view_t shown, want;
    lv_timer_t *tick;
    face_sig_t sig;
    uint32_t released_id; /* the notice whose badge was tapped open */
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

static clock_face_t face_now(const rtc_time_t *t)
{
    return clock_face(svc_settings_get(SET_NIGHT_ON), (uint16_t)svc_settings_get(SET_NIGHT_FROM),
                      (uint16_t)svc_settings_get(SET_NIGHT_TO),
                      (uint8_t)svc_settings_get(SET_NIGHT_COLOR), (uint16_t)(t->h * 60 + t->m),
                      false);
}

static face_sig_t face_sig(void)
{
    rtc_time_t t;
    svc_rtc_now(&t);
    clock_face_t f = face_now(&t);
    return (face_sig_t){
        .valid = t.valid,
        .date = svc_settings_get(SET_SHOW_DATE) != 0,
        .night = f.night,
        .minute = (uint16_t)(t.h * 60 + t.m),
        .mday = t.mday,
        .color = (uint8_t)svc_settings_get(SET_NIGHT_COLOR),
        .badge = clock_badge_count(svc_notice_count(), svc_messages_unplayed_total()),
        .urgent = svc_notice_urgent_waiting(),
    };
}

static void badge_tapped(lv_event_t *e)
{
    app_t *self = lv_event_get_user_data(e);
    clock_t_ *c = self->state;
    notice_t n;
    if (svc_notice_head(&n)) {
        /* The shell asks holds_notices on its next pass and opens it over the face. */
        c->released_id = n.id;
        return;
    }
    char who[16];
    if (svc_messages_first_unplayed(who)) self->host->open(who);
}

static void build_face(app_t *self, lv_obj_t *v)
{
    clock_t_ *c = self->state;
    rtc_time_t t;
    svc_rtc_now(&t);
    clock_face_t f = face_now(&t);
    c->sig = face_sig();
    lv_obj_add_flag(v, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(v, face_tapped, LV_EVENT_CLICKED, self);
    char hm[6];
    clock_fmt_face(&t, hm);
    lv_obj_t *l = ui_label_big(v, hm, 124, f.color);
    lv_obj_set_style_text_opa(l, f.opa, 0);
    lv_obj_center(l);
    char dd[24];
    clock_fmt_daydate(&t, dd, sizeof dd);
    if (c->sig.date && dd[0]) {
        lv_obj_t *d = ui_label(v, dd, 24, f.color);
        lv_obj_set_style_text_opa(d, f.date_opa, 0);
        lv_obj_set_style_text_letter_space(d, 1, 0);
        lv_obj_align(d, LV_ALIGN_BOTTOM_MID, 0, -40);
    }
    notice_t n = {0};
    const uint8_t queued = svc_notice_count();
    if (queued) svc_notice_head(&n);
    if (c->sig.badge && clock_holds_notices(true, queued, n.id, c->released_id)) {
        char count[8];
        snprintf(count, sizeof count, "%u", c->sig.badge > 99 ? 99u : (unsigned)c->sig.badge);
        lv_obj_t *b = ui_corner_badge(v, count, f.color, clock_badge_opa(c->sig.urgent),
                                      badge_tapped, self);
        lv_obj_align(b, LV_ALIGN_TOP_RIGHT, -30, 26);
    }
}

static void alarm_toggled(lv_event_t *e)
{
    lv_obj_t *sw = lv_event_get_current_target(e);
    svc_alarms_set_on((uint8_t)(uintptr_t)lv_obj_get_user_data(sw), lv_obj_has_state(sw, LV_STATE_CHECKED));
    later(lv_event_get_user_data(e), VIEW_ALARMS);
}

static void setting_toggled(lv_event_t *e)
{
    lv_obj_t *sw = lv_event_get_current_target(e);
    svc_settings_set((setting_t)(uintptr_t)lv_obj_get_user_data(sw), lv_obj_has_state(sw, LV_STATE_CHECKED));
    later(lv_event_get_user_data(e), VIEW_ALARMS);
}

static void night_picked(lv_event_t *e)
{
    svc_settings_set(SET_NIGHT_COLOR, (int32_t)(uintptr_t)lv_obj_get_user_data(lv_event_get_current_target(e)));
    later(lv_event_get_user_data(e), VIEW_ALARMS);
}

static void zone_tapped(lv_event_t *e)
{
    app_t *self = lv_event_get_user_data(e);
    const int32_t next = (svc_settings_get(SET_TZ) + 1) % TZ_ZONES;
    svc_settings_set(SET_TZ, next);
    char s[48];
    snprintf(s, sizeof s, "Time zone: %s", svc_rtc_tz_label(next));
    self->host->toast(s);
    later(self, VIEW_ALARMS);
}

static lv_obj_t *switch_for(lv_obj_t *sw, setting_t key)
{
    lv_obj_set_user_data(sw, (void *)(uintptr_t)key);
    return sw;
}

static void card_time(app_t *self, lv_obj_t *page)
{
    rtc_status_t st;
    svc_rtc_status(&st);
    char line[120];
    if (st.source == TIME_SRC_BOX && st.synced_s < 60)
        snprintf(line, sizeof line, "Set from the box just now, and kept by the panel's clock "
                                    "chip through power cuts.");
    else if (st.source == TIME_SRC_BOX)
        snprintf(line, sizeof line, "Set from the box %ld min ago, and kept by the panel's clock "
                                    "chip through power cuts.",
                 (long)(st.synced_s / 60));
    else if (st.source == TIME_SRC_RTC)
        snprintf(line, sizeof line, "From the panel's clock chip; the box corrects it once Wi-Fi "
                                    "is up.");
    else
        snprintf(line, sizeof line, "Waiting for the box: the clock chip held no time.");
    lv_obj_t *c = ui_card(page, "Time");
    ui_label_wrap(c, line, 16, UI_C_MUTED);
    char zone[48];
    snprintf(zone, sizeof zone, "Time zone: %s", svc_rtc_tz_label(svc_settings_get(SET_TZ)));
    ui_button(c, zone, UI_BTN_SECONDARY, true, zone_tapped, self);
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
    if (t.valid && clock_next_alarm(a, n, now, t.wday, &idx, &wait)) {
        char s[48];
        clock_fmt_next(&a[idx], wait, s, sizeof s);
        lv_obj_t *chip = ui_chip(page, s, UI_C_WARN, true);
        lv_obj_set_style_align(chip, LV_ALIGN_TOP_MID, 0);
    }
    lv_obj_t *list = ui_card(page, NULL);
    for (uint8_t i = 0; i < n; i++) {
        char hm[6], sub[48];
        clock_fmt_hm(a[i].h, a[i].m, hm);
        clock_fmt_days(a[i].days, a[i].label, sub, sizeof sub);
        if (i) ui_divider(list);
        lv_obj_t *sw = ui_switch_row_sub(list, hm, 30, sub, a[i].on, true, alarm_toggled, self);
        lv_obj_set_user_data(sw, (void *)(uintptr_t)a[i].id);
        if (!a[i].on) lv_obj_set_style_opa(lv_obj_get_child(lv_obj_get_parent(sw), 0), LV_OPA_50, 0);
    }

    lv_obj_t *night = ui_card(page, NULL);
    switch_for(ui_switch_row(night, "Day and date under the time", svc_settings_get(SET_SHOW_DATE) != 0,
                             true, setting_toggled, self),
               SET_SHOW_DATE);
    ui_divider(night);
    char window[32];
    clock_fmt_window((uint16_t)svc_settings_get(SET_NIGHT_FROM), (uint16_t)svc_settings_get(SET_NIGHT_TO),
                     window, sizeof window);
    bool on = svc_settings_get(SET_NIGHT_ON) != 0;
    switch_for(ui_switch_row_sub(night, "Night clock", 22, window, on, true, setting_toggled, self),
               SET_NIGHT_ON);
    lv_obj_t *colors = ui_box(night, LV_FLEX_FLOW_ROW, 8);
    lv_obj_set_width(colors, LV_PCT(100));
    for (uint8_t k = 0; k < 3; k++) {
        clock_face_t f = clock_face(true, 0, 0, k, 0, true);
        lv_obj_t *b = ui_choice(colors, NIGHT_NAMES[k], f.color, svc_settings_get(SET_NIGHT_COLOR) == k,
                                on, night_picked, self);
        lv_obj_set_user_data(b, (void *)(uintptr_t)k);
    }
    ui_label_wrap(night, "Dim digits on black from bedtime to morning. The hours become settable in "
                         "the PWA with the alarms.",
                  16, UI_C_MUTED);
    card_time(self, page);
    ui_label_wrap(page, "These alarms are samples and do not ring yet. Alarms are set in the PWA "
                        "(jpanel → Panels → Jeff's) from a later update, and ring from the panel's "
                        "own clock, even with Wi-Fi off.",
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
    ui_label_big(o, hm, 92, UI_C_TEXT);
    ui_label(o, a.label[0] ? a.label : "Alarm", 24, UI_C_WARN);
    lv_obj_t *row = ui_box(o, LV_FLEX_FLOW_ROW, 14);
    lv_obj_set_style_margin_top(row, 22, 0);
    lv_obj_t *s = ui_button(row, "Snooze 9 min", UI_BTN_PRIMARY, true, snooze_clicked, self);
    lv_obj_set_size(s, 196, UI_BIG_BUTTON_H);
    lv_obj_t *x = ui_button(row, "Stop", UI_BTN_SECONDARY, true, stop_clicked, self);
    lv_obj_set_size(x, 150, UI_BIG_BUTTON_H);
    ui_label(o, "KEY snoozes · BOOT stops", 16, UI_C_MUTED);
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

static bool sig_eq(const face_sig_t *a, const face_sig_t *b)
{
    return a->valid == b->valid && a->date == b->date && a->night == b->night &&
           a->minute == b->minute && a->mday == b->mday && a->color == b->color &&
           a->badge == b->badge && a->urgent == b->urgent;
}

static void tick(lv_timer_t *tm)
{
    app_t *self = lv_timer_get_user_data(tm);
    clock_t_ *c = self->state;
    if (c->shown != VIEW_FACE) return;
    face_sig_t now = face_sig();
    if (!sig_eq(&now, &c->sig)) show(self, VIEW_FACE);
}

static void clock_open(app_t *self, lv_obj_t *root)
{
    clock_t_ *c = self->state;
    *c = (clock_t_){.root = root};
    c->tick = lv_timer_create(tick, 1000, self);
    show(self, svc_alarms_ringing(NULL) ? VIEW_RINGING : VIEW_FACE);
}

static void clock_close(app_t *self)
{
    clock_t_ *c = self->state;
    lv_async_call_cancel(rebuild, self);
    if (c->tick) lv_timer_delete(c->tick);
    c->tick = NULL;
    c->view = NULL;
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
    default: return false;
    }
}

static bool holds_notices(app_t *self)
{
    clock_t_ *c = self->state;
    notice_t n = {0};
    const uint8_t queued = svc_notice_count();
    if (queued) svc_notice_head(&n);
    return clock_holds_notices(c->shown == VIEW_FACE, queued, n.id, c->released_id);
}

static void tile_state(app_t *self, ui_tile_state_t *out)
{
    (void)self;
    rtc_time_t t;
    svc_rtc_now(&t);
    clock_fmt_face(&t, out->live_label);
}

static void describe(app_t *self, dbg_json_t *w)
{
    static const char *const V[] = {"face", "alarms", "ringing"};
    clock_t_ *c = self->state;
    dbg_json_str(w, "view", V[c->shown]);
    dbg_json_int(w, "badge", c->sig.badge);
    dbg_json_bool(w, "holds_notices", holds_notices(self));
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
    .holds_notices = holds_notices,
    .state = &s_clock,
};
