/* The notice overlay's look, decided apart from LVGL (mock Main.dc.html's `note`, the owner's
 * decisions of 2026-10-13): the colours, the words under the card, the countdown line, whether
 * the picture fills the glass, sits over the words, or waits behind Show picture at night, and
 * the speaker row's line. Pure C, host-tested; shell/notice_overlay.c draws it. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "svc_notice.h"

/* The night clock's colours, the same three as the face's (app_clock's clock_face, a host test
 * holds them equal): red, amber, grey, each at its own opacity. */
enum { NOTICE_NIGHT_RED, NOTICE_NIGHT_AMBER, NOTICE_NIGHT_GREY };

typedef struct {
    bool night;            /* inside the night clock's hours: every colour is the night colour */
    uint32_t accent;       /* the sender, the bell, the first answer, the speaker row */
    uint32_t title, body, muted;
    uint8_t ink_opa;       /* 255 by day; the night palette's opacity at night */
    bool ask_picture;      /* the picture waits behind Show picture (night, not yet asked for) */
    bool full_picture;     /* the picture fills the glass, the sender and the hint as chips */
    bool banner;           /* the picture across the top, the words under it */
    bool timer;            /* the countdown line along the bottom edge */
    uint16_t timer_permille; /* how much of it is left, 1000 = all */
    uint32_t timer_color;
    uint8_t timer_opa;
    char count[12];        /* "1 of 2", "" alone */
    char hint[64];         /* the line under the card */
    char chip_hint[32];    /* a full picture's top-right chip */
} notice_look_t;

/* Whether `now_min` is inside the night clock's hours: only with the night clock on and a time. */
bool notice_night_now(bool night_on, uint16_t from, uint16_t to, bool time_valid, uint16_t now_min);

/* `queued` notices in all; `revealed`: Show picture was tapped; `shown_for_ms`: how long it has
 * been on the glass (its hide clock). */
void notice_look(const notice_t *n, uint8_t queued, bool night, uint8_t night_color, bool revealed,
                 uint32_t shown_for_ms, notice_look_t *out);
/* The night palette entry: colour and opacity. */
void notice_night_palette(uint8_t night_color, uint32_t *color, uint8_t *opa);

/* The speaker row: while it plays, "0:02 / 0:06" and how far along (permille); afterwards
 * "Play again" beside its length. */
typedef struct {
    bool playing;
    uint16_t permille;
    char time[28];
} notice_sound_line_t;
void notice_sound_line(bool playing, uint32_t played_ms, uint32_t total_ms, notice_sound_line_t *out);
