# The in-memory implementation behind the svc_power.h interface, until its driver lands.
idf_component_register(
    SRCS "svc_power_fake.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api svc_debug
)
