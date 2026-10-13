/* JN1, the notice on the wire from the box (plan §2, backend `jpanel/notice.py`), read as it
 * arrives. Pure C, no allocation, host-tested against the box's own fixtures.
 *
 *     "JN1", then records: type u8 | length u16 LE | payload (≤ 4096 bytes)
 *     META   fixed 30 bytes (id 16, flags u8, hide_s u16, ttl_s u32, img_mode u8, img_w u16,
 *            img_h u16, audio_ms u16), then the sender's label
 *     TITLE, BODY?, CHOICE* (UTF-8), STRIPE* (u16 y0, u16 rows, runs of (len-1, rgb565 LE)),
 *     PCM* (16 kHz mono s16le, even), END (u8 0, u32 the byte count before END)
 *
 * The reader never holds the stream: it takes one record at a time into a caller's buffer of
 * JN1_BUF_BYTES, checks it, and says so, and the caller writes that record wherever it keeps
 * the notice (svc_notice: a file on `storage`). It refuses exactly what the box's reference
 * reader `decode()` refuses — the order, the flags against the records, every stripe's rows and
 * runs, PCM's evenness, END's count, a byte after END — so a notice is shown whole or not at all. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define JN1_MAX_RECORD 4096
#define JN1_HEAD 3
/* One record with its header: the only buffer a stream ever needs. */
#define JN1_BUF_BYTES (JN1_MAX_RECORD + JN1_HEAD)
#define JN1_META_FIXED 30

enum { JN1_META = 1, JN1_TITLE, JN1_BODY, JN1_CHOICE, JN1_STRIPE, JN1_PCM, JN1_END };
#define JN1_FLAG_CHIME 0x01
#define JN1_FLAG_QUESTION 0x02
#define JN1_FLAG_PICTURE 0x04
#define JN1_FLAG_SOUND 0x08
enum { JN1_IMG_NONE, JN1_IMG_FULL, JN1_IMG_BANNER };

#define JN1_CHOICES_MAX 4
#define JN1_STRIPE_ROWS_MAX 40
/* What the panel keeps of each text, with its terminator: the box's limits (notice.py
 * TITLE_BYTES, BODY_BYTES, CHOICE_BYTES, SENDER_LABEL_BYTES). The box's reader takes a longer
 * one, so this one does too, cut on a character. */
#define JN1_SENDER_BYTES 33
#define JN1_TITLE_BYTES 61
#define JN1_BODY_BYTES 301
#define JN1_CHOICE_BYTES 17
/* The panel's playback rate (backend PANEL_RATE): the sound's length is checked against it. */
#define JN1_PCM_RATE 16000

/* What a whole stream said, and where in it the picture and the sound begin. */
typedef struct {
    uint8_t id[16];
    uint8_t flags;
    uint16_t hide_s;
    uint32_t ttl_s;
    uint8_t img_mode;
    uint16_t img_w, img_h;
    uint16_t audio_ms;
    char sender[JN1_SENDER_BYTES];
    char title[JN1_TITLE_BYTES];
    char body[JN1_BODY_BYTES];
    uint8_t n_choices;
    char choices[JN1_CHOICES_MAX][JN1_CHOICE_BYTES];
    /* Offsets into the stream (= the file): the first STRIPE record's header, the first PCM
     * record's. 0 when there is none. */
    uint32_t stripe_at, pcm_at;
    uint16_t stripes;
    uint32_t pcm_bytes;
    uint32_t bytes; /* the whole stream, END included */
} jn1_notice_t;

typedef enum {
    JN1_EV_NONE,   /* the bytes so far finish nothing */
    JN1_EV_RECORD, /* `rec`, `rec_n`: the magic or one whole record, checked: keep it */
    JN1_EV_END,    /* END, checked with everything before it: `rec` holds it, keep it too */
    JN1_EV_BAD,    /* refused: `why` says what; nothing more is read */
} jn1_ev_t;

typedef struct {
    uint8_t *rec;    /* the caller's JN1_BUF_BYTES */
    uint32_t rec_n;  /* bytes of the current unit in `rec` */
    uint32_t want;   /* bytes the current unit needs in all */
    uint32_t at;     /* stream offset of the current unit's first byte */
    int8_t order;    /* rank of the last known record */
    bool magic, meta, ended, bad;
    bool done; /* the unit in `rec` is whole and the caller's to keep */
    uint16_t want_row;
    const char *why;
    jn1_notice_t *n;
} jn1_reader_t;

void jn1_init(jn1_reader_t *r, uint8_t *buf, jn1_notice_t *out);
/* Where the next bytes go and how many the current unit still needs (0 once ended or refused):
 * a caller may read the network straight into it. */
size_t jn1_room(jn1_reader_t *r, uint8_t **at);
/* `n` bytes were put where jn1_room said. A RECORD or END stays in `rec` until the next
 * jn1_room. */
jn1_ev_t jn1_took(jn1_reader_t *r, size_t n);
/* The same from any buffer, in any split: consumes up to the byte that finishes a unit and
 * returns how many it took; call again with the rest. */
size_t jn1_feed(jn1_reader_t *r, const uint8_t *b, size_t n, jn1_ev_t *ev);
bool jn1_ended(const jn1_reader_t *r);
/* The stream stopped short (the body ended, the radio dropped): torn unless END was read. */
bool jn1_torn(const jn1_reader_t *r);

/* A STRIPE payload into rgb565 pixels (`img_w` × its rows, host order). False when it does not
 * cover its rows exactly or does not fit `cap` pixels; `y0`/`rows` say where it goes. */
bool jn1_stripe_pixels(const uint8_t *payload, uint32_t len, uint16_t img_w, uint16_t *px,
                       uint32_t cap, uint16_t *y0, uint16_t *rows);

/* The uuid as the box's text form, `8-4-4-4-12` lower-case hex: what the state POST names. */
void jn1_uuid_text(const uint8_t id[16], char out[37]);
/* Strict UTF-8, as Python's decode() takes it: no overlongs, surrogates or past U+10FFFF. */
bool jn1_utf8_ok(const uint8_t *s, size_t n);
