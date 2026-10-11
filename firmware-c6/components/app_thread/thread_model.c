#include "thread_model.h"

#include <stdio.h>

void thread_fmt_dur(uint32_t secs, char out[8])
{
    if (secs > 5999) secs = 5999;
    snprintf(out, 8, "%lu:%02lu", (unsigned long)(secs / 60), (unsigned long)(secs % 60));
}

void thread_bubble(const msg_t *m, uint32_t playing_id, uint8_t play_pct, thread_bubble_t *out)
{
    out->playing = m->id == playing_id;
    /* The dot means "not heard yet"; it goes the moment it starts, not when it finishes. */
    out->unplayed = !m->mine && m->voice && !m->played && !out->playing;
    out->prog = out->playing ? play_pct : (!m->mine && !m->played ? 0 : 100);
    thread_fmt_dur(m->dur_s, out->dur);
}
