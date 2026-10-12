# The PCF85063 and the panel's wall clock. The device builds esp/ and the pure time logic; the
# sim and the host suite compile the top-level files instead, svc_rtc_fake.c among them.
idf_component_register(
    SRCS "esp/svc_rtc_pcf85063.c" "time_logic.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api
    PRIV_REQUIRES svc_debug svc_power svc_settings esp_timer
)
