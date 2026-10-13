# An app may require app_api, ui_kit and convo, nothing else (tools/check_app_deps.py, run in CI).
idf_component_register(
    SRCS "thread.c" "thread_model.c"
    INCLUDE_DIRS "include"
    PRIV_INCLUDE_DIRS "."
    REQUIRES app_api ui_kit convo
)
