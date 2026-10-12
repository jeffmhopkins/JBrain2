#include <string.h>

#include "svc_audio_fake.h"
#include "svc_debug.h"
#include "svc_messages_fake.h"
#include "svc_rtc.h"

static bool s_talking;
static talk_kind_t s_kind;
static char s_to[16];
static uint32_t s_since;
static uint32_t s_playing;
static uint32_t s_spoke;
static unsigned s_sent;
static uint32_t s_next_id = 1000;

bool svc_audio_talk_begin(talk_kind_t kind, const char *principal)
{
    if (s_talking) return false;
    s_talking = true;
    s_kind = kind;
    strncpy(s_to, principal ? principal : "", sizeof s_to - 1);
    s_since = svc_rtc_uptime_ms();
    s_playing = 0;
    return true;
}

uint32_t svc_audio_talk_ms(void)
{
    return s_talking ? svc_rtc_uptime_ms() - s_since : 0;
}

talk_outcome_t svc_audio_talk_end(bool send)
{
    const uint32_t held = svc_audio_talk_ms();
    talk_outcome_t o = talk_policy_end(held, send);
    msg_t m;
    if (s_talking && talk_placeholder(o, held, s_next_id, &m)) {
        s_sent++;
        s_next_id++;
        svc_messages_fake_add(s_to, &m);
    }
    s_talking = false;
    return o;
}

bool svc_audio_talking(void)
{
    return s_talking;
}

uint8_t svc_audio_level(void)
{
    uint32_t t = svc_audio_talk_ms() / 90;
    return (uint8_t)(t % 7 * 14);
}

bool svc_audio_play(uint32_t msg_id)
{
    if (s_talking) return false;
    s_playing = msg_id;
    return true;
}

void svc_audio_stop(void)
{
    s_playing = 0;
}

uint32_t svc_audio_playing(void)
{
    return s_playing;
}

void svc_audio_speak(uint32_t msg_id)
{
    s_spoke = msg_id;
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    dbg_json_bool(w, "talking", s_talking);
    if (s_talking) {
        dbg_json_str(w, "talk_kind", s_kind == TALK_TO_JERV ? "jerv" : "person");
        dbg_json_str(w, "talk_to", s_to);
        dbg_json_int(w, "talk_ms", svc_audio_talk_ms());
    }
    dbg_json_int(w, "playing", s_playing);
    dbg_json_int(w, "spoke", s_spoke);
    dbg_json_int(w, "sent", s_sent);
}

void svc_audio_init(void)
{
    svc_debug_register("audio", describe, NULL);
}
