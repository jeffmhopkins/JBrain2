#pragma once

#include "driver/i2c_master.h"

/* The one I2C bus on this board, shared. The touch controller, the audio codec, the PMU,
   the RTC and the IMU all sit on it, and `i2c_new_master_bus` fails on a port that already
   has one — so ownership belongs in a single place rather than in whichever driver happened
   to start first. NULL means the bus could not be opened at all. */
i2c_master_bus_handle_t i2c_bus_get(void);

/* Log every address that acknowledges on the bus, once.

   This should have been here from the first bring-up. Three nights of display debugging have
   argued about a PMU nobody has confirmed exists: §10.4n named the AXP2101 as a candidate and
   ruled it out by inference, and the vendor BSP does not mention one at all. A soft reset
   leaves the panel dark where a power cycle does not, which means something outside the ESP32
   holds that state — and the list of things that could is exactly the list of chips on this
   bus. Twelve lines of code answers what has been assumed twice. */
void i2c_bus_scan(void);
