/* A stand-in for `stream.c` in `run_jpanel_tests`, built only from `stream.h`'s contract: the
   ring's two cursors, the stop word, the stream word, `{stream_id, out_ms}`, `arb_hb` and the busy
   facts. The engine is tested against this; the end-to-end `jpanel_tests` run it against the real
   arbiter. Cues and the reply slot are inert: the engine never touches them. */

#include <string.h>

#include "fake_stream.h"
#include "stream.h"

static bool s_open;
static uint32_t s_out_samples;
static uint8_t s_next_id;
bool fake_stream_clip_busy;

void stream_init(stream_t *s, int16_t *ring, int16_t *play, stream_cue_buf_t *bufs[STREAM_SLOT_N])
{
    memset(s, 0, sizeof(*s));
    s->ring = ring;
    s->play = play;
    for (int i = 0; i < STREAM_SLOT_N; i++) s->buf[i] = bufs != NULL ? bufs[i] : NULL;
    s_open = false;
    s_out_samples = 0;
    s_next_id = 0;
    fake_stream_clip_busy = false;
}

void stream_set_arbiter(stream_t *s, bool present) { atomic_store(&s->arbiter, present); }

void stream_stop(stream_t *s, bool heard)
{
    const uint32_t w = atomic_load(&s->stop);
    atomic_store(&s->stop, (((w >> 1) + 1) << 1) | (w & 1u) | (heard ? 1u : 0u));
}

uint32_t stream_silence(stream_t *s)
{
    s->render_seq += 2;
    atomic_store(&s->silence, s->render_seq & 0xFFFFFFu);
    return s->render_seq;
}

uint32_t stream_cue(stream_t *s, stream_slot_t slot, cue_t c, int gain, unsigned variant)
{
    (void)gain;
    (void)variant;
    s->render_seq += 2;
    atomic_store(&s->slot[slot], ((s->render_seq & 0xFFFFFFu) << 8) | (uint32_t)c);
    return s->render_seq;
}

void stream_pause_req(stream_t *s, uint8_t stream_id, bool on)
{
    atomic_store(&s->pause_req, ((uint32_t)stream_id << 1) | (on ? 1u : 0u));
}

uint32_t stream_gen(const stream_t *s) { return atomic_load(&s->stop) >> 1; }

static bool word_stale(const stream_t *s)
{
    return ((atomic_load(&s->word) >> 8) & 0xFFFFu) != (stream_gen(s) & 0xFFFFu);
}

stream_facts_t stream_facts(const stream_t *s)
{
    const uint32_t out = atomic_load(&s->out);
    const bool live = !word_stale(s) && (s_open || atomic_load(&s->r) != atomic_load(&s->w));
    stream_facts_t f = {
        .stream_id = (uint8_t)(out >> 24),
        .out_ms = out & 0xFFFFFFu,
        .stream_live = live,
        .clip_busy = fake_stream_clip_busy,
        .speaker_busy = live || fake_stream_clip_busy,
        .reply_busy = false,
        .arb_hb = atomic_load(&s->arb_hb),
        .arbiter = atomic_load(&s->arbiter),
    };
    return f;
}

bool stream_lease_take(stream_t *s, uint32_t lease)
{
    uint32_t free_word = 0;
    return atomic_compare_exchange_strong(&s->lease, &free_word, lease);
}

void stream_lease_release(stream_t *s, uint32_t lease)
{
    uint32_t held = lease;
    (void)atomic_compare_exchange_strong(&s->lease, &held, 0u);
}

bool stream_leased(const stream_t *s) { return atomic_load(&s->lease) != 0; }

void stream_reply(stream_t *s, const int16_t *pcm, uint32_t samples, uint32_t id)
{
    atomic_store(&s->reply_ptr, pcm);
    atomic_store(&s->reply_len, samples);
    atomic_store(&s->reply_id, id);
    atomic_store(&s->reply_done, id);
}

bool stream_reply_done(const stream_t *s, uint32_t id) { return atomic_load(&s->reply_done) == id; }

bool stream_begin(stream_t *s, uint32_t gen, uint8_t *stream_id)
{
    if (!atomic_load(&s->arbiter)) return false;
    if (gen != stream_gen(s)) return false;
    if (s_open || atomic_load(&s->r) != atomic_load(&s->w)) return false;
    if (++s_next_id == 0) s_next_id = 1;
    *stream_id = s_next_id;
    atomic_store(&s->word, ((gen & 0xFFFFu) << 8) | s_next_id);
    s_out_samples = 0;
    atomic_store(&s->out, (uint32_t)s_next_id << 24);
    s_open = true;
    return true;
}

size_t stream_write(stream_t *s, const int16_t *pcm, size_t samples)
{
    if (word_stale(s)) return 0;
    const uint32_t w = atomic_load(&s->w);
    const uint32_t space = STREAM_RING_SAMPLES - (w - atomic_load(&s->r));
    const size_t n = samples < space ? samples : space;
    for (size_t i = 0; i < n; i++) s->ring[(w + i) % STREAM_RING_SAMPLES] = pcm[i];
    atomic_store(&s->w, w + (uint32_t)n);
    return n;
}

void stream_end(stream_t *s)
{
    (void)s;
    s_open = false;
}

bool stream_stale(const stream_t *s, uint32_t gen) { return gen != stream_gen(s); }

uint32_t stream_stop_word(const stream_t *s) { return atomic_load(&s->stop); }

void stream_classified(stream_t *s, uint32_t gen)
{
    const uint32_t w = atomic_load(&s->stop);
    if ((w >> 1) == gen) atomic_store(&s->stop, w & ~1u);
}

stream_tick_t stream_tick(stream_t *s, uint32_t now, int16_t *out, size_t chunk)
{
    (void)now;
    stream_tick_t t = {.n = 0, .capture = STREAM_CAPTURE_COPY};
    atomic_fetch_add(&s->arb_hb, 1);
    const uint32_t w = atomic_load(&s->w);
    uint32_t r = atomic_load(&s->r);
    if (word_stale(s)) {
        atomic_store(&s->r, w);
        if (!s_open) atomic_store(&s->seen_w, w);
        return t;
    }
    const uint32_t pr = atomic_load(&s->pause_req);
    const uint8_t id = (uint8_t)(atomic_load(&s->word) & 0xFFu);
    if ((pr & 1u) && (uint8_t)(pr >> 1) == id) return t;
    const uint32_t avail = w - r;
    const size_t n = avail < chunk ? avail : chunk;
    for (size_t i = 0; i < n; i++) {
        if (out != NULL) out[i] = s->ring[(r + i) % STREAM_RING_SAMPLES];
    }
    atomic_store(&s->r, r + (uint32_t)n);
    atomic_store(&s->seen_w, w);
    if (n > 0) {
        s_out_samples += (uint32_t)n;
        atomic_store(&s->out, ((uint32_t)id << 24) | ((s_out_samples / (AUDIO_RATE / 1000)) & 0xFFFFFFu));
        t.n = n;
        t.capture = STREAM_CAPTURE_SILENCE;
    }
    return t;
}
