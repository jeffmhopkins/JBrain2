# The in-memory implementation behind the svc_alarms.h interface, until its driver lands.
idf_component_register(
    SRCS "svc_alarms_fake.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api svc_debug
    PRIV_REQUIRES svc_rtc
)
