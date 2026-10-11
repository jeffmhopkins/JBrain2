# The CST9220. The device builds esp/ and the pure report decoder; the host suite and the sim
# compile tp_logic.c alone (the sim's pointer is the shell's virtual one).
idf_component_register(
    SRCS "esp/svc_touch_cst9220.c" "tp_logic.c"
    INCLUDE_DIRS "include"
    PRIV_REQUIRES svc_power esp_driver_gpio esp_timer
)
