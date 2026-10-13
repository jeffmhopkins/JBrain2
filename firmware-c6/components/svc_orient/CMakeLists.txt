# Auto-rotate (0.0.18): the QMI8658 over svc_power's bus, the decision and its hysteresis. The
# device builds esp/ (the chip) and the shared files; the sim and the host suite compile the
# top-level files instead, orient_port_fake.c among them.
idf_component_register(
    SRCS "esp/orient_port_qmi8658.c" "orient_logic.c" "svc_orient.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api
    PRIV_REQUIRES svc_debug svc_display svc_power svc_settings
)
