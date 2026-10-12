/* Recent (0.0.15, the mock's ConvoRecent board): the agent's last conversations on the box, over
 * the thread, to switch the panel into one. Its rules are rc_step's (convo_model.c); this draws
 * what they decide. The list itself is svc_convo's, on the heap only while this is open. */
#include <stdio.h>
#include <string.h>

#include "convo.h"
#include "svc_convo.h"
#include "svc_messages.h"
#include "svc_rtc.h"

static void act(convo_t *c, rc_do_t d);

bool convo_recent_up(const convo_t *c)
{
    return c->recent.root != NULL;
}

/* A tap acts after the click that made it: the screen it is on may be redrawn or deleted. */
static void later(void *user)
{
    convo_t *c = user;
    if (!convo_recent_up(c)) return;
    act(c, rc_step(&c->recent.rc, c->recent.later, c->recent.later_arg, false));
}

static void ask_later(convo_t *c, rc_in_t in, int arg)
{
    c->recent.later = in;
    c->recent.later_arg = arg;
    lv_async_call(later, c);
}

static void back_cb(lv_event_t *e)
{
    ask_later(lv_event_get_user_data(e), RC_IN_BACK, 0);
}

static void new_cb(lv_event_t *e)
{
    ask_later(lv_event_get_user_data(e), RC_IN_NEW, 0);
}

static void retry_cb(lv_event_t *e)
{
    ask_later(lv_event_get_user_data(e), RC_IN_OPEN, 0);
}

static void row_later(void *user)
{
    convo_t *c = user;
    if (!convo_recent_up(c)) return;
    const convo_list_t *l = NULL;
    svc_convo_recent(&l);
    const int i = c->recent.later_arg;
    const bool current = l && i < l->n &&
                         convo_row_is_current(l->row[i].current,
                                              svc_audio_fresh_pending(c->cfg->thread));
    act(c, rc_step(&c->recent.rc, RC_IN_TAP, l && i < l->n ? i : -1, current));
}

static void row_cb(lv_event_t *e)
{
    convo_t *c = lv_event_get_user_data(e);
    c->recent.later_arg = (int)(intptr_t)lv_obj_get_user_data(lv_event_get_current_target(e));
    lv_async_call(row_later, c);
}

static void scrolled(lv_event_t *e)
{
    convo_t *c = lv_event_get_user_data(e);
    if (!c->recent.fade) return;
    if (lv_obj_get_scroll_bottom(c->recent.page) > 4)
        lv_obj_remove_flag(c->recent.fade, LV_OBJ_FLAG_HIDDEN);
    else
        lv_obj_add_flag(c->recent.fade, LV_OBJ_FLAG_HIDDEN);
}

/* The panel's zone now, local minus UTC, from its own clock: the rows' times are the panel's. */
static int32_t zone_offset(int64_t now)
{
    rtc_time_t t;
    svc_rtc_now(&t);
    const int64_t local_s = (int64_t)t.h * 3600 + t.m * 60 + t.s;
    int64_t utc_s = now % 86400;
    if (utc_s < 0) utc_s += 86400;
    int64_t d = local_s - utc_s;
    if (d > 14 * 3600) d -= 86400;
    if (d < -14 * 3600) d += 86400;
    return (int32_t)d;
}

static void draw_rows(convo_t *c, lv_obj_t *page)
{
    const convo_list_t *l = NULL;
    svc_convo_recent(&l);
    if (!l) return;
    int64_t now = 0;
    const bool timed = svc_rtc_epoch(&now);
    const int32_t off = timed ? zone_offset(now) : 0;
    const bool fresh = svc_audio_fresh_pending(c->cfg->thread);
    for (uint8_t i = 0; i < l->n; i++) {
        const convo_row_t *r = &l->row[i];
        char when[24] = "";
        const bool opening = c->recent.rc.mode == RC_OPENING && c->recent.rc.opening == i;
        if (opening) snprintf(when, sizeof when, "Opening…");
        else if (timed) convo_when(r->at, now, off, when, sizeof when);
        const ui_convo_row_t row = {
            .title = r->title,
            .when = when,
            .snippet = r->snippet,
            .from_app = r->origin == CONVO_FROM_PWA,
            .current = convo_row_is_current(r->current, fresh),
            .opening = opening,
        };
        lv_obj_t *o = ui_convo_row(page, &row, c->cfg->accent, row_cb, c);
        lv_obj_set_user_data(o, (void *)(intptr_t)i);
    }
}

static void draw(convo_t *c)
{
    convo_recent_t *r = &c->recent;
    const int32_t y = lv_obj_get_scroll_y(r->page);
    lv_obj_clean(r->page);
    ui_menu_row(r->page, UI_ROW_GLYPH_PLUS, "New conversation", ui_accent_color(c->cfg->accent),
                true, false, new_cb, c);
    switch (r->rc.mode) {
    case RC_LOADING: {
        ui_label(r->page, "Loading…", 18, UI_C_MUTED);
        static const int W[3][2] = {{52, 76}, {40, 70}, {47, 80}};
        static const lv_opa_t OPA[3] = {LV_OPA_COVER, LV_OPA_70, LV_OPA_40};
        for (int i = 0; i < 3; i++) ui_skeleton_row(r->page, W[i][0], W[i][1], OPA[i]);
        break;
    }
    case RC_EMPTY:
        ui_empty_state(r->page, "No other conversations yet",
                       "Conversations started on the panel or in the app will be listed here.");
        break;
    case RC_FAILED:
        ui_empty_state(r->page, "Couldn't load conversations",
                       "The box did not answer. Check Wi-Fi, then try again.");
        ui_button(r->page, "Try again", UI_BTN_SECONDARY, true, retry_cb, c);
        break;
    case RC_LIST:
    case RC_OPENING: draw_rows(c, r->page); break;
    default: break;
    }
    lv_obj_update_layout(r->page);
    lv_obj_scroll_to_y(r->page, y, LV_ANIM_OFF);
    lv_obj_send_event(r->page, LV_EVENT_SCROLL, NULL);
}

static void close_screen(convo_t *c)
{
    svc_convo_recent_close();
    if (c->recent.root) lv_obj_delete(c->recent.root);
    c->recent.root = c->recent.page = c->recent.fade = NULL;
    c->recent.rc = (rc_t){RC_CLOSED, 0};
}

static void act(convo_t *c, rc_do_t d)
{
    switch (d) {
    case RC_DO_FETCH:
        if (!svc_convo_recent_open(c->cfg->thread)) rc_step(&c->recent.rc, RC_IN_FAILED, 0, false);
        draw(c);
        break;
    case RC_DO_DRAW: draw(c); break;
    case RC_DO_SWITCH: {
        const convo_list_t *l = NULL;
        svc_convo_recent(&l);
        /* An answer still on its way belongs to the conversation being left: it must not land
           in the one being opened. The box finishes it there regardless. */
        svc_work_t w;
        if (svc_audio_work(c->cfg->thread, &w)) svc_audio_stop_reply();
        if (l) svc_convo_switch(c->cfg->thread, l->row[c->recent.rc.opening].id);
        draw(c);
        break;
    }
    case RC_DO_NOT_OPENED:
        draw(c);
        c->host->toast(CONVO_TOAST_NOT_OPENED);
        break;
    case RC_DO_WAIT: c->host->toast(CONVO_TOAST_OPENING); break;
    case RC_DO_CLOSE:
    case RC_DO_CLOSE_TALK: close_screen(c); break;
    case RC_DO_CLOSE_NEW:
        close_screen(c);
        c->host->toast(convo_new(c));
        break;
    default: break;
    }
}

void convo_recent_open(convo_t *c)
{
    if (convo_recent_up(c)) return;
    convo_recent_t *r = &c->recent;
    r->rc = (rc_t){RC_CLOSED, 0};
    /* Its own screen over the thread, taking every press: the thread under it stays as it was. */
    r->root = ui_overlay(c->root);
    lv_obj_set_layout(r->root, LV_LAYOUT_NONE);
    char sub[40];
    snprintf(sub, sizeof sub, "%s's conversations", c->cfg->title);
    ui_header_sub(r->root, c->cfg->icon, c->cfg->accent, "Recent", sub, back_cb, c, NULL);
    r->page = ui_page(r->root, true, 0);
    lv_obj_set_style_pad_hor(r->page, 16, 0);
    lv_obj_set_style_pad_top(r->page, 14, 0);
    lv_obj_set_style_pad_row(r->page, 10, 0);
    lv_obj_add_event_cb(r->page, scrolled, LV_EVENT_SCROLL, c);
    r->fade = ui_fade_bottom(r->root, 44);
    lv_obj_add_flag(r->fade, LV_OBJ_FLAG_HIDDEN);
    act(c, rc_step(&r->rc, RC_IN_OPEN, 0, false));
}

void convo_recent_close(convo_t *c)
{
    if (convo_recent_up(c)) close_screen(c);
}

bool convo_recent_button(convo_t *c, app_event_type_t type)
{
    const rc_do_t d = rc_step(&c->recent.rc, type == APP_EV_KEY_DOWN ? RC_IN_KEY : RC_IN_BACK, 0,
                              false);
    act(c, d);
    /* KEY closed it: the thread is on screen again and the press is its talk. */
    return d != RC_DO_CLOSE_TALK;
}

void convo_recent_update(convo_t *c)
{
    convo_recent_t *r = &c->recent;
    const convo_list_t *l = NULL;
    switch (r->rc.mode) {
    case RC_LOADING: {
        const convo_state_t s = svc_convo_recent(&l);
        if (s == CONVO_READY) act(c, rc_step(&r->rc, RC_IN_LISTED, l ? l->n : 0, false));
        else if (s == CONVO_FAILED || s == CONVO_IDLE) act(c, rc_step(&r->rc, RC_IN_FAILED, 0, false));
        break;
    }
    case RC_OPENING: {
        const convo_state_t s = svc_convo_switching();
        if (s == CONVO_READY) act(c, rc_step(&r->rc, RC_IN_SWITCHED, 0, false));
        else if (s == CONVO_FAILED) act(c, rc_step(&r->rc, RC_IN_SWITCH_FAILED, 0, false));
        break;
    }
    default: break;
    }
}
