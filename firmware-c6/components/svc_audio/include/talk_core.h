/* A talk to Jerv, apart from the chips and the radio. Pure C, host-tested.
 *
 * Three tasks share one exchange: the UI task (KEY, BOOT, the bubbles), the audio task (the
 * microphone and the speaker) and the main task (the one HTTPS session to the box). Each asks
 * this state what to do and reports what happened; the device wraps every call in one critical
 * section, so no two of them ever hold a different idea of where the talk is. The audio and the
 * network meet in one byte ring, filled by one side and drained by the other: the recording on
 * its way up, then the reply on its way out of the speaker. Nothing holds a whole clip. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "svc_audio.h"
#include "svc_messages.h"

/* What the box takes and sends (backend `PANEL_RATE`): 16 kHz mono signed 16-bit. */
#define TALK_RATE 16000
/* One audio task pass: 20 ms, 640 bytes of mono. */
#define TALK_CHUNK_FRAMES 320
#define TALK_CHUNK_BYTES (TALK_CHUNK_FRAMES * 2)

/* ---- the ring: one producer, one consumer, counters that only grow ---- */
typedef struct {
    uint8_t *buf;
    uint32_t cap;
    uint32_t head; /* bytes ever written; the producer's alone */
    uint32_t tail; /* bytes ever read; the consumer's alone */
} tk_ring_t;

void tk_ring_init(tk_ring_t *r, uint8_t *buf, uint32_t cap);
uint32_t tk_ring_used(const tk_ring_t *r);
uint32_t tk_ring_free(const tk_ring_t *r);
/* Both move what fits and return how much that was: the producer never overwrites unread bytes
 * and the consumer never reads past the producer. */
uint32_t tk_ring_write(tk_ring_t *r, const void *src, uint32_t n);
uint32_t tk_ring_read(tk_ring_t *r, void *dst, uint32_t n);

/* ---- the exchange ---- */
typedef enum {
    TK_IDLE,
    TK_REC,  /* KEY held: the microphone fills the ring, the upload drains it */
    TK_SEND, /* KEY released: the upload finishes and the box is thinking */
    TK_PLAY, /* the reply's speech streams into the ring and out of the speaker */
} tk_phase_t;

typedef enum {
    TK_RES_NONE,
    TK_RES_REPLY,   /* the box heard something and Jerv answered */
    TK_RES_NOTHING, /* the box heard nothing (its 204) */
    TK_RES_FAILED,  /* no answer: `why` says what broke */
    TK_RES_SPOKEN,  /* the reply's speech played to its end */
} tk_res_t;

typedef enum {
    TK_BEGIN_OK,
    TK_BEGIN_STOPPED, /* the press stopped a reply instead: nothing is recorded */
    TK_BEGIN_BUSY,    /* the last exchange has not let go of the radio or the codec yet */
} tk_begin_t;

typedef enum { TK_NET_UPLOAD, TK_NET_FINISH, TK_NET_ABORT } tk_net_t;
typedef enum { TK_AUDIO_OFF, TK_AUDIO_MIC, TK_AUDIO_SPK } tk_audio_t;

/* Who a talk is with: the box's real Jerv or Minecraft_Dave (backend `panel_agent`). Each has its
 * own thread on the panel and its own conversation on the box. */
typedef enum { TK_AGENT_JERV, TK_AGENT_DAVE, TK_AGENT__COUNT } tk_agent_t;
/* "jerv" / "dave": the svc_messages thread and the box's `persona=`, which are the same word. */
const char *tk_agent_id(tk_agent_t a);
/* The agent a thread id names. False for anything else (a person's thread). */
bool tk_agent_from(const char *id, tk_agent_t *out);
/* `/endpoint/converse?persona=<id>&voice=<v>&jt=2&steps=1` (the JT2 stream, 0.0.12, with its
 * step records, 0.0.14), and `&new_thread=true` for a new conversation (Clear chat, or KEY held
 * outside the conversation). */
void tk_converse_path(tk_agent_t a, bool voice, bool fresh, char *out, size_t cap);

/* What an exchange is (0.0.16): a talk to an agent; a voice message to a twin (POST: recorded
 * and uploaded like a talk, answered with the box's id and transcript, no speech); or one of her
 * messages played (LISTEN: no recording — it starts at TK_SEND, asks the box for the audio and
 * plays it through the same ring, the same speaker start and the same stop as a reply). */
typedef enum { TK_KIND_AGENT, TK_KIND_POST, TK_KIND_LISTEN } tk_kind_t;

typedef struct {
    tk_phase_t phase;
    uint32_t since_ms;
    uint32_t held_ms;
    uint32_t msg_id;  /* the voice bubble this exchange fills in; Jerv's reply is msg_id + 1 */
    bool voice;       /* the reply's speech was asked for */
    bool abort;       /* everyone lets go: a cancel, a knock, BOOT or KEY over a reply */
    bool net_busy;    /* the main task is inside the exchange */
    bool net_done;    /* ... and has left it: no more reply audio is coming */
    bool mic_on;      /* the audio task's own word for what it has open */
    bool spk_on;
    tk_res_t res;
    uint8_t agent; /* tk_agent_t: whose thread the bubbles land in, which persona the box runs */
    bool fresh;    /* this exchange asks the box for a new conversation (Clear chat) */
    uint8_t kind;  /* tk_kind_t */
    char why[40];
} tk_t;

void tk_init(tk_t *t);
/* A bounded string copy with no locale or stdio behind it: these run inside the device's
 * critical section, where nothing may take a lock. */
void tk_copy(char *dst, size_t cap, const char *src);

/* UI task. */
tk_begin_t tk_begin(tk_t *t, uint32_t now_ms, bool voice, uint32_t msg_id);
/* Right after a TK_BEGIN_OK, under the same lock: who the hold is with. */
void tk_set_agent(tk_t *t, tk_agent_t agent, bool fresh);
/* Right after a TK_BEGIN_OK, under the same lock: the hold is a voice message to a twin. */
void tk_set_post(tk_t *t);
/* A message played: straight to TK_SEND with no hold, its speech wanted. BUSY while anything else
 * holds the radio or the codec, a reply's voice included (KEY or BOOT stops that first). */
tk_begin_t tk_listen(tk_t *t, uint32_t msg_id);
/* The hold's end. A send that the box was already failed for is TALK_FAILED (the result says
 * why); anything short of a message lets go of everything. */
talk_outcome_t tk_end(tk_t *t, uint32_t now_ms, bool send);
/* BOOT, or KEY, while Jerv is thinking or speaking: drop the reply. False when there is none. */
bool tk_stop(tk_t *t);
bool tk_replying(const tk_t *t);
/* The result, once; TK_RES_NONE when there is nothing new. */
tk_res_t tk_take(tk_t *t, char *why, size_t why_len);
/* Nothing touches the ring: the UI may free it. */
bool tk_quiet(const tk_t *t);

/* Main task. */
bool tk_net_claim(tk_t *t);
tk_net_t tk_net_next(const tk_t *t);
/* The reply's words are in. Its speech plays only if it was asked for, comes, and the hold was
 * not dropped meanwhile; the caller resets the ring before the first byte of it. */
void tk_net_reply(tk_t *t, bool audio_follows);
void tk_net_nothing(tk_t *t);
void tk_net_fail(tk_t *t, const char *why);
bool tk_net_wants_audio(const tk_t *t);
void tk_net_end(tk_t *t);

/* Audio task. */
tk_audio_t tk_audio_want(const tk_t *t);
/* The speaker has played everything the network will ever send. */
void tk_audio_drained(tk_t *t);
/* The speaker would not open: the reply stays as words, the network stops sending speech. */
void tk_audio_drop(tk_t *t);

/* ---- the microphone's samples ---- */
/* ES7210 MIC1 and MIC2 arrive as one stereo frame; the box takes one channel. The two are
 * averaged, and each one's peak kept, so a dead microphone shows in the snapshot. */
void talk_mono(const int16_t *lr, size_t frames, int16_t *out, uint16_t *peak_l, uint16_t *peak_r);
/* The ring's pulse, 0-100, from a chunk's peak: ~6 dB a step over the top 60 dB. */
uint8_t talk_level(uint16_t peak);

/* ---- the reply stream (backend `panel_stream`, JT2) ----
 * "JT2", then records until the body ends: a type byte, a little-endian u16 length, and that
 * many bytes. HEARD and STATUS are UTF-8; ANSWER is a flags byte then UTF-8; PCM is the speech;
 * END is a code byte then UTF-8; ALIVE is empty. Read as it arrives and never held whole: a text
 * record is kept (cut on a character, with an ellipsis, past TS_TEXT_CAP), a PCM record is handed
 * on in the slices it arrived in, a type this panel does not know is skipped by its length, and a
 * length past TS_MAX_RECORD ends the stream as unreadable. */
#define TS_HEARD 0x01
#define TS_STATUS 0x02
#define TS_ALIVE 0x03
#define TS_ANSWER 0x04
#define TS_PCM 0x05
#define TS_END 0x06
#define TS_STEP_START 0x07 /* index: u8 | UTF-8 label; the same index again relabels it */
#define TS_STEP_DONE 0x08  /* index: u8 | ok: u8 */
#define TS_ANSWER_SPEECH 0x01 /* the answer's speech follows */
#define TS_ANSWER_CUT 0x02    /* the box cut the text to fit; the speech says all of it */
#define TS_END_OK 0
#define TS_END_NOTHING 1
#define TS_END_FAILED 2
#define TS_MAX_RECORD 4096
#define TS_TEXT_CAP MSG_LONG_TEXT

typedef enum {
    TS_EV_NONE,   /* the bytes so far finish no record */
    TS_EV_HEARD,  /* `text`: what the box heard */
    TS_EV_STATUS, /* `text`: what the agent is doing */
    TS_EV_ALIVE,
    TS_EV_ANSWER, /* `text`, and `flags` */
    TS_EV_PCM,    /* `*pcm`, `*pcm_n`: a slice of speech, pointing into the bytes fed */
    TS_EV_END,    /* `flags` is the code, `text` why */
    TS_EV_STEP_START, /* `flags` is the index, `text` the label */
    TS_EV_STEP_DONE,  /* `flags` is the index; tk_stream_step_ok says how it went */
    TS_EV_BAD,    /* not a JT2 stream, or a record longer than TS_MAX_RECORD */
} tk_stream_ev_t;

typedef struct {
    uint8_t st;
    uint8_t type;
    uint8_t lo;
    uint8_t flags;
    uint16_t len, seen;
    uint16_t text_n;
    uint32_t records, skipped;
    char text[TS_TEXT_CAP];
} tk_stream_t;

void tk_stream_init(tk_stream_t *s);
/* Consumes bytes of `b` up to and including the one that finishes a record (or a PCM slice), and
 * returns how many: call again with the rest. `*ev` says what finished. After END the rest is
 * consumed and ignored; after BAD nothing more is. */
size_t tk_stream_feed(tk_stream_t *s, const uint8_t *b, size_t n, tk_stream_ev_t *ev,
                      const uint8_t **pcm, size_t *pcm_n);
bool tk_stream_done(const tk_stream_t *s);
/* The STEP_DONE just read: false only when the box said the tool failed. */
bool tk_stream_step_ok(const tk_stream_t *s);
bool tk_stream_bad(const tk_stream_t *s);

/* `src` into `dst`, cut on a character with an ellipsis when it does not fit, never in the middle
 * of a letter. A bubble's text and the status line both pass through this. */
void tk_fit(char *dst, size_t cap, const char *src);

/* ---- the steps a turn took (JT2 STEP records, 0.0.14) ----
 * The last WORK_SHOWN, oldest first, in place; anything older is only counted. A STEP_START for
 * an index already shown relabels it; one at or below an index already seen but no longer shown
 * is old news and changes nothing, so a late relabel never re-counts a folded step. */
typedef struct {
    work_step_t shown[WORK_SHOWN];
    uint8_t n;
    uint16_t total; /* steps started this turn */
    uint16_t next;  /* one past the highest index seen */
} tk_steps_t;

void tk_steps_init(tk_steps_t *s);
void tk_steps_start(tk_steps_t *s, uint8_t index, const char *label);
void tk_steps_done(tk_steps_t *s, uint8_t index, bool ok);
uint16_t tk_steps_folded(const tk_steps_t *s);
/* The answer's one dim line once it lands: "1 step · 9 s", "12 steps · 2:05" — "" for a turn
 * that ran no tool. Fits a msg_t's status. */
void tk_steps_summary(const tk_steps_t *s, uint32_t ms, char *out, size_t cap);

/* ---- the reply's voice, started the same way every time ----
 * The speaker's first word was sometimes lost (owner, 2026-10-12: "the first quarter second … is
 * cut off"): the amplifier rail and the DAC were switched on in the same breath as the first
 * sample was written. Now the audio task opens the codec, turns the amp on and writes silence,
 * and the speech starts only once the amp has had TK_SPK_SETTLE_MS AND the ring holds
 * TK_SPK_START_BYTES (or nothing more is coming). An underrun mid-reply — the box still
 * synthesising the next sentence — re-arms the fill half: a pause between sentences, never a
 * stutter. Playback ends with TK_SPK_TAIL_MS of silence so the DMA's last buffers reach the
 * speaker before the amp goes off. */
#define TK_SPK_SETTLE_MS 200
#define TK_SPK_START_BYTES 4800 /* 150 ms */
#define TK_SPK_TAIL_MS 120
bool tk_spk_may_start(uint32_t since_amp_ms, uint32_t queued, bool more);

/* ---- the conversation's bubbles ---- */
#define TALK_HEARING "Transcribing…"
/* A voice message to a twin on its way up, and once the box has it (0.0.16). */
#define TALK_POSTING "Sending…"
#define TALK_POSTED "Sent"
void talk_bubble_sent(uint32_t id, uint32_t held_ms, msg_t *out);
/* The voice bubble once the box has answered, or failed, or heard nothing. */
void talk_bubble_heard(msg_t *m, const char *heard);
void talk_bubble_unsent(msg_t *m, const char *why);
void talk_bubble_reply(uint32_t id, const char *reply, msg_t *out);

/* ---- what telemetry says ---- */
typedef struct {
    uint32_t talks, sent, failed, replies, nothing, stopped, spoken;
    uint32_t up_bytes, down_bytes;
    uint32_t reply_ms, reply_ms_max;
    uint32_t underruns, overruns, tones;
    uint32_t plays, play_failed, posted; /* 0.0.16: her messages played, and voice messages sent */
    uint16_t peak_l, peak_r;
} tk_stats_t;
/* `audio=<state>,talks:N,sent:N,heard:N,none:N,fail:N,stop:N,up:<KB>,down:<KB>,ms:<last>/<max>,
 * under:N,over:N` */
void tk_stats_note(const tk_stats_t *s, const char *state, char *out, size_t len);
