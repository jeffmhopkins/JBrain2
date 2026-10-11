/* In-memory audio until the ES8311/ES7210 owner task lands (C3). Time comes from svc_rtc's
 * uptime, so the sim advances a recording by advancing the clock. */
#pragma once

#include "svc_audio.h"

void svc_audio_init(void);
