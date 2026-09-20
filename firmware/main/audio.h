#pragma once

#include <stdbool.h>

/* Bring up the ES8311. False means no codec answered; the caller carries on in silence. */
bool audio_start(void);

/* A short tone, played synchronously. Cheap enough to call from a touch handler. */
void audio_beep(void);
