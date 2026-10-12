/* TRANSITIONAL: 0.3.x's speaker calls over `stream.h`, so `display.c`, `jpanel.c` and `talk.c`
 * keep compiling until the machine, the engine and talk's reply slot replace them. Integration
 * deletes this file with the block in `audio.h` that declares these (plan §15). It keeps each
 * call's 0.3.x contract only as far as the new single-writer words allow: the writers here run
 * on the same tasks those calls always ran on. */

#include "audio.h"
#include "stream.h"

static uint32_t s_begun_gen;
static uint32_t s_reply_id;
static bool s_paused;
static bool s_open;

void audio_cue(cue_t c)
{
    stream_slot_t slot = STREAM_SLOT_PRESS;
    if (c == CUE_SENT) slot = STREAM_SLOT_SEND_RESULT;
    if (c == CUE_OOPS) slot = STREAM_SLOT_FAIL;
    stream_cue_play(audio_stream(), slot, c);
}

bool audio_play(const int16_t *pcm, size_t bytes)
{
    stream_t *s = audio_stream();
    if (pcm == NULL || bytes < sizeof(int16_t)) return false;
    if (s_reply_id != 0 && !stream_reply_done(s, s_reply_id)) return false;
    size_t n = bytes / sizeof(int16_t);
    if (n > (size_t)AUDIO_RATE * PLAY_BUF_MS / 1000) n = (size_t)AUDIO_RATE * PLAY_BUF_MS / 1000;
    if (++s_reply_id == 0) s_reply_id = 1;
    stream_reply(s, pcm, (uint32_t)n, s_reply_id);
    return true;
}

bool audio_playing(void)
{
    return stream_facts(audio_stream()).speaker_busy;
}

bool audio_stream_begin(void)
{
    stream_t *s = audio_stream();
    const uint32_t gen = stream_gen(s);
    uint8_t id = 0;
    if (!stream_begin(s, gen, &id)) return false;
    s_begun_gen = gen;
    s_open = true;
    s_paused = false;
    return true;
}

size_t audio_stream_write(const void *pcm, size_t bytes)
{
    return stream_write(audio_stream(), (const int16_t *)pcm, bytes / sizeof(int16_t)) *
           sizeof(int16_t);
}

void audio_stream_end(void)
{
    s_open = false;
    stream_end(audio_stream());
}

void audio_stream_abort(void)
{
    s_open = false;
    stream_end(audio_stream());
    stream_stop(audio_stream(), false);
}

bool audio_stream_active(void)
{
    return stream_facts(audio_stream()).stream_live;
}

bool audio_stream_live(void)
{
    return s_open && !stream_stale(audio_stream(), s_begun_gen);
}

void audio_stream_pause(bool on)
{
    if (s_paused == on) return;
    s_paused = on;
    stream_pause_req(audio_stream(), stream_facts(audio_stream()).stream_id, on);
}

bool audio_stream_paused(void)
{
    return s_paused && stream_facts(audio_stream()).stream_live;
}

void audio_stop(void)
{
    stream_stop(audio_stream(), false);
    stream_silence(audio_stream());
}
