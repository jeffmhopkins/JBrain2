#include <string.h>

#include "app_event.h"

static app_event_sink_t s_sink;
static uint32_t s_unsunk;

void app_events_set_sink(app_event_sink_t sink)
{
    s_sink = sink;
}

void app_events_post(const app_event_t *ev)
{
    if (s_sink)
        s_sink(ev);
    else
        s_unsunk++;
}

void app_events_post_type(app_event_type_t type)
{
    app_event_t ev;
    memset(&ev, 0, sizeof ev);
    ev.type = type;
    app_events_post(&ev);
}

uint32_t app_events_unsunk(void)
{
    return s_unsunk;
}
