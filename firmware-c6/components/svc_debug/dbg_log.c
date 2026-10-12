#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "dbg_core.h"

#ifdef ESP_PLATFORM
#include "freertos/FreeRTOS.h"
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
/* _SAFE: one lock whether the caller is a task or an ISR, so the ring never has to know. */
#define LOCK() portENTER_CRITICAL_SAFE(&s_mux)
#define UNLOCK() portEXIT_CRITICAL_SAFE(&s_mux)
#else
#define LOCK()
#define UNLOCK()
#endif

/* Whole lines, NUL-separated, oldest first. A line that does not fit evicts the oldest whole
 * lines rather than leaving half of one: a dump that starts mid-sentence reads as a fault. */
static char s_ring[DBG_LOG_BYTES];
static size_t s_used;
static uint32_t s_evicted;
static uint32_t (*s_clock)(void);

void dbg_log_set_clock(uint32_t (*now_ms)(void))
{
    s_clock = now_ms;
}

void dbg_log_reset(void)
{
    LOCK();
    s_used = 0;
    s_evicted = 0;
    UNLOCK();
}

uint32_t dbg_log_evicted(void)
{
    return s_evicted;
}

static void store(const char *line, bool clipped)
{
    char kept[DBG_LOG_LINE_MAX + 1];
    size_t n = strlen(line);
    while (n && (line[n - 1] == '\n' || line[n - 1] == '\r')) n--;
    if (n > DBG_LOG_LINE_MAX) {
        n = DBG_LOG_LINE_MAX;
        clipped = true;
    }
    memcpy(kept, line, n);
    kept[n] = '\0';
    dbg_secret_scrub(kept, sizeof kept, clipped);
    n = strlen(kept);
    LOCK();
    /* Find the first line boundary that leaves room, then move once: interrupts are off for
       one copy of at most the ring, not one per evicted line. */
    size_t drop = 0;
    while (s_used - drop + n + 1 > DBG_LOG_BYTES && drop < s_used) {
        drop += strlen(s_ring + drop) + 1;
        s_evicted++;
    }
    if (drop) {
        memmove(s_ring, s_ring + drop, s_used - drop);
        s_used -= drop;
    }
    memcpy(s_ring + s_used, kept, n + 1);
    s_used += n + 1;
    UNLOCK();
}

void dbg_log_line(const char *line)
{
    store(line, false);
}

void svc_debug_log(const char *tag, const char *fmt, ...)
{
    char line[DBG_LOG_LINE_MAX + 1];
    int k = s_clock ? snprintf(line, sizeof line, "%lu %s: ", (unsigned long)s_clock(), tag)
                    : snprintf(line, sizeof line, "%s: ", tag);
    if (k < 0 || (size_t)k >= sizeof line) k = 0;
    va_list ap;
    va_start(ap, fmt);
    const int w = vsnprintf(line + k, sizeof line - (size_t)k, fmt, ap);
    va_end(ap);
    store(line, w >= (int)(sizeof line - (size_t)k));
}

static dbg_vprintf_t s_prev;

void dbg_log_chain(dbg_vprintf_t prev)
{
    s_prev = prev;
}

int dbg_log_vprintf(const char *fmt, va_list ap)
{
    char line[DBG_LOG_LINE_MAX + 1];
    va_list copy;
    va_copy(copy, ap);
    const int n = vsnprintf(line, sizeof line, fmt, copy);
    va_end(copy);
    /* Every esp_log line ends in a newline; losing only that is not a clip. */
    const size_t fits = DBG_LOG_LINE_MAX + (fmt[0] && fmt[strlen(fmt) - 1] == '\n');
    store(line, n > 0 && (size_t)n > fits);
    return s_prev ? s_prev(fmt, ap) : (n > 0 ? n : 0);
}

void dbg_log_write(dbg_json_t *w, const char *key)
{
    static char copy[DBG_LOG_BYTES];
    size_t used;
    LOCK();
    used = s_used;
    memcpy(copy, s_ring, used);
    UNLOCK();
    dbg_json_arr_begin(w, key);
    for (size_t at = 0; at < used; at += strlen(copy + at) + 1) dbg_json_str(w, NULL, copy + at);
    dbg_json_arr_end(w);
}
