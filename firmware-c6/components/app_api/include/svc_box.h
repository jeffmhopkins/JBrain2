/* The box: firmware updates and reporting. Messages, alarms and notices have their own
 * services even though they ride the same connection. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef enum { FW_IDLE, FW_CHECKING, FW_DOWNLOADING, FW_RESTARTING, FW_DONE, FW_UPTODATE } fw_phase_t;

typedef struct {
    char version[16];
    char next[16];
    fw_phase_t phase;
    uint8_t pct;
} fw_info_t;

void svc_box_fw(fw_info_t *out);
/* Forces a manifest check now. False when offline or already busy. */
bool svc_box_update_now(void);
/* Posts telemetry on the next loop pass, as the S3's report-now does. */
void svc_box_report_now(void);
uint32_t svc_box_quiet_s(void);
