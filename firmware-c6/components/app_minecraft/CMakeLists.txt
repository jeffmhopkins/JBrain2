# An app may require app_api, ui_kit and the shared conversation, nothing else
# (tools/check_app_deps.py, run in CI).
idf_component_register(
    SRCS "minecraft.c"
    INCLUDE_DIRS "include"
    PRIV_INCLUDE_DIRS "."
    REQUIRES app_api ui_kit convo
)
