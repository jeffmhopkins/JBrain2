/* In-memory alarms seeded with the mock's, until the NVS copy and RTC wake land (C3). */
#pragma once

#include "svc_alarms.h"

void svc_alarms_init(void);
/* Starts it ringing and raises ALARM_DUE, as the schedule will. */
bool svc_alarms_fake_ring(uint8_t id);
