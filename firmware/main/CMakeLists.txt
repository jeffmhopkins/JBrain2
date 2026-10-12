idf_component_register(
    SRCS "main.c" "cfg.c" "net.c" "link.c" "wsproto.c" "ota.c" "display.c" "overlay.c" "panel_sm.c" "face.c" "font.c" "emotion.c" "rig.c" "variants.c" "gesture.c" "orient.c" "screen.c" "cadence.c" "reach.c" "confirm.c" "ring.c" "calib.c" "caption.c" "cue.c" "vocab.c" "speech.c" "imu.c" "pmu.c" "mem.c" "touch.c" "audio.c" "stream.c" "talk.c" "jpanel.c" "jpanel_core.c" "nudge.c" "i2c_bus.c"
    INCLUDE_DIRS "."
    # See linker.lf: it exiles a 117 KB scratch buffer belonging to a model this
    # firmware never runs out of the internal RAM the Wi-Fi driver needs.
    LDFRAGMENTS "linker.lf"
    # `mbedtls` is here for esp_crt_bundle.h, not for mbedtls itself: the public-CA
    # bundle lives in that component, and without this the header is off the include
    # path even though the feature is enabled in sdkconfig.
    REQUIRES nvs_flash esp_wifi esp_netif lwip esp_event esp_http_client esp_https_ota app_update json
             mbedtls esp_psram esp_lcd driver esp_driver_i2s esp_timer esp-sr
)
# THE DRAWING AT -O2 (0.4.3). The project builds at ESP-IDF's default -Og, where the per-pixel
# helpers in face.c are not inlined and the pet's limbs, stamped circle by circle, cost most of a
# frame. These files are flash-resident (no IRAM_ATTR in any of them), so this spends flash, not
# the internal RAM the recogniser needs. The project-wide level stays: IRAM code shares SRAM with
# the internal heap.
set_source_files_properties(face.c rig.c emotion.c font.c overlay.c confirm.c caption.c display.c
                            PROPERTIES COMPILE_OPTIONS "-O2")
