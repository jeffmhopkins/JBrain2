# The CST9220, and touch calibration (0.0.18). The device builds esp/ (the chip, and the fit kept
# in NVS) with the pure mapping and the shared calibration service; the host suite and the sim
# compile the top-level files instead, tp_cal_port_fake.c among them (the sim's pointer is the
# shell's virtual one).
idf_component_register(
    SRCS "esp/svc_touch_cst9220.c" "esp/tp_cal_port_esp.c" "tp_logic.c" "svc_calib.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api
    PRIV_REQUIRES svc_debug svc_orient svc_rtc svc_power esp_driver_gpio esp_timer nvs_flash
)
