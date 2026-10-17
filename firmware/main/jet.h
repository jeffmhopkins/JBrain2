#pragma once

/* JETPACK (0.4.18): the games page's second game. The owner: *"Same thing as the tilt game, the
 * robot goes small, and instead of flapping, use the tilt to control, but side scroll through
 * obstacles. There should be different levels and obstacle types"*, then, against the first
 * mock, *"make sure obstacles change (these were all basically the same gap)"*. Agreed against
 * a playable mock (artifact 1iQJmXLfdpUsjvC7kaXFTN) before any of this was written.
 *
 * He flies at the left of the field while the obstacles scroll in from the right. Gravity pulls
 * him down and the tilt is a BURN against it: the top tipped away climbs, a little tip holds
 * him, let go he sinks, toward you dives. Not Dodge's push (`game.h`), which the owner found
 * felt the same. The field's top and bottom edges are safe: the floor is a landing. The
 * side tilt lets him drift forward and back a little, which is how a crusher or a laser is
 * waited out. Ten worlds of JET_GATES obstacles each, every world bringing its own kind: its
 * first three are that kind alone, then earlier worlds' kinds mix in. Past the tenth every kind
 * mixes, faster and tighter each time. One bump ends the round; a tap retries the world he had
 * reached, with the score it started at.
 *
 * Pure, and host-tested. Coordinates are field pixels from its top-left (`fw` by `fh`), which
 * `game.c` turns into the centred ones the figure is placed by; `face.c` draws it. */

#include <stdbool.h>
#include <stdint.h>

#define JET_WORLDS 10
#define JET_GATES 8          /* obstacles per world */
#define JET_MAX_OBJ 6        /* a tunnel is long: room for it and what follows */
#define JET_SCALE 0.19f      /* a touch smaller than Dodge's fifth: gaps want the room */
#define JET_HOME_X 96.0f     /* his column, from the field's left */
#define JET_HIT_R 15.0f      /* his hit circle */
#define JET_HIT_DY 7.6f      /* which sits this far above his origin: the middle of the figure */
#define JET_READY_S 1.2f     /* a world's name stands this long before its first obstacle */

typedef enum {
    JET_PIPE = 0, /* a pair with a gap that jumps high and low from one to the next */
    JET_PILLAR,   /* a tower from the ground or the sky: over or under */
    JET_ROCK,     /* a rock that drops from the roof as he nears, over a spike in the floor */
    JET_SLIDE,    /* candy canes whose gap slides up and down */
    JET_TUNNEL,   /* a long winding corridor */
    JET_BIRDS,    /* three in a V, faster than the scroll, bobbing */
    JET_RISERS,   /* balloons floating up a column */
    JET_CRUSHER,  /* jaws that slam shut, wait, and open */
    JET_MINES,    /* two columns of mines, each with a window at a different height */
    JET_SPINNER,  /* one long bar turning */
    JET_SPINNER2, /* two short ones, turning opposite ways */
    JET_LASER,    /* a beam that is off, flickers, then on */
    JET_KINDS
} jet_kind_t;

/* A laser's beam: the whole height, or the top or bottom three fifths. */
enum { JET_SPAN_FULL = 0, JET_SPAN_TOP, JET_SPAN_BOTTOM };
/* A laser's moment. */
enum { JET_LASER_OFF = 0, JET_LASER_WARN, JET_LASER_ON };

typedef struct {
    float x, w;    /* left edge and width, px */
    float gap;     /* the opening, px, where there is one */
    float y0, y1;  /* the kind's heights: a gap's centre, a tower's length, a window, a path */
    float amp;     /* a slide's or a tunnel's swing */
    float rate;    /* a slide's speed (rad/s), a riser's (px/s), a tunnel's waves */
    float len, va; /* a spinner's half-length and turn rate */
    float fy, fv;  /* a falling rock's height and speed */
    float trig;    /* how far from his home column the rock lets go */
    float t0;      /* its own phase, so two of a kind are not in step */
    float extra;   /* px/s faster than the scroll (birds) */
    uint8_t kind;  /* jet_kind_t */
    uint8_t span;  /* a laser's */
    bool live;
    bool passed;   /* gone by him: scored */
    bool star;     /* carries a star in its opening */
    bool got;      /* and he has it */
    bool from_top; /* a tower's */
} jet_obj_t;

typedef struct {
    int world;          /* 0.. ; JET_WORLDS and on are the mixes */
    int gates;          /* gone by in this world */
    int spawned;        /* sent in, in this world */
    int last;           /* the last sent in's slot */
    int score_at_world; /* what a retry starts from */
    bool clear;         /* all JET_GATES sent and gone by: the next world when the field empties */
    float t;            /* seconds into this world: its banner, its speed-up, every phase */
    float jx, jy;       /* his origin */
    float vx, vy;
    float burn;         /* how hard the jetpack burns, 0..1: the flame */
    float fw, fh;       /* the field, as the last step had it */
    float scroll;       /* px scrolled, for the backdrop's parallax */
    float last_gap;     /* the last pipe's gap centre, so the next one is somewhere else */
    jet_obj_t obj[JET_MAX_OBJ];
} jet_t;

/* What happened in a step, as bits. */
enum {
    JET_EV_GO = 1,    /* a world's banner is over: here they come */
    JET_EV_GATE = 2,  /* one went by: a point */
    JET_EV_STAR = 4,  /* a star caught: a point */
    JET_EV_HIT = 8,   /* one got him */
    JET_EV_WORLD = 16 /* a world cleared: the next begins */
};

/* World one, score nothing, him at home mid-field. */
void jet_begin(jet_t *j, float fw, float fh);
/* The world he reached, from its start, at the score it started with. */
void jet_retry(jet_t *j);
/* One frame of play. `tx`, `ty` the tilt, -1..1 (forward, down). Adds to `*score`. */
unsigned jet_step(jet_t *j, uint32_t *rng, float dt, float tx, float ty, float fw, float fh,
                  int *score);

/* For the drawing: where the moving parts are at world time `t`, in field px. */
const char *jet_world_name(int world);
int jet_backdrop(int world);                       /* 0..JET_WORLDS-1, or JET_WORLDS: the mix */
float jet_slide_y(const jet_obj_t *o, float t);
float jet_tunnel_y(const jet_obj_t *o, float x);
void jet_bird(const jet_obj_t *o, float t, int i, float *x, float *y); /* i 0..2 */
float jet_riser_y(const jet_obj_t *o, float t, int k, float fh);      /* k 0..3 */
float jet_crush_open(const jet_obj_t *o, float t);                    /* 0 shut .. 1 open */
#define JET_CRUSH_HALF 95.0f /* half the crusher's opening, fully open */
int jet_laser_state(const jet_obj_t *o, float t);
void jet_laser_span(const jet_obj_t *o, float fh, float *a, float *b);
int jet_spin(const jet_obj_t *o, float t, float fh, int i, float *px, float *py, float *a); /* count */
/* The `k`th mine slot from the top of column `col` (0 or 1): 1 a mine, at (x, y); 0 the window;
   -1 past the last. */
int jet_mine(const jet_obj_t *o, float t, float fh, int col, int k, float *x, float *y);
bool jet_star_at(const jet_obj_t *o, float t, float fh, float *x, float *y);
bool jet_hit(const jet_obj_t *o, float t, float fh, float cx, float cy, float r);
