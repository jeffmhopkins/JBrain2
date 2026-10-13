# The panel's settings. The device builds esp/ (NVS) and the pure store; the sim and the host
# suite compile the top-level files instead, svc_settings_fake.c among them.
idf_component_register(
    SRCS "esp/svc_settings_nvs.c" "settings_store.c" "settings_sync.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api
    PRIV_REQUIRES svc_debug nvs_flash esp_timer
)
