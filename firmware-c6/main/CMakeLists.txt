# `mbedtls` is here for esp_crt_bundle.h: the public-CA bundle lives in that component, and
# without it the header is off the include path even with the bundle enabled in sdkconfig.
idf_component_register(
    SRCS "main.c" "cfg.c" "net.c" "ota.c"
    INCLUDE_DIRS "."
    REQUIRES nvs_flash esp_wifi esp_netif esp_event esp_http_client esp_https_ota app_update
             esp_app_format json mbedtls esp_timer espcoredump
)
