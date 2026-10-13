# Push notices (0.0.19, plan §2 "The panel side"): the JN1 reader, the files on `storage`
# (littlefs), the queue, the state POSTs owed to the box. The device builds esp/ (littlefs, NVS, its
# lock) and the shared files; the sim and the host suite compile the top-level files instead,
# notice_port_fake.c among them.
idf_component_register(
    SRCS "esp/notice_port_esp.c" "jn1.c" "notice_logic.c" "notice_policy.c" "notice_store.c"
         "svc_notice.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api svc_debug
    PRIV_REQUIRES svc_settings svc_rtc svc_audio nvs_flash vfs joltwallet__littlefs
)
