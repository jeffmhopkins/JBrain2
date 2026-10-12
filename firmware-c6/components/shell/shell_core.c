#include "shell_core.h"

#include <stdio.h>
#include <string.h>

#include "ui_theme.h"

sh_overlay_t sh_top_overlay(const sh_state_t *st, const sh_facts_t *f)
{
    if (f->alarm_ringing) return SH_OV_ALARM;
    /* A notice waits for a hold to end rather than burying the talk ring: the person is
     * mid-sentence, and the notice is still there when they let go. */
    if (f->notices && !f->notices_held && !f->menu_open && st->key_owner != SH_KEY_APP &&
        st->key_owner != SH_KEY_SHELL)
        return SH_OV_NOTICE;
    return SH_OV_NONE;
}

void sh_app_closed(sh_state_t *st)
{
    if (st->key_owner == SH_KEY_APP) st->key_owner = SH_KEY_FREE;
}

static sh_route_t r(sh_act_t first, sh_act_t fallback)
{
    return (sh_route_t){first, fallback};
}

sh_route_t sh_route_button(sh_state_t *st, const sh_facts_t *f, sh_button_t b)
{
    /* Lit by the time it is handled (PWR got there first): nothing left to do. */
    if (b == SH_BTN_TOUCH) return r(f->dark ? SH_ACT_WAKE : SH_ACT_NONE, SH_ACT_NONE);
    if (b == SH_BTN_PWR) return r(f->dark ? SH_ACT_WAKE : SH_ACT_DARK, SH_ACT_NONE);

    if (b == SH_BTN_KEY_UP) {
        sh_key_owner_t owner = st->key_owner;
        st->key_owner = SH_KEY_FREE;
        if (owner == SH_KEY_APP && f->app_open) return r(SH_ACT_TO_APP, SH_ACT_NONE);
        if (owner == SH_KEY_SHELL) return r(SH_ACT_SEND_JERV, SH_ACT_NONE);
        return r(SH_ACT_NONE, SH_ACT_NONE);
    }

    /* A press on a dark screen only wakes it: a recording started blind is a message nobody
     * meant to send. */
    if (f->dark) return r(SH_ACT_WAKE, SH_ACT_NONE);

    /* The conversation menu takes every button and does one thing with it: closes. KEY over it
     * starts no recording and its release sends nothing (owner, 2026-10-12). A ringing alarm
     * outranks it — the shell closes the menu when one starts — so it is not asked here. */
    if (f->menu_open && !f->alarm_ringing) {
        if (b == SH_BTN_KEY_DOWN) st->key_owner = SH_KEY_FREE;
        return r(SH_ACT_CLOSE_MENU, SH_ACT_NONE);
    }

    /* BOOT held is the shell's, never offered to an app: the Clock from anywhere, a notice
     * included (the face keeps it as its badge). Over the talk ring or a ringing alarm it is
     * just BOOT — cancel, stop — because those are what a hand on BOOT is reaching for. */
    if (b == SH_BTN_BOOT_LONG) {
        if (!f->talking && !f->alarm_ringing)
            return r(f->clock_open ? SH_ACT_NONE : SH_ACT_OPEN_CLOCK, SH_ACT_NONE);
        b = SH_BTN_BOOT;
    }

    /* The talk ring covers everything: BOOT is its cancel, and a second KEY_DOWN (a debug
     * inject; a finger cannot) must not start another. */
    if (f->talking) return r(b == SH_BTN_BOOT ? SH_ACT_CANCEL_TALK : SH_ACT_NONE, SH_ACT_NONE);

    switch (sh_top_overlay(st, f)) {
    case SH_OV_ALARM:
        if (b == SH_BTN_KEY_DOWN) {
            st->key_owner = SH_KEY_ALARM;
            return r(SH_ACT_TO_APP, SH_ACT_SNOOZE_ALARM);
        }
        return r(SH_ACT_TO_APP, SH_ACT_STOP_ALARM);
    case SH_OV_NOTICE:
        return b == SH_BTN_BOOT ? r(SH_ACT_DISMISS_NOTICE, SH_ACT_NONE) : r(SH_ACT_NONE, SH_ACT_NONE);
    case SH_OV_NONE: break;
    }

    /* A hand on KEY or BOOT while Jerv answers is reaching for "stop", in every app: the reply
     * goes quiet and nothing else happens — no recording starts, and the release that follows
     * sends nothing (owner, 2026-10-12: "KEY or BOOT during playback stops it"). */
    if (f->replying && (b == SH_BTN_KEY_DOWN || b == SH_BTN_BOOT)) {
        if (b == SH_BTN_KEY_DOWN) st->key_owner = SH_KEY_FREE;
        return r(SH_ACT_STOP_REPLY, SH_ACT_NONE);
    }

    if (f->app_open) {
        if (b == SH_BTN_KEY_DOWN) {
            st->key_owner = SH_KEY_APP;
            return r(SH_ACT_TO_APP, SH_ACT_TALK_JERV);
        }
        return r(SH_ACT_TO_APP, SH_ACT_GO_HOME);
    }
    if (b == SH_BTN_KEY_DOWN) {
        st->key_owner = SH_KEY_SHELL;
        return r(SH_ACT_TALK_JERV, SH_ACT_NONE);
    }
    return r(f->page ? SH_ACT_PAGE_FIRST : SH_ACT_NONE, SH_ACT_NONE);
}

sh_act_t sh_declined(sh_state_t *st, sh_route_t rt)
{
    if (rt.fallback == SH_ACT_TALK_JERV) st->key_owner = SH_KEY_SHELL;
    return rt.fallback;
}

unsigned sh_route_event(const sh_facts_t *f, app_event_type_t type)
{
    (void)f;
    switch (type) {
    case APP_EV_ALARM_DUE: return SH_DO_WAKE | SH_DO_OPEN_ALARM_APP | SH_DO_DELIVER;
    /* Delivered too, so an app holding notices (the clock face) can redraw its badge. */
    case APP_EV_NOTICE_ARRIVED: return SH_DO_WAKE | SH_DO_REFRESH_OVERLAY | SH_DO_DELIVER;
    case APP_EV_MSG_ARRIVED:
    case APP_EV_NET_UP:
    case APP_EV_NET_DOWN:
    case APP_EV_TICK_MINUTE: return SH_DO_DELIVER | SH_DO_REFRESH_TILES;
    case APP_EV_WAKE: return SH_DO_DELIVER;
    /* Buttons have their own route, and an event queue carrying one is a caller's bug. */
    default: return 0;
    }
}

void sh_tile_pos(uint8_t slot, int *x, int *y)
{
    *x = UI_HOME_X + (slot % 2) * (UI_TILE_W + UI_TILE_GAP);
    *y = UI_HOME_Y + (slot / 2) * (UI_TILE_H + UI_TILE_GAP);
}

uint8_t sh_swipe(uint8_t page, int dx, uint8_t pages)
{
    if (dx <= -SH_SWIPE_PX && page + 1 < pages) return page + 1;
    if (dx >= SH_SWIPE_PX && page > 0) return page - 1;
    return page;
}

sh_stroke_t sh_stroke(int dx, int dy)
{
    const int ax = dx < 0 ? -dx : dx, ay = dy < 0 ? -dy : dy;
    if (ax < SH_TAP_SLOP && ay < SH_TAP_SLOP) return SH_STROKE_TAP;
    if (ax >= SH_SWIPE_PX && ax > ay) return SH_STROKE_SWIPE;
    return SH_STROKE_DRAG;
}

bool sh_gate(sh_gate_t *g, bool pressed, bool dark, bool *wake)
{
    *wake = false;
    if (!pressed) {
        g->down = false;
        return false;
    }
    if (!g->down) {
        g->down = true;
        g->swallow = dark;
        *wake = dark;
    }
    return !g->swallow;
}

bool sh_long_press(sh_lp_t *lp, bool pressed, int x, int y, uint32_t now_ms)
{
    if (!pressed) {
        lp->down = false;
        return false;
    }
    if (!lp->down) {
        *lp = (sh_lp_t){.down = true, .x0 = x, .y0 = y, .t0 = now_ms};
        return false;
    }
    if (lp->done) return false;
    const int dx = x - lp->x0, dy = y - lp->y0;
    if ((dx < 0 ? -dx : dx) >= SH_TAP_SLOP || (dy < 0 ? -dy : dy) >= SH_TAP_SLOP) {
        /* A scroll or a swipe: this stroke is never a long press, even if it comes back. */
        lp->done = true;
        return false;
    }
    if ((uint32_t)(now_ms - lp->t0) < SH_LONG_PRESS_MS) return false;
    lp->done = true;
    return true;
}

bool sh_menu_may_open(const sh_facts_t *f, bool conversation)
{
    return conversation && f->app_open && !f->dark && !f->talking && !f->menu_open &&
           !f->alarm_ringing && !(f->notices && !f->notices_held);
}

void sh_menu_open(sh_menu_t *m)
{
    *m = (sh_menu_t){.state = SH_MENU_OPEN};
}

bool sh_menu_armed(const sh_menu_t *m, uint32_t now_ms)
{
    return m->state == SH_MENU_OPEN && m->armed && (uint32_t)(now_ms - m->armed_ms) < SH_MENU_ARM_MS;
}

sh_menu_do_t sh_menu_input(sh_menu_t *m, sh_menu_in_t in, uint32_t now_ms)
{
    if (m->state != SH_MENU_OPEN) return SH_MENU_DO_NOTHING;
    if (in == SH_MENU_IN_CLEAR) {
        if (sh_menu_armed(m, now_ms)) {
            sh_menu_close(m);
            return SH_MENU_DO_CLEAR;
        }
        m->armed = true;
        m->armed_ms = now_ms;
        return SH_MENU_DO_ARM;
    }
    sh_menu_close(m);
    return SH_MENU_DO_CLOSE;
}

void sh_menu_close(sh_menu_t *m)
{
    *m = (sh_menu_t){.state = SH_MENU_CLOSED};
}

bool sh_registry_check(const sh_place_t *p, unsigned n, char *err, unsigned err_len)
{
    for (unsigned i = 0; i < n; i++) {
        if (!p[i].id || !p[i].id[0]) {
            snprintf(err, err_len, "app %u has no id", i);
            return false;
        }
        if (p[i].page >= SH_PAGES_MAX || p[i].slot >= SH_SLOTS_PER_PAGE) {
            snprintf(err, err_len, "%s is off the grid (page %u slot %u)", p[i].id, p[i].page,
                     p[i].slot);
            return false;
        }
        for (unsigned j = 0; j < i; j++) {
            if (!strcmp(p[i].id, p[j].id)) {
                snprintf(err, err_len, "two apps are called %s", p[i].id);
                return false;
            }
            if (p[i].page == p[j].page && p[i].slot == p[j].slot) {
                snprintf(err, err_len, "%s and %s share page %u slot %u", p[j].id, p[i].id,
                         p[i].page, p[i].slot);
                return false;
            }
        }
    }
    return true;
}

uint8_t sh_pages_used(const sh_place_t *p, unsigned n)
{
    uint8_t pages = 1;
    for (unsigned i = 0; i < n; i++)
        if (p[i].page + 1 > pages) pages = p[i].page + 1;
    return pages;
}
