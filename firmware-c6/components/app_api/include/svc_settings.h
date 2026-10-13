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
    SET_AUTO_ROTATE,  /* turn the content with the panel (0.0.18, svc_orient.h); off is upright */
    SET_TZ,           /* index into svc_rtc.h's zone table */
    SET_VOLUME,       /* the speaker, 10-100: off is "Read replies aloud" off, not a zero */
    SET_MIC_GAIN,     /* ES7210 PGA in dB, 0-37; no slider, set from the debug `set` command */
    SET_IMU_AXES,     /* how the QMI8658 sits against the glass, 0-7 (orient_logic.h); debug `set` */
    SET__COUNT,
} setting_t;

#define BRIGHTNESS_FLOOR 10
#define BRIGHTNESS_MAX 100
#define VOLUME_FLOOR 10
#define VOLUME_MAX 100
/* The ES7210's PGA steps stop at 37.5 dB (esp_codec_dev es7210.c). */
#define MIC_GAIN_MAX 37
/* Three bits: swap the chip's X and Y, flip the glass's x, flip its y (orient_logic.h). */
#define IMU_AXES_MAX 7

int32_t svc_settings_get(setting_t key);
void svc_settings_set(setting_t key, int32_t value);
int32_t svc_settings_clamp(setting_t key, int32_t value);
int32_t svc_settings_default(setting_t key);
const char *svc_settings_name(setting_t key);

/* Provisioning as the box's flasher wrote it (ssid, api, name … and the secrets). Visited,
 * never returned, so the only way to print one is through dbg_json, which redacts. */
typedef void (*settings_kv_fn)(const char *key, const char *value, void *ctx);
void svc_settings_each_provisioning(settings_kv_fn fn, void *ctx);
