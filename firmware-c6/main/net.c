#include "net.h"

#include <string.h>

#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "svc_net_drv.h"

static const char *TAG = "net";
static EventGroupHandle_t s_events;
#define GOT_IP BIT0
#define FAILED BIT1

/* Retries are bounded rather than infinite: the caller's fallback (roll back, or keep the
   current image and try again later) is more useful than a task spinning on a network that
   is not coming back. */
#define MAX_RETRY 5
static int s_retries;
/* Monotonic, unlike `s_retries`, which a successful connect resets — see net.h. */
static int s_last_reason;
static int s_drops;

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        if (!svc_net_user_off()) esp_wifi_connect();
        return;
    }
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *e = data;
        s_last_reason = (int)e->reason;
        s_drops++;
        /* The owner's Disconnect: stay down, and release a join that was waiting on this. */
        if (svc_net_user_off()) {
            svc_net_set_state(NET_OFF);
            xEventGroupSetBits(s_events, FAILED);
            return;
        }
        if (s_retries < MAX_RETRY) {
            svc_net_set_state(NET_CONNECTING);
            s_retries++;
            ESP_LOGW(TAG, "disconnected (reason %d), retry %d/%d", e->reason, s_retries, MAX_RETRY);
            esp_wifi_connect();
        } else {
            ESP_LOGE(TAG, "giving up after %d retries, last reason %d", MAX_RETRY, e->reason);
            /* Off rather than Connecting: Connect on the glass is then live and tries at once,
               instead of waiting for the update loop's next minute. */
            svc_net_set_state(NET_OFF);
            xEventGroupSetBits(s_events, FAILED);
        }
        return;
    }
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *e = data;
        ESP_LOGI(TAG, "got " IPSTR, IP2STR(&e->ip_info.ip));
        s_retries = 0;
        svc_net_set_state(NET_CONNECTED);
        xEventGroupSetBits(s_events, GOT_IP);
    }
}

esp_err_t net_connect(const cfg_t *cfg, int timeout_ms)
{
    s_events = xEventGroupCreate();
    if (s_events == NULL) return ESP_ERR_NO_MEM;

    /* Not `ESP_ERROR_CHECK`: on the S3 a failed `esp_wifi_init` (out of internal RAM) became
       `abort()` three seconds after boot, forever. A failed init must leave the main loop
       running, which is where a later retry and the rollback gate live. */
    esp_err_t err = esp_netif_init();
    if (err != ESP_OK) return err;
    err = esp_event_loop_create_default();
    if (err != ESP_OK) return err;
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    err = esp_wifi_init(&init);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "wifi init failed (%s) — free internal heap %u B. Carrying on without a "
                      "radio.",
                 esp_err_to_name(err), (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
        return err;
    }
    err = esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL,
                                              NULL);
    if (err != ESP_OK) return err;
    err = esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL,
                                              NULL);
    if (err != ESP_OK) return err;

    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, cfg->ssid, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, cfg->pass, sizeof(wc.sta.password));
    err = esp_wifi_set_mode(WIFI_MODE_STA);
    if (err != ESP_OK) return err;
    err = esp_wifi_set_config(WIFI_IF_STA, &wc);
    if (err != ESP_OK) return err;
    err = esp_wifi_start();
    if (err != ESP_OK) return err;

    svc_net_set_state(NET_CONNECTING);
    ESP_LOGI(TAG, "joining '%s' (2.4 GHz only — this radio has no 5 GHz)", cfg->ssid);
    EventBits_t bits = xEventGroupWaitBits(s_events, GOT_IP | FAILED, pdFALSE, pdFALSE,
                                           pdMS_TO_TICKS(timeout_ms));
    if (bits & GOT_IP) return ESP_OK;
    if (bits & FAILED) return ESP_FAIL;
    ESP_LOGE(TAG, "no IP after %d ms", timeout_ms);
    return ESP_ERR_TIMEOUT;
}

esp_err_t net_retry(int timeout_ms)
{
    /* Nothing to retry on a radio that never started; the caller has a bigger problem. */
    if (s_events == NULL || svc_net_user_off()) return ESP_ERR_INVALID_STATE;
    xEventGroupClearBits(s_events, GOT_IP | FAILED);
    svc_net_set_state(NET_CONNECTING);
    /* The handler gives up after MAX_RETRY disconnects and latches FAILED. A fresh attempt
       gets a fresh budget, or the first bad minute after boot would be permanent. */
    s_retries = 0;
    /* Disconnect first: on a half-open link the radio still believes it is associated, and
       `esp_wifi_connect` on an associated station does not re-associate. Connect regardless —
       whether the disconnect raises an event depends on the state it found, and a second
       connect from `on_event` only returns "already connecting". */
    esp_wifi_disconnect();
    esp_wifi_connect();
    const EventBits_t bits = xEventGroupWaitBits(s_events, GOT_IP | FAILED, pdFALSE, pdFALSE,
                                                 pdMS_TO_TICKS(timeout_ms));
    if (bits & GOT_IP) return ESP_OK;
    if (bits & FAILED) return ESP_FAIL;
    return ESP_ERR_TIMEOUT;
}

void net_link_faults(int *last_reason, int *drops)
{
    if (last_reason != NULL) *last_reason = s_last_reason;
    if (drops != NULL) *drops = s_drops;
}
