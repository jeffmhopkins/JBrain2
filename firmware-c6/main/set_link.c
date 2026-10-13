#include "set_link.h"

#include <stdio.h>
#include <string.h>

#include "box_http.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "settings_sync.h"
#include "svc_display.h"
#include "svc_settings.h"

static const char *TAG = "settings";

#define SYNC_TIMEOUT_MS 8000

static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
/* The main task's: the version the pickup signals, the one last fetched and when. */
static uint32_t s_signalled, s_fetched, s_fetched_at;
/* Fetched, for the UI task: one queue at a time, the newer replacing an unapplied older one. */
static sync_pending_t s_mail;
static bool s_mail_full;
static char s_body[SYNC_BODY_MAX + 2];
static struct {
    uint32_t fetches, applied, kept;
    int last;
} s_st = {.last = -1};

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

void set_link_signal(const char *header)
{
    s_signalled = settings_sync_header(header);
}

bool set_link_run(void)
{
    if (!settings_sync_due(s_signalled, s_fetched, now_ms() - s_fetched_at)) return false;
    size_t got = 0;
    const int status = box_http_call(SYNC_PATH, NULL, NULL, 0, s_body, sizeof s_body - 1, &got,
                                     NULL, SYNC_TIMEOUT_MS);
    s_st.last = status;
    s_st.fetches++;
    s_fetched_at = now_ms();
    if (status != 200 || got >= sizeof s_body - 1) {
        ESP_LOGW(TAG, "panel-settings: HTTP %d, %u bytes", status, (unsigned)got);
        return true;
    }
    s_body[got] = '\0';
    sync_pending_t p;
    if (!settings_sync_parse(s_body, got, &p)) {
        ESP_LOGW(TAG, "panel-settings: a reply this panel could not read");
        return true;
    }
    s_fetched = p.version ? p.version : s_signalled;
    portENTER_CRITICAL(&s_mux);
    s_mail = p;
    s_mail_full = true;
    portEXIT_CRITICAL(&s_mux);
    return true;
}

static int32_t get(setting_t k)
{
    return svc_settings_get(k);
}

static void set(setting_t k, int32_t v)
{
    svc_settings_set(k, v);
}

void set_link_ui_service(void)
{
    if (!s_mail_full) return;
    sync_pending_t p;
    portENTER_CRITICAL(&s_mux);
    p = s_mail;
    s_mail_full = false;
    portEXIT_CRITICAL(&s_mux);
    const uint32_t done = settings_sync_apply(&p, get, set);
    /* The rest read their setting where they use it; the glass's brightness is a request to the
       display task, made here so it is seen at once (a dark screen stays dark). */
    if (done >> SET_BRIGHTNESS & 1 && !svc_display_dark())
        svc_display_set_brightness((uint8_t)svc_settings_get(SET_BRIGHTNESS));
    uint8_t n = 0;
    for (uint32_t m = done; m; m &= m - 1) n++;
    s_st.applied += n;
    s_st.kept += (uint32_t)(p.n - n);
    ESP_LOGI(TAG, "from the PWA (v%lu): %u applied, %u kept as set on the glass",
             (unsigned long)p.version, (unsigned)n, (unsigned)(p.n - n));
}

void set_link_note(char *out, size_t len)
{
    snprintf(out, len, "sync=v:%lu,fetch:%lu,applied:%lu,kept:%lu,last:%d",
             (unsigned long)s_fetched, (unsigned long)s_st.fetches, (unsigned long)s_st.applied,
             (unsigned long)s_st.kept, s_st.last);
}
