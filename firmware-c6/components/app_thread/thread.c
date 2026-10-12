#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "app_thread.h"
#include "svc_audio.h"
#include "svc_messages.h"
#include "svc_siblings.h"
#include "thread_model.h"
#include "ui_kit.h"
#include "ui_pin.h"

typedef struct {
    lv_obj_t *root, *list;
    ui_talk_t talk;
    ui_pin_t pin;
    /* What the tile and the header say, from svc_siblings: app_t points at these, so a sibling
       renamed on the box is renamed here the next time home is drawn. */
    char title[SIB_NAME_BYTES + 1];
    ui_icon_t icon;
} thread_t;

static thread_t s_inst[APP_THREAD_INSTANCES];
static unsigned s_made;

static const app_thread_config_t *cfg(const app_t *self)
{
    return self->config;
}

/* The name, letter and colour of whoever this thread is with, now. */
static void refresh_identity(app_t *self)
{
    thread_t *t = self->state;
    svc_siblings_name(cfg(self)->slot, t->title, sizeof t->title);
    t->icon = (ui_icon_t){UI_GLYPH_INITIAL, svc_siblings_initial(cfg(self)->slot)};
    self->accent = svc_siblings_accent(cfg(self)->slot);
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
        svc_messages_mark_played(cfg(self)->thread, id);
    }
    /* Not now: the list being rebuilt holds the button whose event this is. */
    lv_async_call(refill, self);
}

static void fill_list(app_t *self)
{
    thread_t *t = self->state;
    const char *who = cfg(self)->thread;
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
    if (!n) ui_label_wrap(t->list, "No messages yet. Conversations arrive in a later update.", 18, UI_C_MUTED);
    if (ui_pin_should(&t->pin, n ? m.id : 0)) ui_page_pin_bottom(t->list);
}

static void thread_open(app_t *self, lv_obj_t *root)
{
    thread_t *t = self->state;
    refresh_identity(self);
    t->root = root;
    t->list = NULL;
    t->talk = (ui_talk_t){0};
    t->pin = (ui_pin_t){0};
    ui_header(root, &t->icon, self->accent, t->title, back, self);
    t->list = ui_page(root, true, 60);
    ui_talk_hint(root);
    fill_list(self);
}

static void end_talk(app_t *self, bool send)
{
    thread_t *t = self->state;
    if (!ui_talk_up(&t->talk)) return;
    ui_talk_close(&t->talk);
    talk_outcome_t o = svc_audio_talk_end(send);
    if (o == TALK_CANCELLED) self->host->toast("Cancelled — nothing sent");
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
        if (!svc_audio_talk_begin(TALK_TO_PERSON, cfg(self)->thread)) return true;
        char title[48];
        snprintf(title, sizeof title, "Message to %s", t->title);
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
        if (!strcmp(ev->u.principal, cfg(self)->thread)) fill_list(self);
        return false;
    default: return false;
    }
}

static void tile_state(app_t *self, ui_tile_state_t *out)
{
    /* Before the shell reads the title and the icon for this tile. */
    refresh_identity(self);
    out->badge = svc_messages_unplayed(cfg(self)->thread);
}

static bool can_clear(app_t *self)
{
    (void)self;
    return true;
}

/* This panel's view only: the people's threads have no conversation on the box to restart. */
static void clear(app_t *self)
{
    svc_audio_stop();
    svc_messages_clear(cfg(self)->thread);
}

static void describe(app_t *self, dbg_json_t *w)
{
    thread_t *t = self->state;
    sibling_t s;
    dbg_json_str(w, "thread", cfg(self)->thread);
    dbg_json_str(w, "name", t->title);
    dbg_json_str(w, "device_id", svc_siblings_slot(cfg(self)->slot, &s) ? s.device_id : "");
    dbg_json_bool(w, "talking", ui_talk_up(&t->talk));
    dbg_json_int(w, "playing", svc_audio_playing());
    dbg_json_int(w, "pinned_to", t->pin.last_id);
}

bool app_thread_make(app_t *app, const app_thread_config_t *c, uint8_t page, uint8_t slot)
{
    if (s_made >= APP_THREAD_INSTANCES) return false;
    thread_t *t = &s_inst[s_made++];
    *app = (app_t){
        .id = c->thread,
        .title = t->title,
        .icon = &t->icon,
        .page = page,
        .slot = slot,
        .config = c,
        .open = thread_open,
        .close = thread_close,
        .on_event = on_event,
        .tile_state = tile_state,
        .describe = describe,
        .can_clear = can_clear,
        .clear = clear,
        .state = t,
    };
    refresh_identity(app);
    return true;
}
