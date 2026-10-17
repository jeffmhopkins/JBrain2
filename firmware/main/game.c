#include "game.h"

#include <math.h>
#include <string.h>

/* Full tilt is this much push in px/s^2; friction bleeds the speed off, so he skids to a stop
   rather than either stopping dead or sliding for ever. */
#define RUN_ACCEL 900.0f
#define RUN_FRICTION 4.0f
/* The room he has, from the field's edge to his origin: a little more above, where his head
   is, than below, where his feet are. */
#define EDGE_X 26.0f
#define EDGE_TOP 50.0f
#define EDGE_BOTTOM 34.0f
/* The hit circle sits a little above his origin, at the middle of the shrunken figure. */
#define HIT_DY 8.0f
#define OBJ_R 17.0f   /* a thing's radius at size 1, roughly: what touching him means */
#define SPAWN_M 30.0f /* how far outside the field they appear */

static float clampf(float v, float lo, float hi);

static uint32_t rnd(game_t *g)
{
    /* xorshift32: deterministic for a seed, so the host suite can replay a round. */
    uint32_t x = g->rng ? g->rng : 0x9E3779B9u;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    g->rng = x;
    return x;
}

static float rnd01(game_t *g)
{
    return (float)(rnd(g) >> 8) / 16777216.0f;
}

static float ease(float k)
{
    if (k <= 0.0f) return 0.0f;
    if (k >= 1.0f) return 1.0f;
    return k * k * (3.0f - 2.0f * k);
}

/* How small he is while playing: each game's own. */
static float play_scale(const game_t *g)
{
    return g->kind == GAME_JET ? JET_SCALE : GAME_SCALE;
}

void game_reset(game_t *g)
{
    memset(g, 0, sizeof(*g));
}

bool game_on(const game_t *g)
{
    return g->phase != GAME_OFF;
}

static void new_round(game_t *g, uint32_t now)
{
    memset(g->obj, 0, sizeof(g->obj));
    g->phase = GAME_PLAY;
    g->since = now;
    g->round_at = now;
    g->last_active = now;
    g->score = 0;
    g->vx = g->vy = 0.0f;
    g->spawn_in = (float)GAME_READY_MS / 1000.0f;
    if (g->kind == GAME_JET) {
        /* The world he had reached, from its start, at the score it started with. */
        jet_retry(&g->jet);
        g->score = g->jet.score_at_world;
    }
}

void game_start(game_t *g, uint32_t now, uint32_t seed, float home_x, float home_y)
{
    game_start_as(g, GAME_DODGE, now, seed, home_x, home_y);
}

void game_start_as(game_t *g, game_kind_t kind, uint32_t now, uint32_t seed, float home_x,
                   float home_y)
{
    g->rng = seed ? seed : 1u;
    if ((g->phase == GAME_PLAY || g->phase == GAME_OVER) && g->kind == kind) {
        new_round(g, now);
        return;
    }
    /* Asked again mid-walk: turn round where he is, at the size he is (review), rather than
       snapping back to his full size at home. */
    float fx = home_x, fy = home_y, fs = 1.0f;
    if (g->phase == GAME_WALK_IN || g->phase == GAME_WALK_OUT) game_figure(g, now, &fx, &fy, &fs);
    memset(g->obj, 0, sizeof(g->obj));
    g->kind = kind;
    g->phase = GAME_WALK_IN;
    g->since = now;
    /* Dodge's middle; Jetpack's column is set by the first step, which knows the field. */
    g->to_x = 0.0f;
    g->to_y = 20.0f;
    g->last_active = now;
    g->home_x = home_x;
    g->home_y = home_y;
    g->from_x = fx;
    g->from_y = fy;
    g->from_s = fs;
    g->x = 0.0f;
    g->y = 20.0f;
    g->vx = g->vy = 0.0f;
    g->score = 0;
}

void game_tap(game_t *g, uint32_t now)
{
    if (!game_on(g)) return;
    g->last_active = now;
    if (g->phase == GAME_OVER && now - g->since >= GAME_REPLAY_MS) new_round(g, now);
}

void game_stop(game_t *g, uint32_t now)
{
    if (g->phase == GAME_OFF || g->phase == GAME_WALK_OUT) return;
    g->out_s = play_scale(g);
    if (g->phase == GAME_WALK_IN) {
        /* Turned back halfway: walk out from where the walk in had got to, at the size it had
           reached (review), not snapped down to a fifth. */
        float x, y, s;
        game_figure(g, now, &x, &y, &s);
        g->x = x;
        g->y = y;
        g->out_s = s;
    }
    memset(g->obj, 0, sizeof(g->obj));
    g->phase = GAME_WALK_OUT;
    g->since = now;
}

bool game_deaf(const game_t *g)
{
    return g->phase == GAME_WALK_IN || g->phase == GAME_PLAY || g->phase == GAME_OVER;
}

void game_figure(const game_t *g, uint32_t now, float *x, float *y, float *scale)
{
    const float k = ease((float)(now - g->since) / (float)GAME_WALK_MS);
    switch (g->phase) {
    case GAME_WALK_IN:
        *x = g->from_x + (g->to_x - g->from_x) * k;
        *y = g->from_y + (g->to_y - g->from_y) * k;
        *scale = g->from_s + (play_scale(g) - g->from_s) * k;
        return;
    case GAME_WALK_OUT:
        *x = g->x + (g->home_x - g->x) * k;
        *y = g->y + (g->home_y - g->y) * k;
        *scale = g->out_s + (1.0f - g->out_s) * k;
        return;
    case GAME_PLAY:
    case GAME_OVER:
        *x = g->x;
        *y = g->y;
        *scale = play_scale(g);
        return;
    case GAME_OFF:
    default:
        *x = g->home_x;
        *y = g->home_y;
        *scale = 1.0f;
        return;
    }
}

float game_pitch(int az, int down)
{
    return atan2f((float)az, (float)down);
}

float game_tilt_y(float pitch, float pitch0)
{
    float d = pitch - pitch0;
    if (d > 3.14159265f) d -= 6.2831853f;
    if (d < -3.14159265f) d += 6.2831853f;
    return clampf(d / GAME_PITCH_FULL, -1.0f, 1.0f);
}

float game_play_s(const game_t *g, uint32_t now)
{
    /* Frozen at the hit once a round is over: the level it reached, not the time since. */
    if (g->phase == GAME_PLAY) return (float)(now - g->round_at) / 1000.0f;
    if (g->phase == GAME_OVER) return (float)(g->since - g->round_at) / 1000.0f;
    return 0.0f;
}

int game_level(const game_t *g, uint32_t now)
{
    if (g->kind == GAME_JET) return 1 + g->jet.world;
    return 1 + (int)(game_play_s(g, now) / 12.0f);
}

/* SLOW, THEN NOT. Speed and the rate they come both climb with the time survived: gentle for
   a four-year-old's first ten seconds, a real test by the end of a minute. The climb is 0.4.14's
   own, from a slower start: today's first speed comes about eight seconds in. */
#define SPEED_CLIMB 5.5f /* px/s gained per second survived */
#define SPEED_MAX 300.0f

static float speed_from(float start, float t)
{
    const float v = start + t * SPEED_CLIMB;
    return v < SPEED_MAX ? v : SPEED_MAX;
}

static float spawn_speed(float t)
{
    return speed_from(GAME_START_SPEED, t);
}

/* AS MANY ON THE SCREEN AS 0.4.15 HAD. How many are in flight is how often they come times how
   long each takes to cross, so slowing them to a third and leaving the gap alone tripled the
   crowd (owner, 0.4.17: "the spawn rate actually didn't decrease, so it ended up having a lot
   more on screen"). The gap is 0.4.15's, stretched by how much slower they are now than then:
   three times as long at the start, the two meeting as the speeds converge at the cap. */
static float spawn_gap(float t)
{
    float s = 1.5f - t * 0.03f;
    if (s < 0.32f) s = 0.32f;
    return s * speed_from(GAME_OLD_START_SPEED, t) / spawn_speed(t);
}

static void spawn(game_t *g, float t, float hw, float hh)
{
    game_obj_t *o = NULL;
    for (int i = 0; i < GAME_MAX_OBJ; i++) {
        if (!g->obj[i].live) {
            o = &g->obj[i];
            break;
        }
    }
    if (o == NULL) return;
    const float speed = spawn_speed(t) * (0.85f + 0.3f * rnd01(g));
    float x, y;
    switch (rnd(g) % 4u) {
    case 0:
        x = -hw - SPAWN_M;
        y = (rnd01(g) * 2.0f - 1.0f) * hh;
        break;
    case 1:
        x = hw + SPAWN_M;
        y = (rnd01(g) * 2.0f - 1.0f) * hh;
        break;
    case 2:
        x = (rnd01(g) * 2.0f - 1.0f) * hw;
        y = -hh - SPAWN_M;
        break;
    default:
        x = (rnd01(g) * 2.0f - 1.0f) * hw;
        y = hh + SPAWN_M;
        break;
    }
    /* Most come at him, give or take; some just cross. */
    const bool aim = rnd01(g) < 0.65f;
    const float tx = aim ? g->x + (rnd01(g) - 0.5f) * 80.0f : (rnd01(g) * 2.0f - 1.0f) * hw;
    const float ty = aim ? g->y + (rnd01(g) - 0.5f) * 80.0f : (rnd01(g) * 2.0f - 1.0f) * hh;
    float d = hypotf(tx - x, ty - y);
    if (d < 1.0f) d = 1.0f;
    memset(o, 0, sizeof(*o));
    o->live = true;
    o->kind = (uint8_t)(rnd(g) % GAME_KINDS);
    o->x = x;
    o->y = y;
    o->vx = (tx - x) / d * speed;
    o->vy = (ty - y) / d * speed;
    o->a = rnd01(g) * 6.2831853f;
    o->va = (rnd01(g) - 0.5f) * 6.0f;
    o->size = 0.9f + 0.35f * rnd01(g);
}

static float clampf(float v, float lo, float hi)
{
    return v < lo ? lo : v > hi ? hi : v;
}

unsigned game_step(game_t *g, uint32_t now, uint32_t dt_ms, float tx, float ty, float hw,
                   float hh)
{
    unsigned ev = 0;
    if (g->phase == GAME_OFF) return 0;
    if (dt_ms > 100u) dt_ms = 100u; /* a stalled frame is not a teleport */
    const float dt = (float)dt_ms / 1000.0f;
    tx = clampf(tx, -1.0f, 1.0f);
    ty = clampf(ty, -1.0f, 1.0f);
    if (fabsf(tx) > GAME_ACTIVE_TILT || fabsf(ty) > GAME_ACTIVE_TILT) g->last_active = now;

    if (g->phase == GAME_WALK_IN) {
        if (g->kind == GAME_JET) {
            g->to_x = JET_HOME_X - hw;
            g->to_y = 0.0f;
        }
        if (now - g->since >= GAME_WALK_MS) {
            if (g->kind == GAME_JET) jet_begin(&g->jet, hw * 2.0f, hh * 2.0f);
            new_round(g, now);
            g->x = g->to_x;
            g->y = g->to_y;
        }
        return 0;
    }
    if (g->phase == GAME_WALK_OUT) {
        if (now - g->since >= GAME_WALK_MS) {
            g->phase = GAME_OFF;
            return GAME_EV_DONE;
        }
        return 0;
    }
    if (now - g->last_active >= GAME_IDLE_MS) {
        game_stop(g, now);
        return 0;
    }
    /* THE SCORE STANDS FOR A TAP, and only so long: a held tilt keeps the idle clock above
       alive, so the round over is timed from the hit (owner: "a timeout to exit the game when
       not clicked"). */
    if (g->phase == GAME_OVER) {
        if (now - g->since >= GAME_OVER_WAIT_MS) game_stop(g, now);
        return 0;
    }

    if (g->kind == GAME_JET) {
        /* UP IS THE TOP TIPPED AWAY, as in Dodge below: a marble's way. */
        const unsigned je = jet_step(&g->jet, &g->rng, dt, tx, -ty, hw * 2.0f, hh * 2.0f, &g->score);
        g->x = g->jet.jx - hw;
        g->y = g->jet.jy - hh;
        if (je & JET_EV_GO) ev |= GAME_EV_GO;
        if (je & (JET_EV_GATE | JET_EV_STAR)) ev |= GAME_EV_DODGE;
        if (je & JET_EV_WORLD) ev |= GAME_EV_WORLD;
        if (je & JET_EV_HIT) {
            g->phase = GAME_OVER;
            g->since = now;
            if (g->score > g->best) g->best = g->score;
            ev |= GAME_EV_HIT;
        }
        return ev;
    }

    /* PLAY. */
    const float t = (float)(now - g->since) / 1000.0f;
    const float was_t = t - dt;
    const float ready = (float)GAME_READY_MS / 1000.0f;
    if (was_t < ready && t >= ready) ev |= GAME_EV_GO;

    /* LIKE A MARBLE, NOT A BUBBLE (owner: *"tilt should be opposite of bubble. Like a marble
       (both games)"*): he runs toward whichever edge goes down. The side tilt already did; the
       top tipped away lowers the top edge, so it runs him up the glass, where it ran him down. */
    g->vx += tx * RUN_ACCEL * dt;
    g->vy -= ty * RUN_ACCEL * dt;
    const float drag = expf(-RUN_FRICTION * dt);
    g->vx *= drag;
    g->vy *= drag;
    g->x += g->vx * dt;
    g->y += g->vy * dt;
    if (g->x < -hw + EDGE_X || g->x > hw - EDGE_X) {
        g->x = clampf(g->x, -hw + EDGE_X, hw - EDGE_X);
        g->vx = 0.0f;
    }
    if (g->y < -hh + EDGE_TOP || g->y > hh - EDGE_BOTTOM) {
        g->y = clampf(g->y, -hh + EDGE_TOP, hh - EDGE_BOTTOM);
        g->vy = 0.0f;
    }

    if (t >= ready) {
        g->spawn_in -= dt;
        if (g->spawn_in <= 0.0f) {
            spawn(g, t, hw, hh);
            g->spawn_in = spawn_gap(t) * (0.7f + 0.6f * rnd01(g));
        }
    }

    const float cx = g->x, cy = g->y - HIT_DY;
    for (int i = 0; i < GAME_MAX_OBJ; i++) {
        game_obj_t *o = &g->obj[i];
        if (!o->live) continue;
        o->x += o->vx * dt;
        o->y += o->vy * dt;
        o->a += o->va * dt;
        const float r = OBJ_R * o->size;
        const bool inside = fabsf(o->x) < hw + 10.0f && fabsf(o->y) < hh + 10.0f;
        if (inside) o->in = true;
        if (hypotf(o->x - cx, o->y - cy) < GAME_HIT_R + r * 0.8f) {
            g->phase = GAME_OVER;
            g->since = now;
            if (g->score > g->best) g->best = g->score;
            /* Leave the things where they are: what hit him stays in the picture. */
            return ev | GAME_EV_HIT;
        }
        if (o->in && !inside) {
            o->live = false;
            g->score++;
            ev |= GAME_EV_DODGE;
        } else if (!o->in && (fabsf(o->x) > hw * 3.0f || fabsf(o->y) > hh * 3.0f)) {
            o->live = false; /* missed the field entirely: no point, no harm */
        }
    }
    return ev;
}
