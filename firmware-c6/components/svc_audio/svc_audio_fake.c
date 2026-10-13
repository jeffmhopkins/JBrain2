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
/* What the device would have asked the box for: the converse path of the last claimed talk. */
static char s_path[112];

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
    bool voice, fresh;
    tk_agent_t agent;
    s_net = talk_net_claim(&voice, &agent, &fresh);
    if (!s_net) return;
    if (talk_net_kind(s_path, sizeof s_path) == TK_KIND_AGENT)
        tk_converse_path(agent, voice, fresh, s_path, sizeof s_path);
}

const char *svc_audio_fake_last_path(void)
{
    return s_path;
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

static uint8_t s_ring;

void talk_port_describe(dbg_json_t *w)
{
    static const char *const SOUND[] = {"none", "chime", "beeps", "rise"};
    dbg_json_str(w, "codecs", "none (host)");
    dbg_json_str(w, "alarm_sound", SOUND[s_ring < 4 ? s_ring : 0]);
}

/* The alarm's sound, as state only (s_ring, above): what is "playing" is what the snapshot and
 * the tests read. */
void svc_audio_ring(uint8_t sound)
{
    s_ring = (uint8_t)(sound + 1);
}

void svc_audio_ring_stop(void)
{
    s_ring = 0;
}

bool svc_audio_ringing(void)
{
    return s_ring != 0;
}

int svc_audio_fake_ring_sound(void)
{
    return (int)s_ring - 1;
}

void svc_audio_fake_progress(const char *heard, const char *status)
{
    if (heard) talk_net_heard(heard);
    if (status) talk_net_status(status);
}

void svc_audio_fake_step(uint8_t index, const char *label)
{
    talk_net_step_start(index, label);
}

void svc_audio_fake_step_done(uint8_t index, bool ok)
{
    talk_net_step_done(index, ok);
}

void svc_audio_fake_reply(const char *heard, const char *reply, bool speaking)
{
    talk_net_heard(heard);
    talk_net_reply(reply, speaking);
    s_net = false;
    talk_net_end(32000, speaking ? 64000 : 0, 2400);
}

static unsigned s_chimes;

bool svc_audio_chime(void)
{
    if (svc_audio_talking() || svc_audio_replying() || s_ring) return false;
    s_chimes++;
    return true;
}

unsigned svc_audio_fake_chimes(void)
{
    return s_chimes;
}

void svc_audio_fake_posted(int status, const char *heard)
{
    if (!s_net) return;
    if (status == 201) {
        talk_net_heard(heard ? heard : "");
        talk_net_reply("", false);
    } else if (status == 400) {
        talk_net_nothing();
    } else {
        talk_net_fail(status == 422 ? "the upload was damaged" : "the box did not answer");
    }
    s_net = false;
    talk_net_end(32000, 0, 0);
}

void svc_audio_fake_listen(bool arrives, bool to_the_end, bool verified)
{
    if (!s_net) return;
    if (!arrives) {
        talk_net_fail("the box said 404");
        s_net = false;
        talk_net_end(0, 0, 0);
        return;
    }
    talk_net_reply("", true);
    talk_port_kick();
    if (!to_the_end) return;
    talk_net_listened(verified);
    s_net = false;
    talk_net_end(0, 64000, 0);
    talk_audio_drained();
    talk_port_kick();
}
