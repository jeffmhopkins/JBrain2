#include "jet.h"

#include <math.h>
#include <string.h>

/* Full tilt is this much push, px/s^2; friction bleeds it off, so he coasts to a hover. Up and
   down is the steering and wants the snap; forward and back is only for waiting, so it is
   softer. */
#define FLY_ACCEL 1000.0f
#define DRIFT_ACCEL 500.0f
#define FLY_FRICTION 4.5f
#define DRIFT_FRICTION 4.0f
/* Where his origin may go: room for his head above and his feet below, and a band either side
   of his home column. */
#define EDGE_TOP 48.0f
#define EDGE_BOTTOM 30.0f
#define DRIFT_MIN 50.0f
#define DRIFT_MAX 200.0f
#define MIX_KINDS_P 0.3f /* after a world's first three, the chance one is from an earlier world */
#define NEW_ALONE 3      /* a world's first this many are its own kind, so it is met alone */
#define PIPE_JUMP 110.0f /* the least a pipe's gap moves from the last one's */

static const char *const NAMES[JET_WORLDS] = {
    "MEADOW", "HILLS", "CAVE", "CANDY", "JUNGLE", "SKY", "FACTORY", "OCEAN", "SPACE", "STATION",
};

/* Each world's own kinds; a second one, when there is one, comes as often as the first. */
static const uint8_t KINDS[JET_WORLDS][2] = {
    {JET_PIPE, JET_PIPE},       {JET_PILLAR, JET_PILLAR},     {JET_ROCK, JET_ROCK},
    {JET_SLIDE, JET_SLIDE},     {JET_TUNNEL, JET_TUNNEL},     {JET_BIRDS, JET_RISERS},
    {JET_CRUSHER, JET_CRUSHER}, {JET_MINES, JET_MINES},       {JET_SPINNER, JET_SPINNER2},
    {JET_LASER, JET_LASER},
};

static uint32_t rnd(uint32_t *s)
{
    /* xorshift32, as `game.c`: a seed replays a round, which is what the host suite needs. */
    uint32_t x = *s ? *s : 0x9E3779B9u;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    *s = x;
    return x;
}

static float rnd01(uint32_t *s)
{
    return (float)(rnd(s) >> 8) / 16777216.0f;
}

static float clampf(float v, float lo, float hi)
{
    return v < lo ? lo : v > hi ? hi : v;
}

const char *jet_world_name(int world)
{
    return world < JET_WORLDS ? NAMES[world] : "MIX";
}

int jet_backdrop(int world)
{
    return world < JET_WORLDS ? world : JET_WORLDS;
}

/* SLOW, THEN NOT, A WORLD AT A TIME. The Meadow scrolls at about a third of a screen a second
   with wide gaps; each world is a little faster and tighter. Past the tenth, the mixes climb on
   to a ceiling and a floor. */
static float world_speed(int w)
{
    if (w < JET_WORLDS) return 46.0f + 4.0f * (float)w;
    const float v = 86.0f + 5.0f * (float)(w - JET_WORLDS + 1);
    return v < 120.0f ? v : 120.0f;
}

static float world_gap(int w)
{
    if (w < JET_WORLDS) return 196.0f - 3.0f * (float)w;
    const float g = 166.0f - 4.0f * (float)(w - JET_WORLDS + 1);
    return g > 150.0f ? g : 150.0f;
}

/* The clear run after one obstacle's tail before the next comes in. */
static float world_space(int w)
{
    return w < JET_WORLDS ? 200.0f - 3.0f * (float)w : 170.0f;
}

static uint8_t pick_kind(int world, int n, uint32_t *rng)
{
    if (world >= JET_WORLDS) return (uint8_t)(rnd(rng) % JET_KINDS);
    if (n >= NEW_ALONE && world > 0 && rnd01(rng) < MIX_KINDS_P) {
        const int w = (int)(rnd(rng) % (uint32_t)world);
        return KINDS[w][rnd(rng) & 1u];
    }
    return KINDS[world][rnd(rng) & 1u];
}

/* A GAP THAT IS NEVER WHERE THE LAST ONE WAS: the owner's "these were all basically the same
   gap". A pipe's gap is PIPE_JUMP above or below the last where the field has the room, and as
   far as it has where it does not, so pipe after pipe is a climb then a dive. */
static float jump_gap(float gap, float fh, float prev, uint32_t *rng)
{
    const float lo = 70.0f + gap / 2.0f, hi = fh - 50.0f - gap / 2.0f;
    if (hi <= lo) return fh / 2.0f;
    float y = lo + rnd01(rng) * (hi - lo);
    /* Too close: to the far side of the last one, whichever side has the more room. */
    if (prev > 0.0f && fabsf(y - prev) < PIPE_JUMP) {
        y = hi - prev > prev - lo ? fminf(hi, prev + PIPE_JUMP + rnd01(rng) * 40.0f)
                                  : fmaxf(lo, prev - PIPE_JUMP - rnd01(rng) * 40.0f);
    }
    return y;
}

static void make(jet_t *j, jet_obj_t *o, uint8_t kind, uint32_t *rng)
{
    const float fh = j->fh, gap = world_gap(j->world);
    memset(o, 0, sizeof(*o));
    o->live = true;
    o->kind = kind;
    o->x = j->fw + 10.0f;
    o->w = 60.0f;
    o->gap = gap;
    o->t0 = rnd01(rng) * 6.0f;
    switch (kind) {
    case JET_PIPE:
        o->y0 = j->last_gap = jump_gap(gap, fh, j->last_gap, rng);
        o->gap = gap - 10.0f + rnd01(rng) * 30.0f;
        o->star = rnd01(rng) < 0.4f;
        break;
    case JET_PILLAR:
        o->w = 52.0f;
        o->from_top = rnd01(rng) < 0.5f;
        o->y0 = fh * (0.5f + rnd01(rng) * 0.14f);
        o->star = rnd01(rng) < 0.5f;
        break;
    case JET_ROCK:
        o->w = 50.0f;
        o->y0 = 60.0f + rnd01(rng) * 90.0f; /* the floor spike's height */
        o->fy = 22.0f;
        o->trig = 200.0f + rnd01(rng) * 90.0f;
        break;
    case JET_SLIDE:
        o->y0 = fh / 2.0f + (rnd01(rng) - 0.5f) * 60.0f;
        o->amp = 70.0f + rnd01(rng) * 30.0f;
        o->rate = 1.0f + rnd01(rng) * 0.6f;
        o->star = rnd01(rng) < 0.4f;
        break;
    case JET_TUNNEL: {
        o->w = 440.0f;
        o->gap = gap - 50.0f > 132.0f ? gap - 50.0f : 132.0f;
        o->amp = 72.0f;
        const float m = o->amp + o->gap / 2.0f + 24.0f;
        o->y0 = fh > 2.0f * m ? m + rnd01(rng) * (fh - 2.0f * m) : fh / 2.0f;
        o->rate = rnd01(rng) < 0.5f ? 1.0f : 1.5f;
        o->star = true;
        break;
    }
    case JET_BIRDS:
        o->w = 70.0f;
        o->y0 = 110.0f + rnd01(rng) * (fh - 220.0f);
        o->extra = 45.0f;
        break;
    case JET_RISERS:
        o->w = 44.0f;
        o->rate = 45.0f + rnd01(rng) * 20.0f;
        break;
    case JET_CRUSHER:
        o->w = 66.0f;
        o->y0 = fh / 2.0f + (rnd01(rng) - 0.5f) * 120.0f;
        o->star = true;
        break;
    case JET_MINES:
        o->w = 110.0f;
        o->y0 = jump_gap(150.0f, fh, 0.0f, rng);
        o->y1 = jump_gap(150.0f, fh, o->y0, rng);
        break;
    case JET_SPINNER:
        o->w = 20.0f;
        o->y0 = 150.0f + rnd01(rng) * (fh - 300.0f);
        o->len = 100.0f;
        o->va = (rnd01(rng) < 0.5f ? -1.0f : 1.0f) * (1.0f + rnd01(rng) * 0.5f);
        break;
    case JET_SPINNER2:
        o->w = 20.0f;
        o->len = 64.0f;
        o->va = 1.4f + rnd01(rng) * 0.4f;
        break;
    case JET_LASER:
    default:
        o->w = 14.0f;
        o->span = (uint8_t)(rnd(rng) % 3u);
        break;
    }
}

float jet_slide_y(const jet_obj_t *o, float t)
{
    return o->y0 + sinf(t * o->rate + o->t0) * o->amp;
}

float jet_tunnel_y(const jet_obj_t *o, float x)
{
    return o->y0 + sinf((x - o->x) / o->w * 6.2831853f * o->rate) * o->amp;
}

void jet_bird(const jet_obj_t *o, float t, int i, float *x, float *y)
{
    const float c = o->y0 + sinf(t * 2.0f + o->t0) * 40.0f;
    *x = o->x + (i == 0 ? 10.0f : 42.0f);
    *y = c + (i == 0 ? 0.0f : i == 1 ? -30.0f : 30.0f);
}

float jet_riser_y(const jet_obj_t *o, float t, int k, float fh)
{
    const float loop = fh + 140.0f;
    const float v = (float)k * loop / 4.0f - t * o->rate - o->t0 * 40.0f;
    return fmodf(fmodf(v, loop) + loop, loop) - 70.0f;
}

/* Open most of the time, a quick slam, a short wait, then open again: a 3.2 s cycle. */
float jet_crush_open(const jet_obj_t *o, float t)
{
    const float p = fmodf(t + o->t0, 3.2f) / 3.2f;
    if (p < 0.55f) return 1.0f;
    if (p < 0.62f) return 1.0f - (p - 0.55f) / 0.07f;
    if (p < 0.78f) return 0.0f;
    return (p - 0.78f) / 0.22f;
}

/* Off, a flicker of warning, then on: a 2.8 s cycle, so a full-height beam can be timed. */
int jet_laser_state(const jet_obj_t *o, float t)
{
    const float p = fmodf(t + o->t0, 2.8f);
    return p < 1.3f ? JET_LASER_OFF : p < 1.85f ? JET_LASER_WARN : JET_LASER_ON;
}

void jet_laser_span(const jet_obj_t *o, float fh, float *a, float *b)
{
    *a = o->span == JET_SPAN_BOTTOM ? fh * 0.4f : 0.0f;
    *b = o->span == JET_SPAN_TOP ? fh * 0.6f : fh;
}

int jet_spin(const jet_obj_t *o, float t, float fh, int i, float *px, float *py, float *a)
{
    *px = o->x + 10.0f;
    if (o->kind == JET_SPINNER) {
        *py = o->y0;
        *a = t * o->va + o->t0;
        return 1;
    }
    *py = fh * (i == 0 ? 0.3f : 0.7f);
    *a = (i == 0 ? 1.0f : -1.0f) * (t * o->va + o->t0);
    return 2;
}

int jet_mine(const jet_obj_t *o, float t, float fh, int col, int k, float *x, float *y)
{
    const float y0 = 16.0f + 50.0f * (float)k;
    if (y0 >= fh) return -1;
    if (fabsf(y0 - (col == 0 ? o->y0 : o->y1)) <= 70.0f) return 0; /* the window */
    *x = o->x + (col == 0 ? 15.0f : 95.0f);
    *y = y0 + sinf(t * 2.0f + y0) * 4.0f;
    return 1;
}

bool jet_star_at(const jet_obj_t *o, float t, float fh, float *x, float *y)
{
    *x = o->x + o->w / 2.0f;
    switch (o->kind) {
    case JET_PIPE:
    case JET_CRUSHER: *y = o->y0; return true;
    case JET_SLIDE: *y = jet_slide_y(o, t); return true;
    case JET_PILLAR: *y = o->from_top ? (o->y0 + fh) / 2.0f : (fh - o->y0) / 2.0f; return true;
    case JET_TUNNEL: *y = jet_tunnel_y(o, *x); return true;
    default: return false;
    }
}

static bool rect_hit(float cx, float cy, float r, float x, float y, float w, float h)
{
    const float nx = clampf(cx, x, x + w), ny = clampf(cy, y, y + h);
    return hypotf(cx - nx, cy - ny) < r;
}

static bool seg_hit(float cx, float cy, float r, float x1, float y1, float x2, float y2, float th)
{
    const float dx = x2 - x1, dy = y2 - y1, l = dx * dx + dy * dy;
    const float k = l > 0.0f ? clampf(((cx - x1) * dx + (cy - y1) * dy) / l, 0.0f, 1.0f) : 0.0f;
    return hypotf(cx - (x1 + dx * k), cy - (y1 + dy * k)) < r + th;
}

bool jet_hit(const jet_obj_t *o, float t, float fh, float cx, float cy, float r)
{
    switch (o->kind) {
    case JET_PIPE:
    case JET_SLIDE: {
        const float y = o->kind == JET_PIPE ? o->y0 : jet_slide_y(o, t);
        const float top = y - o->gap / 2.0f, bot = y + o->gap / 2.0f;
        return rect_hit(cx, cy, r, o->x, -100.0f, o->w, top + 100.0f) ||
               rect_hit(cx, cy, r, o->x, bot, o->w, fh - bot + 100.0f);
    }
    case JET_PILLAR:
        return o->from_top ? rect_hit(cx, cy, r, o->x, -100.0f, o->w, o->y0 + 100.0f)
                           : rect_hit(cx, cy, r, o->x, fh - o->y0, o->w, o->y0 + 100.0f);
    case JET_ROCK:
        /* The spike is a triangle: a narrower box stands in for it, a little forgiving. */
        return hypotf(cx - (o->x + 25.0f), cy - o->fy) < r + 16.0f ||
               rect_hit(cx, cy, r, o->x + 16.0f, fh - o->y0 * 0.8f, 18.0f, o->y0);
    case JET_TUNNEL: {
        const float xs[3] = {cx - r * 0.7f, cx, cx + r * 0.7f};
        for (int i = 0; i < 3; i++) {
            if (xs[i] < o->x || xs[i] > o->x + o->w) continue;
            const float y = jet_tunnel_y(o, xs[i]);
            if (cy - r * 0.8f < y - o->gap / 2.0f || cy + r * 0.8f > y + o->gap / 2.0f) return true;
        }
        return false;
    }
    case JET_BIRDS:
        for (int i = 0; i < 3; i++) {
            float x, y;
            jet_bird(o, t, i, &x, &y);
            if (hypotf(cx - x, cy - y) < r + 11.0f) return true;
        }
        return false;
    case JET_RISERS:
        for (int k = 0; k < 4; k++) {
            if (hypotf(cx - (o->x + 22.0f), cy - jet_riser_y(o, t, k, fh)) < r + 17.0f) return true;
        }
        return false;
    case JET_CRUSHER: {
        const float h = jet_crush_open(o, t) * JET_CRUSH_HALF;
        return rect_hit(cx, cy, r, o->x, -100.0f, o->w, o->y0 - h + 100.0f) ||
               rect_hit(cx, cy, r, o->x, o->y0 + h, o->w, fh - o->y0 - h + 100.0f);
    }
    case JET_MINES:
        for (int col = 0; col < 2; col++) {
            for (int k = 0;; k++) {
                float x, y;
                const int m = jet_mine(o, t, fh, col, k, &x, &y);
                if (m < 0) break;
                if (m > 0 && hypotf(cx - x, cy - y) < r + 12.0f) return true;
            }
        }
        return false;
    case JET_SPINNER:
    case JET_SPINNER2:
        for (int i = 0;; i++) {
            float px, py, a;
            const int n = jet_spin(o, t, fh, i, &px, &py, &a);
            const float dx = cosf(a) * o->len, dy = sinf(a) * o->len;
            if (seg_hit(cx, cy, r, px - dx, py - dy, px + dx, py + dy, 7.0f) ||
                hypotf(cx - px, cy - py) < r + 12.0f) {
                return true;
            }
            if (i + 1 >= n) return false;
        }
    case JET_LASER: {
        if (jet_laser_state(o, t) != JET_LASER_ON) return false;
        float a, b;
        jet_laser_span(o, fh, &a, &b);
        return rect_hit(cx, cy, r, o->x + 2.0f, a, 10.0f, b - a);
    }
    default:
        return false;
    }
}

static void start_world(jet_t *j)
{
    memset(j->obj, 0, sizeof(j->obj));
    j->gates = 0;
    j->spawned = 0;
    j->last = -1;
    j->clear = false;
    j->t = 0.0f;
    j->last_gap = 0.0f;
}

void jet_begin(jet_t *j, float fw, float fh)
{
    memset(j, 0, sizeof(*j));
    j->fw = fw;
    j->fh = fh;
    j->jx = JET_HOME_X;
    j->jy = fh / 2.0f;
    start_world(j);
}

void jet_retry(jet_t *j)
{
    start_world(j);
    j->jx = JET_HOME_X;
    j->jy = j->fh / 2.0f;
    j->vx = j->vy = 0.0f;
}

unsigned jet_step(jet_t *j, uint32_t *rng, float dt, float tx, float ty, float fw, float fh,
                  int *score)
{
    unsigned ev = 0;
    j->fw = fw;
    j->fh = fh;
    const float was = j->t;
    j->t += dt;
    if (was < JET_READY_S && j->t >= JET_READY_S) ev |= JET_EV_GO;

    j->vy += clampf(ty, -1.0f, 1.0f) * FLY_ACCEL * dt;
    j->vx += clampf(tx, -1.0f, 1.0f) * DRIFT_ACCEL * dt;
    j->vy *= expf(-FLY_FRICTION * dt);
    j->vx *= expf(-DRIFT_FRICTION * dt);
    j->jy += j->vy * dt;
    j->jx += j->vx * dt;
    if (j->jy < EDGE_TOP || j->jy > fh - EDGE_BOTTOM) {
        j->jy = clampf(j->jy, EDGE_TOP, fh - EDGE_BOTTOM);
        j->vy = 0.0f;
    }
    if (j->jx < DRIFT_MIN || j->jx > DRIFT_MAX) {
        j->jx = clampf(j->jx, DRIFT_MIN, DRIFT_MAX);
        j->vx = 0.0f;
    }

    /* A world speeds up a quarter over its first minute, so its last gates are its hardest. */
    const float v = world_speed(j->world) * (1.0f + fminf(0.25f, j->t * 0.004f));
    j->scroll += v * dt;
    for (int i = 0; i < JET_MAX_OBJ; i++) {
        jet_obj_t *o = &j->obj[i];
        if (!o->live) continue;
        o->x -= (v + o->extra) * dt;
        /* The rock lets go as he comes, from where he is: hanging back holds it. */
        if (o->kind == JET_ROCK && o->x < o->trig + (j->jx - JET_HOME_X) && o->fy < fh + 40.0f) {
            o->fv += 380.0f * dt;
            o->fy += o->fv * dt;
        }
    }

    /* THE NEXT ONE COMES A SET RUN BEHIND THE LAST ONE'S TAIL, so a long tunnel gets the same
       breathing room after it as a pipe. */
    const jet_obj_t *last = j->last >= 0 ? &j->obj[j->last] : NULL;
    if (j->t >= JET_READY_S && j->spawned < JET_GATES &&
        (last == NULL || !last->live || last->x + last->w < fw + 10.0f - world_space(j->world))) {
        for (int i = 0; i < JET_MAX_OBJ; i++) {
            if (j->obj[i].live) continue;
            make(j, &j->obj[i], pick_kind(j->world, j->spawned, rng), rng);
            j->last = i;
            j->spawned++;
            break;
        }
    }

    const float cx = j->jx, cy = j->jy - JET_HIT_DY;
    bool any = false;
    for (int i = 0; i < JET_MAX_OBJ; i++) {
        jet_obj_t *o = &j->obj[i];
        if (!o->live) continue;
        if (jet_hit(o, j->t, fh, cx, cy, JET_HIT_R)) return ev | JET_EV_HIT;
        float sx, sy;
        if (o->star && !o->got && jet_star_at(o, j->t, fh, &sx, &sy) &&
            hypotf(cx - sx, cy - sy) < JET_HIT_R + 14.0f) {
            o->got = true;
            (*score)++;
            ev |= JET_EV_STAR;
        }
        if (!o->passed && o->x + o->w < cx - JET_HIT_R) {
            o->passed = true;
            j->gates++;
            (*score)++;
            ev |= JET_EV_GATE;
            if (j->gates >= JET_GATES) j->clear = true;
        }
        if (o->x + o->w < -60.0f) o->live = false;
        any = any || o->live;
    }
    if (j->clear && !any) {
        j->world++;
        j->score_at_world = *score;
        start_world(j);
        ev |= JET_EV_WORLD;
    }
    return ev;
}
