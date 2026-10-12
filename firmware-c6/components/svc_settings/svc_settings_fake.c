#include "settings_store.h"
#include "svc_debug.h"
#include "svc_settings.h"
#include "svc_settings_fake.h"

/* Never written anywhere, so nothing is ever owed: the store keeps the clamps in one place. */
static settings_store_t s_store;
static bool s_ready;

/* The `jbrain` NVS namespace the box's flasher writes (README "How it talks to the box"). */
static const struct {
    const char *key, *value;
} PROV[] = {
    {"ssid", "[Your network]"}, {"pass", SETTINGS_FAKE_PASS}, {"api", "https://box.lan/api"},
    {"token", SETTINGS_FAKE_TOKEN}, {"ca", SETTINGS_FAKE_CA}, {"name", "Jeff's panel"},
    {"role", "owner"},
};

static settings_store_t *store(void)
{
    if (!s_ready) {
        settings_store_defaults(&s_store);
        s_ready = true;
    }
    return &s_store;
}

int32_t svc_settings_get(setting_t key)
{
    return key < SET__COUNT ? store()->val[key] : 0;
}

void svc_settings_set(setting_t key, int32_t value)
{
    settings_store_set(store(), key, value, 0);
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
    for (int i = 0; i < SET__COUNT; i++) dbg_json_int(w, svc_settings_name((setting_t)i), svc_settings_get((setting_t)i));
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
