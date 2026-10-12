/* The voice post service's lock on the device: the main task hands requests, answers and the
 * pickup's tag to the UI task through a few small copies, so a critical section is enough. */
#include "freertos/FreeRTOS.h"
#include "svc_post_drv.h"

static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

void post_port_lock(void)
{
    portENTER_CRITICAL(&s_mux);
}

void post_port_unlock(void)
{
    portEXIT_CRITICAL(&s_mux);
}
