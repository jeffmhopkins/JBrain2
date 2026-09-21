idf_component_register(
    SRCS "main.c" "cfg.c" "net.c" "ota.c" "display.c" "face.c" "font.c" "emotion.c" "rig.c" "variants.c" "gesture.c" "calib.c" "caption.c" "vocab.c" "speech.c" "imu.c" "pmu.c" "mem.c" "touch.c" "audio.c" "i2c_bus.c"
    INCLUDE_DIRS "."
    # See linker.lf: it exiles a 117 KB scratch buffer belonging to a model this
    # firmware never runs out of the internal RAM the Wi-Fi driver needs.
    LDFRAGMENTS "linker.lf"
    # `mbedtls` is here for esp_crt_bundle.h, not for mbedtls itself: the public-CA
    # bundle lives in that component, and without this the header is off the include
    # path even though the feature is enabled in sdkconfig.
    REQUIRES nvs_flash esp_wifi esp_netif esp_event esp_http_client esp_https_ota app_update json
             mbedtls esp_psram esp_lcd driver esp_driver_i2s esp_timer esp-sr
)
