#include <string.h>

#include "app_event.h"
#include "svc_debug.h"
#include "svc_net_fake.h"

static net_info_t s_info = {NET_CONNECTED, "[Your network]", -58, "192.168.1.50", 0};

static const char *const STATE[] = {"off", "connecting", "connected"};

void svc_net_info(net_info_t *out)
{
    *out = s_info;
}

void svc_net_fake_set(net_state_t state)
{
    net_state_t was = s_info.state;
    s_info.state = state;
    if (state == NET_CONNECTED && was != NET_CONNECTED) {
        s_info.reconnects++;
        app_events_post_type(APP_EV_NET_UP);
    } else if (state != NET_CONNECTED && was == NET_CONNECTED) {
        app_events_post_type(APP_EV_NET_DOWN);
    }
}

void svc_net_connect(void)
{
    if (s_info.state == NET_OFF) svc_net_fake_set(NET_CONNECTING);
}

void svc_net_disconnect(void)
{
    svc_net_fake_set(NET_OFF);
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    dbg_json_str(w, "state", STATE[s_info.state]);
    dbg_json_str(w, "ssid", s_info.ssid);
    dbg_json_int(w, "rssi", s_info.rssi);
    dbg_json_str(w, "ip", s_info.state == NET_CONNECTED ? s_info.ip : "");
    dbg_json_int(w, "reconnects", s_info.reconnects);
}

void svc_net_init(void)
{
    svc_debug_register("net", describe, NULL);
}
