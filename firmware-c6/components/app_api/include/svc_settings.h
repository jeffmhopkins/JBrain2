/* Panel settings. The service clamps every write, so a floor holds whoever writes — the
 * glass, the box, or a debug command — and it is the one writer of their saved copy. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    SET_BRIGHTNESS,   /* 10-100: zero would look exactly like the blanking fault */
    SET_TTS,          /* read Jerv's replies aloud */
    SET_NOTIF_ACCEPT, /* accept messages from the box */
    SET_NOTIF_QUIET,  /* refuse them during night hours */
    SET_NOTIF_CHIME,
    SET_NIGHT_ON,
    SET_NIGHT_COLOR,  /* 0 red, 1 amber, 2 grey */
    SET_NIGHT_FROM,   /* minute of day; the same window is the notice quiet hours */
    SET_NIGHT_TO,
    SET_SHOW_DATE,    /* the day and date under the clock face */
    SET_AUTO_ROTATE,  /* read by the auto-rotate wave (plan §2b); stored now so it survives */
    SET_TZ,           /* index into svc_rtc.h's zone table */
    SET__COUNT,
} setting_t;

#define BRIGHTNESS_FLOOR 10
#define BRIGHTNESS_MAX 100

int32_t svc_settings_get(setting_t key);
void svc_settings_set(setting_t key, int32_t value);
int32_t svc_settings_clamp(setting_t key, int32_t value);
int32_t svc_settings_default(setting_t key);
const char *svc_settings_name(setting_t key);

/* Provisioning as the box's flasher wrote it (ssid, api, name … and the secrets). Visited,
 * never returned, so the only way to print one is through dbg_json, which redacts. */
typedef void (*settings_kv_fn)(const char *key, const char *value, void *ctx);
void svc_settings_each_provisioning(settings_kv_fn fn, void *ctx);
