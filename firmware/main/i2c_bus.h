#pragma once

#include "driver/i2c_master.h"

/* The one I2C bus on this board, shared. The touch controller, the audio codec, the PMU,
   the RTC and the IMU all sit on it, and `i2c_new_master_bus` fails on a port that already
   has one — so ownership belongs in a single place rather than in whichever driver happened
   to start first. NULL means the bus could not be opened at all. */
i2c_master_bus_handle_t i2c_bus_get(void);
