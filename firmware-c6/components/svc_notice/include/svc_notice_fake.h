/* The notice service's start-up and the sim's way in. Since 0.0.19 the queue is the real one
 * (svc_notice_drv.h): a pushed notice is written to a file as the box would send it and read
 * back through the panel's own path. */
#pragma once

#include "notice_policy.h"
#include "svc_notice.h"

#define NOTICE_QUEUE 4

/* Registers the `notice` snapshot section; on the host and in the sim it also starts the files
 * (the device starts them on the main task, svc_notice_start). */
void svc_notice_init(void);
/* Queues one as a fetch from the box would and raises NOTICE_ARRIVED (the sim, the debug path). */
bool svc_notice_fake_push(const notice_t *n);
/* What the panel reports to the box on each pickup, built from the settings it owns. */
void svc_notice_report(notice_report_t *out);
