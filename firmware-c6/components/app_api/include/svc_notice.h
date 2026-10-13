/* Push notices from the box (plan §2). The service holds the queue; the shell draws the
 * overlay. A refused notice never reaches here — the box answers the sender at the door. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#define NOTICE_CHOICES_MAX 4

/* The head notice as the overlay draws it. The texts are the box's limits with their
 * terminators (backend `jpanel/notice.py`): sender 32, title 60, body 300, a choice 16 bytes. */
typedef struct {
    uint32_t id; /* this panel's own number for it, never 0; the box's id stays in svc_notice */
    char source[33];
    char title[61];
    char body[301];
    uint8_t n_choices; /* 0: a plain notice with OK */
    char choices[NOTICE_CHOICES_MAX][17];
    bool urgent;
    bool chime;
    uint16_t hide_s;   /* hides itself this long after it is shown; 0 until tapped */
    uint8_t img_mode;  /* 0 none, 1 full (480×480), 2 banner (480 × img_h) */
    uint16_t img_w, img_h;
    uint16_t stripes;  /* svc_notice_stripe's indexes, 0 .. stripes-1 */
    uint16_t audio_ms; /* its sound, 0 for none: played once as it is shown */
} notice_t;

uint8_t svc_notice_count(void);
bool svc_notice_head(notice_t *out);
/* Any queued notice is urgent: the clock face's badge shows it at full strength. */
bool svc_notice_urgent_waiting(void);
/* A choice tapped (a plain notice's OK is a dismissal). */
void svc_notice_answer(uint8_t choice);
/* BOOT: gone without an answer. */
void svc_notice_dismiss(void);
/* The shell put the head on the glass: its hide clock starts, its sound plays once (never over an
 * alarm), and the box hears `shown`. A repeat for the same notice changes nothing. */
void svc_notice_on_glass(uint32_t id);
/* The picture a few rows at a time, for the overlay's image decoder: open it, ask for rows top
 * to bottom (asking above the last row read starts again from the first stripe), close it. Its
 * runs are read through a small buffer, so a 480×240 banner costs this struct and the caller's
 * rows, never the picture. UI task. */
typedef struct {
    int fd;
    uint16_t w, h;
    uint32_t first_at;
    uint16_t row;         /* the next row the stream will give */
    uint32_t run_left;    /* pixels left of the run being read */
    uint16_t color;
    uint32_t stripe_left; /* run bytes left in the stripe being read */
    uint16_t buf_n, buf_at;
    uint8_t buf[96];
} notice_pic_t;
bool svc_notice_pic_open(uint32_t id, notice_pic_t *p);
/* `rows` whole rows from `y0` into `px` (rgb565, w per row). False past the picture or on a file
 * that does not read as it did when it was checked. */
bool svc_notice_pic_rows(notice_pic_t *p, uint16_t y0, uint16_t rows, uint16_t *px);
void svc_notice_pic_close(notice_pic_t *p);

/* How long notice `id` has been on the glass (its hide clock), false before it is shown. */
bool svc_notice_shown_for(uint32_t id, uint32_t *ms);

/* Notice `id`'s sound, for its speaker row: false when it has none. `playing` while it plays
 * (`played_ms` of it so far); otherwise it can be played again. */
bool svc_notice_sound(uint32_t id, bool *playing, uint32_t *played_ms);
/* The speaker row's tap: plays it again from the start (never over an alarm or anything else on
 * the speaker). False when it could not. */
bool svc_notice_replay(uint32_t id);

/* Stripe `index` of notice `id`'s picture, decoded into `px` (rgb565, `rows` × img_w, row-major):
 * one stripe is at most 40 rows, so it fits one 480×40 draw buffer. `y0` is its first row. False
 * when there is no such stripe, it does not fit `cap_px`, or the file cannot be read. UI task. */
bool svc_notice_stripe(uint32_t id, uint16_t index, uint16_t *px, uint32_t cap_px, uint16_t *y0,
                       uint16_t *rows);
