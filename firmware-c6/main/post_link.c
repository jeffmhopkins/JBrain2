#include "post_link.h"

#include <stdlib.h>

#include "box_http.h"
#include "esp_log.h"
#include "svc_post_drv.h"

static const char *TAG = "post";

/* Small answers from a kept session: the conversations' bound. */
#define POST_TIMEOUT_MS 8000

bool post_link_run(void)
{
    post_req_t r;
    if (!svc_post_take(&r)) return false;
    if (r.kind == POST_RQ_PLAYED) {
        /* A POST with no body: the message is named by the path. */
        const int status = box_http_call(r.path, "application/json", "", 0, NULL, 0, NULL, NULL,
                                         POST_TIMEOUT_MS);
        if (status != 204) ESP_LOGW(TAG, "%s: HTTP %d", r.path, status);
        svc_post_done(&r, status, NULL, 0);
        return true;
    }
    /* The longest list, its terminator and one byte more: a read that fills it is cut off. Held
       only until the UI task has applied it. */
    char *buf = malloc(POST_BODY_MAX + 2);
    if (!buf) {
        svc_post_done(&r, -3, NULL, 0);
        return true;
    }
    size_t got = 0;
    int status = box_http_call(r.path, NULL, NULL, 0, buf, POST_BODY_MAX + 1, &got, NULL,
                               POST_TIMEOUT_MS);
    if (status == 200 && got > POST_BODY_MAX) status = -4;
    if (status != 200) {
        ESP_LOGW(TAG, "%s: HTTP %d", r.path, status);
        free(buf);
        svc_post_done(&r, status, NULL, 0);
        return true;
    }
    buf[got] = '\0';
    svc_post_done(&r, status, buf, got);
    return true;
}
