#include "notice_view.h"

#include <stdio.h>
#include <string.h>

#include "minutes.h"
#include "ui_theme.h"

void notice_night_palette(uint8_t night_color, uint32_t *color, uint8_t *opa)
{
    static const struct {
        uint32_t color;
        uint8_t opa;
    } P[] = {{0xB23A3A, 140}, {0xA47A3E, 128}, {0x6E7178, 140}};
    const uint8_t i = night_color <= NOTICE_NIGHT_GREY ? night_color : NOTICE_NIGHT_RED;
    *color = P[i].color;
    *opa = P[i].opa;
}

bool notice_night_now(bool night_on, uint16_t from, uint16_t to, bool time_valid, uint16_t now_min)
{
    return night_on && time_valid && min_window_contains(now_min, from, to);
}

void notice_look(const notice_t *n, uint8_t queued, bool night, uint8_t night_color, bool revealed,
                 uint32_t shown_for_ms, notice_look_t *out)
{
    memset(out, 0, sizeof *out);
    out->night = night;
    if (night) {
        uint32_t c;
        uint8_t opa;
        notice_night_palette(night_color, &c, &opa);
        out->accent = out->title = out->body = out->muted = c;
        out->ink_opa = opa;
    } else {
        out->accent = n->urgent ? UI_C_WARN : UI_C_STEEL;
        out->title = UI_C_TEXT;
        out->body = UI_C_TEXT_SOFT;
        out->muted = UI_C_MUTED;
        out->ink_opa = 255;
    }
    /* A picture at night would light the whole glass at 3 a.m.: its words come first, on the night
       palette, and the picture only when asked for (owner, 2026-10-13). */
    const bool picture = n->img_mode != 0 && n->stripes;
    out->ask_picture = picture && night && !revealed;
    out->full_picture = picture && !out->ask_picture && n->img_mode == 1;
    out->banner = picture && !out->ask_picture && n->img_mode == 2;
    if (queued > 1) snprintf(out->count, sizeof out->count, "1 of %u", (unsigned)queued);

    const uint32_t hide_ms = (uint32_t)n->hide_s * 1000u;
    const uint32_t left_ms = hide_ms > shown_for_ms ? hide_ms - shown_for_ms : 0;
    const unsigned left_s = (unsigned)((left_ms + 999) / 1000);
    out->timer = hide_ms != 0;
    out->timer_permille = hide_ms ? (uint16_t)((uint64_t)left_ms * 1000u / hide_ms) : 0;
    out->timer_color = out->full_picture ? 0xFFFFFF : out->accent;
    out->timer_opa = out->full_picture ? 217 : out->ink_opa;
    /* Every notice hides on a tap away from its buttons, a question too (dismissed, no answer). */
    if (n->n_choices) {
        if (hide_ms)
            snprintf(out->hint, sizeof out->hint, "Tap an answer · hides in %u s", left_s);
        else
            snprintf(out->hint, sizeof out->hint, "Tap an answer · tap elsewhere to hide");
    } else if (hide_ms) {
        snprintf(out->hint, sizeof out->hint, "Tap to hide · hides in %u s", left_s);
    } else {
        snprintf(out->hint, sizeof out->hint, "Tap to hide");
    }
    if (hide_ms)
        snprintf(out->chip_hint, sizeof out->chip_hint, "Tap to hide · %u s", left_s);
    else
        snprintf(out->chip_hint, sizeof out->chip_hint, "Tap to hide");
}

static void mmss(uint32_t s, char *out, size_t cap)
{
    snprintf(out, cap, "%u:%02u", (unsigned)(s / 60 % 100), (unsigned)(s % 60));
}

void notice_sound_line(bool playing, uint32_t played_ms, uint32_t total_ms, notice_sound_line_t *out)
{
    memset(out, 0, sizeof *out);
    out->playing = playing;
    const uint32_t total_s = (total_ms + 999) / 1000;
    char total[8];
    mmss(total_s, total, sizeof total);
    if (!playing) {
        snprintf(out->time, sizeof out->time, "%s", total);
        return;
    }
    if (played_ms > total_ms) played_ms = total_ms;
    char done[8];
    mmss(played_ms / 1000, done, sizeof done);
    snprintf(out->time, sizeof out->time, "%s / %s", done, total);
    out->permille = total_ms ? (uint16_t)((uint64_t)played_ms * 1000u / total_ms) : 0;
}
