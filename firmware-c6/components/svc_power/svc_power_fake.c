#include "svc_debug.h"
#include "svc_power_fake.h"

static power_info_t s_info = {.fitted = true, .usb = true, .charging = true, .pct = 87, .mv = 4110,
                              .usb_mv = 5020};

void svc_power_info(power_info_t *out)
{
    *out = s_info;
}

void svc_power_fake_set(const power_info_t *info)
{
    s_info = *info;
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    dbg_json_bool(w, "battery", s_info.fitted);
    dbg_json_int(w, "pct", s_info.pct);
    dbg_json_int(w, "mv", s_info.mv);
    dbg_json_bool(w, "usb", s_info.usb);
    dbg_json_bool(w, "charging", s_info.charging);
    dbg_json_str(w, "driver", "fake");
}

void svc_power_init(void)
{
    svc_debug_register("power", describe, NULL);
}
