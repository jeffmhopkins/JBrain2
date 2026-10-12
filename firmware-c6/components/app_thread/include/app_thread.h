#pragma once

#include "app_api.h"

/* One component, one instance per sibling panel: a voice thread with whoever the box says sits in
 * `slot` (svc_siblings), shown by the name the owner gave that panel — never one compiled in.
 * Until the box has answered (and nothing is cached) the thread is "Panel <slot+1>". */
typedef struct {
    const char *thread; /* the svc_messages thread, and the app id */
    uint8_t slot;       /* which sibling: svc_siblings_slot */
} app_thread_config_t;

#define APP_THREAD_INSTANCES 4

/* Fills `app` as an instance of Thread; `cfg` must outlive it. False past the instance cap. */
bool app_thread_make(app_t *app, const app_thread_config_t *cfg, uint8_t page, uint8_t slot);
