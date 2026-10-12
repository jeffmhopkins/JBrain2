# The box client's face to the apps. The device builds esp/ (fed by main's update loop) and the
# pure rule; the sim and the host suite compile the top-level files, svc_box_fake.c among them.
idf_component_register(
    SRCS "esp/svc_box_esp.c" "box_logic.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api svc_debug
    PRIV_REQUIRES svc_net esp_timer
)
