/* The arithmetic behind the panel's clock, with no chip and no OS: the PCF85063's registers,
 * calendar dates to and from Unix time, the box's HTTP Date header, the zone table and when a
 * time from the box should replace the one the panel has. Pure C, host-tested. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* A UTC calendar instant. wday 0 = Monday … 6 = Sunday, as svc_rtc.h counts. */
typedef struct {
    uint16_t year;
    uint8_t mon, mday, h, m, s, wday;
} civil_t;

int64_t civil_to_epoch(const civil_t *c);
void epoch_to_civil(int64_t epoch, civil_t *out);

/* Earlier than this is a clock nobody set: the panel shows --:-- rather than 1970. */
#define TIME_EPOCH_MIN 1767225600 /* 2026-01-01T00:00:00Z */

/* The PCF85063's time registers 0x04-0x0A as read in one burst. The chip keeps UTC: a zone
 * change on the panel must not need the chip rewritten, and nothing else reads it. False when
 * the oscillator-stopped flag is set (the cell ran out, or it was never set) or a field is out
 * of range — either way the time in it is not one to show. */
bool pcf_decode(const uint8_t regs[7], civil_t *out);
/* Writing the seconds register also clears the oscillator-stopped flag. */
void pcf_encode(const civil_t *c, uint8_t regs[7]);
uint8_t bcd_to_bin(uint8_t v);
uint8_t bin_to_bcd(uint8_t v);

/* RFC 9110's IMF-fixdate, "Sun, 06 Nov 1994 08:49:37 GMT" — the only form a current server
 * sends. Anything else is refused rather than half-read. */
bool http_date_parse(const char *s, int64_t *epoch);

/* The box's time is one-second resolution and a request's latency old, so a panel within two
 * seconds of it is left alone: stepping on that jitter would make the seconds jump. */
#define TIME_STEP_S 2
/* The chip drifts a few seconds a day; rewriting it daily bounds what a power cut can cost. */
#define TIME_RTC_REWRITE_S (24 * 3600)
bool time_should_step(bool have_time, int64_t now, int64_t box);
bool time_should_write_rtc(bool rtc_ok, bool stepped, int64_t since_write_s);

/* The zone table behind svc_rtc.h's TZ_ZONES: a short label for the glass and the POSIX rule
 * newlib's localtime computes daylight saving from. */
const char *tz_posix(int32_t idx);
