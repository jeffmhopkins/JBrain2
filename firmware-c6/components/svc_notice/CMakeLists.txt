# The in-memory implementation behind the svc_notice.h interface, until its driver lands.
idf_component_register(
    SRCS "notice_policy.c" "svc_notice_fake.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api svc_debug
    PRIV_REQUIRES svc_settings
)
