/* The tilt the sim and the host suite give the fake accelerometer (orient_port_fake.c). */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* The raw reading every later read returns, in counts (ORIENT_1G a g). */
void orient_port_fake_set(int16_t ax, int16_t ay, int16_t az);
/* The next start fails at `step` (NULL: it answers). */
void orient_port_fake_fail(const char *step);
bool orient_port_fake_powered(void);
unsigned orient_port_fake_reads(void);
