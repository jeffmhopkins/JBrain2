# The AXP2101 behind the svc_power.h interface. The device builds esp/ and the pure decoder; the
# sim and the host suite compile the top-level files instead, svc_power_fake.c among them.
idf_component_register(
    SRCS "esp/svc_power_axp2101.c" "axp_logic.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api
    PRIV_REQUIRES svc_debug esp_driver_i2c
)
