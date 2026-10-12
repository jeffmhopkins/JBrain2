/* The conversations service's lock on the device: the main task hands answers to the UI task
 * through a few pointers and counts, so a critical section (never a sleep) is enough. */
#include "freertos/FreeRTOS.h"
#include "svc_convo_drv.h"

static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

void convo_port_lock(void)
{
    portENTER_CRITICAL(&s_mux);
}

void convo_port_unlock(void)
{
    portEXIT_CRITICAL(&s_mux);
}
