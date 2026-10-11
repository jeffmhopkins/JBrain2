/* In-memory box client until main/ota.c is wrapped as this service (C2). */
#pragma once

#include "svc_box.h"

void svc_box_init(void);
void svc_box_fake_set(const fw_info_t *fw);
unsigned svc_box_fake_reports(void);
