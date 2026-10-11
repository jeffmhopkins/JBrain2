#include <stdint.h>
#include <stdio.h>

#include "app_settings.h"
#include "settings_model.h"
#include "svc_box.h"
#include "svc_display.h"
#include "svc_net.h"
#include "svc_power.h"
#include "svc_settings.h"
#include "ui_kit.h"

typedef struct {
    lv_obj_t *root, *page;
    lv_obj_t *bright_label;
} settings_t;

static settings_t s_set;
static const ui_icon_t ICON = {UI_GLYPH_GEAR, 0};
static const setting_t NOTIF_KEYS[] = {SET_NOTIF_ACCEPT, SET_NOTIF_QUIET, SET_NOTIF_CHIME};
static const char *const NOTIF_LABELS[] = {"Accept messages from the box", "Refuse during night hours",
                                           "Chime when one arrives"};

static void build(app_t *self);

/* A switch changes what other cards say, so the page is rebuilt — after the event, because the
 * switch being pressed is part of it — and put back where it was scrolled to. */
static void rebuild(void *self)
{
    settings_t *s = ((app_t *)self)->state;
    int32_t y = s->page ? lv_obj_get_scroll_y(s->page) : 0;
    build(self);
    lv_obj_update_layout(s->page);
    lv_obj_scroll_to_y(s->page, y, LV_ANIM_OFF);
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
    net_info_t n;
    svc_net_info(&n);
    if (n.state == NET_CONNECTED)
        svc_net_disconnect();
    else
        svc_net_connect();
    lv_async_call(rebuild, lv_event_get_user_data(e));
}

static void update_pressed(lv_event_t *e)
{
    app_t *self = lv_event_get_user_data(e);
    if (!svc_box_update_now()) self->host->toast("Connect Wi-Fi first");
    lv_async_call(rebuild, self);
}

static void calibrate_pressed(lv_event_t *e)
{
    app_t *self = lv_event_get_user_data(e);
    self->host->toast("Calibration arrives with the touch driver");
}

static void bright_changed(lv_event_t *e)
{
    app_t *self = lv_event_get_user_data(e);
    settings_t *s = self->state;
    int32_t v = settings_brightness_from_slider(lv_slider_get_value(lv_event_get_current_target(e)));
    svc_settings_set(SET_BRIGHTNESS, v);
    svc_display_set_brightness((uint8_t)svc_settings_get(SET_BRIGHTNESS));
    lv_label_set_text_fmt(s->bright_label, "%ld%%", (long)svc_settings_get(SET_BRIGHTNESS));
}

static lv_obj_t *title_row(lv_obj_t *card, const char *title)
{
    lv_obj_t *r = ui_box(card, LV_FLEX_FLOW_ROW, 12);
    lv_obj_set_width(r, LV_PCT(100));
    lv_obj_set_flex_align(r, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    ui_label(r, title, 22, UI_C_TEXT);
    return r;
}

static void card_wifi(app_t *self, lv_obj_t *page)
{
    net_info_t n;
    svc_net_info(&n);
    settings_wifi_t v;
    settings_wifi(&n, &v);
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
        lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
        lv_obj_set_style_bg_color(b, lv_color_hex(i < v.bars ? UI_C_TEXT : UI_C_LINE_STRONG), 0);
    }
    lv_obj_t *ssid = ui_label(net, n.ssid, 20, UI_C_TEXT);
    lv_obj_set_flex_grow(ssid, 1);
    if (n.state == NET_CONNECTED) {
        char rssi[12];
        snprintf(rssi, sizeof rssi, "%d dBm", n.rssi);
        ui_label(net, rssi, 16, UI_C_MUTED);
    }
    ui_label_wrap(c, v.note, 16, UI_C_MUTED);
    ui_button(c, v.button, v.primary ? UI_BTN_PRIMARY : UI_BTN_SECONDARY, v.button_live, wifi_pressed, self);
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
    char note[160];
    settings_notif_note(accept, svc_settings_get(SET_NOTIF_QUIET), (uint16_t)svc_settings_get(SET_NIGHT_FROM),
                        (uint16_t)svc_settings_get(SET_NIGHT_TO), note, sizeof note);
    ui_label_wrap(c, note, 16, UI_C_MUTED);
}

static void card_jerv(app_t *self, lv_obj_t *page)
{
    lv_obj_t *c = ui_card(page, "Jerv");
    bool tts = svc_settings_get(SET_TTS) != 0;
    lv_obj_t *sw = ui_switch_row(c, "Read replies aloud", tts, true, toggled, self);
    lv_obj_set_user_data(sw, (void *)(uintptr_t)SET_TTS);
    ui_label_wrap(c, tts ? "Replies are spoken and shown as text."
                         : "Replies are text only. Tap Listen on a reply to hear it.",
                  16, UI_C_MUTED);
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
}

static void card_touch(app_t *self, lv_obj_t *page)
{
    lv_obj_t *c = ui_card(page, NULL);
    lv_obj_t *r = title_row(c, "Touch screen");
    ui_label(r, "Never", 18, UI_C_MUTED);
    ui_label_wrap(c, "Taps landing off-target? Tap each cross until it turns green.", 18, UI_C_MUTED);
    ui_button(c, "Calibrate", UI_BTN_PRIMARY, true, calibrate_pressed, self);
}

static void build(app_t *self)
{
    settings_t *s = self->state;
    lv_obj_clean(s->root);
    ui_header(s->root, &ICON, UI_ACCENT_NEUTRAL, "Settings", back, self);
    s->page = ui_page(s->root, true, 0);
    card_wifi(self, s->page);
    card_power(s->page);
    card_notifications(self, s->page);
    card_jerv(self, s->page);
    card_firmware(self, s->page);
    card_brightness(self, s->page);
    card_touch(self, s->page);
}

static void settings_open(app_t *self, lv_obj_t *root)
{
    settings_t *s = self->state;
    *s = (settings_t){.root = root};
    build(self);
}

static void settings_close(app_t *self)
{
    lv_async_call_cancel(rebuild, self);
}

static bool on_event(app_t *self, const app_event_t *ev)
{
    if (ev->type == APP_EV_NET_UP || ev->type == APP_EV_NET_DOWN) rebuild(self);
    return false;
}

static void describe(app_t *self, dbg_json_t *w)
{
    settings_t *s = self->state;
    dbg_json_int(w, "scroll_y", s->page ? lv_obj_get_scroll_y(s->page) : 0);
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
