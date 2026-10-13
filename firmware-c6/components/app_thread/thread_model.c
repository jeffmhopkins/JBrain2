#include "thread_model.h"

#include <stddef.h>
#include <stdio.h>
#include <string.h>

void thread_fmt_dur(uint32_t secs, char out[8])
{
    if (secs > 5999) secs = 5999;
    snprintf(out, 8, "%lu:%02lu", (unsigned long)(secs / 60), (unsigned long)(secs % 60));
}

uint8_t thread_play_pct(const msg_t *m, uint32_t played_ms)
{
    const uint32_t total = m->dur_ms ? m->dur_ms : (uint32_t)m->dur_s * 1000u;
    if (!total) return 0;
    return played_ms >= total ? 100 : (uint8_t)(played_ms * 100u / total);
}

void thread_bubble(const msg_t *m, uint32_t playing_id, uint32_t played_ms, thread_bubble_t *out)
{
    out->playing = m->id == playing_id;
    /* The dot means "not heard yet"; it goes the moment it starts, not when it finishes. */
    out->unplayed = !m->mine && m->voice && !m->played && !out->playing;
    out->prog = out->playing ? thread_play_pct(m, played_ms) : (!m->mine && !m->played ? 0 : 100);
    thread_fmt_dur(m->dur_s, out->dur);
}

/* FNV-1a: a few hundred bytes a redraw, no table. */
static uint32_t fold(uint32_t h, const void *p, size_t n)
{
    const uint8_t *b = p;
    for (size_t i = 0; i < n; i++) h = (h ^ b[i]) * 16777619u;
    return h;
}

uint32_t thread_shape_msg(uint32_t h, const msg_t *m)
{
    const uint8_t flags = (uint8_t)(m->mine | m->voice << 1 | m->pending << 2);
    h = fold(h, &m->id, sizeof m->id);
    h = fold(h, &flags, 1);
    h = fold(h, &m->dur_s, sizeof m->dur_s);
    /* The terminators too: "ab"+"c" and "a"+"bc" are different bubbles. */
    h = fold(h, m->text, strnlen(m->text, sizeof m->text - 1) + 1);
    return fold(h, m->status, strnlen(m->status, sizeof m->status - 1) + 1);
}

uint32_t thread_shape_end(uint32_t h, uint16_t n, bool loaded)
{
    const uint8_t l = loaded;
    h = fold(h, &n, sizeof n);
    return fold(h, &l, 1);
}

uint32_t thread_shape_hist(uint32_t h, const hist_view_t *v)
{
    const uint8_t rows[3] = {v->top, v->bottom, v->paged};
    h = fold(h, rows, sizeof rows);
    h = fold(h, &v->older, sizeof v->older);
    h = fold(h, &v->newer, sizeof v->newer);
    return fold(h, &v->start_at, sizeof v->start_at);
}
