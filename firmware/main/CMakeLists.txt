idf_component_register(
    SRCS "main.c" "cfg.c" "net.c" "ota.c" "display.c" "face.c" "touch.c"
    INCLUDE_DIRS "."
    # `mbedtls` is here for esp_crt_bundle.h, not for mbedtls itself: the public-CA
    # bundle lives in that component, and without this the header is off the include
    # path even though the feature is enabled in sdkconfig.
    REQUIRES nvs_flash esp_wifi esp_netif esp_event esp_http_client esp_https_ota app_update json
             mbedtls esp_psram esp_lcd driver
)
