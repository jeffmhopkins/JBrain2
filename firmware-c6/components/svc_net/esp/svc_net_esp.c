/* The Wi-Fi card's window onto the radio. The network name and signal are read from the
 * driver when asked, so the card shows the access point the panel is on now, not the one it was
 * provisioned for.
 *
 * Disconnect acts at once — esp_wifi_disconnect may be called from any task, and the flag it
 * raises first is what stops main/net.c's handler re-joining — while Connect is carried to the
 * update loop, because a join blocks for up to 30 s and the UI task must not. */

#include <stdio.h>
#include <string.h>

#include "app_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "svc_debug.h"
#include "svc_net.h"
#include "svc_net_drv.h"

static void (*s_wake)(void);
static volatile net_state_t s_state = NET_CONNECTING;
static volatile bool s_user_off, s_connect;
static uint16_t s_reconnects, s_user_offs;

static const char *const STATE[] = {"off", "connecting", "connected"};

void svc_net_info(net_info_t *out)
{
    memset(out, 0, sizeof *out);
    out->state = s_state;
    out->reconnects = s_reconnects;
    wifi_ap_record_t ap;
    if (out->state == NET_CONNECTED && esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
        snprintf(out->ssid, sizeof out->ssid, "%s", (const char *)ap.ssid);
        out->rssi = ap.rssi;
    } else {
        /* Not associated: the network it will try is the one the box provisioned. */
        wifi_config_t wc;
        if (esp_wifi_get_config(WIFI_IF_STA, &wc) == ESP_OK)
            snprintf(out->ssid, sizeof out->ssid, "%s", (const char *)wc.sta.ssid);
    }
    esp_netif_t *nif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    esp_netif_ip_info_t ip;
    if (out->state == NET_CONNECTED && nif && esp_netif_get_ip_info(nif, &ip) == ESP_OK)
        snprintf(out->ip, sizeof out->ip, IPSTR, IP2STR(&ip.ip));
}

void svc_net_disconnect(void)
{
    s_user_off = true;
    s_user_offs++;
    esp_wifi_disconnect();
    svc_net_set_state(NET_OFF);
}

void svc_net_connect(void)
{
    if (!s_user_off && s_state != NET_OFF) return;
    s_user_off = false;
    s_connect = true;
    svc_net_set_state(NET_CONNECTING);
    if (s_wake) s_wake();
}

void svc_net_set_state(net_state_t state)
{
    /* A late disconnect event, or a join that was already under way, must not show Connected
       or Connecting to an owner who switched the radio off. */
    if (s_user_off && state != NET_OFF) return;
    const net_state_t was = s_state;
    s_state = state;
    if (state == NET_CONNECTED && was != NET_CONNECTED) {
        s_reconnects++;
        app_events_post_type(APP_EV_NET_UP);
    } else if (state != NET_CONNECTED && was == NET_CONNECTED) {
        app_events_post_type(APP_EV_NET_DOWN);
    }
}

bool svc_net_user_off(void)
{
    return s_user_off;
}

bool svc_net_take_connect(void)
{
    const bool c = s_connect;
    s_connect = false;
    return c;
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    net_info_t n;
    svc_net_info(&n);
    dbg_json_str(w, "state", STATE[n.state]);
    dbg_json_str(w, "ssid", n.ssid);
    dbg_json_int(w, "rssi", n.rssi);
    dbg_json_str(w, "ip", n.ip);
    dbg_json_int(w, "reconnects", n.reconnects);
    dbg_json_bool(w, "user_off", s_user_off);
    dbg_json_int(w, "user_offs", s_user_offs);
}

void svc_net_start(void (*wake)(void))
{
    s_wake = wake;
    svc_debug_register("net", describe, NULL);
}
