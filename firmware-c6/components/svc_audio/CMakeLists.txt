# The ES8311 (speaker) and ES7210 (microphones) behind the svc_audio.h interface, and the talk to
# Jerv. The device builds esp/ with the shared talk state and its pure core; the sim and the host
# suite compile the top-level files instead, svc_audio_fake.c among them.
idf_component_register(
    SRCS "esp/svc_audio_es.c" "svc_audio_talk.c" "talk_core.c" "talk_policy.c"
    INCLUDE_DIRS "include"
    REQUIRES app_api svc_debug
    PRIV_REQUIRES svc_rtc svc_messages svc_net svc_settings svc_power esp_driver_i2s
)
