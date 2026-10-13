#include <stdint.h>
#include <stdio.h>

#include "app_settings.h"
#include "settings_model.h"
#include "svc_audio.h"
#include "svc_box.h"
#include "svc_calib.h"
#include "svc_display.h"
#include "svc_net.h"
#include "svc_orient.h"
#include "svc_power.h"
#include "svc_rtc.h"
#include "svc_settings.h"
#include "ui_kit.h"

/* What the live cards show. The page is rebuilt when this changes — a download's percentage, the
 * radio, the battery — so the cards report the panel as it is, not as it was when opened. */
typedef struct {
    net_state_t net;
    uint8_t bars;
    bool armed;
    fw_phase_t phase;
    uint8_t pct;
    bool fitted, charging, usb;
    uint8_t bat_pct;
    uint16_t mv10, usb_mv10;
} live_t;

/* The calibration screen over the page while it runs (0.0.18, mock Calib.dc.html). `shown` is the
 * cross drawn, which stays on a finished one (green) for CAL_NEXT_MS before the next appears. */
typedef struct {
    lv_obj_t *root, *title, *line, *cross, *dots_row, *dots[CAL_TAPS];
    lv_obj_t *bar_h, *bar_v, *ring;
    lv_timer_t *next;
    calib_flow_t flow;
    calib_step_t last;
    uint8_t shown;
} calib_view_t;

typedef struct {
    lv_obj_t *root, *page;
    lv_obj_t *bright_label, *volume_label;
    lv_timer_t *tick;
    live_t live;
    bool wifi_armed;
    uint32_t wifi_armed_ms;
    calib_view_t cal;
} settings_t;

static settings_t s_set;
static const ui_icon_t ICON = {UI_GLYPH_GEAR, 0};
static const setting_t NOTIF_KEYS[] = {SET_NOTIF_ACCEPT, SET_NOTIF_QUIET, SET_NOTIF_CHIME};
static const char *const NOTIF_LABELS[] = {"Accept messages from the box", "Refuse during night hours",
                                           "Chime when one arrives"};

static void build(app_t *self);

static bool armed_now(const settings_t *s, net_state_t net)
{
    return settings_wifi_armed(net, s->wifi_armed, s->wifi_armed_ms, svc_rtc_uptime_ms());
}

static live_t live_now(const settings_t *s)
{
    net_info_t n;
    svc_net_info(&n);
    settings_wifi_t w;
    const bool armed = armed_now(s, n.state);
    settings_wifi(&n, armed, &w);
    fw_info_t fw;
    svc_box_fw(&fw);
    power_info_t p;
    svc_power_info(&p);
    return (live_t){.net = n.state, .bars = w.bars, .armed = armed, .phase = fw.phase,
                    .pct = fw.pct, .fitted = p.fitted, .charging = p.charging, .usb = p.usb,
                    .bat_pct = p.pct, .mv10 = (uint16_t)(p.mv / 10),
                    .usb_mv10 = (uint16_t)(p.usb_mv / 10)};
}

static bool live_eq(const live_t *a, const live_t *b)
{
    return a->net == b->net && a->bars == b->bars && a->armed == b->armed && a->phase == b->phase &&
           a->pct == b->pct && a->fitted == b->fitted && a->charging == b->charging &&
           a->usb == b->usb && a->bat_pct == b->bat_pct && a->mv10 == b->mv10 &&
           a->usb_mv10 == b->usb_mv10;
}

/* A switch changes what other cards say, so the page is rebuilt — after the event, because the
 * switch being pressed is part of it — and put back where it was scrolled to. */
static void rebuild(void *self)
{
    settings_t *s = ((app_t *)self)->state;
    /* Never under the calibration screen, which is a child of the page's root: its end asks
       again. */
    if (s->cal.root) return;
    int32_t y = s->page ? lv_obj_get_scroll_y(s->page) : 0;
    build(self);
    lv_obj_update_layout(s->page);
    lv_obj_scroll_to_y(s->page, y, LV_ANIM_OFF);
}

/* Never under a finger: a rebuild mid-drag would drop the slider or the scroll it holds. */
static void tick(lv_timer_t *tm)
{
    app_t *self = lv_timer_get_user_data(tm);
    settings_t *s = self->state;
    lv_indev_t *in = lv_indev_get_next(NULL);
    if (in && lv_indev_get_state(in) == LV_INDEV_STATE_PRESSED) return;
    if (s->cal.root) return; /* nothing under the calibration screen moves */
    live_t now = live_now(s);
    if (!live_eq(&now, &s->live)) rebuild(self);
}

static void back(lv_event_t *e)
{
    app_t *self = lv_event_get_user_data(e);
    self->host->go_home();
}

static void toggled(lv_event_t *e)
{
    lv_obj_t *sw = lv_event_get_current_target(e);
    svc_settings_set((setting_t)(uintptr_t)lv_obj_get_user_data(sw), lv_obj_has_state(sw, LV_STATE_CHECKED));
    lv_async_call(rebuild, lv_event_get_user_data(e));
}

static void wifi_pressed(lv_event_t *e)
{
    app_t *self = lv_event_get_user_data(e);
    settings_t *s = self->state;
    net_info_t n;
    svc_net_info(&n);
    const uint32_t now = svc_rtc_uptime_ms();
    switch (settings_wifi_tap(n.state, s->wifi_armed, s->wifi_armed_ms, now)) {
    case WIFI_TAP_ARM:
        s->wifi_armed = true;
        s->wifi_armed_ms = now;
        break;
    case WIFI_TAP_DISCONNECT:
        s->wifi_armed = false;
        svc_net_disconnect();
        break;
    case WIFI_TAP_CONNECT: svc_net_connect(); break;
    default: break;
    }
    lv_async_call(rebuild, self);
}

static void update_pressed(lv_event_t *e)
{
    app_t *self = lv_event_get_user_data(e);
    net_info_t n;
    svc_net_info(&n);
    if (!svc_box_update_now() && n.state != NET_CONNECTED) self->host->toast("Connect Wi-Fi first");
    lv_async_call(rebuild, self);
}

/* ---- touch calibration (0.0.18) ---- */

static void cal_paint(settings_t *s)
{
    calib_view_t *v = &s->cal;
    int16_t x, y;
    calib_target(v->shown, &x, &y);
    const bool green = v->last == CALF_NEXT || v->last == CALF_DONE;
    const uint32_t c = green ? UI_C_OK : UI_C_WARN;
    lv_obj_set_pos(v->cross, x - 24, y - 24);
    lv_obj_set_style_bg_color(v->bar_h, lv_color_hex(c), 0);
    lv_obj_set_style_bg_color(v->bar_v, lv_color_hex(c), 0);
    lv_obj_set_style_border_color(v->ring, lv_color_hex(c), 0);
    const uint8_t taps = green ? CAL_TAPS : v->flow.taps;
    for (int i = 0; i < CAL_TAPS; i++)
        lv_obj_set_style_bg_color(v->dots[i], lv_color_hex(i < taps ? UI_C_WARN : UI_C_LINE_STRONG), 0);
    /* The dots under the cross, or over it near the bottom of the glass (the mock's `dotsTop`). */
    lv_obj_set_pos(v->dots_row, x - 18, y > 400 ? y - 48 : y + 32);
    lv_label_set_text(v->title, calib_flow_title(v->last));
    const bool wrong = v->last == CALF_MISSED || v->last == CALF_DISAGREED;
    lv_obj_set_style_text_color(v->title, lv_color_hex(wrong ? UI_C_WARN : UI_C_TEXT), 0);
    char line[48];
    calib_flow_line(v->shown, line, sizeof line);
    lv_label_set_text(v->line, line);
}

static void cal_close(settings_t *s)
{
    calib_view_t *v = &s->cal;
    if (v->next) lv_timer_delete(v->next);
    v->next = NULL;
    if (v->root) lv_obj_delete(v->root);
    v->root = NULL;
}

static void cal_next(lv_timer_t *tm)
{
    app_t *self = lv_timer_get_user_data(tm);
    settings_t *s = self->state;
    lv_timer_delete(tm);
    s->cal.next = NULL;
    if (s->cal.last == CALF_DONE) {
        const calib_result_t r = svc_calib_finish(s->cal.flow.done, CAL_TARGETS);
        cal_close(s);
        self->host->toast(settings_calib_result(r));
        lv_async_call(rebuild, self);
        return;
    }
    s->cal.last = CALF_COUNTED;
    s->cal.shown = s->cal.flow.target;
    cal_paint(s);
}

static void cal_pressed(lv_event_t *e)
{
    app_t *self = lv_event_get_user_data(e);
    settings_t *s = self->state;
    /* The green cross is showing: this tap belongs to no cross. */
    if (s->cal.next) return;
    lv_point_t p;
    lv_indev_get_point(lv_indev_active(), &p);
    s->cal.last = calib_flow_tap(&s->cal.flow, (int16_t)p.x, (int16_t)p.y);
    cal_paint(s);
    if (s->cal.last == CALF_NEXT || s->cal.last == CALF_DONE)
        s->cal.next = lv_timer_create(cal_next, CAL_NEXT_MS, self);
}

static lv_obj_t *bar(lv_obj_t *parent, int x, int y, int w, int h)
{
    lv_obj_t *b = lv_obj_create(parent);
    lv_obj_remove_style_all(b);
    lv_obj_remove_flag(b, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_pos(b, x, y);
    lv_obj_set_size(b, w, h);
    lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
    return b;
}

static void cal_open(app_t *self)
{
    settings_t *s = self->state;
    calib_view_t *v = &s->cal;
    cal_close(s);
    calib_flow_start(&v->flow);
    v->last = CALF_COUNTED;
    v->shown = 0;
    v->root = lv_obj_create(s->root);
    lv_obj_remove_style_all(v->root);
    lv_obj_set_size(v->root, UI_W, UI_H);
    lv_obj_set_pos(v->root, 0, 0);
    lv_obj_set_style_bg_color(v->root, lv_color_hex(UI_C_BG), 0);
    lv_obj_set_style_bg_opa(v->root, LV_OPA_COVER, 0);
    lv_obj_add_flag(v->root, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_remove_flag(v->root, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_event_cb(v->root, cal_pressed, LV_EVENT_PRESSED, self);
    /* The mock's words 214 px down, the middle of the glass, clear of every corner's cross. */
    lv_obj_t *words = ui_box(v->root, LV_FLEX_FLOW_COLUMN, 6);
    lv_obj_set_width(words, UI_W);
    lv_obj_set_pos(words, 0, 214);
    lv_obj_set_flex_align(words, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_remove_flag(words, LV_OBJ_FLAG_CLICKABLE);
    v->title = ui_label(words, "Tap the cross", 22, UI_C_TEXT);
    v->line = ui_label(words, "", 18, UI_C_MUTED);
    /* The cross: two 3 px bars 48 long and a 28 px ring, centred on the target. */
    v->cross = lv_obj_create(v->root);
    lv_obj_remove_style_all(v->cross);
    lv_obj_remove_flag(v->cross, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(v->cross, 48, 48);
    v->bar_h = bar(v->cross, 0, 22, 48, 3);
    v->bar_v = bar(v->cross, 22, 0, 3, 48);
    v->ring = lv_obj_create(v->cross);
    lv_obj_remove_style_all(v->ring);
    lv_obj_remove_flag(v->ring, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_pos(v->ring, 10, 10);
    lv_obj_set_size(v->ring, 28, 28);
    lv_obj_set_style_radius(v->ring, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_border_width(v->ring, 3, 0);
    lv_obj_set_style_border_opa(v->ring, LV_OPA_COVER, 0);
    v->dots_row = lv_obj_create(v->root);
    lv_obj_remove_style_all(v->dots_row);
    lv_obj_remove_flag(v->dots_row, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_size(v->dots_row, 36, 8);
    for (int i = 0; i < CAL_TAPS; i++) {
        v->dots[i] = bar(v->dots_row, i * 14, 0, 8, 8);
        lv_obj_set_style_radius(v->dots[i], 4, 0);
    }
    svc_calib_begin();
    cal_paint(s);
}

/* BOOT, or the app closing: the old calibration (or none) stays in force. */
static bool cal_cancel(settings_t *s)
{
    if (!s->cal.root) return false;
    cal_close(s);
    svc_calib_cancel();
    return true;
}

static void calibrate_pressed(lv_event_t *e)
{
    cal_open(lv_event_get_user_data(e));
}

static void reset_pressed(lv_event_t *e)
{
    app_t *self = lv_event_get_user_data(e);
    svc_calib_reset();
    self->host->toast("Calibration reset");
    lv_async_call(rebuild, self);
}

static void bright_changed(lv_event_t *e)
{
    app_t *self = lv_event_get_user_data(e);
    settings_t *s = self->state;
    int32_t v = settings_brightness_from_slider(lv_slider_get_value(lv_event_get_current_target(e)));
    /* Every tick reaches the glass at once (svc_display records, the UI task sends between
       frames); flash is written only once the slider has been left alone. */
    svc_settings_set(SET_BRIGHTNESS, v);
    svc_display_set_brightness((uint8_t)svc_settings_get(SET_BRIGHTNESS));
    lv_label_set_text_fmt(s->bright_label, "%ld%%", (long)svc_settings_get(SET_BRIGHTNESS));
}

/* Heard at the next reply: the speaker is only open while one plays. */
static void volume_changed(lv_event_t *e)
{
    app_t *self = lv_event_get_user_data(e);
    settings_t *s = self->state;
    svc_settings_set(SET_VOLUME,
                     settings_volume_from_slider(lv_slider_get_value(lv_event_get_current_target(e))));
    lv_label_set_text_fmt(s->volume_label, "%ld%%", (long)svc_settings_get(SET_VOLUME));
}

static lv_obj_t *title_row(lv_obj_t *card, const char *title)
{
    lv_obj_t *r = ui_box(card, LV_FLEX_FLOW_ROW, 12);
    lv_obj_set_width(r, LV_PCT(100));
    lv_obj_set_flex_align(r, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    ui_label(r, title, 22, UI_C_TEXT);
    return r;
}

static void card_brightness(app_t *self, lv_obj_t *page)
{
    settings_t *s = self->state;
    lv_obj_t *c = ui_card(page, NULL);
    lv_obj_t *r = title_row(c, "Brightness");
    char pct[16];
    snprintf(pct, sizeof pct, "%ld%%", (long)svc_settings_get(SET_BRIGHTNESS));
    s->bright_label = ui_label(r, pct, 20, UI_C_TEXT);
    ui_slider_row(c, BRIGHTNESS_FLOOR, BRIGHTNESS_MAX, svc_settings_get(SET_BRIGHTNESS), bright_changed, self);
    ui_label_wrap(c, "10% is the floor, so the screen can never be set to look switched off.", 16,
                  UI_C_MUTED);
    ui_divider(c);
    const bool rot = svc_settings_get(SET_AUTO_ROTATE) != 0, can = svc_orient_available();
    lv_obj_t *sw = ui_switch_row_sub(c, "Auto-rotate", 20, settings_rotate_note(rot, can),
                                     rot && can, can, toggled, self);
    lv_obj_set_user_data(sw, (void *)(uintptr_t)SET_AUTO_ROTATE);
}

static void card_power(lv_obj_t *page)
{
    power_info_t p;
    svc_power_info(&p);
    settings_power_t v;
    settings_power(&p, &v);
    lv_obj_t *c = ui_card(page, NULL);
    lv_obj_t *r = title_row(c, v.title);
    ui_label(r, v.big, 22, v.low ? UI_C_WARN : v.bar ? UI_C_OK : UI_C_TEXT);
    if (v.bar) ui_progress(c, p.pct, v.low ? UI_C_WARN : UI_C_OK, 10);
    ui_label(c, v.line, 18, UI_C_MUTED);
}

static void card_notifications(app_t *self, lv_obj_t *page)
{
    lv_obj_t *c = ui_card(page, "Notifications");
    bool accept = svc_settings_get(SET_NOTIF_ACCEPT) != 0;
    for (int i = 0; i < 3; i++) {
        lv_obj_t *sw = ui_switch_row(c, NOTIF_LABELS[i], svc_settings_get(NOTIF_KEYS[i]) != 0,
                                     settings_notif_row_live(i, accept), toggled, self);
        lv_obj_set_user_data(sw, (void *)(uintptr_t)NOTIF_KEYS[i]);
    }
    char note[192];
    settings_notif_note(accept, svc_settings_get(SET_NOTIF_QUIET), (uint16_t)svc_settings_get(SET_NIGHT_FROM),
                        (uint16_t)svc_settings_get(SET_NIGHT_TO), note, sizeof note);
    ui_label_wrap(c, note, 16, UI_C_MUTED);
}

static void card_jerv(app_t *self, lv_obj_t *page)
{
    settings_t *s = self->state;
    lv_obj_t *c = ui_card(page, "Jerv");
    bool tts = svc_settings_get(SET_TTS) != 0;
    bool speaker;
    const char *note = settings_jerv_note(tts, svc_audio_state(), &speaker);
    lv_obj_t *sw = ui_switch_row(c, "Read replies aloud", tts, speaker, toggled, self);
    lv_obj_set_user_data(sw, (void *)(uintptr_t)SET_TTS);
    ui_label_wrap(c, note, 16, speaker ? UI_C_MUTED : UI_C_WARN);
    if (!speaker || !tts) return;
    ui_divider(c);
    lv_obj_t *r = title_row(c, "Speaker volume");
    char pct[16];
    snprintf(pct, sizeof pct, "%ld%%", (long)svc_settings_get(SET_VOLUME));
    s->volume_label = ui_label(r, pct, 20, UI_C_TEXT);
    ui_slider_row(c, VOLUME_FLOOR, VOLUME_MAX, svc_settings_get(SET_VOLUME), volume_changed, self);
}

static void card_firmware(app_t *self, lv_obj_t *page)
{
    fw_info_t fw;
    svc_box_fw(&fw);
    net_info_t n;
    svc_net_info(&n);
    char note[96];
    settings_fw_t v;
    settings_fw(&fw, n.state == NET_CONNECTED, &v, note, sizeof note);
    lv_obj_t *c = ui_card(page, NULL);
    lv_obj_t *r = title_row(c, "Firmware");
    ui_label(r, fw.version, 18, UI_C_MUTED);
    ui_label_wrap(c, v.note, 18, v.done ? UI_C_OK : UI_C_MUTED);
    if (v.show_bar) ui_progress(c, fw.pct, UI_C_STEEL, 8);
    ui_button(c, v.button, UI_BTN_PRIMARY, v.button_live, update_pressed, self);
}

static void card_touch(app_t *self, lv_obj_t *page)
{
    lv_obj_t *c = ui_card(page, NULL);
    lv_obj_t *r = title_row(c, "Touch screen");
    rtc_time_t t;
    svc_rtc_now(&t);
    char when[16];
    settings_calib_when(svc_calib_date(), t.valid ? t.year * 10000u + t.mon * 100u + t.mday : 0,
                        when, sizeof when);
    const bool custom = svc_calib_custom();
    ui_label(r, custom ? when : "Never", 18, UI_C_MUTED);
    ui_label_wrap(c, "Taps landing off-target? Tap each cross until it turns green.", 18, UI_C_MUTED);
    ui_button(c, "Calibrate", UI_BTN_PRIMARY, true, calibrate_pressed, self);
    if (custom) ui_button(c, "Reset calibration", UI_BTN_SECONDARY, true, reset_pressed, self);
}

static void card_wifi(app_t *self, lv_obj_t *page)
{
    settings_t *s = self->state;
    net_info_t n;
    svc_net_info(&n);
    settings_wifi_t v;
    settings_wifi(&n, armed_now(s, n.state), &v);
    lv_obj_t *c = ui_card(page, NULL);
    lv_obj_t *r = title_row(c, "Wi-Fi");
    uint32_t chip = n.state == NET_CONNECTED ? UI_C_OK : n.state == NET_CONNECTING ? UI_C_WARN : UI_C_MUTED;
    ui_chip(r, v.state, chip, n.state != NET_OFF);
    lv_obj_t *net = ui_box(c, LV_FLEX_FLOW_ROW, 12);
    lv_obj_set_width(net, LV_PCT(100));
    lv_obj_t *bars = ui_box(net, LV_FLEX_FLOW_ROW, 3);
    lv_obj_set_flex_align(bars, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_END);
    for (int i = 0; i < 4; i++) {
        lv_obj_t *b = lv_obj_create(bars);
        lv_obj_remove_style_all(b);
        lv_obj_remove_flag(b, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_size(b, 5, 6 + 4 * i);
        lv_obj_set_style_radius(b, 1, 0);
        lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
        lv_obj_set_style_bg_color(b, lv_color_hex(i < v.bars ? UI_C_TEXT : UI_C_LINE_STRONG), 0);
    }
    lv_obj_t *ssid = ui_label(net, n.ssid[0] ? n.ssid : "No network saved", 20, UI_C_TEXT);
    lv_label_set_long_mode(ssid, LV_LABEL_LONG_DOT);
    lv_obj_set_width(ssid, LV_SIZE_CONTENT);
    lv_obj_set_flex_grow(ssid, 1);
    if (n.state == NET_CONNECTED) {
        char rssi[16];
        snprintf(rssi, sizeof rssi, "%s%d dBm", n.rssi < 0 ? "−" : "", n.rssi < 0 ? -n.rssi : n.rssi);
        ui_label(net, rssi, 16, UI_C_MUTED);
    }
    ui_label_wrap(c, v.note, 16, UI_C_MUTED);
    static const ui_btn_kind_t KIND[] = {UI_BTN_SECONDARY, UI_BTN_PRIMARY, UI_BTN_DESTRUCTIVE};
    ui_button(c, v.button, KIND[v.kind], v.button_live, wifi_pressed, self);
}

/* The mock's order (owner, 2026-10-11): brightness first because it is the one reached for
 * most, Wi-Fi last so a scroll or a stray tap near the top cannot reach it. */
static void build(app_t *self)
{
    settings_t *s = self->state;
    lv_obj_clean(s->root);
    s->live = live_now(s);
    ui_header(s->root, &ICON, UI_ACCENT_NEUTRAL, "Settings", back, self);
    s->page = ui_page(s->root, true, 0);
    card_brightness(self, s->page);
    card_power(s->page);
    card_notifications(self, s->page);
    card_jerv(self, s->page);
    card_firmware(self, s->page);
    card_touch(self, s->page);
    card_wifi(self, s->page);
}

static void settings_open(app_t *self, lv_obj_t *root)
{
    settings_t *s = self->state;
    *s = (settings_t){.root = root};
    s->tick = lv_timer_create(tick, 500, self);
    build(self);
}

static void settings_close(app_t *self)
{
    settings_t *s = self->state;
    cal_cancel(s);
    lv_async_call_cancel(rebuild, self);
    if (s->tick) lv_timer_delete(s->tick);
    s->tick = NULL;
}

static bool on_event(app_t *self, const app_event_t *ev)
{
    /* BOOT over the calibration screen cancels it and stays in Settings (the mock's rule); KEY is
       still Jerv's from anywhere (plan §1 "KEY routing"). */
    return ev->type == APP_EV_BOOT && cal_cancel(self->state);
}

static void describe(app_t *self, dbg_json_t *w)
{
    settings_t *s = self->state;
    dbg_json_int(w, "scroll_y", s->page ? lv_obj_get_scroll_y(s->page) : 0);
    dbg_json_bool(w, "wifi_armed", s->live.armed);
    dbg_json_bool(w, "calibrating", s->cal.root != NULL);
    if (s->cal.root) {
        dbg_json_int(w, "cal_target", s->cal.flow.target);
        dbg_json_int(w, "cal_taps", s->cal.flow.taps);
    }
}

app_t app_settings = {
    .id = "settings",
    .title = "Settings",
    .icon = &ICON,
    .accent = UI_ACCENT_NEUTRAL,
    .page = 1,
    .slot = 1,
    .open = settings_open,
    .close = settings_close,
    .on_event = on_event,
    .describe = describe,
    .state = &s_set,
};
