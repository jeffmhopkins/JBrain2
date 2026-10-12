# The twins' panels by name. The device builds esp/ (the NVS cache) and the shared files; the sim
# and the host suite compile the top-level files instead, sib_port_fake.c among them.
idf_component_register(
    SRCS "esp/sib_port_nvs.c" "sib_logic.c" "svc_siblings.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api
    PRIV_REQUIRES svc_debug svc_messages nvs_flash
)
