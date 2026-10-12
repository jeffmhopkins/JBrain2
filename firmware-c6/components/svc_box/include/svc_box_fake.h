/* In-memory box client: the sim's and the host suite's (the device's is esp/svc_box_esp.c). */
#pragma once

#include "svc_box.h"

void svc_box_init(void);
void svc_box_fake_set(const fw_info_t *fw);
unsigned svc_box_fake_reports(void);
