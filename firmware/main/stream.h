#pragma once

/* The speaker's one owner (plan §7.4). Every per-chunk decision lives in `stream.c`, which is pure
 * and host-tested; `audio.c` keeps codec I/O, ALC, levels and `speech_feed` and only calls
 * `stream_tick` (`check_no_logic_in_audio` enforces that).
 *
 * WHO WRITES WHAT — every word has exactly one writer:
 *   render  the stop word, `silence`, the five cue slots and their buffers, `pause_req`, lease
 *           acquire (and lease 1's release when a post fails)
 *   engine  the ring's write cursor and the stream word (via stream_begin/write/end), lease 1
 *           release, the classified gen
 *   talk    the reply slot, lease 2 release
 *   arbiter the ring's read cursor, {stream_id, out_ms}, `arb_hb`, the busy facts, every *_done
 *
 * The struct is in this header so it can be one static instance and so host tests can build one;
 * nobody outside `stream.c` touches its fields. Words used with compare-and-swap (the lease) must
 * sit in internal RAM: S32C1I does not work on PSRAM on the S3. An all-zero instance is valid: no
 * arbiter, no buffers, every call a safe no-op — which is the no-codec panel. */

#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "audio.h" /* AUDIO_RATE, AUDIO_CHUNK */
#include "cue.h"
#include "ring.h"

/* The recording's ceiling, here rather than in `audio.c` so the machine and the backend pins read
   one number. UNDER the box's `/send` limit (`jpanel.MAX_MESSAGE_MS`, 30 s on today's box, checked
   on the raw length): a recording over it is refused with a 413 and lost (A3). Half a second under
   it, so a full one never sits on the line. W1 may raise both together. */
#define CAPTURE_MAX_MS 29500
/* The longest pet reply talk will read; the box's `PANEL_REPLY_MAX` is the same ten seconds. */
#define PLAY_BUF_MS 10000

/* 65,536 samples (4.096 s): a power of two, so `w % cap` stays continuous across the u32 wrap of
   the monotonic cursors, which are never reset (A9). The depth is a guess about the worst Wi-Fi
   gap in a bedroom, which the ring only has to outlast. */
#define STREAM_RING_SAMPLES 65536u
_Static_assert((STREAM_RING_SAMPLES & (STREAM_RING_SAMPLES - 1)) == 0, "ring must be 2^n");

/* What lands before the first sound: an ordinary hiccup stays inaudible and the tap still feels
   answered. A stream that ends shorter than this plays at once. */
#define STREAM_PREROLL_SAMPLES (AUDIO_RATE * 1500 / 1000)

/* Chunks the microphone stays deaf after the speaker last ran. The codec routes its DAC into its
   ADC (REG44), so the panel hears its own sound; this covers a chunk's tail. */
#define STREAM_DEAF_CHUNKS 6

/* A cue's loudness against the voice: the cue is the part heard closest to a child's face. */
#define STREAM_CUE_GAIN 20

#define CUE_MAX_MS (CUE_MAX_SAMPLES * 1000 / AUDIO_RATE)

/* How long an outcome cue may wait once it is eligible (no reply pending or sounding). */
#define STREAM_SEND_RESULT_TTL_MS 15000u
#define STREAM_REFUSAL_TTL_MS 2000u
#define STREAM_CHIME_TTL_MS 20000u

/* The arbiter is wedged when `arb_hb` has not moved for this long (§7.5d). */
#define STREAM_WD_ARBITER_MS 10000u

/* The cue slots, in the order the arbiter processes them each tick (after `silence`, stale-stream
   discard and the reply). */
typedef enum {
    STREAM_SLOT_PRESS = 0, /* today's rule: dropped if a cue or reply is sounding */
    STREAM_SLOT_SEND_RESULT, /* survives a silence (E1.2); latest wins; expires 15 s after eligible */
    STREAM_SLOT_FAIL,        /* latest wins; never overwrites a pending SENT */
    STREAM_SLOT_REFUSAL,     /* CUE_BUSY, coalesced; expires 2 s after eligible */
    STREAM_SLOT_CHIME,       /* expires 20 s after eligible */
    STREAM_SLOT_N,
} stream_slot_t;

/* A rendered cue: render owns it; the arbiter copies exactly `n` samples. */
typedef struct {
    uint32_t n;
    int16_t pcm[CUE_MAX_SAMPLES];
} stream_cue_buf_t;

typedef enum { STREAM_CAPTURE_NONE = 0, STREAM_CAPTURE_COPY, STREAM_CAPTURE_SILENCE } stream_capture_t;

/* One arbiter step: what to write to the codec this chunk, and what the capture gets. */
typedef struct {
    size_t n;                 /* samples written into `out`; 0 = nothing to play (IDLE) */
    stream_capture_t capture; /* SILENCE while our own sound would be heard (deafness) */
} stream_tick_t;

/* What render, and through it the machine, may know about the speaker. Render's copy also
   counts what the arbiter has not yet picked up: any cue-slot seq it wrote that the slot's done
   word has not reached, and a reply whose id is not yet done. */
typedef struct {
    uint8_t stream_id;  /* the stream sounding; 0 = none begun */
    uint32_t out_ms;    /* audible ms of that stream */
    bool speaker_busy;  /* a cue, a reply or a current-gen stream (today's audio_playing) */
    bool clip_busy;     /* a cue or a reply: the press-drop rule and the mic gate use this */
    bool reply_busy;    /* a reply pending or sounding */
    bool stream_live;   /* the current-gen stream is open or the ring is undrained */
    uint32_t arb_hb;
    bool arbiter;       /* the audio task exists (audio_start succeeded and xTaskCreate ran) */
    uint32_t cue_expired; /* outcome cues that waited past their TTL (telemetry) */
} stream_facts_t;

typedef enum { STREAM_SND_NONE = 0, STREAM_SND_CUE, STREAM_SND_REPLY } stream_snd_t;

/* Named so `audio.h`, which this header includes, can hand out a pointer to the instance. */
typedef struct stream_s {
    /* render */
    _Atomic uint32_t stop;      /* {gen31, heard1}; heard is sticky until classified */
    _Atomic uint32_t silence;   /* seq24 of the last silence; seqs come from one render counter */
    _Atomic uint32_t slot[STREAM_SLOT_N]; /* seq24 | cue8 each; odd seq = being written */
    stream_cue_buf_t *buf[STREAM_SLOT_N]; /* PSRAM, allocated by stream_init's caller */
    _Atomic uint32_t pause_req; /* {stream_id8, on1} */
    uint32_t render_seq;        /* render-private: the shared counter */
    uint8_t variant[CUE_COUNT]; /* render-private: per-cue, so two plays in a row differ */
    /* engine */
    _Atomic uint32_t word;      /* {open1, stream_id8, gen16} of the stream being produced */
    _Atomic uint32_t classified_gen;
    /* talk */
    const int16_t *_Atomic reply_ptr;
    _Atomic uint32_t reply_len, reply_stamp, reply_id; /* id written last, read first */
    /* arbiter */
    _Atomic uint32_t seen_w;
    _Atomic uint32_t out;       /* {stream_id8, out_ms24} */
    _Atomic uint32_t arb_hb;
    _Atomic uint32_t busy;      /* the busy facts as bits */
    _Atomic uint32_t done[STREAM_SLOT_N]; /* written on every disposal: played/dropped/cut/expired */
    _Atomic uint32_t reply_done;
    _Atomic uint32_t cue_expired;
    _Atomic bool arbiter;       /* set only after the audio task was created */
    /* arbiter-private */
    uint32_t a_silence;         /* the last silence processed */
    uint8_t a_stream_id;        /* the stream word's id last seen */
    bool primed;                /* the preroll landed for this stream */
    int deaf;
    uint32_t out_samples;
    stream_snd_t snd;           /* what `play_pos/play_len` are walking */
    int snd_slot;
    uint32_t snd_seq, snd_reply;
    const int16_t *snd_src;
    uint32_t elig_seq[STREAM_SLOT_N], elig_at[STREAM_SLOT_N];
    /* shared lease word: 0 free, 1 SEND, 2 talk. Internal RAM. */
    _Atomic uint32_t lease;
    /* STREAM_RING_SAMPLES in PSRAM; monotonic cursors, never reset: `w` is the engine's,
       `r` the arbiter's. */
    ring_t ring;
    int16_t *play;              /* CUE_MAX_SAMPLES: the cue being sounded; NULL if PSRAM failed */
    uint32_t play_pos, play_len;
} stream_t;

/* Allocation is the caller's (device: PSRAM; host: static). Any NULL buffer leaves the panel
   silent but consistent: done words are still written, so busy facts never latch. */
void stream_init(stream_t *s, int16_t *ring, int16_t *play, stream_cue_buf_t *bufs[STREAM_SLOT_N]);
void stream_set_arbiter(stream_t *s, bool present);

/* ── render ── */
/* Write the stop word before `silence`: the arbiter must never see the silence without the gen.
   `heard`: a REPLY stop, which acks even below the heard threshold. */
void stream_stop(stream_t *s, bool heard);
uint32_t stream_silence(stream_t *s);
/* Renders `c` into the slot's buffer under the per-slot seqlock and returns its seq; 0 (nothing
   written) with no arbiter or no buffer, so a slot can never wait on an arbiter that is not
   there. */
uint32_t stream_cue(stream_t *s, stream_slot_t slot, cue_t c, int gain, unsigned variant);
/* stream_cue at STREAM_CUE_GAIN with this cue's next variant (render's per-cue counter). */
uint32_t stream_cue_play(stream_t *s, stream_slot_t slot, cue_t c);
void stream_pause_req(stream_t *s, uint8_t stream_id, bool on);
stream_facts_t stream_facts(const stream_t *s);
uint32_t stream_gen(const stream_t *s);

/* The capture lease: 1 = SEND (render takes it before posting; the engine releases it at every
   end of the SEND), 2 = talk (render takes it before talk_send; talk releases it). A capture may
   not open while either is held. */
#define STREAM_LEASE_SEND 1u
#define STREAM_LEASE_TALK 2u
bool stream_lease_take(stream_t *s, uint32_t lease);
void stream_lease_release(stream_t *s, uint32_t lease);
bool stream_leased(const stream_t *s);

/* ── talk ── */
/* Plays `pcm` from talk's own buffer, no copy. Talk must not reuse the buffer or call this again
   until stream_reply_done(id). Stamped with the silence seq talk reads now, so a silence that
   comes later cuts it and one that came earlier does not. `id` is never 0 and differs from the
   previous one. With no arbiter it is done at once. */
void stream_reply(stream_t *s, const int16_t *pcm, uint32_t samples, uint32_t id);
bool stream_reply_done(const stream_t *s, uint32_t id);

/* ── engine (the producer) ── */
/* True when a stream could ever sound: an arbiter with a ring and a cue buffer. When false the
   engine ends a PLAY `failed("no-audio")` at once instead of waiting for a drain. */
bool stream_can_play(const stream_t *s);
/* False until the previous stream is drained or discarded (r == w == seen_w), or when there is
   no arbiter: refuses at once rather than waiting for a backstop. Also false for a stale `gen`,
   so the caller's wait must check stream_stale. */
bool stream_begin(stream_t *s, uint32_t gen, uint8_t *stream_id);
/* Samples accepted; 0 when the ring is full (the caller waits, ticking progress) or the gen is
   stale (the caller stops). */
size_t stream_write(stream_t *s, const int16_t *pcm, size_t samples);
void stream_end(stream_t *s);
bool stream_stale(const stream_t *s, uint32_t gen);
/* The stop word as the engine classifies a stream; then it publishes that it has (passing the
   gen of the word it read), which ends the heard bit's stickiness. */
uint32_t stream_stop_word(const stream_t *s);
void stream_classified(stream_t *s, uint32_t gen);

/* ── arbiter ── */
/* One chunk. Order: silence → stale-stream discard (r = w, every tick) → reply → cue slots in
   enum order → the stream, which yields to all of them chunk by chunk. Never blocks. */
stream_tick_t stream_tick(stream_t *s, uint32_t now, int16_t *out, size_t chunk);

/* ── the arbiter watchdog (§7.5d), pure so the no-codec rule is host-tested ── */
typedef struct {
    uint32_t hb, since;
    bool seen; /* an arb_hb change has been observed since boot */
} stream_wd_t;
/* True when the arbiter is wedged. `armed` is false with no sound and while an OTA runs; that
   restarts the clock. The 10 s count from the first observed change, never from boot, so a
   panel whose audio task never ran can never trip it. */
bool stream_wd_check(stream_wd_t *wd, bool armed, uint32_t arb_hb, uint32_t now);

#ifdef STREAM_TEST_HOOKS
/* Host-only interleave points: render mid-write, the arbiter between its copy and re-read, and
   the arbiter just after loading `w` and the stream word. */
typedef enum {
    STREAM_HOOK_RENDER_MID = 0,
    STREAM_HOOK_ARB_COPIED,
    STREAM_HOOK_ARB_WORD,
} stream_hook_t;
extern void (*stream_hook)(stream_t *s, stream_hook_t point);
#endif
