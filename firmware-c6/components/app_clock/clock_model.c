#include "clock_model.h"

#include <stdio.h>
#include <string.h>

#include "minutes.h"
#include "ui_theme.h"

void clock_fmt_hm(uint8_t h, uint8_t m, char out[6])
{
    snprintf(out, 6, "%02u:%02u", (unsigned)(h % 24), (unsigned)(m % 60));
}

void clock_fmt_face(const rtc_time_t *t, char out[6])
{
    if (t->valid)
        clock_fmt_hm(t->h, t->m, out);
    else
        snprintf(out, 6, "--:--");
}

void clock_fmt_daydate(const rtc_time_t *t, char *buf, size_t len)
{
    static const char *const D[7] = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"};
    static const char *const M[12] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};
    if (!t->valid || t->wday > 6 || t->mon < 1 || t->mon > 12) {
        if (len) buf[0] = '\0';
        return;
    }
    snprintf(buf, len, "%s %u %s", D[t->wday], (unsigned)t->mday, M[t->mon - 1]);
}

static bool rings_on(uint8_t days, uint8_t wday)
{
    return days == 0 || (days >> wday & 1);
}

bool clock_next_alarm(const alarm_t *a, uint8_t n, uint16_t now_min, uint8_t wday, uint8_t *idx,
                      uint32_t *wait_min)
{
    bool found = false;
    uint32_t best = 0;
    for (uint8_t i = 0; i < n; i++) {
        if (!a[i].on) continue;
        int32_t at = a[i].h * 60 + a[i].m;
        for (uint8_t d = 0; d <= 7; d++) {
            int32_t wait = (int32_t)d * MIN_PER_DAY + at - now_min;
            if (wait <= 0 || !rings_on(a[i].days, (uint8_t)((wday + d) % 7))) continue;
            if (!found || (uint32_t)wait < best) {
                found = true;
                best = (uint32_t)wait;
                *idx = i;
            }
            break;
        }
    }
    if (found) *wait_min = best;
    return found;
}

void clock_fmt_next(const alarm_t *a, uint32_t wait_min, char *buf, size_t len)
{
    char hm[6];
    clock_fmt_hm(a->h, a->m, hm);
    uint32_t h = wait_min / 60, m = wait_min % 60;
    if (h)
        snprintf(buf, len, "Next %s · in %lu h %lu min", hm, (unsigned long)h, (unsigned long)m);
    else
        snprintf(buf, len, "Next %s · in %lu min", hm, (unsigned long)m);
}

void clock_fmt_days(uint8_t days, const char *label, char *buf, size_t len)
{
    static const char *const D[7] = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"};
    char d[40] = "";
    days &= 0x7F;
    if (days == 0)
        strcpy(d, "Once");
    else if (days == 0x7F)
        strcpy(d, "Every day");
    else if (days == 0x1F)
        strcpy(d, "Weekdays");
    else
        for (int i = 0; i < 7; i++)
            if (days >> i & 1) {
                if (d[0]) strcat(d, ", ");
                strcat(d, D[i]);
            }
    if (label && label[0])
        snprintf(buf, len, "%s · %s", d, label);
    else
        snprintf(buf, len, "%s", d);
}

uint16_t clock_snooze(uint8_t h, uint8_t m, uint8_t minutes)
{
    return min_add((uint16_t)(h * 60 + m), minutes);
}

clock_face_t clock_face(bool night_on, uint16_t from, uint16_t to, uint8_t color, uint16_t now_min,
                        bool force_night)
{
    /* The mock's opacities; the date is the digits' times 0.7 at night and 0.55 by day. */
    static const clock_face_t NIGHT[] = {
        {0xB23A3A, 140, 98, true}, /* red, 55% */
        {0xA47A3E, 128, 89, true}, /* amber, 50% */
        {0x6E7178, 140, 98, true}, /* grey, 55% */
    };
    if (!force_night && !(night_on && min_window_contains(now_min, from, to)))
        return (clock_face_t){UI_C_TEXT, 255, 140, false};
    return NIGHT[color > CLOCK_NIGHT_GREY ? CLOCK_NIGHT_RED : color];
}

void clock_fmt_window(uint16_t from, uint16_t to, char *buf, size_t len)
{
    snprintf(buf, len, "%02u:%02u – %02u:%02u", from / 60, from % 60, to / 60, to % 60);
}

bool clock_holds_notices(bool face_up, uint8_t notices, uint32_t head_id, uint32_t released_id)
{
    if (!face_up) return false;
    return !(notices && head_id == released_id);
}

uint16_t clock_badge_count(uint8_t notices, uint16_t unplayed)
{
    return (uint16_t)(notices + unplayed);
}

uint8_t clock_badge_opa(bool urgent)
{
    return urgent ? 255 : 204;
}
