#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "app_thread.h"
#include "svc_audio.h"
#include "svc_messages.h"
#include "thread_model.h"
#include "ui_kit.h"
#include "ui_pin.h"

typedef struct {
    lv_obj_t *root, *list;
    ui_talk_t talk;
    ui_pin_t pin;
} thread_t;

static thread_t s_inst[APP_THREAD_INSTANCES];
static unsigned s_made;

static const app_thread_config_t *cfg(const app_t *self)
{
    return self->config;
}

static void back(lv_event_t *e)
{
    app_t *self = lv_event_get_user_data(e);
    self->host->go_home();
}

static void fill_list(app_t *self);

static void refill(void *self)
{
    fill_list(self);
}

static void play(lv_event_t *e)
{
    app_t *self = lv_event_get_user_data(e);
    uint32_t id = (uint32_t)(uintptr_t)lv_obj_get_user_data(lv_event_get_current_target(e));
    if (svc_audio_playing() == id) {
        svc_audio_stop();
    } else if (svc_audio_play(id)) {
        svc_messages_mark_played(cfg(self)->principal, id);
    }
    /* Not now: the list being rebuilt holds the button whose event this is. */
    lv_async_call(refill, self);
}

static void fill_list(app_t *self)
{
    thread_t *t = self->state;
    const char *who = cfg(self)->principal;
    lv_obj_clean(t->list);
    uint16_t n = svc_messages_count(who);
    msg_t m = {0};
    for (uint16_t i = 0; i < n; i++) {
        if (!svc_messages_get(who, i, &m)) continue;
        thread_bubble_t tb;
        thread_bubble(&m, svc_audio_playing(), 0, &tb);
        ui_bubble_t b = {.mine = m.mine, .voice = m.voice, .pending = m.pending,
                         .playing = tb.playing, .unplayed = tb.unplayed, .prog = tb.prog,
                         .text = m.text, .dur = tb.dur, .status = m.status, .tag = m.id};
        ui_bubble(t->list, &b, self->accent, play, self);
    }
    if (ui_pin_should(&t->pin, n ? m.id : 0)) ui_page_pin_bottom(t->list);
}

static void thread_open(app_t *self, lv_obj_t *root)
{
    thread_t *t = self->state;
    *t = (thread_t){.root = root};
    ui_header(root, &cfg(self)->icon, self->accent, cfg(self)->name, back, self);
    t->list = ui_page(root, true, 60);
    lv_obj_t *foot = ui_label(root, LV_SYMBOL_AUDIO "  Hold KEY to talk", 18, UI_C_MUTED);
    lv_obj_align(foot, LV_ALIGN_BOTTOM_MID, 0, -20);
    fill_list(self);
}

static void end_talk(app_t *self, bool send)
{
    thread_t *t = self->state;
    if (!ui_talk_up(&t->talk)) return;
    ui_talk_close(&t->talk);
    talk_outcome_t o = svc_audio_talk_end(send);
    if (o == TALK_CANCELLED) self->host->toast("Cancelled - nothing sent");
    if (o == TALK_TOO_SHORT) self->host->toast("Hold KEY the whole time you talk");
}

static void tapped(void *user)
{
    end_talk(user, false);
}

static void capped(void *user)
{
    end_talk(user, true);
}

static void thread_close(app_t *self)
{
    end_talk(self, false);
    svc_audio_stop();
    lv_async_call_cancel(refill, self);
}

static bool on_event(app_t *self, const app_event_t *ev)
{
    thread_t *t = self->state;
    switch (ev->type) {
    case APP_EV_KEY_DOWN: {
        if (!svc_audio_talk_begin(TALK_TO_PERSON, cfg(self)->principal)) return true;
        char title[40];
        snprintf(title, sizeof title, "Message to %s", cfg(self)->name);
        t->talk = (ui_talk_t){.cap_ms = TALK_MAX_MS, .ms = svc_audio_talk_ms,
                              .level = svc_audio_level, .on_tap = tapped, .on_cap = capped,
                              .user = self};
        ui_talk_open(&t->talk, t->root, UI_C_STEEL, title);
        return true;
    }
    case APP_EV_KEY_UP: end_talk(self, true); return true;
    case APP_EV_BOOT:
        if (!ui_talk_up(&t->talk)) return false;
        end_talk(self, false);
        return true;
    case APP_EV_MSG_ARRIVED:
        if (!strcmp(ev->u.principal, cfg(self)->principal)) fill_list(self);
        return false;
    default: return false;
    }
}

static void tile_state(app_t *self, ui_tile_state_t *out)
{
    out->badge = svc_messages_unplayed(cfg(self)->principal);
}

static void describe(app_t *self, dbg_json_t *w)
{
    thread_t *t = self->state;
    dbg_json_str(w, "principal", cfg(self)->principal);
    dbg_json_bool(w, "talking", ui_talk_up(&t->talk));
    dbg_json_int(w, "playing", svc_audio_playing());
    dbg_json_int(w, "pinned_to", t->pin.last_id);
}

bool app_thread_make(app_t *app, const app_thread_config_t *c, ui_accent_t accent, uint8_t page,
                     uint8_t slot)
{
    if (s_made >= APP_THREAD_INSTANCES) return false;
    *app = (app_t){
        .id = c->principal,
        .title = c->name,
        .icon = &c->icon,
        .accent = accent,
        .page = page,
        .slot = slot,
        .config = c,
        .open = thread_open,
        .close = thread_close,
        .on_event = on_event,
        .tile_state = tile_state,
        .describe = describe,
        .state = &s_inst[s_made++],
    };
    return true;
}
