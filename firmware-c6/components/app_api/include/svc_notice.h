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
/* Stripe `index` of notice `id`'s picture, decoded into `px` (rgb565, `rows` × img_w, row-major):
 * one stripe is at most 40 rows, so it fits one 480×40 draw buffer. `y0` is its first row. False
 * when there is no such stripe, it does not fit `cap_px`, or the file cannot be read. UI task. */
bool svc_notice_stripe(uint32_t id, uint16_t index, uint16_t *px, uint32_t cap_px, uint16_t *y0,
                       uint16_t *rows);
