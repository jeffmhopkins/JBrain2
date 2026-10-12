/* The shell's decisions, with no LVGL: which overlay is on top, where a button goes, what an
 * event does, where a tile sits. shell.c asks these and only executes the answer — it keeps no
 * second opinion of its own. That is the twins' lesson twice over: their arbitration was
 * extracted into ui.c and host-tested, and then display.c went on running its own copy, so
 * the tests tested a machine the panel did not run (JPANEL_MESSAGING_FIXES_PLAN.md B0). */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "app_event.h"

#define SH_SLOTS_PER_PAGE 4
#define SH_PAGES_MAX 4
#define SH_SWIPE_PX 60
/* How far a finger may wander and still be a tap: ~1.6 mm on this glass. LVGL's scroll start is
 * set to the same number, so "a tap" and "not yet a scroll" are one decision. */
#define SH_TAP_SLOP 20

/* SH_BTN_TOUCH is a finger landing on a dark screen (sh_gate), queued like a button so it keeps
 * its place among them. */
typedef enum { SH_BTN_KEY_DOWN, SH_BTN_KEY_UP, SH_BTN_BOOT, SH_BTN_PWR, SH_BTN_TOUCH } sh_button_t;

/* In priority order: an alarm outranks a notice, which outranks every app (plan §2). */
typedef enum { SH_OV_NONE, SH_OV_NOTICE, SH_OV_ALARM } sh_overlay_t;

typedef enum {
    SH_ACT_NONE,
    SH_ACT_TO_APP,         /* offer it to the open app; `fallback` runs if not consumed */
    SH_ACT_DISMISS_NOTICE,
    SH_ACT_SNOOZE_ALARM,
    SH_ACT_STOP_ALARM,
    SH_ACT_GO_HOME,
    SH_ACT_PAGE_FIRST,
    SH_ACT_HINT_TALK,      /* KEY on home: there is nobody to talk to yet */
    SH_ACT_WAKE,
    SH_ACT_DARK,
} sh_act_t;

typedef struct {
    sh_act_t first, fallback;
} sh_route_t;

/* The facts of this instant, gathered by shell.c from the services. */
typedef struct {
    bool alarm_ringing;
    uint8_t notices;
    bool app_open;
    uint8_t page;
    bool dark;
} sh_facts_t;

typedef enum { SH_KEY_FREE, SH_KEY_APP, SH_KEY_ALARM } sh_key_owner_t;

/* The only state the decisions need across calls. */
typedef struct {
    sh_key_owner_t key_owner; /* where the KEY_DOWN of a hold went */
} sh_state_t;

sh_overlay_t sh_top_overlay(const sh_state_t *st, const sh_facts_t *f);
/* KEY_UP always goes where its KEY_DOWN went, whatever arrived in between: a hold whose
 * release is swallowed by a pop-up is a microphone left open. */
sh_route_t sh_route_button(sh_state_t *st, const sh_facts_t *f, sh_button_t b);
/* The app is gone (closed, or the shell went home): its hold is over. */
void sh_app_closed(sh_state_t *st);

enum {
    SH_DO_DELIVER = 1 << 0,       /* to the open app's on_event */
    SH_DO_OPEN_ALARM_APP = 1 << 1,
    SH_DO_WAKE = 1 << 2,
    SH_DO_REFRESH_TILES = 1 << 3,
    SH_DO_REFRESH_OVERLAY = 1 << 4,
};
unsigned sh_route_event(const sh_facts_t *f, app_event_type_t type);

void sh_tile_pos(uint8_t slot, int *x, int *y);
uint8_t sh_swipe(uint8_t page, int dx, uint8_t pages);

/* What a finger did, from where it went down to where it came up. A swipe is mostly sideways,
 * so a scroll attempt on home turns no page; anything between a tap and a swipe does nothing,
 * so a short swipe never opens the tile it started on. */
typedef enum { SH_STROKE_TAP, SH_STROKE_SWIPE, SH_STROKE_DRAG } sh_stroke_t;
sh_stroke_t sh_stroke(int dx, int dy);

/* The dark-screen rule, applied to every pointer sample before LVGL sees it: a stroke that
 * starts on a dark screen wakes it and reaches nothing — not its press, its moves or its
 * release — however long the finger stays (the twins' rule: a finger on a dark screen buys the
 * screen, nothing else). A stroke that started lit is never cut, even if the screen goes dark
 * under it, so its release reaches what its press did. Returns whether LVGL may see this sample
 * as pressed; `*wake` is set on the sample that starts a swallowed stroke. */
typedef struct {
    bool down, swallow;
} sh_gate_t;
bool sh_gate(sh_gate_t *g, bool pressed, bool dark, bool *wake);

typedef struct {
    const char *id;
    uint8_t page, slot;
} sh_place_t;
/* False, with `err` naming the clash, if two apps share an id or a slot or one is off the grid. */
bool sh_registry_check(const sh_place_t *p, unsigned n, char *err, unsigned err_len);
uint8_t sh_pages_used(const sh_place_t *p, unsigned n);
