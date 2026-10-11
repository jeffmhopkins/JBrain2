#include "axp_logic.h"

axp_status_t axp_status_decode(uint8_t s1, uint8_t s2)
{
    return (axp_status_t){
        .vbus_good = (s1 >> 5) & 1,
        .battery = (s1 >> 3) & 1,
        .charging = ((s2 >> 5) & 3) == 1,
    };
}

uint16_t axp_adc14(uint8_t hi, uint8_t lo)
{
    return (uint16_t)(((hi & 0x3F) << 8) | lo);
}

uint16_t axp_adc13(uint8_t hi, uint8_t lo)
{
    return (uint16_t)(((hi & 0x1F) << 8) | lo);
}

bool axp_pek_pressed(uint8_t irq2)
{
    return (irq2 & AXP_PEK_DOWN) != 0;
}

uint8_t axp_field(uint8_t cur, uint8_t mask, uint8_t val)
{
    return (uint8_t)((cur & (uint8_t)~mask) | (val & mask));
}
