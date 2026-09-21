#include "net.h"

#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"

static const char *TAG = "net";
static EventGroupHandle_t s_events;
#define GOT_IP BIT0
#define FAILED BIT1

/* Retries are bounded rather than infinite: the caller's fallback (roll back, or keep the
   current image and try again later) is more useful than a task spinning on a network that
   is not coming back. */
#define MAX_RETRY 5
static int s_retries;

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
        return;
    }
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *e = data;
        if (s_retries < MAX_RETRY) {
            s_retries++;
            ESP_LOGW(TAG, "disconnected (reason %d), retry %d/%d", e->reason, s_retries, MAX_RETRY);
            esp_wifi_connect();
        } else {
            ESP_LOGE(TAG, "giving up after %d retries, last reason %d", MAX_RETRY, e->reason);
            xEventGroupSetBits(s_events, FAILED);
        }
        return;
    }
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *e = data;
        ESP_LOGI(TAG, "got " IPSTR, IP2STR(&e->ip_info.ip));
        s_retries = 0;
        xEventGroupSetBits(s_events, GOT_IP);
    }
}

esp_err_t net_connect(const cfg_t *cfg, int timeout_ms)
{
    s_events = xEventGroupCreate();
    if (s_events == NULL) return ESP_ERR_NO_MEM;

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event,
                                                        NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event,
                                                        NULL, NULL));

    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, cfg->ssid, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, cfg->pass, sizeof(wc.sta.password));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());

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
    if (s_events == NULL) return ESP_ERR_INVALID_STATE;
    xEventGroupClearBits(s_events, GOT_IP | FAILED);
    /* The handler gives up after MAX_RETRY disconnects and latches FAILED. A fresh attempt
       gets a fresh budget, or the first bad minute after boot would be permanent. */
    s_retries = 0;
    esp_wifi_connect();
    const EventBits_t bits = xEventGroupWaitBits(s_events, GOT_IP | FAILED, pdFALSE, pdFALSE,
                                                 pdMS_TO_TICKS(timeout_ms));
    if (bits & GOT_IP) return ESP_OK;
    if (bits & FAILED) return ESP_FAIL;
    return ESP_ERR_TIMEOUT;
}
