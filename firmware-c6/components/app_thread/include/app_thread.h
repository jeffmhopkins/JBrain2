#pragma once

#include "app_api.h"

/* One component, one instance per person: a voice thread with someone, who is named only by
 * this config. The names live where the instances are declared, never in here. */
typedef struct {
    const char *principal; /* the svc_messages thread, and the app id */
    const char *name;      /* shown */
    ui_icon_t icon;
} app_thread_config_t;

#define APP_THREAD_INSTANCES 4

/* Fills `app` as an instance of Thread; `cfg` must outlive it. False past the instance cap. */
bool app_thread_make(app_t *app, const app_thread_config_t *cfg, ui_accent_t accent,
                     uint8_t page, uint8_t slot);
