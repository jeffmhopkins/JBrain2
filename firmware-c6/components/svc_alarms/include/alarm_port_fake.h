/* The host's stand-in for the alarms cache (alarm_port_fake.c). Only the sim, tests and main
 * include this; apps cannot (check_app_deps). */
#pragma once

#include <stdint.h>

/* A first boot: nothing cached. */
void alarm_port_fake_wipe(void);
/* How many times the cache was written: a burst of switches must be one write. */
uint32_t alarm_port_fake_saves(void);
