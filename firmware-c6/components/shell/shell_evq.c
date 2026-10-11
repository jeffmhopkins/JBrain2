#include "shell_evq.h"

#include <string.h>

#ifdef ESP_PLATFORM
#include "freertos/FreeRTOS.h"
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
#define LOCK() portENTER_CRITICAL(&s_mux)
#define UNLOCK() portEXIT_CRITICAL(&s_mux)
#else
#define LOCK()
#define UNLOCK()
#endif

void sh_evq_init(sh_evq_t *q)
{
    memset(q, 0, sizeof *q);
}

bool sh_evq_push(sh_evq_t *q, const app_event_t *ev)
{
    bool ok;
    LOCK();
    ok = q->count < SH_EVQ_DEPTH;
    if (ok) {
        q->ev[q->tail] = *ev;
        q->tail = (uint8_t)((q->tail + 1) % SH_EVQ_DEPTH);
        q->count++;
    } else {
        q->dropped++;
    }
    UNLOCK();
    return ok;
}

bool sh_evq_pop(sh_evq_t *q, app_event_t *out)
{
    bool ok;
    LOCK();
    ok = q->count > 0;
    if (ok) {
        *out = q->ev[q->head];
        q->head = (uint8_t)((q->head + 1) % SH_EVQ_DEPTH);
        q->count--;
    }
    UNLOCK();
    return ok;
}
