#include "convo.h"

#include <stdio.h>
#include <string.h>

#include "convo_model.h"
#include "svc_audio.h"
#include "svc_convo.h"
#include "svc_messages.h"
#include "svc_settings.h"

/* The working line's clock ticks once a second while the box works. */
#define WORK_TICK_MS 1000

static void listen(lv_event_t *e)
{
    svc_audio_speak((uint32_t)(uintptr_t)lv_obj_get_user_data(lv_event_get_current_target(e)));
}

static convo_sig_t sig_now(const convo_t *c, svc_work_t *w)
{
    const char *who = c->cfg->thread;
    const bool working = svc_audio_work(who, w);
    return (convo_sig_t){.rev = svc_messages_rev(who),
                         .speaking = svc_audio_playing(),
                         .tts = svc_settings_get(SET_TTS) != 0,
                         .can_speak = svc_audio_can_speak(),
                         .working = working,
                         .work_seq = working ? w->seq : 0,
                         .meta = svc_convo_rev(),
                         .fresh = svc_audio_fresh_pending(who)};
}

/* ---- the working view: built once per question, then changed where it stands ---- */

static void work_tick(lv_timer_t *t)
{
    convo_t *c = lv_timer_get_user_data(t);
    convo_refresh(c);
}

static void work_gone(lv_event_t *e)
{
    convo_work_t *w = lv_event_get_user_data(e);
    if (w->tick) lv_timer_delete(w->tick);
    *w = (convo_work_t){0};
}

static void work_build(convo_t *c)
{
    convo_work_t *w = &c->work;
    w->root = ui_box(c->list, LV_FLEX_FLOW_COLUMN, 6);
    lv_obj_set_width(w->root, LV_PCT(100));
    lv_obj_set_flex_align(w->root, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    /* "+2 more" lines up with the steps' words, past their marks. */
    w->fold = ui_note_line(w->root, "");
    lv_obj_set_style_pad_left(w->fold, 6 + 18 + 10, 0);
    for (int i = 0; i < WORK_SHOWN; i++) ui_step_row(w->root, &w->mark[i], &w->text[i]);
    lv_obj_t *phase_mark;
    w->phase_row = ui_step_row(w->root, &phase_mark, &w->phase);
    lv_obj_delete(phase_mark);
    w->spin = ui_spinner(w->phase_row, ui_accent_color(c->cfg->accent));
    lv_obj_move_to_index(w->spin, 0);
    w->tick = lv_timer_create(work_tick, WORK_TICK_MS, c);
    lv_obj_add_event_cb(w->root, work_gone, LV_EVENT_DELETE, w);
}

static void show(lv_obj_t *o, bool on)
{
    if (on) lv_obj_remove_flag(o, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_add_flag(o, LV_OBJ_FLAG_HIDDEN);
}

static void set_text(lv_obj_t *l, const char *text)
{
    /* LVGL re-lays the label out on every set, same text or not. */
    if (strcmp(lv_label_get_text(l), text)) lv_label_set_text(l, text);
}

/* The steps as they stand: done ✓, failed a dim ✗, and the spinner on whatever is happening now —
 * the running step, or the phase line ("Thinking… 0:42") between them. The spinner is moved, never
 * remade. */
static void work_update(convo_t *c, const svc_work_t *s)
{
    convo_work_t *w = &c->work;
    char line[96];
    snprintf(line, sizeof line, "+%u more", (unsigned)s->folded);
    set_text(w->fold, line);
    show(w->fold, s->folded > 0);
    lv_obj_t *busy = w->phase_row;
    for (int i = 0; i < WORK_SHOWN; i++) {
        lv_obj_t *row = lv_obj_get_parent(w->mark[i]);
        show(row, i < s->n);
        if (i >= s->n) continue;
        const work_step_t *st = &s->step[i];
        set_text(w->text[i], st->label);
        const bool running = st->state == WORK_RUNNING;
        if (running) busy = row;
        show(w->mark[i], !running);
        set_text(w->mark[i], st->state == WORK_FAILED ? LV_SYMBOL_CLOSE : LV_SYMBOL_OK);
        const uint32_t c_mark = st->state == WORK_FAILED ? UI_C_FAINT : UI_C_MUTED;
        lv_obj_set_style_text_color(w->mark[i], lv_color_hex(c_mark), 0);
        lv_obj_set_style_text_color(w->text[i],
                                    lv_color_hex(st->state == WORK_FAILED ? UI_C_FAINT : UI_C_MUTED), 0);
    }
    char clock[12];
    talk_clock(s->elapsed_ms, clock, sizeof clock);
    snprintf(line, sizeof line, "%s %s", s->phase, clock);
    set_text(w->phase, line);
    show(w->phase_row, busy == w->phase_row);
    if (lv_obj_get_parent(w->spin) != busy) {
        lv_obj_set_parent(w->spin, busy);
        lv_obj_move_to_index(w->spin, 0);
    }
}

/* ---- the whole list ---- */

static void fill_list(convo_t *c, const convo_sig_t *sig, const svc_work_t *work)
{
    /* A redraw (a transcript filled in, "Speaking…" gone) keeps the reader where they were:
       emptied, the page would clamp its scroll to the top and stay there. */
    const int32_t y = lv_obj_get_scroll_y(c->list);
    /* A reader at the end stays at the end as the working view comes and goes. */
    const bool at_end = lv_obj_get_scroll_bottom(c->list) <= 8;
    lv_obj_clean(c->list);
    const char *who = c->cfg->thread;
    const uint16_t n = svc_messages_count(who);
    msg_t m = {0};
    /* A switched-to conversation shows its last few exchanges; the rest are in the app. */
    if (n && !sig->fresh && svc_convo_earlier(who)) {
        lv_obj_t *row = ui_box(c->list, LV_FLEX_FLOW_ROW, 0);
        lv_obj_set_width(row, LV_PCT(100));
        lv_obj_set_flex_align(row, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        ui_app_chip(row, "Earlier messages are in the app");
    }
    for (uint16_t i = 0; i < n; i++) {
        if (!svc_messages_get(who, i, &m)) continue;
        char dur[8];
        snprintf(dur, sizeof dur, "0:%02u", m.dur_s % 60);
        /* An answer's "2 steps · 58 s" sits over it, where the steps were. */
        if (!m.mine && m.status[0]) ui_note_line(c->list, m.status);
        ui_bubble_t b = {.mine = m.mine, .voice = m.voice, .pending = m.pending,
                         .text = svc_messages_text(who, &m),
                         .dur = dur, .status = m.mine ? m.status : NULL,
                         .speaking = sig->speaking && m.id == sig->speaking,
                         .listen = sig->can_speak && convo_shows_listen(&m, sig->tts, sig->speaking),
                         .tag = m.id};
        ui_bubble(c->list, &b, c->cfg->accent, listen, NULL);
    }
    if (!n && sig->fresh) {
        /* A new conversation, not yet begun: said in the middle of the glass (the mock's
           ConvoNew board). */
        lv_obj_t *col = ui_box(c->list, LV_FLEX_FLOW_COLUMN, 10);
        lv_obj_set_size(col, LV_PCT(100), LV_PCT(100));
        lv_obj_set_style_pad_hor(col, 24, 0);
        lv_obj_set_flex_align(col, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        ui_label(col, "New conversation", 22, UI_C_TEXT);
        char line[120];
        snprintf(line, sizeof line,
                 "Hold KEY and ask %s something. The last one is in Recent conversations.",
                 c->cfg->title);
        lv_obj_t *l = ui_label_wrap(col, line, 18, UI_C_MUTED);
        lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
    } else if (!n) {
        ui_label_wrap(c->list, c->cfg->empty, 18, UI_C_MUTED);
    }
    /* What the agent is doing while the answer is on its way (JT2): under the question, where the
       answer will land. */
    if (sig->working) {
        work_build(c);
        work_update(c, work);
    }
    c->drawn = *sig;
    if (ui_pin_should(&c->pin, n ? m.id : 0) || (sig->working && at_end)) {
        ui_page_pin_bottom(c->list);
    } else {
        lv_obj_update_layout(c->list);
        lv_obj_scroll_to_y(c->list, y, LV_ANIM_OFF);
    }
}

static void draw_title(convo_t *c, const convo_sig_t *now)
{
    set_text(c->sub, convo_subtitle(now->fresh, svc_convo_title(c->cfg->thread)));
}

void convo_refresh(convo_t *c)
{
    svc_work_t work;
    const convo_sig_t now = sig_now(c, &work);
    /* An answer has landed: the box may have named the conversation in that turn. */
    if (c->drawn.working && !now.working) svc_convo_refresh_title(c->cfg->thread);
    draw_title(c, &now);
    switch (convo_redraw(&c->drawn, &now)) {
    case CONVO_REBUILD: fill_list(c, &now, &work); break;
    case CONVO_WORK: {
        const bool at_end = lv_obj_get_scroll_bottom(c->list) <= 8;
        work_update(c, &work);
        c->drawn = now;
        if (at_end) ui_page_pin_bottom(c->list);
        break;
    }
    case CONVO_SAME:
        /* Only the clock moved. */
        if (now.working) work_update(c, &work);
        break;
    }
}

void convo_open(convo_t *c, const convo_config_t *cfg, const app_host_t *host, lv_obj_t *root,
                lv_event_cb_t on_back, void *back_user)
{
    *c = (convo_t){.cfg = cfg, .host = host, .root = root};
    snprintf(c->listening, sizeof c->listening, "%s is listening", cfg->title);
    svc_work_t work;
    const convo_sig_t now = sig_now(c, &work);
    ui_header_sub(root, cfg->icon, cfg->accent, cfg->title, "", on_back, back_user, &c->sub);
    draw_title(c, &now);
    c->list = ui_page(root, true, 60);
    ui_talk_hint(root);
    fill_list(c, &now, &work);
    /* What the box calls the conversation now: the PWA may have named it, or the panel's last
       switch moved it on. A new one has none yet. */
    if (!now.fresh) svc_convo_refresh_title(cfg->thread);
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
    convo_recent_close(c);
}

bool convo_event(convo_t *c, const app_event_t *ev)
{
    /* Over Recent, KEY closes it and talks in the thread; BOOT only closes it. */
    if ((ev->type == APP_EV_KEY_DOWN || ev->type == APP_EV_BOOT) && convo_recent_up(c) &&
        convo_recent_button(c, ev->type))
        return true;
    switch (ev->type) {
    case APP_EV_KEY_DOWN: {
        /* Inside this conversation: KEY continues it (plan "KEY routing"). */
        const talk_route_t route = talk_key_route(c->cfg->thread);
        if (!svc_audio_talk_begin(route.kind, route.agent)) {
            const char *why = svc_audio_talk_refused();
            if (why) c->host->toast(why);
            return true;
        }
        c->talk = (ui_talk_t){.cap_ms = TALK_MAX_MS, .ms = svc_audio_talk_ms,
                              .level = svc_audio_level, .on_tap = tapped, .on_cap = capped,
                              .user = c};
        ui_talk_open(&c->talk, c->root, UI_C_ROSE, c->listening);
        return true;
    }
    case APP_EV_KEY_UP: end_talk(c, true); return true;
    case APP_EV_BOOT:
        if (!ui_talk_up(&c->talk)) return false;
        end_talk(c, false);
        return true;
    case APP_EV_MSG_ARRIVED:
        if (!strcmp(ev->u.principal, c->cfg->thread)) {
            convo_refresh(c);
            if (convo_recent_up(c)) convo_recent_update(c);
        }
        return false;
    default: return false;
    }
}

bool convo_menu(const convo_t *c, char *sub, size_t cap, ui_accent_t *accent)
{
    if (!c->root || convo_recent_up(c) || ui_talk_up(&c->talk)) return false;
    const char *who = c->cfg->thread;
    snprintf(sub, cap, "%s · %s", c->cfg->title,
             convo_subtitle(svc_audio_fresh_pending(who), svc_convo_title(who)));
    *accent = c->cfg->accent;
    return true;
}

const char *convo_new(convo_t *c)
{
    const char *who = c->cfg->thread;
    if (convo_new_decide(svc_audio_fresh_pending(who), svc_messages_count(who)) ==
        CONVO_NEW_ALREADY)
        return CONVO_TOAST_ALREADY;
    svc_audio_stop_reply();
    svc_messages_clear(who);
    svc_audio_fresh(who);
    svc_convo_new(who);
    return CONVO_TOAST_NEW;
}

void convo_recent(convo_t *c)
{
    convo_recent_open(c);
}

void convo_describe(const convo_t *c, dbg_json_t *w)
{
    dbg_json_str(w, "thread", c->cfg->thread);
    dbg_json_bool(w, "talking", ui_talk_up(&c->talk));
    dbg_json_int(w, "shown", svc_messages_count(c->cfg->thread));
    dbg_json_int(w, "pinned_to", c->pin.last_id);
    dbg_json_int(w, "speaking", svc_audio_playing());
    dbg_json_bool(w, "new_chat_next", svc_audio_fresh_pending(c->cfg->thread));
    dbg_json_bool(w, "working", c->work.root != NULL);
    static const char *const RC[] = {"closed", "loading", "list", "empty", "failed", "opening"};
    dbg_json_str(w, "recent", RC[c->recent.rc.mode]);
    dbg_json_str(w, "subtitle", c->sub ? lv_label_get_text(c->sub) : "");
}
