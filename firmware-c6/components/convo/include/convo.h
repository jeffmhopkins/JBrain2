/* A conversation with one of the box's agents — Jerv, or Minecraft_Dave — as ONE parameterised
 * view: the thread's bubbles under the conversation's title, hold-KEY-to-talk inside it, the
 * reply's "Speaking…", and (0.0.15) New conversation and the Recent list that switches between
 * the agent's conversations on the box. Shared code, separate instances (owner, 2026-10-12: "I
 * don't want Dave and Jerv to share the same screen … they should be separate instances with
 * separate history"): each app owns its own convo_t and names its own thread, so two
 * conversations never share a widget, a scroll position or a message, and a new conversation
 * with one leaves the other alone.
 *
 * Below the apps and above ui_kit and the service interfaces, so an app may require it
 * (tools/check_app_deps.py) without one app ever reaching into another. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "app_api.h"
#include "convo_model.h"
#include "svc_audio.h"
#include "ui_kit.h"
#include "ui_pin.h"

typedef struct {
    const char *thread; /* the svc_messages thread and the svc_audio agent: "jerv" or "dave" */
    const char *title;  /* the header, and the ring's "<title> is listening" */
    const ui_icon_t *icon;
    ui_accent_t accent;
    const char *empty; /* what an empty thread says */
} convo_config_t;

/* The working view under the waiting question: made once per question and then changed in place,
 * its spinner moved between rows rather than remade. Its widgets are the list's; `tick` (the
 * clock's second) goes with them. All NULL while there is none. */
typedef struct {
    lv_obj_t *root, *fold, *phase_row, *phase, *spin;
    lv_obj_t *mark[WORK_SHOWN], *text[WORK_SHOWN];
    lv_timer_t *tick;
} convo_work_t;

/* Recent, over the thread while it is open: its own screen, freed when it closes. */
typedef struct {
    rc_t rc;
    lv_obj_t *root, *page, *fade;
    rc_in_t later; /* a tap's input, acted on after the click that made it */
    int later_arg;
} convo_recent_t;

typedef struct {
    const convo_config_t *cfg;
    const app_host_t *host;
    lv_obj_t *root, *list, *sub;
    ui_talk_t talk;
    ui_pin_t pin;
    convo_work_t work;
    convo_sig_t drawn;
    convo_recent_t recent;
    char listening[40];
} convo_t;

/* Builds the conversation under `root`: the header with the conversation's title (its back
 * arrow reaching `on_back` with `back_user`), the thread and the "Hold KEY to talk" footer, and
 * asks the box for the title. */
void convo_open(convo_t *c, const convo_config_t *cfg, const app_host_t *host, lv_obj_t *root,
                lv_event_cb_t on_back, void *back_user);
/* Lets go of a hold in progress and of Recent's list. The widgets are the caller's root's,
 * freed with it. */
void convo_close(convo_t *c);
/* Draws what changed since the last draw (convo_redraw): the whole list, or only the working view
 * where it stands; the header's title in place. */
void convo_refresh(convo_t *c);
/* KEY down/up talk to this conversation's agent; BOOT cancels a hold in progress, or leaves
 * Recent; a message on this thread redraws it. True when consumed. */
bool convo_event(convo_t *c, const app_event_t *ev);
/* The long-press menu's line, "Jerv · Shopping list": false while the thread is not what is on
 * screen (Recent is open, a hold is up). */
bool convo_menu(const convo_t *c, char *sub, size_t cap, ui_accent_t *accent);
/* New conversation: the thread on this panel empties, the agent's next talk asks the box for a
 * new conversation, and the old one stays in Recent and the app. A reply in flight is stopped
 * first, so it cannot land in the emptied thread. Returns the toast: nothing happens, and it says
 * so, when the thread already is a new, empty one. */
const char *convo_new(convo_t *c);
/* Opens Recent over the thread and asks the box for the list. */
void convo_recent(convo_t *c);
void convo_describe(const convo_t *c, dbg_json_t *w);

/* ---- convo_recent.c: the Recent screen, the convo's own ---- */
void convo_recent_open(convo_t *c);
void convo_recent_close(convo_t *c);
bool convo_recent_up(const convo_t *c);
/* KEY or BOOT while Recent is open. True when consumed; false for a KEY that closed it and is
 * to talk in the thread now. */
bool convo_recent_button(convo_t *c, app_event_type_t type);
/* The box answered (the list, or a switch). */
void convo_recent_update(convo_t *c);
