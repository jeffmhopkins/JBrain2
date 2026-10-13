/* The panel's settings from the PWA (0.0.21, owner-approved mock 2026-10-13). Pure C, no
 * allocation, host-tested.
 *
 * THE PANEL IS THE SOURCE OF TRUTH. Every debug pickup reports the settings the PWA shows
 * (settings_sync_query, beside the notice switches' `na nq nc nf nt tz`); a change made in the PWA
 * waits on the box, and the pickup's answer says so (`X-Panel-Settings: <version>`). The panel
 * then fetches it (`GET /endpoint/panel-settings`: `{"v":7,"s":{"br":[60,50],…}}` — each value and
 * the one it was asked over) and applies each through svc_settings, which clamps as for every
 * writer — but only while it still holds that old value: a change made at the glass meanwhile
 * wins (owner, 2026-10-13: "the panel's last report wins"), and the next report tells the box. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "svc_settings.h"

/* Every setting the PWA may change (backend `panel_settings.SETTINGS`). */
#define SYNC_MAX 13
/* The panel-settings answer: thirteen `"xx":[nnnn,nnnn]` and the version, with room. */
#define SYNC_BODY_MAX 512
#define SYNC_PATH "/endpoint/panel-settings"
/* A request still signalled after it was fetched (the apply and the next report cross) is
 * fetched again no sooner than this. */
#define SYNC_REFETCH_MS 15000

typedef struct {
    uint8_t key; /* setting_t */
    int32_t value, base;
} sync_change_t;

typedef struct {
    uint32_t version;
    uint8_t n;
    sync_change_t c[SYNC_MAX];
} sync_pending_t;

/* `&no=1&nk=0&sd=1&br=50&vo=80&ra=1&ar=1`: the settings the notice switches do not already carry.
 * Sent with them or alone (a panel that cannot show notices reports only these). */
void settings_sync_query(int32_t (*get)(setting_t), char *out, size_t cap);
/* The pickup's `X-Panel-Settings`: the queue's version, 0 for none or anything not a number. */
uint32_t settings_sync_header(const char *header);
/* Whether to fetch: a version is signalled that was not fetched, or fetched SYNC_REFETCH_MS ago. */
bool settings_sync_due(uint32_t signalled, uint32_t fetched, uint32_t since_fetch_ms);
/* `{"v":N,"s":{"xx":[value,base],…}}`. A short name this panel does not take is skipped. False on
 * anything that is not that shape. */
bool settings_sync_parse(const char *json, size_t len, sync_pending_t *out);
/* Applies what it may: each change whose setting still holds its `base`, through `set` (which
 * clamps). Returns a mask of the settings changed (bit = setting_t). */
uint32_t settings_sync_apply(const sync_pending_t *p, int32_t (*get)(setting_t),
                             void (*set)(setting_t, int32_t));
