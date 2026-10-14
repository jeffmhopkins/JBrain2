#pragma once

/* "I'M GOING TO TICKLE YOU" (0.4.11). The owner: *"let's change tickle, to 'I'm going to tickle
 * you' and then when that happens, pokes make him be tickled ... after five tickles."*
 *
 * So the phrase does not tickle him: it WARNS him. He braces, giggling nervously (ACT_BRACE),
 * and for a while after, a poke is a tickle rather than whatever the zone it lands in would
 * pick. The first four each get a giggle and a squirm; the fifth sets off the whole laughing
 * fit (ACT_TICKLE). Nobody poking for TICKLE_WAIT_MS and the moment has passed.
 *
 * Pure, and host-tested; `display.c` asks it what a poke is. */

#include <stdbool.h>
#include <stdint.h>

#define TICKLE_POKES 5          /* the fifth tickle is the one that gets him */
#define TICKLE_WAIT_MS 10000u   /* how long the warning holds, renewed by every tickle */

typedef enum {
    TICKLE_NO = 0, /* not warned: the poke is an ordinary poke */
    TICKLE_SMALL,  /* a tickle he is holding out against: a giggle */
    TICKLE_BIG,    /* the one that gets him: the laughing fit, and the warning is spent */
} tickle_poke_t;

typedef struct {
    bool armed;
    uint32_t until;
    int pokes;
} tickle_t;

void tickle_reset(tickle_t *t);

/* He has been warned, at `now`. A second warning starts the count again. */
void tickle_arm(tickle_t *t, uint32_t now);

/* A poke reached him at `now`: what it is. */
tickle_poke_t tickle_poke(tickle_t *t, uint32_t now);

/* Still waiting for the tickles at `now`. */
bool tickle_armed(const tickle_t *t, uint32_t now);

/* How hard the tickle just counted squirms (the action's `mag`): rising tickle by tickle, so
   the four build to the fifth instead of repeating. */
float tickle_mag(const tickle_t *t);

/* WAITING TO BE TICKLED: while warned and doing nothing else he holds the brace, hunched and
   shivering, so a child can see the game is on. This is the ACT_BRACE progress to draw at
   `now` — inside its held stretch, wandering so the shiver keeps going. */
float tickle_wait_p(uint32_t now);
