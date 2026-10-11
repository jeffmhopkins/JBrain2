/* In-memory notice queue until C3b's settings-poll client fills it. */
#pragma once

#include "notice_policy.h"
#include "svc_notice.h"

#define NOTICE_QUEUE 4

void svc_notice_init(void);
/* Queues one as the poll would and raises NOTICE_ARRIVED. */
bool svc_notice_fake_push(const notice_t *n);
/* What the panel reports to the box on each poll, built from the settings it owns. */
void svc_notice_report(notice_report_t *out);
