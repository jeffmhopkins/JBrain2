/* The audio service's host port: svc_audio_talk.c with no codec, no audio task and no box. The
 * "network" claims a hold the moment it begins and answers only when the sim or a test says
 * what the box said (svc_audio_fake_reply); the "audio task" opens at once whatever the talk
 * wants, and a reply's speech plays until it is stopped. */
#include "svc_audio_fake.h"
#include "svc_audio_drv.h"
#include "svc_rtc.h"

void talk_port_lock(void) {}

void talk_port_unlock(void) {}

static bool s_net;

/* The audio task and the network in miniature: the codec opens whatever the talk wants at once,
 * and a dropped exchange lets go of the "connection" as the main task would. */
void talk_port_kick(void)
{
    const tk_audio_t want = talk_audio_want();
    talk_audio_on(want == TK_AUDIO_MIC, want == TK_AUDIO_SPK);
    bool mic_off;
    if (s_net && talk_net_next(&mic_off) == TK_NET_ABORT) {
        s_net = false;
        talk_net_end(0, 0, 0);
    }
}

void talk_port_net(void)
{
    bool voice;
    s_net = talk_net_claim(&voice);
}

const char *talk_port_refusal(void)
{
    return NULL;
}

bool talk_port_speaker(void)
{
    return true;
}

/* A believable pulse for the ring, from the hold's own clock. */
uint8_t talk_port_level(void)
{
    uint32_t t = svc_audio_talk_ms() / 90;
    return (uint8_t)(t % 7 * 14);
}

const char *talk_port_state(void)
{
    return "fake";
}

void talk_port_describe(dbg_json_t *w)
{
    dbg_json_str(w, "codecs", "none (host)");
}

void svc_audio_fake_reply(const char *heard, const char *reply, bool speaking)
{
    talk_net_reply(heard, reply, speaking);
    s_net = false;
    talk_net_end(32000, speaking ? 64000 : 0, 2400);
}
