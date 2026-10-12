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
    char why[40];
} tk_t;

void tk_init(tk_t *t);
/* A bounded string copy with no locale or stdio behind it: these run inside the device's
 * critical section, where nothing may take a lock. */
void tk_copy(char *dst, size_t cap, const char *src);

/* UI task. */
tk_begin_t tk_begin(tk_t *t, uint32_t now_ms, bool voice, uint32_t msg_id);
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

/* ---- the reply frame (backend `jerv_frame`) ----
 * "JT1", a flags byte (bit 0: speech follows), then heard and reply as little-endian u16
 * lengths and UTF-8, then the speech to the end of the body. Read as it arrives: the words are
 * kept, cut at a character boundary to what a bubble holds, and the speech is handed on. */
#define TF_FLAG_AUDIO 0x01
typedef struct {
    uint8_t st;
    uint8_t flags;
    uint16_t need, seen;
    uint8_t len_lo;
    char heard[sizeof(((msg_t *)0)->text)];
    char reply[sizeof(((msg_t *)0)->text)];
    uint16_t heard_n, reply_n;
} tk_frame_t;

void tk_frame_init(tk_frame_t *f);
/* How many of `n` bytes belonged to the frame's head; the rest, once tk_frame_done, are speech. */
size_t tk_frame_feed(tk_frame_t *f, const uint8_t *b, size_t n);
bool tk_frame_done(const tk_frame_t *f);
bool tk_frame_bad(const tk_frame_t *f);

/* ---- the conversation's bubbles ---- */
/* What the conversation shows for a hold to a person this panel could not send: a voice bubble
 * of the length held that says plainly nothing was recorded — never a made-up transcript.
 * False for a hold that is not a message at all (cancelled, or too short). Pure, host-tested. */
#define TALK_PLACEHOLDER "Not sent: this panel cannot send messages yet. They arrive in a later update."
bool talk_placeholder(talk_outcome_t o, uint32_t held_ms, uint32_t id, msg_t *out);

#define TALK_HEARING "Transcribing…"
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
    uint16_t peak_l, peak_r;
} tk_stats_t;
/* `audio=<state>,talks:N,sent:N,heard:N,none:N,fail:N,stop:N,up:<KB>,down:<KB>,ms:<last>/<max>,
 * under:N,over:N` */
void tk_stats_note(const tk_stats_t *s, const char *state, char *out, size_t len);
