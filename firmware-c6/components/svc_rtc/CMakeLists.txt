# The in-memory implementation behind the svc_rtc.h interface, until its driver lands.
idf_component_register(
    SRCS "svc_rtc_fake.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api svc_debug
)
