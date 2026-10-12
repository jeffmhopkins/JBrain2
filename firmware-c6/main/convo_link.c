#include "convo_link.h"

#include <stdlib.h>
#include <string.h>

#include "box_http.h"
#include "esp_log.h"
#include "svc_convo_drv.h"

static const char *TAG = "convo";

/* Small answers from a kept session: the alarms' bound. */
#define CONVO_TIMEOUT_MS 8000

/* The box's answer into a buffer of its own, which passes to the UI task on a 200. */
static int fetch(const char *path, const char *body, char **out, size_t *len)
{
    *out = NULL;
    *len = 0;
    /* The longest answer, its terminator, and one byte more: a read that fills the buffer is
       taken as cut off. Held only for the request and until the UI task has applied it. */
    char *buf = malloc(CONVO_BODY_MAX + 2);
    if (!buf) return -3;
    size_t got = 0;
    const int status = box_http_call(path, body ? "application/json" : NULL, body,
                                     body ? strlen(body) : 0, buf, CONVO_BODY_MAX + 1, &got, NULL,
                                     CONVO_TIMEOUT_MS);
    if (status != 200 || got > CONVO_BODY_MAX) {
        free(buf);
        return status == 200 ? -4 : status;
    }
    buf[got] = '\0';
    *out = buf;
    *len = got;
    return status;
}

bool convo_link_run(void)
{
    convo_req_t r;
    if (!svc_convo_take(&r)) return false;
    char path[64], *body = NULL;
    size_t len = 0;
    int status;
    convo_path((convo_rq_t)r.kind, r.agent, false, path, sizeof path);
    if (r.kind == CONVO_RQ_SWITCH) {
        char sel[64];
        if (!convo_select_body(r.id, sel, sizeof sel)) {
            svc_convo_done(&r, -5, NULL, 0);
            return true;
        }
        char *ack = NULL;
        status = fetch(path, sel, &ack, &len);
        free(ack);
        if (status == 200) {
            /* Switched on the box. Its last exchanges are the second request; if they do not
               come, a 200 with no body still says the switch held. */
            convo_path(CONVO_RQ_SWITCH, r.agent, true, path, sizeof path);
            if (fetch(path, NULL, &body, &len) != 200) body = NULL;
        }
    } else {
        status = fetch(path, NULL, &body, &len);
    }
    if (status != 200) ESP_LOGW(TAG, "%s: HTTP %d", path, status);
    svc_convo_done(&r, status, body, body ? len : 0);
    return true;
}
