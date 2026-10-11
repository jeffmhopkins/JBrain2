/* The app contract (JPANEL_C6_PLAN.md §1). The shell knows an app only through its app_t, and
 * an app knows the rest of the panel only through this header, ui_kit and the svc_*.h
 * interfaces beside it. tools/check_app_deps.py fails CI if an app reaches past them. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "app_event.h"
#include "dbg_json.h"
#include "lvgl.h"
#include "ui_theme.h"

typedef struct app app_t;

/* What an app may ask of the shell. Injected at registration rather than declared as shell
 * functions so an app links without the shell (sim subsets, host tests) and the dependency
 * keeps pointing down. */
typedef struct {
    void (*go_home)(void);
    void (*toast)(const char *text);
} app_host_t;

struct app {
    const char *id;        /* "jerv", "clock" … stable: telemetry and settings keys */
    const char *title;
    const ui_icon_t *icon; /* tile glyph, from ui_kit */
    ui_accent_t accent;
    uint8_t page, slot;    /* where its tile sits on the home pages */
    const void *config;    /* per-instance: Thread gets {principal, name} */

    void (*open)(app_t *self, lv_obj_t *root);
    void (*close)(app_t *self);
    bool (*on_event)(app_t *self, const app_event_t *ev); /* true = consumed */
    void (*tile_state)(app_t *self, ui_tile_state_t *out);
    /* Optional. Writes the open app's state into a debug snapshot. Called only while open. */
    void (*describe)(app_t *self, dbg_json_t *w);

    /* Set by shell_register(); never by the app. */
    const app_host_t *host;
    /* The instance's runtime state while open. Static per component would merge the two Thread
     * instances into one. */
    void *state;
};
