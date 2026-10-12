/* A conversation with one of the box's agents — Jerv, or Minecraft_Dave — as ONE parameterised
 * view: the thread's bubbles, hold-KEY-to-talk inside it, the reply's "Speaking…", and Clear chat.
 * Shared code, separate instances (owner, 2026-10-12: "I don't want Dave and Jerv to share the
 * same screen … they should be separate instances with separate history"): each app owns its own
 * convo_t and names its own thread, so two conversations never share a widget, a scroll position
 * or a message, and clearing one leaves the other alone.
 *
 * Below the apps and above ui_kit and the service interfaces, so an app may require it
 * (tools/check_app_deps.py) without one app ever reaching into another. */
#pragma once

#include <stdbool.h>
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

typedef struct {
    const convo_config_t *cfg;
    const app_host_t *host;
    lv_obj_t *root, *list;
    ui_talk_t talk;
    ui_pin_t pin;
    convo_work_t work;
    convo_sig_t drawn;
    char listening[40];
} convo_t;

/* Builds the conversation under `root`: the header (its back arrow reaching `on_back` with
 * `back_user`), the thread and the "Hold KEY to talk" footer. */
void convo_open(convo_t *c, const convo_config_t *cfg, const app_host_t *host, lv_obj_t *root,
                lv_event_cb_t on_back, void *back_user);
/* Lets go of a hold in progress. The widgets are the caller's root's, freed with it. */
void convo_close(convo_t *c);
/* Draws what changed since the last draw (convo_redraw): the whole list, or only the working view
 * where it stands. */
void convo_refresh(convo_t *c);
/* KEY down/up talk to this conversation's agent; BOOT cancels a hold in progress; a message on
 * this thread redraws it. True when consumed. */
bool convo_event(convo_t *c, const app_event_t *ev);
/* Clear chat: the thread on this panel, and a fresh conversation for the agent's next talk on the
 * box. A reply in flight is stopped first, so it cannot land in the emptied thread. */
void convo_clear(convo_t *c);
void convo_describe(const convo_t *c, dbg_json_t *w);
