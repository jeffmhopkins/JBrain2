# The contract every app and the shell compile against: app_t, app_event_t, the svc_*.h
# interfaces and the redacting debug writer. LVGL is public because app_t takes an lv_obj_t.
idf_component_register(
    SRCS "app_event.c" "dbg_json.c"
    INCLUDE_DIRS "include"
    REQUIRES ui_kit lvgl__lvgl
)
