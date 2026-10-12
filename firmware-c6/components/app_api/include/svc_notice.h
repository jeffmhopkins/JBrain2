/* Push notices from the box (plan §2). The service holds the queue; the shell draws the
 * overlay. A refused notice never reaches here — the box answers the sender at the door. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#define NOTICE_CHOICES_MAX 4

typedef struct {
    uint32_t id;
    char source[24];
    char title[48];
    char body[160];
    uint8_t n_choices; /* 0: a plain notice with OK */
    char choices[NOTICE_CHOICES_MAX][16];
    bool urgent;
} notice_t;

uint8_t svc_notice_count(void);
bool svc_notice_head(notice_t *out);
/* Any queued notice is urgent: the clock face's badge shows it at full strength. */
bool svc_notice_urgent_waiting(void);
void svc_notice_answer(uint8_t choice);
/* BOOT: gone without an answer. */
void svc_notice_dismiss(void);
