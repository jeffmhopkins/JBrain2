/* Decoding the AXP2101's registers, pure so the host suite checks every bit against the sources
 * it came from (XPowersLib's AXP2101 driver, MIT; the Waveshare BSP, Apache-2.0 — HARDWARE.md §1). */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Register 0x00 (status 1) and 0x01 (status 2). */
typedef struct {
    bool vbus_good; /* 0x00 bit 5 */
    bool battery;   /* 0x00 bit 3: a cell is on the connector */
    bool charging;  /* 0x01 bits 6:5 == 01 */
} axp_status_t;

axp_status_t axp_status_decode(uint8_t s1, uint8_t s2);

/* The ADC results: VBUS and VSYS are 14 bits (high byte bits 5:0), the battery 13 bits (high
 * byte bits 4:0), all in mV. */
uint16_t axp_adc14(uint8_t hi, uint8_t lo);
uint16_t axp_adc13(uint8_t hi, uint8_t lo);

/* IRQ status 2 (0x49): the power key's four latched events. They latch whether or not the IRQ
 * pin is wired — and on this board it is not (HARDWARE.md §1), so they are polled. */
#define AXP_PEK_UP 0x01    /* positive edge: released */
#define AXP_PEK_DOWN 0x02  /* negative edge: pressed */
#define AXP_PEK_LONG 0x04
#define AXP_PEK_SHORT 0x08
#define AXP_PEK_ALL 0x0F

/* True when the latched flags hold a new press. Only the falling edge counts: a short press also
 * latches SHORT and UP when it is released, and a poll that saw the release later would turn one
 * press into two. A press caught entirely between two polls still latched DOWN. */
bool axp_pek_pressed(uint8_t irq2);

/* A read-modify-write's new value: `val` in the bits of `mask`, the rest untouched. */
uint8_t axp_field(uint8_t cur, uint8_t mask, uint8_t val);
