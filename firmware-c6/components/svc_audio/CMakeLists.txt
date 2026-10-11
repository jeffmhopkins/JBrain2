# The in-memory implementation behind the svc_audio.h interface, until its driver lands.
idf_component_register(
    SRCS "svc_audio_fake.c" "talk_policy.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api svc_debug
    PRIV_REQUIRES svc_rtc
)
