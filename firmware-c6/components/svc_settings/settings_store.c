#include "settings_store.h"

#include "svc_rtc.h"

/* Also the NVS keys, so each must stay within NVS's 15 characters and never be renamed: a
 * renamed key reads back as its default after the update. */
static const char *const NAMES[SET__COUNT] = {
    "brightness", "tts",       "notif_accept", "notif_quiet", "notif_chime", "night_on",
    "night_color", "night_from", "night_to",   "show_date",   "auto_rotate", "tz",
    "volume",      "mic_gain",
};

/* Brightness 50: what 0.0.4 settled the glass at and the owner has lived with since. Volume
 * 80 and 30 dB of microphone gain: the middle of what the vendor's demos use on this board
 * (HARDWARE.md §5), to be moved from what the owner hears and the snapshot's mic peaks. */
static const int32_t DEFAULTS[SET__COUNT] = {
    50, 1, 1, 1, 1, 1, 0, 21 * 60 + 30, 6 * 60 + 30, 1, 1, TZ_DEFAULT, 80, 30,
};

int32_t svc_settings_clamp(setting_t key, int32_t v)
{
    switch (key) {
    case SET_BRIGHTNESS: return v < BRIGHTNESS_FLOOR ? BRIGHTNESS_FLOOR : v > BRIGHTNESS_MAX ? BRIGHTNESS_MAX : v;
    case SET_NIGHT_COLOR: return v < 0 ? 0 : v > 2 ? 2 : v;
    case SET_NIGHT_FROM:
    case SET_NIGHT_TO: return v < 0 ? 0 : v > 1439 ? 1439 : v;
    case SET_TZ: return v < 0 || v >= TZ_ZONES ? TZ_DEFAULT : v;
    case SET_VOLUME: return v < VOLUME_FLOOR ? VOLUME_FLOOR : v > VOLUME_MAX ? VOLUME_MAX : v;
    case SET_MIC_GAIN: return v < 0 ? 0 : v > MIC_GAIN_MAX ? MIC_GAIN_MAX : v;
    default: return v ? 1 : 0;
    }
}

int32_t svc_settings_default(setting_t key)
{
    return key < SET__COUNT ? DEFAULTS[key] : 0;
}

const char *svc_settings_name(setting_t key)
{
    return key < SET__COUNT ? NAMES[key] : "";
}

void settings_store_defaults(settings_store_t *s)
{
    for (int k = 0; k < SET__COUNT; k++) s->val[k] = s->saved[k] = DEFAULTS[k];
    s->changed_ms = 0;
}

void settings_store_loaded(settings_store_t *s, setting_t key, int32_t raw)
{
    if (key >= SET__COUNT) return;
    s->val[key] = svc_settings_clamp(key, raw);
    /* Flash holds the raw value; a clamped one differs and is written back once. */
    s->saved[key] = raw;
}

bool settings_store_set(settings_store_t *s, setting_t key, int32_t value, uint32_t now_ms)
{
    if (key >= SET__COUNT) return false;
    const int32_t v = svc_settings_clamp(key, value);
    if (v == s->val[key]) return false;
    s->val[key] = v;
    s->changed_ms = now_ms;
    return true;
}

uint32_t settings_store_due(const settings_store_t *s, uint32_t now_ms)
{
    uint32_t mask = 0;
    for (int k = 0; k < SET__COUNT; k++)
        if (s->val[k] != s->saved[k]) mask |= 1u << k;
    if (mask && (uint32_t)(now_ms - s->changed_ms) < SETTINGS_QUIET_MS) return 0;
    return mask;
}

void settings_store_mark_saved(settings_store_t *s, setting_t key)
{
    if (key < SET__COUNT) s->saved[key] = s->val[key];
}
