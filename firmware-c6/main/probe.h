#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

/* Starts the read-only hardware probe. `notify` is woken (task notification) when a button
   changes, so a press reaches the box in seconds rather than at the next minute's check. */
void probe_start(TaskHandle_t notify);

/* The probe's findings as one telemetry-safe token string (no quotes or backslashes). */
void probe_note(char *out, size_t len);

/* True once since the last call if a button changed: the caller sends a report. */
bool probe_take_dirty(void);
