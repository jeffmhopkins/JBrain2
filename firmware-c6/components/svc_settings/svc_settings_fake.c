#include "svc_settings.h"

#include "svc_debug.h"
#include "svc_settings_fake.h"

static const char *const NAMES[SET__COUNT] = {
    "brightness", "tts", "notif_accept", "notif_quiet", "notif_chime",
    "night_on", "night_color", "night_from", "night_to",
};

static int32_t s_val[SET__COUNT] = {80, 1, 1, 1, 1, 1, 0, 21 * 60 + 30, 6 * 60 + 30};

/* The `jbrain` NVS namespace the box's flasher writes (README "How it talks to the box"). */
static const struct {
    const char *key, *value;
} PROV[] = {
    {"ssid", "[Your network]"}, {"pass", SETTINGS_FAKE_PASS}, {"api", "https://box.lan/api"},
    {"token", SETTINGS_FAKE_TOKEN}, {"ca", SETTINGS_FAKE_CA}, {"name", "Jeff's panel"},
    {"role", "owner"},
};

int32_t svc_settings_clamp(setting_t key, int32_t v)
{
    switch (key) {
    case SET_BRIGHTNESS: return v < BRIGHTNESS_FLOOR ? BRIGHTNESS_FLOOR : v > BRIGHTNESS_MAX ? BRIGHTNESS_MAX : v;
    case SET_NIGHT_COLOR: return v < 0 ? 0 : v > 2 ? 2 : v;
    case SET_NIGHT_FROM:
    case SET_NIGHT_TO: return v < 0 ? 0 : v > 1439 ? 1439 : v;
    default: return v ? 1 : 0;
    }
}

int32_t svc_settings_get(setting_t key)
{
    return key < SET__COUNT ? s_val[key] : 0;
}

void svc_settings_set(setting_t key, int32_t value)
{
    if (key < SET__COUNT) s_val[key] = svc_settings_clamp(key, value);
}

const char *svc_settings_name(setting_t key)
{
    return key < SET__COUNT ? NAMES[key] : "";
}

void svc_settings_each_provisioning(settings_kv_fn fn, void *ctx)
{
    for (unsigned i = 0; i < sizeof PROV / sizeof *PROV; i++) fn(PROV[i].key, PROV[i].value, ctx);
}

static void prov_kv(const char *key, const char *value, void *ctx)
{
    dbg_json_str((dbg_json_t *)ctx, key, value);
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    for (int i = 0; i < SET__COUNT; i++) dbg_json_int(w, NAMES[i], s_val[i]);
    dbg_json_obj_begin(w, "provisioning");
    svc_settings_each_provisioning(prov_kv, w);
    dbg_json_obj_end(w);
}

void svc_settings_init(void)
{
    /* Value-scrubbing as well as the key deny-list: a secret that reaches a log line or a
     * differently named field is still caught. */
    for (unsigned i = 0; i < sizeof PROV / sizeof *PROV; i++)
        if (dbg_json_key_is_secret(PROV[i].key)) dbg_secret_add(PROV[i].value);
    svc_debug_register("settings", describe, NULL);
}
