#include "ui_pin.h"

bool ui_pin_should(ui_pin_t *p, uint32_t last_id)
{
    bool pin = !p->seen || last_id != p->last_id;
    p->seen = true;
    p->last_id = last_id;
    return pin;
}
