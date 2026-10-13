idf_component_register(
    SRCS "shell.c" "shell_core.c" "shell_evq.c" "notice_overlay.c" "notice_view.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api ui_kit svc_debug
    PRIV_REQUIRES svc_alarms svc_audio svc_display svc_notice svc_rtc svc_settings
)
