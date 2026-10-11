/* Wi-Fi. The network is the one the box wrote at flash time; there is no SSID or password
 * entry on the glass, so the only controls are connect and disconnect. */
#pragma once

#include <stdint.h>

typedef enum { NET_OFF, NET_CONNECTING, NET_CONNECTED } net_state_t;

typedef struct {
    net_state_t state;
    char ssid[33];
    int8_t rssi;
    char ip[16];
    uint16_t reconnects;
} net_info_t;

void svc_net_info(net_info_t *out);
void svc_net_connect(void);
void svc_net_disconnect(void);
