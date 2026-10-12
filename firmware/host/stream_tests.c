/* The arbiter's suite (`run_stream_tests`): `stream.c` driven tick by tick with a fake clock.
 *
 * Every assertion is about what reaches the SPEAKER — the samples `stream_tick` hands the codec —
 * or what render may know about it (`stream_facts`), because those are the two things a child
 * and the machine can observe. Cues are compared against `cue_render` at the same gain and
 * variant, so "the stop cue sounded" means its samples came out, in order and whole. Fields are
 * touched only to start the cursors near their u32 wrap. */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "stream.h"

static int checks;
#define CHECK(c, msg)                                                       \
    do {                                                                    \
        checks++;                                                           \
        if (!(c)) {                                                         \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, (msg)); \
            exit(1);                                                        \
        }                                                                   \
    } while (0)

#define CHUNK AUDIO_CHUNK
#define TICK_MS AUDIO_CHUNK_MS
#define CAP_SAMPLES (AUDIO_RATE * 30)

static stream_t st;
static int16_t ring_buf[STREAM_RING_SAMPLES];
static int16_t play_buf[CUE_MAX_SAMPLES];
static stream_cue_buf_t slot_buf[STREAM_SLOT_N];
static uint32_t now;
static int16_t heard[CAP_SAMPLES]; /* what the speaker said since the last reset */
static int heard_n;
static int16_t reply_pcm[AUDIO_RATE * 10];
static int16_t pcm_in[STREAM_RING_SAMPLES];

static void setup(bool arbiter, bool with_play)
{
    stream_cue_buf_t *bufs[STREAM_SLOT_N];
    for (int i = 0; i < STREAM_SLOT_N; i++) bufs[i] = &slot_buf[i];
    stream_init(&st, ring_buf, with_play ? play_buf : NULL, bufs);
    stream_set_arbiter(&st, arbiter);
    now = 1000;
    heard_n = 0;
#ifdef STREAM_TEST_HOOKS
    stream_hook = NULL;
#endif
}

static stream_tick_t tick(void)
{
    int16_t out[CHUNK];
    const stream_tick_t t = stream_tick(&st, now, out, CHUNK);
    for (size_t i = 0; i < t.n && heard_n < CAP_SAMPLES; i++) heard[heard_n++] = out[i];
    now += TICK_MS;
    return t;
}

static void ticks(int n)
{
    for (int i = 0; i < n; i++) tick();
}

/* Ticks until the speaker has been quiet for a whole second, or a minute passes. */
static void until_quiet(void)
{
    int quiet = 0;
    for (int i = 0; i < 1500 && quiet < 25; i++) quiet = tick().n == 0 ? quiet + 1 : 0;
}

static int ref(cue_t c, unsigned variant, int16_t *buf)
{
    return cue_render(c, buf, AUDIO_RATE, STREAM_CUE_GAIN, variant);
}

/* True when `heard[from..]` starts with cue `c` at `variant`, whole. */
static bool sounded(int from, cue_t c, unsigned variant)
{
    static int16_t r[CUE_MAX_SAMPLES];
    const int n = ref(c, variant, r);
    return n > 0 && heard_n - from >= n && memcmp(&heard[from], r, (size_t)n * 2) == 0;
}

static void fill_reply(int n, int16_t v)
{
    for (int i = 0; i < n; i++) reply_pcm[i] = v;
}

/* A ramp no cue or reply contains, so a message sample is recognisable wherever it lands. */
static int16_t ramp(uint32_t i)
{
    return (int16_t)(20000 + (i % 5000));
}

static uint32_t write_ramp(uint32_t from, uint32_t n)
{
    for (uint32_t i = 0; i < n; i++) pcm_in[i] = ramp(from + i);
    return (uint32_t)stream_write(&st, pcm_in, n);
}

static uint8_t begin(void)
{
    uint8_t id = 0;
    CHECK(stream_begin(&st, stream_gen(&st), &id), "a stream begins on a drained speaker");
    CHECK(id != 0, "and gets a real id");
    return id;
}

static int count_ramp(void)
{
    int n = 0;
    for (int i = 0; i < heard_n; i++) n += heard[i] >= 20000 && heard[i] < 25000;
    return n;
}

/* ---- the stream -------------------------------------------------------------------------- */

static void test_stop_while_paused_drains(void)
{
    /* A1: a pause held the ring, the stop came, and the next message could never begin —
       the drain the producer waited on was itself paused. Discard ignores the pause. */
    setup(true, true);
    const uint8_t id = begin();
    CHECK(write_ramp(0, 30000) == 30000, "the message lands");
    ticks(45);
    CHECK(count_ramp() > 0, "and plays");
    stream_pause_req(&st, id, true);
    const int before = heard_n;
    ticks(5);
    CHECK(heard_n == before, "a pause holds it");
    CHECK(stream_facts(&st).stream_live, "and a held stream is still live");
    stream_stop(&st, false);
    tick();
    const stream_facts_t f = stream_facts(&st);
    CHECK(!f.stream_live && !f.speaker_busy, "a stop frees the speaker in one tick, paused or not");
    CHECK(heard_n == before, "and nothing of the stopped message sounds");
    uint8_t next = 0;
    CHECK(stream_begin(&st, stream_gen(&st), &next), "so the next message can begin");
    CHECK(next != id, "as a new stream");
}

static void test_stop_with_no_stream_then_play_starts(void)
{
    setup(true, true);
    stream_stop(&st, false);
    tick();
    begin();
    write_ramp(0, STREAM_PREROLL_SAMPLES + CHUNK);
    ticks(2);
    CHECK(count_ramp() > 0, "a stop with nothing playing leaves the next message free to sound");
}

static void test_stale_body_not_drained(void)
{
    /* The producer can be stalled in an HTTPS body for LINK_STALL_MS without noticing a stop;
       the arbiter knows the gen, so none of it sounds and none of it counts as busy. */
    setup(true, true);
    const uint32_t gen = stream_gen(&st);
    begin();
    write_ramp(0, 20000);
    stream_stop(&st, false);
    CHECK(stream_stale(&st, gen), "the producer's gen is stale");
    CHECK(write_ramp(20000, 1000) == 0, "and the sink refuses its next chunk");
    ticks(60);
    CHECK(count_ramp() == 0, "what was already in the ring never plays");
    CHECK(!stream_facts(&st).speaker_busy, "and a stalled producer cannot hold the speaker");
    CHECK(!stream_begin(&st, gen, NULL), "a stale gen cannot begin");
}

static void test_stale_word_never_eats_next_preroll(void)
{
    setup(true, true);
    begin();
    write_ramp(0, 10000);
    stream_stop(&st, false);
    tick();
    begin();
    write_ramp(0, STREAM_PREROLL_SAMPLES);
    tick();
    CHECK(count_ramp() == CHUNK, "the next message's preroll survives the previous discard");
}

/* The engine begins the next message and lands its preroll just after the arbiter read `w` and
   the (stale) stream word, before it discards. */
static void hook_begin_between(stream_t *s, stream_hook_t p)
{
    if (p != STREAM_HOOK_ARB_WORD) return;
    stream_hook = NULL;
    uint8_t id = 0;
    CHECK(stream_begin(s, stream_gen(s), &id), "the next stream may begin");
    write_ramp(0, STREAM_PREROLL_SAMPLES + CHUNK);
}

static void test_stale_word_discard_races_the_next_begin(void)
{
    /* The stale discard catches up to the `w` it read with the word, never a fresher one: that
       could already cover the next stream's preroll, published under a word this tick never
       saw. */
    setup(true, true);
    begin();
    write_ramp(0, 10000);
    stream_stop(&st, false);
    tick();
    stream_hook = hook_begin_between;
    tick();
    stream_hook = NULL;
    ticks(3);
    CHECK(count_ramp() >= 3 * CHUNK, "the racing preroll is not eaten");
}

static void test_stop_during_drain_frees_speaker(void)
{
    setup(true, true);
    begin();
    write_ramp(0, 30000);
    stream_end(&st);
    ticks(10);
    CHECK(stream_facts(&st).speaker_busy, "a draining message holds the speaker");
    stream_stop(&st, false);
    tick();
    CHECK(!stream_facts(&st).speaker_busy, "and a stop frees it at the next tick");
}

static void test_short_message_plays_without_its_preroll(void)
{
    setup(true, true);
    begin();
    write_ramp(0, 1000);
    tick();
    CHECK(count_ramp() == 0, "an open stream waits for its preroll");
    stream_end(&st);
    until_quiet();
    CHECK(count_ramp() == 1000, "a finished short one plays whole");
    CHECK(!stream_facts(&st).stream_live, "and then the speaker is free");
}

static void test_pause_scoped_to_stream_id(void)
{
    /* A pause pressed before the first byte must not hold the message that then plays. */
    setup(true, true);
    const uint8_t id = begin();
    stream_end(&st);
    tick();
    stream_pause_req(&st, id, true);
    const uint8_t next = begin();
    write_ramp(0, STREAM_PREROLL_SAMPLES);
    ticks(2);
    CHECK(count_ramp() > 0, "another stream's pause is ignored");
    stream_pause_req(&st, next, true);
    const int before = count_ramp();
    ticks(3);
    CHECK(count_ramp() == before, "its own pause holds it");
    stream_pause_req(&st, next, false);
    ticks(2);
    CHECK(count_ramp() > before, "and resume picks up where it stopped");
}

static void test_cursor_writers(void)
{
    /* A9: no reset, two writers. The cursors start just short of the u32 wrap; a message crossing
       it plays continuous, a stop moves only `r`, and the next stream carries on from there. */
    setup(true, true);
    const uint32_t start = 0xFFFFFF00u - 3000u;
    atomic_store(&st.ring.w, start);
    atomic_store(&st.ring.r, start);
    atomic_store(&st.seen_w, start);
    begin();
    uint32_t sent = 0;
    while (sent < 40000) {
        const uint32_t got = write_ramp(sent, 40000 - sent);
        sent += got;
        if (got == 0) tick();
    }
    stream_end(&st);
    until_quiet();
    int ordered = 1;
    int k = 0;
    for (int i = 0; i < heard_n; i++) {
        if (heard[i] < 20000) continue;
        ordered &= heard[i] == ramp((uint32_t)k++);
    }
    CHECK(k == 40000 && ordered, "every sample across the counter wrap plays once, in order");
    const uint32_t w = atomic_load(&st.ring.w);
    CHECK(w == start + 40000u && w < start, "the write cursor wrapped and was never reset");
    begin();
    write_ramp(0, 5000);
    stream_stop(&st, false);
    tick();
    CHECK(atomic_load(&st.ring.w) == w + 5000u, "a stop never touches the producer's cursor");
    CHECK(atomic_load(&st.ring.r) == w + 5000u, "the arbiter's discard moves only its own");
}

/* ---- cues and the reply ------------------------------------------------------------------- */

static void test_heard_then_oops_both_sound(void)
{
    /* B4: today's tick path played CUE_HEARD and then a failed send's CUE_OOPS into the same
       busy buffer, and the OOPS was always swallowed. An outcome waits out a press cue. */
    setup(true, true);
    stream_cue(&st, STREAM_SLOT_PRESS, CUE_HEARD, STREAM_CUE_GAIN, 0);
    tick();
    stream_cue(&st, STREAM_SLOT_FAIL, CUE_OOPS, STREAM_CUE_GAIN, 0);
    until_quiet();
    static int16_t r[CUE_MAX_SAMPLES];
    CHECK(sounded(0, CUE_HEARD, 0), "the press cue plays whole");
    CHECK(sounded(ref(CUE_HEARD, 0, r), CUE_OOPS, 0), "and the outcome follows it, whole");
    CHECK(!stream_facts(&st).speaker_busy, "and then nothing is owed");
}

static void test_press_never_cuts_outcome(void)
{
    /* A poke during the outcome cue is dropped, as a press behind any sounding clip always was,
       and the outcome plays to its end. */
    setup(true, true);
    stream_cue(&st, STREAM_SLOT_SEND_RESULT, CUE_SENT, STREAM_CUE_GAIN, 0);
    tick();
    stream_cue(&st, STREAM_SLOT_PRESS, CUE_GIGGLE, STREAM_CUE_GAIN, 0);
    until_quiet();
    static int16_t r[CUE_MAX_SAMPLES];
    CHECK(sounded(0, CUE_SENT, 0), "the outcome is whole");
    CHECK(heard_n == ref(CUE_SENT, 0, r), "and the poke never sounded");
    CHECK(!stream_facts(&st).speaker_busy, "and the dropped press still wrote its done word");
}

static void test_poke_during_stream_sounds(void)
{
    /* The stream does not count for the press rule: PAUSE/RESUME/REPLY sound during playback. */
    setup(true, true);
    begin();
    write_ramp(0, 40000);
    ticks(40);
    const int at = heard_n;
    stream_cue(&st, STREAM_SLOT_PRESS, CUE_PAUSE, STREAM_CUE_GAIN, 0);
    ticks(10);
    CHECK(sounded(at, CUE_PAUSE, 0), "a press cue interleaves with the message, chunk by chunk");
}

static void test_outcome_waits_out_reply(void)
{
    /* B4: SENT written while the pet speaks waits for the reply, however long, because its
       expiry runs from when it first became eligible, not from when it was written. */
    setup(true, true);
    const int n = AUDIO_RATE * 4;
    fill_reply(n, 1234);
    stream_reply(&st, reply_pcm, (uint32_t)n, 7);
    tick();
    stream_cue(&st, STREAM_SLOT_SEND_RESULT, CUE_SENT, STREAM_CUE_GAIN, 0);
    CHECK(stream_facts(&st).reply_busy, "the reply is busy");
    now += 30000; /* a starved arbiter: far past the 15 s TTL since the write */
    until_quiet();
    int reply_ok = 1;
    for (int i = 0; i < n; i++) reply_ok &= heard[i] == 1234;
    CHECK(reply_ok, "the reply plays whole and first");
    CHECK(sounded(n, CUE_SENT, 0), "then the outcome, which never expired while it was waiting");
    CHECK(stream_reply_done(&st, 7), "the reply's done word is written");
    CHECK(atomic_load(&st.cue_expired) == 0, "and nothing counted as expired");
}

static void test_reply_waits_out_a_sounding_cue(void)
{
    setup(true, true);
    stream_cue(&st, STREAM_SLOT_PRESS, CUE_BLUSH, STREAM_CUE_GAIN, 0);
    tick();
    fill_reply(2000, 777);
    stream_reply(&st, reply_pcm, 2000, 3);
    until_quiet();
    static int16_t r[CUE_MAX_SAMPLES];
    const int c = ref(CUE_BLUSH, 0, r);
    CHECK(sounded(0, CUE_BLUSH, 0), "the cue is not cut by a reply");
    CHECK(heard_n == c + 2000 && heard[c] == 777, "and the reply follows it, never refused");
}

static void test_standby_silences_then_sounds_its_own_stop(void)
{
    /* On 0.3.39 standby's CUE_STOP was cut in the same frame by audio_stop(). Now silence comes
       first, so the stop cue — a later seq — is never dropped against what silence cuts; a
       pending SENT survives the silence and plays after it (E1.2). */
    setup(true, true);
    begin();
    write_ramp(0, 40000);
    stream_cue(&st, STREAM_SLOT_PRESS, CUE_GIGGLE, STREAM_CUE_GAIN, 0);
    tick();
    stream_cue(&st, STREAM_SLOT_SEND_RESULT, CUE_SENT, STREAM_CUE_GAIN, 0);
    stream_cue(&st, STREAM_SLOT_CHIME, CUE_MESSAGE, STREAM_CUE_GAIN, 0);
    stream_stop(&st, false);
    stream_silence(&st);
    stream_cue(&st, STREAM_SLOT_PRESS, CUE_STOP, STREAM_CUE_GAIN, 0);
    const int at = heard_n;
    until_quiet();
    static int16_t r[CUE_MAX_SAMPLES];
    CHECK(sounded(at, CUE_STOP, 0), "the giggle is cut and the stop cue sounds at once");
    CHECK(sounded(at + ref(CUE_STOP, 0, r), CUE_SENT, 0), "a pending SENT survives and follows");
    CHECK(heard_n == at + ref(CUE_STOP, 0, r) + ref(CUE_SENT, 0, r),
          "the chime from before the silence never sounds");
    CHECK(count_ramp() == 0, "and the message is discarded");
    CHECK(!stream_facts(&st).speaker_busy, "nothing is owed after");
}

static void test_exit_during_pet_reply_silences_it_then_sounds_stop(void)
{
    setup(true, true);
    fill_reply(AUDIO_RATE * 5, 4321);
    stream_reply(&st, reply_pcm, AUDIO_RATE * 5, 11);
    ticks(5);
    stream_silence(&st);
    stream_cue(&st, STREAM_SLOT_PRESS, CUE_STOP, STREAM_CUE_GAIN, 0);
    const int at = heard_n;
    tick();
    CHECK(stream_reply_done(&st, 11), "the reply is cut and its done word written");
    until_quiet();
    static int16_t r[CUE_MAX_SAMPLES];
    CHECK(sounded(at, CUE_STOP, 0), "the stop cue is not dropped behind the reply it cut");
    CHECK(heard_n == at + ref(CUE_STOP, 0, r), "and nothing of the reply follows");
}

static void test_silence_discards_the_ring_once(void)
{
    /* Silence cuts what is buffered at that moment; a producer that was not stopped carries on,
       and what it writes afterwards plays. */
    setup(true, true);
    begin();
    write_ramp(0, 30000);
    ticks(40);
    const int before = count_ramp();
    stream_silence(&st);
    tick();
    CHECK(count_ramp() == before, "the buffered rest is discarded");
    write_ramp(50000, 3000);
    stream_end(&st);
    until_quiet();
    CHECK(count_ramp() == before + 3000, "and only what came after the silence plays");
    CHECK(heard[heard_n - 3000] == ramp(50000), "starting from its first sample");
}

static void test_reply_slot_stamp_and_silence(void)
{
    /* A reply submitted after a silence plays, as today, even before the arbiter has seen that
       silence; one submitted before it is cut, even if the arbiter never saw it sound. */
    setup(true, true);
    stream_silence(&st);
    fill_reply(1000, 999);
    stream_reply(&st, reply_pcm, 1000, 1);
    until_quiet();
    CHECK(heard_n == 1000 && heard[0] == 999, "a reply after the silence plays");
    heard_n = 0;
    stream_reply(&st, reply_pcm, 1000, 2);
    stream_silence(&st);
    until_quiet();
    CHECK(heard_n == 0, "a reply before the silence is cut before it sounds");
    CHECK(stream_reply_done(&st, 2), "and talk is told it is done with its buffer");
}

static void test_refusal_coalesced_and_expires_from_eligibility(void)
{
    setup(true, true);
    stream_cue(&st, STREAM_SLOT_REFUSAL, CUE_BUSY, STREAM_CUE_GAIN, 0);
    stream_cue(&st, STREAM_SLOT_REFUSAL, CUE_BUSY, STREAM_CUE_GAIN, 1);
    until_quiet();
    static int16_t r[CUE_MAX_SAMPLES];
    CHECK(sounded(0, CUE_BUSY, 1) && heard_n == ref(CUE_BUSY, 1, r),
          "two refusals in one tick sound once, the latest");

    /* Behind a cue: eligible at once, so a starved arbiter expires it 2 s later. */
    setup(true, true);
    stream_cue(&st, STREAM_SLOT_PRESS, CUE_FART, STREAM_CUE_GAIN, 0);
    tick();
    stream_cue(&st, STREAM_SLOT_REFUSAL, CUE_BUSY, STREAM_CUE_GAIN, 0);
    tick();
    now += STREAM_REFUSAL_TTL_MS;
    until_quiet();
    CHECK(sounded(0, CUE_FART, 0) && heard_n == ref(CUE_FART, 0, r),
          "a refusal past its TTL never sounds late");
    CHECK(atomic_load(&st.cue_expired) == 1, "and is counted as expired");
    CHECK(!stream_facts(&st).speaker_busy, "and its done word is written");

    /* Behind a reply: not eligible, so 3 s of reply do not expire a 2 s refusal. */
    setup(true, true);
    fill_reply(AUDIO_RATE * 3, 55);
    stream_reply(&st, reply_pcm, AUDIO_RATE * 3, 4);
    tick();
    stream_cue(&st, STREAM_SLOT_REFUSAL, CUE_BUSY, STREAM_CUE_GAIN, 0);
    until_quiet();
    CHECK(sounded(AUDIO_RATE * 3, CUE_BUSY, 0), "the refusal sounds after the reply");
}

static void test_chime_and_send_result_expire_from_eligibility(void)
{
    setup(true, true);
    stream_cue(&st, STREAM_SLOT_PRESS, CUE_SLEEP, STREAM_CUE_GAIN, 0);
    tick();
    stream_cue(&st, STREAM_SLOT_SEND_RESULT, CUE_SENT, STREAM_CUE_GAIN, 0);
    stream_cue(&st, STREAM_SLOT_CHIME, CUE_MESSAGE, STREAM_CUE_GAIN, 0);
    tick();
    now += STREAM_SEND_RESULT_TTL_MS;
    until_quiet();
    static int16_t r[CUE_MAX_SAMPLES];
    CHECK(sounded(0, CUE_SLEEP, 0), "the sounding cue plays out");
    CHECK(sounded(ref(CUE_SLEEP, 0, r), CUE_MESSAGE, 0), "a 20 s chime outlives a 15 s SENT");
    CHECK(atomic_load(&st.cue_expired) == 1, "which expired");
}

static void test_busy_facts_count_unacknowledged_slot_seqs(void)
{
    /* The frame after a cue is written, before the arbiter picks it up, the speaker reads busy,
       so a PLAY is not posted on top of it. Likewise a reply talk wrote a moment ago. */
    setup(true, true);
    CHECK(!stream_facts(&st).speaker_busy, "a fresh speaker is free");
    stream_cue(&st, STREAM_SLOT_CHIME, CUE_MESSAGE, STREAM_CUE_GAIN, 0);
    stream_facts_t f = stream_facts(&st);
    CHECK(f.speaker_busy && f.clip_busy, "an unpicked cue counts at once");
    until_quiet();
    CHECK(!stream_facts(&st).speaker_busy, "and stops counting once played");
    fill_reply(100, 1);
    stream_reply(&st, reply_pcm, 100, 9);
    f = stream_facts(&st);
    CHECK(f.reply_busy && f.speaker_busy, "an unpicked reply counts at once");
    until_quiet();
    CHECK(!stream_facts(&st).reply_busy, "and is released when done");
}

static void test_done_words_written_even_with_no_buffers(void)
{
    /* The codec answered but the cue buffer's PSRAM did not: the arbiter disposes of every cue
       it cannot copy, so `speaker_busy` cannot latch, and a PLAY fails at once. */
    setup(true, false);
    stream_cue(&st, STREAM_SLOT_PRESS, CUE_BLIP, STREAM_CUE_GAIN, 0);
    stream_cue(&st, STREAM_SLOT_SEND_RESULT, CUE_SENT, STREAM_CUE_GAIN, 0);
    CHECK(stream_facts(&st).speaker_busy, "written cues count until disposed");
    tick();
    CHECK(!stream_facts(&st).speaker_busy, "and are disposed in one tick with nowhere to play");
    CHECK(heard_n == 0, "silently");
    CHECK(!stream_can_play(&st), "no cue buffer: nothing can stream");
    CHECK(!stream_begin(&st, stream_gen(&st), NULL), "so a PLAY is refused at once");
}

static void test_no_codec_never_arms_arbiter_watchdog(void)
{
    /* FW-2: no ES8311 means no audio task. A watchdog armed on that path would reboot the panel
       every 10 s, out of the owner's reach; and nothing may wait on a speaker that is not there. */
    static stream_t none; /* all zero: what render holds when audio_start failed */
    CHECK(stream_cue(&none, STREAM_SLOT_PRESS, CUE_BLIP, STREAM_CUE_GAIN, 0) == 0,
          "no slots are written");
    CHECK(!stream_begin(&none, stream_gen(&none), NULL), "a stream is refused at once");
    stream_reply(&none, reply_pcm, 100, 5);
    CHECK(stream_reply_done(&none, 5), "a reply is done at once, so talk never waits on it");
    const stream_facts_t f = stream_facts(&none);
    CHECK(!f.arbiter && !f.speaker_busy && !f.clip_busy && !f.reply_busy, "and nothing is busy");

    stream_wd_t wd = {0};
    bool fired = false;
    for (uint32_t t = 0; t < 120000; t += 40) fired |= stream_wd_check(&wd, false, 0, t);
    CHECK(!fired, "unarmed (sound false), it never fires");
    for (uint32_t t = 0; t < 120000; t += 40) fired |= stream_wd_check(&wd, true, 0, t);
    CHECK(!fired, "and even armed, it counts only from a first observed heartbeat");

    stream_wd_t live = {0};
    for (uint32_t t = 0; t <= 1000; t += 40) fired |= stream_wd_check(&live, true, t / 40, t);
    CHECK(!fired, "a ticking arbiter is fine");
    CHECK(!stream_wd_check(&live, true, 25, 1000 + STREAM_WD_ARBITER_MS - 40), "9.96 s is fine");
    CHECK(stream_wd_check(&live, true, 25, 1000 + STREAM_WD_ARBITER_MS), "10 s still is wedged");
    stream_wd_t ota = live;
    CHECK(!stream_wd_check(&ota, false, 25, 60000), "an OTA suspends it");
    CHECK(!stream_wd_check(&ota, true, 25, 60040), "and restarts its clock when it clears");
}

/* Render stopped half-way through a slot write: the arbiter runs a tick right there. */
static void hook_mid(stream_t *s, stream_hook_t p)
{
    if (p != STREAM_HOOK_RENDER_MID) return;
    int16_t out[CHUNK];
    const stream_tick_t t = stream_tick(s, now, out, CHUNK);
    for (size_t i = 0; i < t.n; i++) heard[heard_n++] = out[i];
}

/* The arbiter stopped between its copy and its re-read: render rewrites the slot right there. */
static void hook_copied(stream_t *s, stream_hook_t p)
{
    if (p != STREAM_HOOK_ARB_COPIED) return;
    stream_hook = NULL; /* the rewrite's own hook must not re-enter */
    stream_cue(s, STREAM_SLOT_FAIL, CUE_FART, STREAM_CUE_GAIN, 4);
}

static void test_slot_seqlock_never_pairs_seq_with_other_samples(void)
{
    /* §7.4: the arbiter copies a cue while render may be re-rendering that very slot. A read in
       the middle of a write, or a write in the middle of a copy, must never sound one render's
       seq with another's samples or length — the fart's five lengths (170–749 ms) are what a
       fixed-length copy would splice. */
    setup(true, true);
    static int16_t r[CUE_MAX_SAMPLES];

    /* Render interrupted half-way: the arbiter sees an odd seq and leaves the slot alone. */
    stream_cue(&st, STREAM_SLOT_FAIL, CUE_FART, STREAM_CUE_GAIN, 0);
    until_quiet();
    heard_n = 0;
    stream_hook = hook_mid;
    stream_cue(&st, STREAM_SLOT_FAIL, CUE_FART, STREAM_CUE_GAIN, 4);
    stream_hook = NULL;
    CHECK(heard_n == 0, "a slot being written is skipped, not played half-rendered");
    until_quiet();
    CHECK(sounded(0, CUE_FART, 4) && heard_n == ref(CUE_FART, 4, r),
          "then the finished render plays, exactly its own length");

    /* Render rewrites the slot while the arbiter copies: the re-read sees it and skips. */
    setup(true, true);
    stream_cue(&st, STREAM_SLOT_FAIL, CUE_FART, STREAM_CUE_GAIN, 0);
    stream_hook = hook_copied;
    tick();
    stream_hook = NULL;
    CHECK(heard_n == 0, "a copy torn by a rewrite is not played");
    until_quiet();
    CHECK(sounded(0, CUE_FART, 4) && heard_n == ref(CUE_FART, 4, r),
          "the newer render plays with its own length, never the older one's tail");
}

static void test_heard_bit_is_sticky_until_classified(void)
{
    setup(true, true);
    stream_stop(&st, true);
    stream_stop(&st, false);
    CHECK(stream_stop_word(&st) & 1u, "a later stop inside the window keeps the REPLY's heard");
    stream_classified(&st, stream_stop_word(&st) >> 1);
    stream_stop(&st, false);
    CHECK(!(stream_stop_word(&st) & 1u), "once classified, a new stop starts clean");
}

static void test_the_lease_has_one_holder(void)
{
    setup(true, true);
    CHECK(stream_lease_take(&st, STREAM_LEASE_SEND), "the send takes it");
    CHECK(!stream_lease_take(&st, STREAM_LEASE_TALK), "talk cannot while it is held");
    stream_lease_release(&st, STREAM_LEASE_TALK);
    CHECK(stream_leased(&st), "and cannot release what it does not hold");
    stream_lease_release(&st, STREAM_LEASE_SEND);
    CHECK(!stream_leased(&st), "the holder releases it");
}

static void test_a_cue_deafens_and_keeps_the_recording_timeline(void)
{
    /* The capture gets SILENCE while the panel hears itself — never NONE, which would delete
       that span and splice a child's sentence — and the deaf window outlasts the cue. */
    setup(true, true);
    CHECK(tick().capture == STREAM_CAPTURE_COPY, "a quiet panel hears the room");
    stream_cue(&st, STREAM_SLOT_PRESS, CUE_BLIP, STREAM_CUE_GAIN, 0);
    int silent = 0;
    for (int i = 0; i < 20; i++) silent += tick().capture == STREAM_CAPTURE_SILENCE;
    static int16_t r[CUE_MAX_SAMPLES];
    const int cue_chunks = (ref(CUE_BLIP, 0, r) + CHUNK - 1) / CHUNK;
    CHECK(silent == cue_chunks + STREAM_DEAF_CHUNKS - 1, "deaf through the cue and its tail");
    CHECK(tick().capture == STREAM_CAPTURE_COPY, "then hearing again");
}

int main(void)
{
    test_stop_while_paused_drains();
    test_stop_with_no_stream_then_play_starts();
    test_stale_body_not_drained();
    test_stale_word_never_eats_next_preroll();
    test_stale_word_discard_races_the_next_begin();
    test_stop_during_drain_frees_speaker();
    test_short_message_plays_without_its_preroll();
    test_pause_scoped_to_stream_id();
    test_cursor_writers();
    test_heard_then_oops_both_sound();
    test_press_never_cuts_outcome();
    test_poke_during_stream_sounds();
    test_outcome_waits_out_reply();
    test_reply_waits_out_a_sounding_cue();
    test_standby_silences_then_sounds_its_own_stop();
    test_exit_during_pet_reply_silences_it_then_sounds_stop();
    test_silence_discards_the_ring_once();
    test_reply_slot_stamp_and_silence();
    test_refusal_coalesced_and_expires_from_eligibility();
    test_chime_and_send_result_expire_from_eligibility();
    test_busy_facts_count_unacknowledged_slot_seqs();
    test_done_words_written_even_with_no_buffers();
    test_no_codec_never_arms_arbiter_watchdog();
    test_slot_seqlock_never_pairs_seq_with_other_samples();
    test_heard_bit_is_sticky_until_classified();
    test_the_lease_has_one_holder();
    test_a_cue_deafens_and_keeps_the_recording_timeline();
    printf("stream ok — %d checks\n", checks);
    return 0;
}
