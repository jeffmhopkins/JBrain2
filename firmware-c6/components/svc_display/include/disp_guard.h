/* Whether this boot may start the display. Pure, so the host suite holds it to its promise.
 *
 * The display path is the newest code on the panel and the likeliest to fault, and a fault in it
 * would recur on every boot. The boot after a panic, a watchdog or a brownout therefore leaves
 * the screen off and runs headless — which is exactly the boot that can reach the box and take
 * the fixed image. The next clean boot tries the display again. */
#pragma once

#include <stdbool.h>

/* esp_reset_reason_t's values; the device build checks them against ESP-IDF's enum. */
enum {
    DG_RST_UNKNOWN = 0,
    DG_RST_POWERON = 1,
    DG_RST_EXT = 2,
    DG_RST_SW = 3,
    DG_RST_PANIC = 4,
    DG_RST_INT_WDT = 5,
    DG_RST_TASK_WDT = 6,
    DG_RST_WDT = 7,
    DG_RST_DEEPSLEEP = 8,
    DG_RST_BROWNOUT = 9,
    DG_RST_PWR_GLITCH = 14,
    DG_RST_CPU_LOCKUP = 15,
};

/* True: start the display. False: this boot stays headless. */
bool dg_may_start(int reset_reason);
