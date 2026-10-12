#include "tp_logic.h"

tp_report_t tp_decode(const uint8_t d[TP_REPORT_LEN])
{
    tp_report_t r = {TP_NOACK, 0, 0};
    if (d[6] != TP_ACK) return r;
    r.kind = TP_UP;
    if ((d[5] & 0x7F) == 0 || (d[0] & 0x0F) != 0x06) return r;
    r.kind = TP_DOWN;
    r.raw_a = (uint16_t)((d[1] << 4) | (d[3] >> 4));
    r.raw_b = (uint16_t)((d[2] << 4) | (d[3] & 0x0F));
    return r;
}

static int16_t on_glass(uint16_t v)
{
    return (int16_t)(v >= TP_SIDE ? TP_SIDE - 1 : v);
}

void tp_map(uint16_t raw_a, uint16_t raw_b, int16_t *x, int16_t *y)
{
    *x = (int16_t)(TP_SIDE - 1 - on_glass(raw_b));
    *y = on_glass(raw_a);
}

tp_ev_t tp_track(tp_track_t *t, const tp_report_t *r)
{
    if (r && r->kind == TP_DOWN) {
        int16_t x, y;
        tp_map(r->raw_a, r->raw_b, &x, &y);
        const bool was = t->down, moved = x != t->x || y != t->y;
        t->down = true;
        t->quiet = 0;
        t->x = x;
        t->y = y;
        return !was ? TP_EV_DOWN : moved ? TP_EV_MOVE : TP_EV_NONE;
    }
    if (!t->down) return TP_EV_NONE;
    if (r && r->kind == TP_UP) {
        t->down = false;
        return TP_EV_UP;
    }
    if (++t->quiet < TP_HOLD_POLLS) return TP_EV_NONE;
    t->down = false;
    return TP_EV_UP;
}

void tp_ring_push(tp_ring_t *q, const tp_sample_t *s)
{
    if (q->count) {
        tp_sample_t *last = &q->s[(q->head + q->count - 1) % TP_RING];
        if (s->ev == TP_EV_MOVE && last->ev == TP_EV_MOVE) {
            *last = *s;
            return;
        }
    }
    if (q->count == TP_RING) {
        q->head = (uint8_t)((q->head + 1) % TP_RING);
        q->count--;
        q->dropped++;
    }
    q->s[(q->head + q->count) % TP_RING] = *s;
    q->count++;
}

bool tp_ring_pop(tp_ring_t *q, tp_sample_t *out)
{
    if (!q->count) return false;
    *out = q->s[q->head];
    q->head = (uint8_t)((q->head + 1) % TP_RING);
    q->count--;
    return true;
}
