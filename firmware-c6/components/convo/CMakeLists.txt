# One conversation view for every agent (Jerv, Dave): shared code, an instance per app. Below the
# apps, so tools/check_app_deps.py lets an app require it; it requires only what an app may.
idf_component_register(
    SRCS "convo.c" "convo_model.c" "convo_recent.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api ui_kit
)
