#pragma once

#include <stdbool.h>

/* Open the touch controller. False means no panel answered; the caller carries on without it. */
bool touch_start(void);

/* True once per press, on the edge. Never true twice for one finger held down. */
bool touch_tapped(void);
