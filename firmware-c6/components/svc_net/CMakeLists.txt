# Wi-Fi as the glass sees it. The device builds esp/ (fed by main/net.c and the update loop);
# the sim and the host suite compile the top-level svc_net_fake.c instead.
idf_component_register(
    SRCS "esp/svc_net_esp.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api svc_debug
    PRIV_REQUIRES esp_wifi esp_netif
)
