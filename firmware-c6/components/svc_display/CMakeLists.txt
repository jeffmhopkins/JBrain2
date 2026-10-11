# The in-memory implementation behind the svc_display.h interface, until its driver lands.
idf_component_register(
    SRCS "svc_display_fake.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api svc_debug
)
