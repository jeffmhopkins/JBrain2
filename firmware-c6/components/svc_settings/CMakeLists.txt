# The in-memory implementation behind the svc_settings.h interface, until its driver lands.
idf_component_register(
    SRCS "svc_settings_fake.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api svc_debug
)
