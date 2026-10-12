#include "alarm_link.h"

#include <stdio.h>

#include "box_http.h"
#include "esp_log.h"
#include "svc_alarms_drv.h"

static const char *TAG = "alarms";

#define ALARM_TIMEOUT_MS 8000

/* Eight alarms of a 24-byte ASCII label are ~700 bytes of the box's compact JSON. */
static char s_body[1024];

static void send_switches(void)
{
    alarm_put_t p;
    for (int i = 0; i < ALARM_MAX && svc_alarms_take_put(&p); i++) {
        char path[40];
        snprintf(path, sizeof path, "/endpoint/alarms/%lu", (unsigned long)p.id);
        const char *body = p.on ? "{\"on\":true}" : "{\"on\":false}";
        const int status = box_http_put(path, "application/json", body, p.on ? 11 : 12,
                                        ALARM_TIMEOUT_MS);
        svc_alarms_put_done(&p, status);
        if (status == 404) ESP_LOGW(TAG, "alarm %lu is gone on the box: switch dropped", (unsigned long)p.id);
        /* Not settled: it stays queued for the next pass, and so does everything after it. */
        if (!((status >= 200 && status < 300) || status == 404)) {
            ESP_LOGW(TAG, "switch of alarm %lu: HTTP %d, kept", (unsigned long)p.id, status);
            return;
        }
    }
}

void alarm_link_poll(void)
{
    send_switches();
    char etag[ALARM_ETAG_BYTES], path[64];
    svc_alarms_etag(etag, sizeof etag);
    if (etag[0])
        snprintf(path, sizeof path, "/endpoint/alarms?etag=%s", etag);
    else
        snprintf(path, sizeof path, "/endpoint/alarms");
    size_t got = 0;
    const int status = box_http_call(path, NULL, NULL, 0, s_body, sizeof s_body - 1, &got, NULL,
                                     ALARM_TIMEOUT_MS);
    if (status == 304) {
        svc_alarms_unchanged();
        return;
    }
    if (status != 200 || got >= sizeof s_body - 1) {
        ESP_LOGW(TAG, "alarms: HTTP %d, %u bytes", status, (unsigned)got);
        svc_alarms_failed(status);
        return;
    }
    s_body[got] = '\0';
    if (!svc_alarms_offer(s_body, got)) ESP_LOGW(TAG, "alarms: a reply this panel could not read");
}
