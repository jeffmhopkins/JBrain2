# `mbedtls` is here for esp_crt_bundle.h: the public-CA bundle lives in that component, and
# without it the header is off the include path even with the bundle enabled in sdkconfig.
idf_component_register(
    SRCS "main.c" "cfg.c" "net.c" "ota.c" "box_http.c" "dbg_link.c" "talk_link.c" "sib_link.c"
         "ui_task.c"
         "apps.c"
    INCLUDE_DIRS "."
    REQUIRES nvs_flash esp_wifi esp_netif esp_event esp_http_client esp_https_ota app_update
             esp_app_format json mbedtls esp_timer espcoredump
             app_api ui_kit shell svc_debug svc_display svc_power svc_buttons svc_touch
             svc_alarms svc_audio svc_box svc_messages svc_net svc_notice svc_rtc svc_settings
             svc_siblings convo
             app_jerv app_thread app_clock app_settings app_minecraft lvgl__lvgl
)
