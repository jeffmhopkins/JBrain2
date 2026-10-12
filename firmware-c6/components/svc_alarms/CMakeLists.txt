# The alarms: the box's list, cached in NVS, fired from the panel's own clock. The device builds
# esp/ (the NVS cache) and the shared files; the sim and the host suite compile the top-level files
# instead, alarm_port_fake.c among them.
idf_component_register(
    SRCS "esp/alarm_port_nvs.c" "alarm_logic.c" "svc_alarms.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api svc_rtc
    PRIV_REQUIRES svc_audio svc_debug svc_settings nvs_flash
)
