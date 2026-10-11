idf_component_register(
    SRCS "dbg_cmd.c" "dbg_log.c" "dbg_registry.c" "dbg_shot.c" "dbg_sys.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api
    PRIV_REQUIRES esp_timer espcoredump
)
