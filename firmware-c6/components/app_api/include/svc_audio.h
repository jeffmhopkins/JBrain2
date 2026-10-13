/* Speaker and microphones. The audio task is the only owner of the ES8311/ES7210 handles and
 * every call here is a request it performs (esp_codec_dev has no lock — firmware/README.md).
 * Audio streams both ways: without PSRAM a clip does not fit. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define TALK_MIN_MS 600u
#define TALK_MAX_MS 60000u

/* TALK_TO_AGENT: `principal` is the agent's thread, "jerv" or "dave" — the box's real agent —
 * and the hold continues that conversation. TALK_TO_PERSON: `principal` is a twin's thread, and
 * the hold is a voice message to her panel (0.0.16), uploaded as it is recorded. TALK_TO_NEW_AGENT starts a new one with it: once the
 * hold is sent, this panel's view of that thread is cleared to just the new exchange and the box
 * is asked for a fresh conversation (the old one stays in the app). */
typedef enum { TALK_TO_AGENT, TALK_TO_PERSON, TALK_TO_NEW_AGENT } talk_kind_t;

/* KEY ROUTING (plan "KEY routing", owner 2026-10-12: "If I start off from not within the Jerv
 * conversation it starts a new one"). KEY held inside an agent's conversation — `conversation` is
 * "jerv" or "dave" — continues it; KEY held anywhere else (home, the clock, Settings, Minecraft's
 * menu: `conversation` NULL or any other id) starts a NEW conversation with Jerv, never Dave.
 * Pure, host-tested: the one rule both the conversation view and the shell's own talk ask. */
typedef struct {
    talk_kind_t kind;
    const char *agent; /* "jerv" or "dave" */
} talk_route_t;
talk_route_t talk_key_route(const char *conversation);

/* TALK_FAILED: the box could not be reached mid-hold, so nothing was sent. */
typedef enum { TALK_SENT, TALK_TOO_SHORT, TALK_CANCELLED, TALK_CAPPED, TALK_FAILED } talk_outcome_t;

/* False when no hold starts; then svc_audio_talk_refused() says why, or is NULL when the press
 * did something else on purpose (it stopped Jerv speaking). */
bool svc_audio_talk_begin(talk_kind_t kind, const char *principal);
/* Clear chat on an agent's thread: its next talk asks the box for a new conversation. Held until
 * a talk is answered (a hold the box heard nothing in keeps it); the other agent is untouched. */
void svc_audio_fresh(const char *agent);
bool svc_audio_fresh_pending(const char *agent);
/* The panel's thread was switched to another conversation on the box (0.0.15, svc_convo): a
 * new conversation asked for before is forgotten, so the next talk continues that one. */
void svc_audio_continue(const char *agent);
const char *svc_audio_talk_refused(void);
/* `send` false is a tap on the glass. The outcome is what the app tells the user. */
talk_outcome_t svc_audio_talk_end(bool send);
bool svc_audio_talking(void);
uint32_t svc_audio_talk_ms(void);
uint8_t svc_audio_level(void); /* 0-100, the ring's pulse */

/* Jerv is thinking about, or speaking, a reply. */
bool svc_audio_replying(void);
/* ... and speaking it: KEY or BOOT now stops the voice. While only thinking, KEY says the agent is
 * still working and BOOT leaves the thread — the turn goes on and its answer lands when it comes
 * (owner, 2026-10-12). */
bool svc_audio_speaking(void);
/* What KEY says then: no new talk starts (owner, 2026-10-12). */
#define TALK_STILL_WORKING "Still working on the last one"
/* The working view under `thread`'s waiting question (JT2 steps, 0.0.14): the tools the agent
 * ran, newest last, and what it is doing between them. Fixed buffers, no heap: the last
 * WORK_SHOWN steps are kept and the rest only counted (`folded`, the "+N more" line). */
#define WORK_SHOWN 4
/* The box sends a label of at most 60 bytes (backend `panel_stream.STEP_BYTES`). */
#define WORK_LABEL 64
typedef enum { WORK_RUNNING, WORK_OK, WORK_FAILED } work_state_t;
typedef struct {
    uint8_t index; /* the box's step number */
    uint8_t state; /* work_state_t */
    char label[WORK_LABEL];
} work_step_t;
typedef struct {
    char phase[64];      /* "Thinking…", "Writing the answer…" */
    uint32_t elapsed_ms; /* since the question was sent */
    uint32_t seq;        /* moves with every change but the clock: a redraw is due */
    uint16_t folded;     /* steps older than the shown ones */
    uint8_t n;
    work_step_t step[WORK_SHOWN]; /* oldest first */
} svc_work_t;
/* "0:42", "12:05": the working line's clock. Pure, host-tested. */
void talk_clock(uint32_t ms, char *out, size_t cap);
/* True, with the view, while `thread`'s agent works on an answer the box has started streaming. */
bool svc_audio_work(const char *thread, svc_work_t *out);
/* Drops that reply (BOOT or KEY over it). False when there was none. */
bool svc_audio_stop_reply(void);

/* One of a twin's messages through the speaker (0.0.16): streamed from the box as it plays, with
 * the reply's speaker start. Heard, here and on the box, once its audio starts — the box's answer
 * to the play, a fraction of a second after the tap (0.0.17, owner 2026-10-13: "A message should
 * no longer be considered unread if I even hit play once on it"): stopped early, cut off or
 * failing its digest after that, it stays heard. A play the box never answers with audio (no
 * reply, not a 200, no memory for the ring) leaves it unheard and sends no stamp; so does a
 * press refused here (false, svc_audio_talk_refused() saying why). A message already heard is
 * never stamped again. */
bool svc_audio_play(uint32_t msg_id);
void svc_audio_stop(void);
uint32_t svc_audio_playing(void); /* msg id, 0 when silent */
/* How much of the message playing has gone to the speaker, in ms: counted from the samples the
 * audio task wrote, so it stands still through an underrun and the speaker's pre-roll. 0 when
 * no message plays. */
uint32_t svc_audio_played_ms(void);
/* A new message's chime: two soft notes, once, when nothing else has the speaker. */
bool svc_audio_chime(void);
void svc_audio_speak(uint32_t msg_id);
/* Whether a reply can be listened to on demand: not until the box can speak a stored one. */
bool svc_audio_can_speak(void);

/* An alarm's sound (svc_alarms.h's alarm_sound_t), looped with a rising volume until
 * svc_audio_ring_stop. It OUTRANKS every other sound: the microphone, a reply's voice and a test
 * tone all give way the moment it is asked for, and nothing else may open the speaker while it
 * plays. Safe from the UI task; the audio task carries it out. */
void svc_audio_ring(uint8_t sound);
void svc_audio_ring_stop(void);
bool svc_audio_ringing(void);

/* "ok", "off" (not started: a fault boot), "mic-only:<why>" or "failed:<step>": what Settings
 * and the snapshot say about the sound. */
const char *svc_audio_state(void);

/* Pure policy, host-tested: what ending a hold of `held_ms` means. */
talk_outcome_t talk_policy_end(uint32_t held_ms, bool send);
