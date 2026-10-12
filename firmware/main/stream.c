#include "stream.h"

#include <string.h>

/* Every per-chunk decision about the speaker (plan §7.4), kept free of ESP-IDF so the host suite
 * drives it with a fake clock. Four tasks touch `stream_t` and each word has one writer; the
 * orderings below are the whole of the synchronisation, so each one says what it pairs with. */

#define SEQ_MASK 0xFFFFFFu
#define SEQ_HALF 0x800000u

#define WORD_GEN(w) ((w) & 0xFFFFu)
#define WORD_ID(w) ((uint8_t)(((w) >> 16) & 0xFFu))
#define WORD_OPEN 0x1000000u

#define BUSY_SPEAKER 1u
#define BUSY_CLIP 2u
#define BUSY_REPLY 4u
#define BUSY_LIVE 8u

#ifdef STREAM_TEST_HOOKS
void (*stream_hook)(stream_t *s, stream_hook_t point);
#define HOOK(s, p)                     \
    do {                               \
        if (stream_hook) stream_hook(s, p); \
    } while (0)
#else
#define HOOK(s, p) ((void)0)
#endif

#define LOAD(p, o) atomic_load_explicit(p, memory_order_##o)
#define STORE(p, v, o) atomic_store_explicit(p, v, memory_order_##o)

/* "a is at or after b" across the 24-bit wrap of the render counter. */
static bool seq24_ge(uint32_t a, uint32_t b)
{
    return ((a - b) & SEQ_MASK) < SEQ_HALF;
}

static bool seq24_before(uint32_t a, uint32_t b)
{
    return !seq24_ge(a, b);
}

static bool due(uint32_t now, uint32_t at)
{
    return (int32_t)(now - at) >= 0;
}

void stream_init(stream_t *s, int16_t *ring, int16_t *play, stream_cue_buf_t *bufs[STREAM_SLOT_N])
{
    memset(s, 0, sizeof(*s));
    ring_init(&s->ring, ring, ring != NULL ? (int)STREAM_RING_SAMPLES : 0);
    s->play = play;
    for (int i = 0; i < STREAM_SLOT_N; i++) s->buf[i] = bufs != NULL ? bufs[i] : NULL;
}

/* Called only after `xTaskCreate` succeeded: a flag raised before the task exists would promise
   render a speaker nobody drains, and every slot it wrote would wait forever. */
void stream_set_arbiter(stream_t *s, bool present)
{
    STORE(&s->arbiter, present, release);
}

/* ── render ── */

/* The next even seq of the one render counter; the odd value before it marks "being written".
   0 is skipped, because a zero slot word means "never written". */
static uint32_t next_seq(stream_t *s)
{
    s->render_seq = (s->render_seq + 2u) & SEQ_MASK;
    if (s->render_seq == 0) s->render_seq = 2u;
    return s->render_seq;
}

uint32_t stream_gen(const stream_t *s)
{
    return LOAD(&s->stop, acquire) >> 1;
}

void stream_stop(stream_t *s, bool heard)
{
    const uint32_t w = LOAD(&s->stop, relaxed);
    /* STICKY UNTIL CLASSIFIED: a standby or a recovery STOP inside the engine's classification
       window must not clear the REPLY stop's heard bit it is about to read. */
    const bool keep = (w & 1u) && LOAD(&s->classified_gen, acquire) != (w >> 1);
    const uint32_t gen = ((w >> 1) + 1u) & 0x7FFFFFFFu;
    STORE(&s->stop, (gen << 1) | ((heard || keep) ? 1u : 0u), release);
}

uint32_t stream_silence(stream_t *s)
{
    const uint32_t seq = next_seq(s);
    STORE(&s->silence, seq, release);
    return seq;
}

uint32_t stream_cue(stream_t *s, stream_slot_t slot, cue_t c, int gain, unsigned variant)
{
    if ((unsigned)slot >= STREAM_SLOT_N || (unsigned)c >= CUE_COUNT) return 0;
    stream_cue_buf_t *b = s->buf[slot];
    if (b == NULL || !LOAD(&s->arbiter, acquire)) return 0;
    const uint32_t seq = next_seq(s);
    /* §7.2's writer: odd, release fence, the samples and their length, then even with release.
       The arbiter re-reads the word after copying, so it can never pair this seq with the
       previous render's samples or `n`. */
    STORE(&s->slot[slot], ((seq - 1u) << 8) | (uint32_t)c, relaxed);
    atomic_thread_fence(memory_order_release);
    HOOK(s, STREAM_HOOK_RENDER_MID);
    const int n = cue_render(c, b->pcm, AUDIO_RATE, gain, variant);
    b->n = n > 0 ? (uint32_t)n : 0u;
    STORE(&s->slot[slot], (seq << 8) | (uint32_t)c, release);
    return seq;
}

/* The variant is counted per cue, not drawn at random: what a child does is poke the same spot
   four times, so two consecutive plays of the SAME cue are what must differ. */
uint32_t stream_cue_play(stream_t *s, stream_slot_t slot, cue_t c)
{
    if ((unsigned)c >= CUE_COUNT) return 0;
    return stream_cue(s, slot, c, STREAM_CUE_GAIN, s->variant[c]++);
}

void stream_pause_req(stream_t *s, uint8_t stream_id, bool on)
{
    STORE(&s->pause_req, ((uint32_t)stream_id << 8) | (on ? 1u : 0u), release);
}

stream_facts_t stream_facts(const stream_t *s)
{
    stream_facts_t f = {0};
    f.arbiter = LOAD(&s->arbiter, acquire);
    const uint32_t busy = LOAD(&s->busy, acquire);
    const uint32_t out = LOAD(&s->out, acquire);
    f.stream_id = (uint8_t)(out >> 24);
    f.out_ms = out & 0xFFFFFFu;
    f.speaker_busy = (busy & BUSY_SPEAKER) != 0;
    f.clip_busy = (busy & BUSY_CLIP) != 0;
    f.reply_busy = (busy & BUSY_REPLY) != 0;
    f.stream_live = (busy & BUSY_LIVE) != 0;
    f.arb_hb = LOAD(&s->arb_hb, relaxed);
    f.cue_expired = LOAD(&s->cue_expired, relaxed);
    /* THE STREAM IS READ OFF THE RING ITSELF, NOT OFF THE ARBITER'S LAST TICK. The busy word is a
       tick old, so a short body that landed whole and was closed between two ticks read as "not
       live" — and the engine classified a message it had not yet sounded as heard, acked it and
       ended the PLAY with the audio still to come (found by `run_e2e_tests`). A current-gen
       stream that is open or undrained is live from the producer's first write. */
    const uint32_t word = LOAD(&s->word, acquire);
    if (WORD_ID(word) != 0 && WORD_GEN(word) == (stream_gen(s) & 0xFFFFu) &&
        ((word & WORD_OPEN) != 0 || ring_filled(&s->ring) != 0)) {
        f.stream_live = f.speaker_busy = true;
    }
    /* WHAT THE ARBITER HAS NOT PICKED UP YET COUNTS. Without this the frame after a cue is
       written would read a quiet speaker and post a PLAY on top of it. Every disposal writes a
       done word, so none of these can latch. */
    for (int i = 0; i < STREAM_SLOT_N; i++) {
        uint32_t seq = LOAD(&s->slot[i], acquire) >> 8;
        if (seq == 0) continue;
        if (seq & 1u) seq = (seq + 1u) & SEQ_MASK;
        if (!seq24_ge(LOAD(&s->done[i], acquire), seq)) f.speaker_busy = f.clip_busy = true;
    }
    const uint32_t rid = LOAD(&s->reply_id, acquire);
    if (rid != 0 && rid != LOAD(&s->reply_done, acquire)) {
        f.reply_busy = f.speaker_busy = f.clip_busy = true;
    }
    return f;
}

/* ── the lease ── */

bool stream_lease_take(stream_t *s, uint32_t lease)
{
    uint32_t free_word = 0;
    return atomic_compare_exchange_strong_explicit(&s->lease, &free_word, lease,
                                                   memory_order_acq_rel, memory_order_acquire);
}

void stream_lease_release(stream_t *s, uint32_t lease)
{
    uint32_t held = lease;
    atomic_compare_exchange_strong_explicit(&s->lease, &held, 0u, memory_order_acq_rel,
                                            memory_order_acquire);
}

bool stream_leased(const stream_t *s)
{
    return LOAD(&s->lease, acquire) != 0;
}

/* ── talk ── */

void stream_reply(stream_t *s, const int16_t *pcm, uint32_t samples, uint32_t id)
{
    if (id == 0) return;
    if (!LOAD(&s->arbiter, acquire)) {
        /* Nobody will ever dispose of it, so it is disposed here; talk's wait on reply_done
           must not outlive a panel that has no speaker. */
        STORE(&s->reply_done, id, release);
        return;
    }
    STORE(&s->reply_ptr, pcm, relaxed);
    STORE(&s->reply_len, samples, relaxed);
    STORE(&s->reply_stamp, LOAD(&s->silence, acquire), relaxed);
    STORE(&s->reply_id, id, release); /* last: the arbiter reads id first, with acquire */
}

bool stream_reply_done(const stream_t *s, uint32_t id)
{
    return LOAD(&s->reply_done, acquire) == id;
}

/* ── engine ── */

bool stream_can_play(const stream_t *s)
{
    return LOAD(&s->arbiter, acquire) && s->ring.buf != NULL && s->play != NULL;
}

bool stream_stale(const stream_t *s, uint32_t gen)
{
    return (gen & 0x7FFFFFFFu) != stream_gen(s);
}

bool stream_begin(stream_t *s, uint32_t gen, uint8_t *stream_id)
{
    if (!stream_can_play(s) || stream_stale(s, gen)) return false;
    /* `seen_w == w` says the arbiter has observed the last write, so its `r == w` covers every
       sample of the previous stream — drained or discarded, never cut short by a reset. */
    const uint32_t w = ring_written(&s->ring);
    if (LOAD(&s->seen_w, acquire) != w || ring_filled(&s->ring) != 0) return false;
    uint8_t id = (uint8_t)(WORD_ID(LOAD(&s->word, relaxed)) + 1u);
    if (id == 0) id = 1;
    /* Published before the first write to `w`, with release; the arbiter loads `w` with
       acquire and then this word, so new samples always arrive under the new word. */
    STORE(&s->word, WORD_OPEN | ((uint32_t)id << 16) | (gen & 0xFFFFu), release);
    if (stream_id != NULL) *stream_id = id;
    return true;
}

size_t stream_write(stream_t *s, const int16_t *pcm, size_t samples)
{
    if (pcm == NULL) return 0;
    if (samples > STREAM_RING_SAMPLES) samples = STREAM_RING_SAMPLES;
    const uint32_t word = LOAD(&s->word, relaxed);
    if (!(word & WORD_OPEN) || WORD_GEN(word) != (stream_gen(s) & 0xFFFFu)) return 0;
    return (size_t)ring_write(&s->ring, pcm, (int)(samples * sizeof(int16_t))) / sizeof(int16_t);
}

void stream_end(stream_t *s)
{
    STORE(&s->word, LOAD(&s->word, relaxed) & ~WORD_OPEN, release);
}

uint32_t stream_stop_word(const stream_t *s)
{
    return LOAD(&s->stop, acquire);
}

void stream_classified(stream_t *s, uint32_t gen)
{
    STORE(&s->classified_gen, gen & 0x7FFFFFFFu, release);
}

/* ── arbiter ── */

static void dispose_cue(stream_t *s, int slot, uint32_t seq)
{
    STORE(&s->done[slot], seq, release);
}

static void finish_sound(stream_t *s)
{
    if (s->snd == STREAM_SND_CUE) dispose_cue(s, s->snd_slot, s->snd_seq);
    if (s->snd == STREAM_SND_REPLY) STORE(&s->reply_done, s->snd_reply, release);
    s->snd = STREAM_SND_NONE;
    s->snd_src = NULL;
    s->play_pos = s->play_len = 0;
}

/* Copies a cue whole into `play` under the slot's seqlock. False when render was writing or
   rewrote the slot meanwhile: the slot is skipped until the next tick, bounded, so the arbiter
   never spins against the lower-priority render task. */
static bool start_cue(stream_t *s, int slot, uint32_t word)
{
    const uint32_t seq = word >> 8;
    const stream_cue_buf_t *b = s->buf[slot];
    if (s->play == NULL || b == NULL) {
        dispose_cue(s, slot, seq);
        return true;
    }
    uint32_t n = b->n;
    if (n > CUE_MAX_SAMPLES) n = CUE_MAX_SAMPLES;
    memcpy(s->play, b->pcm, (size_t)n * sizeof(int16_t));
    HOOK(s, STREAM_HOOK_ARB_COPIED);
    atomic_thread_fence(memory_order_acquire);
    if (LOAD(&s->slot[slot], relaxed) != word) return false;
    if (n == 0) {
        dispose_cue(s, slot, seq);
        return true;
    }
    s->snd = STREAM_SND_CUE;
    s->snd_slot = slot;
    s->snd_seq = seq;
    s->snd_src = s->play;
    s->play_pos = 0;
    s->play_len = n;
    return true;
}

static uint32_t slot_ttl(int slot)
{
    switch (slot) {
    case STREAM_SLOT_SEND_RESULT: return STREAM_SEND_RESULT_TTL_MS;
    case STREAM_SLOT_REFUSAL: return STREAM_REFUSAL_TTL_MS;
    case STREAM_SLOT_CHIME: return STREAM_CHIME_TTL_MS;
    default: return 0;
    }
}

stream_tick_t stream_tick(stream_t *s, uint32_t now, int16_t *out, size_t chunk)
{
    stream_tick_t t = {0, STREAM_CAPTURE_COPY};

    /* The slot words BEFORE `silence`: render publishes a silence before the press it pairs
       with, so a press seen here always comes with its silence. */
    uint32_t sw[STREAM_SLOT_N];
    for (int i = 0; i < STREAM_SLOT_N; i++) sw[i] = LOAD(&s->slot[i], acquire);
    const uint32_t sil = LOAD(&s->silence, acquire);
    const uint32_t gen16 = (LOAD(&s->stop, acquire) >> 1) & 0xFFFFu;
    /* `w` with acquire and THEN the word, which the producer publishes before its first write:
       samples under this `w` belong to this word or an older one, never to the next stream. Every
       discard below is to this `w`, so it can never eat the next message's preroll. */
    const uint32_t w = ring_written(&s->ring);
    const uint32_t word = LOAD(&s->word, acquire);
    HOOK(s, STREAM_HOOK_ARB_WORD);

    /* 1. silence: cuts what is sounding from before it, and discards the stream once. Pending
          slots and replies from before it are disposed below, where each is read. */
    if (sil != s->a_silence) {
        s->a_silence = sil;
        if ((s->snd == STREAM_SND_CUE && seq24_before(s->snd_seq, sil)) ||
            (s->snd == STREAM_SND_REPLY && seq24_before(LOAD(&s->reply_stamp, relaxed), sil))) {
            finish_sound(s);
        }
        ring_discard(&s->ring, w);
        s->primed = false;
    }

    /* 2. stale-stream discard, every tick and whatever the pause says: `stream_begin` waits at
          most one tick after a stop, and a producer stalled in an HTTPS body cannot hold the
          speaker. */
    const bool current = WORD_GEN(word) == gen16;
    if (!current) {
        ring_discard(&s->ring, w);
        s->primed = false;
    }
    if (WORD_ID(word) != s->a_stream_id) {
        s->a_stream_id = WORD_ID(word);
        s->out_samples = 0;
        s->primed = false;
        STORE(&s->out, (uint32_t)s->a_stream_id << 24, release);
    }
    STORE(&s->seen_w, w, release);

    /* 3. the reply: highest priority, waits out a cue already sounding, never refused. */
    const uint32_t rid = LOAD(&s->reply_id, acquire);
    bool reply_pending = false;
    if (rid != 0 && rid != LOAD(&s->reply_done, relaxed) &&
        !(s->snd == STREAM_SND_REPLY && s->snd_reply == rid)) {
        const int16_t *ptr = LOAD(&s->reply_ptr, relaxed);
        const uint32_t len = LOAD(&s->reply_len, relaxed);
        if (seq24_before(LOAD(&s->reply_stamp, relaxed), s->a_silence) || ptr == NULL ||
            len == 0) {
            STORE(&s->reply_done, rid, release);
        } else if (s->snd == STREAM_SND_NONE) {
            s->snd = STREAM_SND_REPLY;
            s->snd_reply = rid;
            s->snd_src = ptr;
            s->play_pos = 0;
            s->play_len = len;
        } else {
            reply_pending = true;
        }
    }
    const bool reply_active = reply_pending || s->snd == STREAM_SND_REPLY;

    /* 4. the cue slots, in enum order. */
    for (int i = 0; i < STREAM_SLOT_N; i++) {
        const uint32_t seq = sw[i] >> 8;
        if (seq == 0 || (seq & 1u)) continue;
        if (seq24_ge(LOAD(&s->done[i], relaxed), seq)) continue;
        if (s->snd == STREAM_SND_CUE && s->snd_slot == i && s->snd_seq == seq) continue;
        /* Only a pending send_result outlives a silence (E1.2): a failed turn's OOPS must not
           sound in the dark after a stop. */
        if (i != STREAM_SLOT_SEND_RESULT && seq24_before(seq, s->a_silence)) {
            dispose_cue(s, i, seq);
            continue;
        }
        if (i == STREAM_SLOT_PRESS) {
            /* Today's rule: a press never cuts a cue or a reply, it is dropped behind one. The
               stream does not count; it yields chunk by chunk below. */
            if (s->snd != STREAM_SND_NONE) {
                dispose_cue(s, i, seq);
            } else {
                start_cue(s, i, sw[i]);
            }
            continue;
        }
        if (reply_active) continue;
        if (s->elig_seq[i] != seq) {
            s->elig_seq[i] = seq;
            s->elig_at[i] = now;
        }
        const uint32_t ttl = slot_ttl(i);
        if (ttl != 0 && due(now, s->elig_at[i] + ttl)) {
            dispose_cue(s, i, seq);
            atomic_fetch_add_explicit(&s->cue_expired, 1u, memory_order_relaxed);
            continue;
        }
        if (s->snd == STREAM_SND_NONE) start_cue(s, i, sw[i]);
    }

    /* 5. one chunk: the clip if one sounds, else the stream. */
    const uint32_t filled = (uint32_t)ring_filled(&s->ring);
    const uint32_t pr = LOAD(&s->pause_req, acquire);
    const bool paused = (pr & 1u) && (uint8_t)(pr >> 8) == WORD_ID(word);
    const bool open = (word & WORD_OPEN) != 0;
    if (s->snd != STREAM_SND_NONE) {
        const uint32_t left = s->play_len - s->play_pos;
        const size_t take = left < chunk ? left : chunk;
        memcpy(out, s->snd_src + s->play_pos, take * sizeof(int16_t));
        s->play_pos += (uint32_t)take;
        t.n = take;
        if (s->play_pos >= s->play_len) finish_sound(s);
    } else if (current && !paused && filled > 0) {
        /* A message shorter than the preroll plays once its producer is done with it. */
        if (!s->primed && (filled >= STREAM_PREROLL_SAMPLES || !open)) s->primed = true;
        /* An underrun waits rather than playing short: a partial chunk into a blocking codec
           write is a click, a skipped pass is 40 ms the ear does not catch. */
        if (s->primed && (filled >= chunk || !open)) {
            const int want = filled < chunk ? (int)filled : (int)chunk;
            int got = 0;
            while (got < want) {
                int at = 0;
                const int run = ring_read_run(&s->ring, want - got, &at);
                if (run <= 0) break;
                memcpy(out + got, &s->ring.buf[at], (size_t)run * sizeof(int16_t));
                ring_advance(&s->ring, run); /* the producer may reuse these samples now */
                got += run;
            }
            s->out_samples += (uint32_t)got;
            const uint32_t ms = (uint32_t)((uint64_t)s->out_samples * 1000u / AUDIO_RATE);
            STORE(&s->out, ((uint32_t)s->a_stream_id << 24) | (ms & 0xFFFFFFu), release);
            t.n = (size_t)got;
        }
    }

    /* 6. deafness, and what the capture keeps. A recording keeps its timeline through the panel's
          own sound: SILENCE, never NONE, so a cue mid-sentence leaves a gap the length of the cue
          instead of splicing the ends together. */
    if (t.n > 0) s->deaf = STREAM_DEAF_CHUNKS;
    if (s->deaf > 0) {
        s->deaf--;
        t.capture = STREAM_CAPTURE_SILENCE;
    }

    /* 7. the busy facts. A stale stream counts in none of them. */
    const bool live = current && (open || ring_filled(&s->ring) != 0);
    const bool clip = s->snd != STREAM_SND_NONE;
    const uint32_t rd = LOAD(&s->reply_done, relaxed);
    const bool reply = s->snd == STREAM_SND_REPLY || (rid != 0 && rid != rd);
    STORE(&s->busy,
          (clip || live ? BUSY_SPEAKER : 0u) | (clip ? BUSY_CLIP : 0u) | (reply ? BUSY_REPLY : 0u) |
              (live ? BUSY_LIVE : 0u),
          release);
    atomic_fetch_add_explicit(&s->arb_hb, 1u, memory_order_release);
    return t;
}

bool stream_wd_check(stream_wd_t *wd, bool armed, uint32_t arb_hb, uint32_t now)
{
    if (arb_hb != wd->hb) {
        wd->hb = arb_hb;
        wd->seen = true;
        wd->since = now;
    }
    if (!armed) {
        wd->since = now;
        return false;
    }
    return wd->seen && due(now, wd->since + STREAM_WD_ARBITER_MS);
}
