# The CO5300 behind the svc_display.h interface. The device builds esp/ and the pure logic; the
# sim and the host suite compile the top-level files instead, svc_display_fake.c among them.
idf_component_register(
    SRCS "esp/svc_display_co5300.c" "disp_logic.c" "disp_guard.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api
    PRIV_REQUIRES svc_debug svc_power esp_lcd esp_driver_spi esp_timer lvgl__lvgl
)
