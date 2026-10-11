/* AXP2101 fuel gauge and supply state. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    bool fitted; /* false: no battery on the connector, running from USB */
    bool usb;
    bool charging;
    uint8_t pct;
    uint16_t mv;
    uint16_t usb_mv;
} power_info_t;

void svc_power_info(power_info_t *out);
