/* The sim's notices, sent the way the box sends them (sim_notice.c). */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef enum { SIM_PIC_NONE, SIM_PIC_WEATHER, SIM_PIC_MINECRAFT } sim_pic_t;

typedef struct {
    uint8_t id; /* the last byte of its uuid: one per notice in a state */
    const char *from, *title, *body;
    uint8_t n_choices;
    const char *choices[4];
    bool chime;
    uint16_t hide_s;
    sim_pic_t picture;
    uint16_t audio_ms;
} sim_notice_t;

bool sim_notice_push(const sim_notice_t *n);
