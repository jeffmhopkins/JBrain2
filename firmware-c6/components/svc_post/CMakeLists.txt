# The owner's voice post with the twins' panels (0.0.16): the people's threads from the box, a
# play's "played", the new-message signal. The device builds esp/ (its lock) and the shared files;
# the sim and the host suite compile the top-level files instead, post_port_fake.c among them.
idf_component_register(
    SRCS "esp/post_port_esp.c" "post_logic.c" "svc_post.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api svc_messages
    PRIV_REQUIRES svc_debug svc_siblings svc_settings svc_rtc
)
