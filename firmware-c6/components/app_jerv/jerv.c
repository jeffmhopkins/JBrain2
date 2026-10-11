#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "app_jerv.h"
#include "jerv_model.h"
#include "svc_audio.h"
#include "svc_messages.h"
#include "svc_settings.h"
#include "ui_kit.h"
#include "ui_pin.h"

#define PRINCIPAL "jerv"

typedef struct {
    lv_obj_t *root, *list;
    ui_talk_t talk;
    ui_pin_t pin;
} jerv_t;

static jerv_t s_jerv;
static const ui_icon_t ICON = {UI_GLYPH_STAR, 0};

static void back(lv_event_t *e)
{
    app_t *self = lv_event_get_user_data(e);
    self->host->go_home();
}

static void listen(lv_event_t *e)
{
    svc_audio_speak((uint32_t)(uintptr_t)lv_obj_get_user_data(lv_event_get_current_target(e)));
}

static void fill_list(jerv_t *j)
{
    lv_obj_clean(j->list);
    bool tts = svc_settings_get(SET_TTS) != 0;
    uint16_t n = svc_messages_count(PRINCIPAL);
    msg_t m = {0};
    for (uint16_t i = 0; i < n; i++) {
        if (!svc_messages_get(PRINCIPAL, i, &m)) continue;
        char dur[8];
        snprintf(dur, sizeof dur, "0:%02u", m.dur_s % 60);
        ui_bubble_t b = {.mine = m.mine, .voice = m.voice, .pending = m.pending, .text = m.text,
                         .dur = dur, .status = m.status,
                         .listen = jerv_shows_listen(&m, tts, 0), .tag = m.id};
        ui_bubble(j->list, &b, UI_ACCENT_JERV, listen, NULL);
    }
    if (ui_pin_should(&j->pin, n ? m.id : 0)) ui_page_pin_bottom(j->list);
}

static void jerv_open(app_t *self, lv_obj_t *root)
{
    jerv_t *j = self->state;
    *j = (jerv_t){.root = root};
    ui_header(root, &ICON, UI_ACCENT_JERV, "Jerv", back, self);
    j->list = ui_page(root, true, 60);
    lv_obj_t *foot = ui_label(root, LV_SYMBOL_AUDIO "  Hold KEY to talk", 18, UI_C_MUTED);
    lv_obj_align(foot, LV_ALIGN_BOTTOM_MID, 0, -20);
    fill_list(j);
}

static void end_talk(jerv_t *j, bool send, const app_host_t *host)
{
    if (!ui_talk_up(&j->talk)) return;
    ui_talk_close(&j->talk);
    const char *t = jerv_talk_toast(svc_audio_talk_end(send));
    if (t) host->toast(t);
}

static void tapped(void *user)
{
    app_t *self = user;
    end_talk(self->state, false, self->host);
}

static void capped(void *user)
{
    app_t *self = user;
    end_talk(self->state, true, self->host);
}

static void jerv_close(app_t *self)
{
    jerv_t *j = self->state;
    end_talk(j, false, self->host);
}

static bool on_event(app_t *self, const app_event_t *ev)
{
    jerv_t *j = self->state;
    switch (ev->type) {
    case APP_EV_KEY_DOWN:
        if (!svc_audio_talk_begin(TALK_TO_JERV, PRINCIPAL)) return true;
        j->talk = (ui_talk_t){.cap_ms = TALK_MAX_MS, .ms = svc_audio_talk_ms,
                              .level = svc_audio_level, .on_tap = tapped, .on_cap = capped,
                              .user = self};
        ui_talk_open(&j->talk, j->root, UI_C_ROSE, "Jerv is listening");
        return true;
    case APP_EV_KEY_UP: end_talk(j, true, self->host); return true;
    case APP_EV_BOOT:
        if (!ui_talk_up(&j->talk)) return false;
        end_talk(j, false, self->host);
        return true;
    case APP_EV_MSG_ARRIVED:
        if (!strcmp(ev->u.principal, PRINCIPAL)) fill_list(j);
        return false;
    default: return false;
    }
}

static void describe(app_t *self, dbg_json_t *w)
{
    jerv_t *j = self->state;
    dbg_json_bool(w, "talking", ui_talk_up(&j->talk));
    dbg_json_int(w, "shown", svc_messages_count(PRINCIPAL));
    dbg_json_int(w, "pinned_to", j->pin.last_id);
}

app_t app_jerv = {
    .id = "jerv",
    .title = "Jerv",
    .icon = &ICON,
    .accent = UI_ACCENT_JERV,
    .page = 0,
    .slot = 0,
    .open = jerv_open,
    .close = jerv_close,
    .on_event = on_event,
    .describe = describe,
    .state = &s_jerv,
};
