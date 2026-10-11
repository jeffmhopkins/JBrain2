# The three top buttons. The device builds esp/ and the pure debounce/merge; the host suite
# compiles btn_logic.c alone.
idf_component_register(
    SRCS "esp/svc_buttons.c" "btn_logic.c"
    INCLUDE_DIRS "include"
    REQUIRES freertos
    PRIV_REQUIRES esp_driver_gpio esp_timer
)
