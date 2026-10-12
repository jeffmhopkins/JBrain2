# An agent's conversations on the box (0.0.15): Recent, switching, the current title. The device
# builds esp/ (its lock) and the shared files; the sim and the host suite compile the top-level
# files instead, convo_port_fake.c among them.
idf_component_register(
    SRCS "esp/convo_port_esp.c" "convo_wire.c" "svc_convo.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api
    PRIV_REQUIRES svc_audio svc_debug svc_messages
)
