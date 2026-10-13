# The in-memory implementation behind the svc_messages.h interface, until its driver lands.
idf_component_register(
    SRCS "svc_messages_fake.c" "hist_logic.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api svc_debug
)
