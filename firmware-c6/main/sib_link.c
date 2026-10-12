#include "sib_link.h"

#include "box_http.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "svc_siblings_drv.h"

static const char *TAG = "siblings";

/* A name changes when the owner renames a panel, which is rare: once a boot, then every fifteen
   minutes, rides the manifest pass that already holds the session. A failure tries again on the
   next pass (a minute) rather than waiting out the period. */
#define SIB_PERIOD_MS (15 * 60 * 1000)
#define SIB_TIMEOUT_MS 8000

/* Four siblings of a 36-byte id and a 24-byte name are ~330 bytes of JSON: room for escapes. */
static char s_body[768];
static uint32_t s_ok_ms;
static bool s_ok;

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

void sib_link_poll(void)
{
    if (s_ok && (uint32_t)(now_ms() - s_ok_ms) < SIB_PERIOD_MS) return;
    size_t got = 0;
    const int status = box_http_call("/endpoint/siblings", NULL, NULL, 0, s_body, sizeof s_body - 1,
                                     &got, NULL, SIB_TIMEOUT_MS);
    if (status != 200 || got >= sizeof s_body - 1) {
        ESP_LOGW(TAG, "siblings: HTTP %d, %u bytes", status, (unsigned)got);
        svc_siblings_failed(status);
        return;
    }
    s_body[got] = '\0';
    if (!svc_siblings_offer(s_body, got)) {
        ESP_LOGW(TAG, "siblings: a reply this panel could not read");
        return;
    }
    s_ok = true;
    s_ok_ms = now_ms();
}
