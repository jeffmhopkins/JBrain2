/* The shell's decisions, with no LVGL: which overlay is on top, where a button goes, what an
 * event does, where a tile sits. shell.c asks these and only executes the answer — it keeps no
 * second opinion of its own. That is the twins' lesson twice over: their arbitration was
 * extracted into ui.c and host-tested, and then display.c went on running its own copy, so
 * the tests tested a machine the panel did not run (JPANEL_MESSAGING_FIXES_PLAN.md B0). */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "app_event.h"
#include "ui_hold.h"

#define SH_SLOTS_PER_PAGE 4
#define SH_PAGES_MAX 4
#define SH_SWIPE_PX 60
/* How far a finger may wander and still be a tap: ~1.6 mm on this glass. LVGL's scroll start is
 * set to the same number, so "a tap" and "not yet a scroll" are one decision. */
#define SH_TAP_SLOP UI_HOLD_SLOP

/* SH_BTN_TOUCH is a finger landing on a dark screen (sh_gate), queued like a button so it keeps
 * its place among them. SH_BTN_BOOT is a short press (it arrives on release); SH_BTN_BOOT_LONG a
 * hold past svc_buttons' threshold, which opens the Clock from anywhere (owner, 2026-10-12). */
typedef enum {
    SH_BTN_KEY_DOWN,
    SH_BTN_KEY_UP,
    SH_BTN_BOOT,
    SH_BTN_PWR,
    SH_BTN_TOUCH,
    SH_BTN_BOOT_LONG,
} sh_button_t;

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
    SH_ACT_TALK_JERV,      /* KEY outside a conversation: the shell's own talk to Jerv */
    SH_ACT_SEND_JERV,      /* its release: send, then open the Jerv thread */
    SH_ACT_CANCEL_TALK,    /* BOOT during it */
    SH_ACT_WAKE,
    SH_ACT_DARK,
    SH_ACT_OPEN_CLOCK,     /* BOOT held: the clock app, over home or any app */
    SH_ACT_STOP_REPLY,     /* KEY or BOOT while Jerv speaks: the voice stops */
    SH_ACT_STILL_WORKING,  /* KEY while Jerv is still working on the last one: say so, record nothing */
    SH_ACT_CLOSE_MENU,     /* KEY or BOOT over the conversation menu: close it, nothing else */
} sh_act_t;

typedef struct {
    sh_act_t first, fallback;
} sh_route_t;

/* The facts of this instant, gathered by shell.c from the services. */
typedef struct {
    bool alarm_ringing;
    uint8_t notices;
    bool notices_held; /* the open app keeps them as a badge: the clock face (plan §2) */
    bool app_open;
    uint8_t page;
    bool dark;
    bool talking;      /* the shell's own talk to Jerv is up */
    bool clock_open;   /* the open app is the clock */
    bool replying;     /* Jerv is thinking about, or speaking, an answer */
    bool speaking;     /* ... speaking it */
    bool menu_open;    /* the conversation menu (long press) is up */
} sh_facts_t;

/* SH_KEY_SHELL: the hold is the shell's talk to Jerv, so its release sends to Jerv whatever
 * opened or closed in between. */
typedef enum { SH_KEY_FREE, SH_KEY_APP, SH_KEY_ALARM, SH_KEY_SHELL } sh_key_owner_t;

/* The only state the decisions need across calls. */
typedef struct {
    sh_key_owner_t key_owner; /* where the KEY_DOWN of a hold went */
} sh_state_t;

sh_overlay_t sh_top_overlay(const sh_state_t *st, const sh_facts_t *f);
/* KEY_UP always goes where its KEY_DOWN went, whatever arrived in between: a hold whose
 * release is swallowed by a pop-up is a microphone left open. */
sh_route_t sh_route_button(sh_state_t *st, const sh_facts_t *f, sh_button_t b);
/* The open app was offered `r.first` and did not consume it: the fallback to run. KEY held in an
 * app that is not a conversation is Jerv's (owner, 2026-10-12), and the shell takes the hold. */
sh_act_t sh_declined(sh_state_t *st, sh_route_t r);
/* The app is gone (closed, or the shell went home): its hold is over. */
void sh_app_closed(sh_state_t *st);

enum {
    SH_DO_DELIVER = 1 << 0,       /* to the open app's on_event */
    SH_DO_OPEN_ALARM_APP = 1 << 1,
    SH_DO_WAKE = 1 << 2,
    SH_DO_REFRESH_TILES = 1 << 3,
    SH_DO_REFRESH_OVERLAY = 1 << 4,
    SH_DO_ALARM_TAKES_OVER = 1 << 5, /* end the talk, the menu and the voice; light the glass */
    SH_DO_ALARM_GIVES_BACK = 1 << 6, /* the owner's brightness again */
};
/* The least a ringing alarm lights the glass at: readable from the pillow, not the full 100 %
 * of a panel turned down for the night (owner's brightness kept when it is higher). */
#define ALARM_WAKE_BRIGHTNESS 40
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

/* ---- the conversation menu (owner, 2026-10-12: "a long press in a chat window pulls up a
 * menu") ----
 *
 * A finger held still on a conversation for SH_LONG_PRESS_MS opens a small menu: Clear chat,
 * Cancel. "Still" is the tap's own slop, so a scroll — which moves past it first — never opens
 * it, and a long press is decided while the finger is still down, once. The rule itself is
 * ui_hold's (ui_kit), which the Clock face's two-second hold uses too. */
#define SH_LONG_PRESS_MS 600
typedef ui_hold_t sh_lp_t;
/* Every pointer sample: true exactly once per stroke, on the sample that crosses the threshold
 * with the finger still within SH_TAP_SLOP of where it landed. */
bool sh_long_press(sh_lp_t *lp, bool pressed, int x, int y, uint32_t now_ms);

/* Whether a long press opens the menu now: only over a conversation that can be cleared, and
 * only when nothing is covering it (a dark screen, a hold, an overlay, the menu itself). */
bool sh_menu_may_open(const sh_facts_t *f, bool conversation);

/* Clear chat takes two taps, no more than SH_MENU_ARM_MS apart — the Wi-Fi Disconnect pattern:
 * the first arms it ("Tap again to clear"), the second clears. A history is not coming back. */
#define SH_MENU_ARM_MS 3000
typedef enum { SH_MENU_CLOSED, SH_MENU_OPEN } sh_menu_state_t;
typedef struct {
    sh_menu_state_t state;
    bool armed;
    uint32_t armed_ms;
} sh_menu_t;
typedef enum {
    SH_MENU_IN_CLEAR,   /* the Clear chat button */
    SH_MENU_IN_CANCEL,  /* the Cancel button */
    SH_MENU_IN_OUTSIDE, /* a tap on the dim around the card */
} sh_menu_in_t;
typedef enum {
    SH_MENU_DO_NOTHING,
    SH_MENU_DO_ARM,   /* redraw: "Tap again to clear" */
    SH_MENU_DO_CLEAR, /* clear the thread, close */
    SH_MENU_DO_CLOSE,
} sh_menu_do_t;
void sh_menu_open(sh_menu_t *m);
sh_menu_do_t sh_menu_input(sh_menu_t *m, sh_menu_in_t in, uint32_t now_ms);
/* Armed and not yet lapsed: what the Clear button says. */
bool sh_menu_armed(const sh_menu_t *m, uint32_t now_ms);
void sh_menu_close(sh_menu_t *m);

typedef struct {
    const char *id;
    uint8_t page, slot;
} sh_place_t;
/* False, with `err` naming the clash, if two apps share an id or a slot or one is off the grid. */
bool sh_registry_check(const sh_place_t *p, unsigned n, char *err, unsigned err_len);
uint8_t sh_pages_used(const sh_place_t *p, unsigned n);
