#pragma once

/* WHAT GOES IN MUST COME OUT (0.4.11). The owner: *"If he eats three times and within 1 minute,
 * he should have to poop afterwards. The poop should stay on the screen for say 20 seconds. And
 * should slide around with the angle of the device."*
 *
 * Pure, and host-tested: the meal count, when one is owed, and the poop itself — a thing on the
 * floor that slides downhill. Not the pet's lean, which is a position the tilt picks; this has
 * momentum, friction and walls, so it skids and stops, and stays put on a level table.
 * `display.c` feeds it the tilt and draws `x`. */

#include <stdbool.h>
#include <stdint.h>

#define POOP_MEALS 3            /* this many meals... */
#define POOP_WINDOW_MS 60000u   /* ...inside this long, and one is owed */
#define POOP_SHOW_MS 20000u     /* how long it stays on the floor */
#define POOP_POP_MS 220u        /* it plops in rather than appearing */
#define POOP_SHRINK_MS 600u     /* and shrinks away rather than vanishing */
/* The floor it slides on, either side of centre, and where it lands behind him, in figure units
   (which the panel's 368 square on its side scales down with everything else). Here rather than
   in `display.c` so the bench uses the same numbers. */
#define POOP_HALF 150.0f
#define POOP_BEHIND 110.0f

/* EIGHT POOPS (0.4.12, owner: *"make eight variations of poops also. Maybe we can vary
   graphic, that poops expression, and the amount of stink, and maybe it has flies or not"*).
   A kind is a row of `POOP_LOOKS` (`poop.c`); the next is never the last (`poop_drop`). */
#define POOP_KINDS 8

typedef enum {
    POOP_SWIRL,   /* the three-tier swirl every child draws */
    POOP_TOWER,   /* four tiers, showing off */
    POOP_BLOB,    /* a flat puddle with a lump */
    POOP_PELLETS, /* a pile of little balls */
    POOP_LOG,     /* one long one */
    POOP_SHAPE_COUNT,
} poop_shape_t;

typedef enum {
    POOP_SMILE,
    POOP_COOL,    /* sunglasses */
    POOP_SLEEPY,
    POOP_SHOCKED,
    POOP_TONGUE,
    POOP_GRUMPY,
    POOP_QUEASY,  /* worried, with a sweat drop */
    POOP_WINK,
    POOP_FACE_COUNT,
} poop_face_t;

typedef struct {
    uint8_t shape;  /* poop_shape_t */
    uint8_t face;   /* poop_face_t */
    uint8_t stink;  /* 0 none .. 3 rank: how many lines rise, and how fast and high */
    uint8_t flies;  /* 0 .. 3 buzzing round it */
    uint8_t r, g, b;
} poop_look_t;

const poop_look_t *poop_look(int kind);

typedef struct {
    uint32_t meals[POOP_MEALS]; /* when the last few meals started, oldest first */
    int n;
    bool owed;                  /* three meals in a minute: the next quiet moment is for this */
    bool on;                    /* on the floor */
    uint32_t born;
    float x;                    /* figure units from the floor's centre */
    float v;                    /* figure units a second */
    int kind;                   /* which of POOP_KINDS this one is */
} poop_t;

void poop_reset(poop_t *p);

/* A meal started at `now`. True when it makes one owed (POOP_MEALS inside the window); the
   count starts again from there, so a fourth meal does not owe a second. */
bool poop_ate(poop_t *p, uint32_t now);

/* Put one on the floor at `x`, at rest, for POOP_SHOW_MS. Pays off what was owed. Behind a pet
   leaning far over can be past the floor's end (`half`), so it lands ON the end instead. `rnd`
   picks its kind, never the one before. */
void poop_drop(poop_t *p, uint32_t now, float x, float half, uint32_t rnd);

/* One step of the slide. `g` is the tilt, -1 (left side down) .. 1 (right side down); the floor
   runs from `-half` to `half`. Clears `on` once its time is up. */
void poop_step(poop_t *p, uint32_t now, float g, uint32_t dt_ms, float half);

/* Its size now, 0..1: popping in, then whole, then shrinking away. 0 when not on the floor. */
float poop_scale(const poop_t *p, uint32_t now);
