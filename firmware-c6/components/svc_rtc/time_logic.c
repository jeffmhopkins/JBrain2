#include "time_logic.h"

#include <string.h>

#include "svc_rtc.h"

/* Howard Hinnant's days_from_civil / civil_from_days: exact for every proleptic Gregorian
 * date, with no table and no dependence on the C library's timegm, which newlib lacks. */
static int64_t days_from_civil(int64_t y, unsigned m, unsigned d)
{
    y -= m <= 2;
    const int64_t era = (y >= 0 ? y : y - 399) / 400;
    const unsigned yoe = (unsigned)(y - era * 400);
    const unsigned doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + (int64_t)doe - 719468;
}

int64_t civil_to_epoch(const civil_t *c)
{
    return days_from_civil(c->year, c->mon, c->mday) * 86400 + c->h * 3600 + c->m * 60 + c->s;
}

void epoch_to_civil(int64_t epoch, civil_t *out)
{
    int64_t days = epoch / 86400, rem = epoch % 86400;
    if (rem < 0) {
        rem += 86400;
        days--;
    }
    /* 1970-01-01 was a Thursday: day 0 is wday 3 counting Monday as 0. */
    out->wday = (uint8_t)(((days % 7) + 7 + 3) % 7);
    const int64_t z = days + 719468;
    const int64_t era = (z >= 0 ? z : z - 146096) / 146097;
    const unsigned doe = (unsigned)(z - era * 146097);
    const unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    const unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    const unsigned mp = (5 * doy + 2) / 153;
    out->mday = (uint8_t)(doy - (153 * mp + 2) / 5 + 1);
    out->mon = (uint8_t)(mp < 10 ? mp + 3 : mp - 9);
    out->year = (uint16_t)((int64_t)yoe + era * 400 + (out->mon <= 2));
    out->h = (uint8_t)(rem / 3600);
    out->m = (uint8_t)(rem / 60 % 60);
    out->s = (uint8_t)(rem % 60);
}

uint8_t bcd_to_bin(uint8_t v)
{
    return (uint8_t)((v >> 4) * 10 + (v & 0x0F));
}

uint8_t bin_to_bcd(uint8_t v)
{
    return (uint8_t)(((v / 10) << 4) | (v % 10));
}

bool pcf_decode(const uint8_t r[7], civil_t *out)
{
    if (r[0] & 0x80) return false; /* OS: the oscillator stopped since the time was set */
    civil_t c = {
        .s = bcd_to_bin(r[0] & 0x7F),
        .m = bcd_to_bin(r[1] & 0x7F),
        .h = bcd_to_bin(r[2] & 0x3F),
        .mday = bcd_to_bin(r[3] & 0x3F),
        .mon = bcd_to_bin(r[5] & 0x1F),
        .year = (uint16_t)(2000 + bcd_to_bin(r[6])),
    };
    if (c.s > 59 || c.m > 59 || c.h > 23 || c.mday < 1 || c.mday > 31 || c.mon < 1 || c.mon > 12)
        return false;
    /* The chip's own weekday is whatever was last written; the date decides it. */
    const int64_t e = civil_to_epoch(&c);
    if (e < TIME_EPOCH_MIN) return false;
    epoch_to_civil(e, out);
    return true;
}

void pcf_encode(const civil_t *c, uint8_t r[7])
{
    r[0] = bin_to_bcd(c->s); /* bit 7 clear: OS cleared, the time is good from here */
    r[1] = bin_to_bcd(c->m);
    r[2] = bin_to_bcd(c->h);
    r[3] = bin_to_bcd(c->mday);
    r[4] = (uint8_t)((c->wday + 1) % 7); /* the chip counts 0 = Sunday */
    r[5] = bin_to_bcd(c->mon);
    r[6] = bin_to_bcd((uint8_t)(c->year % 100));
}

static int digits(const char *s, int n)
{
    int v = 0;
    for (int i = 0; i < n; i++) {
        if (s[i] < '0' || s[i] > '9') return -1;
        v = v * 10 + (s[i] - '0');
    }
    return v;
}

bool http_date_parse(const char *s, int64_t *epoch)
{
    static const char MON[] = "JanFebMarAprMayJunJulAugSepOctNovDec";
    /* "Sun, 06 Nov 1994 08:49:37 GMT": fixed width, 29 characters. */
    if (!s || strlen(s) != 29 || s[3] != ',' || s[4] != ' ' || s[7] != ' ' || s[11] != ' ' ||
        s[16] != ' ' || s[19] != ':' || s[22] != ':' || strcmp(s + 25, " GMT") != 0)
        return false;
    int mon = 0;
    for (int i = 0; i < 12; i++)
        if (!strncmp(s + 8, MON + i * 3, 3)) mon = i + 1;
    const int d = digits(s + 5, 2), y = digits(s + 12, 4), h = digits(s + 17, 2),
              m = digits(s + 20, 2), sec = digits(s + 23, 2);
    if (!mon || d < 1 || d > 31 || y < 1970 || h < 0 || h > 23 || m < 0 || m > 59 || sec < 0 ||
        sec > 60)
        return false;
    civil_t c = {.year = (uint16_t)y, .mon = (uint8_t)mon, .mday = (uint8_t)d, .h = (uint8_t)h,
                 .m = (uint8_t)m, .s = (uint8_t)(sec > 59 ? 59 : sec)};
    *epoch = civil_to_epoch(&c);
    return true;
}

bool time_should_step(bool have_time, int64_t now, int64_t box)
{
    if (box < TIME_EPOCH_MIN) return false;
    if (!have_time) return true;
    const int64_t d = now > box ? now - box : box - now;
    return d >= TIME_STEP_S;
}

bool time_should_write_rtc(bool rtc_ok, bool stepped, int64_t since_write_s)
{
    return !rtc_ok || stepped || since_write_s < 0 || since_write_s >= TIME_RTC_REWRITE_S;
}

/* US zones first because the owner's box is in one; the rest are there so a move is a tap, not
 * a firmware. Rules are the US 2007 and EU 1996 ones newlib understands directly. */
static const struct {
    const char *label, *posix;
} ZONES[TZ_ZONES] = {
    {"Honolulu", "HST10"},
    {"Anchorage", "AKST9AKDT,M3.2.0,M11.1.0"},
    {"Los Angeles", "PST8PDT,M3.2.0,M11.1.0"},
    {"Denver", "MST7MDT,M3.2.0,M11.1.0"},
    {"Chicago", "CST6CDT,M3.2.0,M11.1.0"},
    {"New York", "EST5EDT,M3.2.0,M11.1.0"},
    {"Phoenix", "MST7"},
    {"UTC", "UTC0"},
    {"London", "GMT0BST,M3.5.0/1,M10.5.0"},
    {"Paris", "CET-1CEST,M3.5.0,M10.5.0/3"},
    {"Athens", "EET-2EEST,M3.5.0/3,M10.5.0/4"},
    {"Sydney", "AEST-10AEDT,M10.1.0,M4.1.0/3"},
};

const char *tz_posix(int32_t idx)
{
    return ZONES[idx >= 0 && idx < TZ_ZONES ? idx : TZ_DEFAULT].posix;
}

const char *svc_rtc_tz_label(int32_t idx)
{
    return ZONES[idx >= 0 && idx < TZ_ZONES ? idx : TZ_DEFAULT].label;
}
