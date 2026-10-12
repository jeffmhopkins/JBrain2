#include "convo.h"

#include <stdio.h>
#include <string.h>

#include "convo_model.h"
#include "svc_audio.h"
#include "svc_messages.h"
#include "svc_settings.h"

static void listen(lv_event_t *e)
{
    svc_audio_speak((uint32_t)(uintptr_t)lv_obj_get_user_data(lv_event_get_current_target(e)));
}

static void fill_list(convo_t *c)
{
    /* A redraw (a transcript filled in, "Speaking…" gone) keeps the reader where they were:
       emptied, the page would clamp its scroll to the top and stay there. */
    const int32_t y = lv_obj_get_scroll_y(c->list);
    /* A reader at the end stays at the end as the status line comes and goes. */
    const bool at_end = lv_obj_get_scroll_bottom(c->list) <= 8;
    lv_obj_clean(c->list);
    const bool tts = svc_settings_get(SET_TTS) != 0;
    const bool can_speak = svc_audio_can_speak();
    const char *who = c->cfg->thread;
    const uint16_t n = svc_messages_count(who);
    msg_t m = {0};
    for (uint16_t i = 0; i < n; i++) {
        if (!svc_messages_get(who, i, &m)) continue;
        char dur[8];
        snprintf(dur, sizeof dur, "0:%02u", m.dur_s % 60);
        const uint32_t speaking = svc_audio_playing();
        ui_bubble_t b = {.mine = m.mine, .voice = m.voice, .pending = m.pending,
                         .text = svc_messages_text(who, &m),
                         .dur = dur, .status = m.status, .speaking = speaking && m.id == speaking,
                         .listen = can_speak && convo_shows_listen(&m, tts, speaking),
                         .tag = m.id};
        ui_bubble(c->list, &b, c->cfg->accent, listen, NULL);
    }
    if (!n) ui_label_wrap(c->list, c->cfg->empty, 18, UI_C_MUTED);
    /* What the agent is doing while the answer is on its way (JT2): under the question, where the
       answer will land, redrawn with each step. */
    char status[64];
    const bool working = svc_audio_status(who, status, sizeof status);
    if (working) ui_status_row(c->list, status, c->cfg->accent);
    if (ui_pin_should(&c->pin, n ? m.id : 0) || (working && at_end)) {
        ui_page_pin_bottom(c->list);
    } else {
        lv_obj_update_layout(c->list);
        lv_obj_scroll_to_y(c->list, y, LV_ANIM_OFF);
    }
}

void convo_open(convo_t *c, const convo_config_t *cfg, const app_host_t *host, lv_obj_t *root,
                lv_event_cb_t on_back, void *back_user)
{
    *c = (convo_t){.cfg = cfg, .host = host, .root = root};
    snprintf(c->listening, sizeof c->listening, "%s is listening", cfg->title);
    ui_header(root, cfg->icon, cfg->accent, cfg->title, on_back, back_user);
    c->list = ui_page(root, true, 60);
    ui_talk_hint(root);
    fill_list(c);
}

static void end_talk(convo_t *c, bool send)
{
    if (!ui_talk_up(&c->talk)) return;
    ui_talk_close(&c->talk);
    const char *t = convo_talk_toast(svc_audio_talk_end(send));
    if (t) c->host->toast(t);
}

static void tapped(void *user)
{
    end_talk(user, false);
}

static void capped(void *user)
{
    end_talk(user, true);
}

void convo_close(convo_t *c)
{
    end_talk(c, false);
}

bool convo_event(convo_t *c, const app_event_t *ev)
{
    switch (ev->type) {
    case APP_EV_KEY_DOWN:
        if (!svc_audio_talk_begin(TALK_TO_AGENT, c->cfg->thread)) {
            const char *why = svc_audio_talk_refused();
            if (why) c->host->toast(why);
            return true;
        }
        c->talk = (ui_talk_t){.cap_ms = TALK_MAX_MS, .ms = svc_audio_talk_ms,
                              .level = svc_audio_level, .on_tap = tapped, .on_cap = capped,
                              .user = c};
        ui_talk_open(&c->talk, c->root, UI_C_ROSE, c->listening);
        return true;
    case APP_EV_KEY_UP: end_talk(c, true); return true;
    case APP_EV_BOOT:
        if (!ui_talk_up(&c->talk)) return false;
        end_talk(c, false);
        return true;
    case APP_EV_MSG_ARRIVED:
        if (!strcmp(ev->u.principal, c->cfg->thread)) fill_list(c);
        return false;
    default: return false;
    }
}

void convo_clear(convo_t *c)
{
    svc_audio_stop_reply();
    svc_messages_clear(c->cfg->thread);
    svc_audio_fresh(c->cfg->thread);
}

void convo_describe(const convo_t *c, dbg_json_t *w)
{
    dbg_json_str(w, "thread", c->cfg->thread);
    dbg_json_bool(w, "talking", ui_talk_up(&c->talk));
    dbg_json_int(w, "shown", svc_messages_count(c->cfg->thread));
    dbg_json_int(w, "pinned_to", c->pin.last_id);
    dbg_json_int(w, "speaking", svc_audio_playing());
    dbg_json_bool(w, "new_chat_next", svc_audio_fresh_pending(c->cfg->thread));
}
