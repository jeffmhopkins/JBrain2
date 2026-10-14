#pragma once

/* "LET'S PLAY A GAME" (0.4.14). The owner: *"in game mode, it uses the accelerometer to tilt
 * the robot left and right, forwards and backwards, and scrolls objects from random directions.
 * The robot is 1/4 to 1/5 normal size, starts center screen (he walks there while getting
 * smaller) and when tilted runs as different objects come from different directions ... start
 * of slow and go faster, giving a score for each one dodged."* Agreed against a playable mock
 * (artifact HoUATxevCfmDDiJdHZKM5A) before any of this was written.
 *
 * Tilt is ACCELERATION, not position: he runs downhill and skids a little, so a held tilt is a
 * run and a quick one is a hop. Things fly in from all four edges, spinning, most aimed at where
 * he is and some just crossing, so standing still is neither safe nor certain death. Each one
 * that leaves the field without touching him scores; one touching him ends the round, and a tap
 * starts the next. No tap within GAME_OVER_WAIT_MS of the hit, or a minute with nobody tilting
 * or tapping, and he walks back and grows.
 *
 * Pure, and host-tested. Coordinates are pixels from the centre of the visible field (the whole
 * frame upright, the square on its side); `display.c` feeds it the tilt and draws it (`face.c`). */

#include <stdbool.h>
#include <stdint.h>

#define GAME_KINDS 12          /* twelve things to dodge (`face.c` draws them) */
#define GAME_MAX_OBJ 14
#define GAME_SCALE 0.21f       /* about a fifth of his size */
#define GAME_WALK_MS 1600u     /* the walk to the middle, shrinking; and back, growing */
#define GAME_READY_MS 1200u    /* "ready... go!" before the first one comes */
#define GAME_REPLAY_MS 600u    /* a tap this soon after the hit is the same tap, not a replay */
#define GAME_IDLE_MS 60000u    /* nobody tilting or tapping this long and the game ends */
#define GAME_OVER_WAIT_MS 15000u /* the score stands this long for a tap, then the game ends */
#define GAME_HIT_R 24.0f       /* his hit circle, px: a little forgiving, as a child wants */
#define GAME_ACTIVE_TILT 0.15f /* tilt past this is somebody playing */

typedef enum {
    GAME_OFF = 0,
    GAME_WALK_IN,  /* walking to the middle, shrinking */
    GAME_PLAY,     /* ready, then go */
    GAME_OVER,     /* hit: dazed, the score up; a tap plays again */
    GAME_WALK_OUT, /* walking back, growing */
} game_phase_t;

/* What happened in a step, as bits: for sounds. */
enum {
    GAME_EV_GO = 1,    /* the ready is over: here they come */
    GAME_EV_DODGE = 2, /* one went by: a point */
    GAME_EV_HIT = 4,   /* one got him */
    GAME_EV_DONE = 8,  /* back to full size: the game is over */
};

typedef struct {
    float x, y;   /* centre */
    float vx, vy; /* px/s */
    float a, va;  /* rotation and spin, radians and radians/s */
    float size;   /* 0.9..1.25 of the kind's own size */
    uint8_t kind; /* 0..GAME_KINDS-1 */
    bool in;      /* has been inside the field, so leaving it is a dodge */
    bool live;
} game_obj_t;

typedef struct {
    game_phase_t phase;
    uint32_t since;       /* when this phase began */
    uint32_t last_active; /* the last tilt or tap */
    uint32_t round_at;    /* when this round's play began */
    uint32_t rng;
    float x, y;           /* his position: the figure's origin */
    float vx, vy;
    float home_x, home_y; /* where he stood, to walk back to */
    float from_x, from_y; /* where the walk in started */
    float from_s;         /* and at what size: 1, or less when asked again on the way out */
    int score, best;
    float spawn_in;       /* seconds to the next one */
    game_obj_t obj[GAME_MAX_OBJ];
} game_t;

void game_reset(game_t *g);
bool game_on(const game_t *g);

/* Start from where he stands (`home`, field px). From OFF it walks in; mid-game it restarts. */
void game_start(game_t *g, uint32_t now, uint32_t seed, float home_x, float home_y);

/* One frame. `tx`, `ty` the tilt, -1..1 (right, toward the viewer's bottom); `hw`, `hh` the
   field's half extents. Returns GAME_EV_* bits. */
unsigned game_step(game_t *g, uint32_t now, uint32_t dt_ms, float tx, float ty, float hw,
                   float hh);

/* A tap on the panel: the next round once one is over. */
void game_tap(game_t *g, uint32_t now);

/* Walk back and grow (the wake word, a minute idle, leaving the pet screen). */
void game_stop(game_t *g, uint32_t now);

/* Where to draw him and how big (1 = his own size). */
void game_figure(const game_t *g, uint32_t now, float *x, float *y, float *scale);

/* FORWARD AND BACK, AS AN ANGLE. `az` across the glass and `down` along it (`orient_axes`):
   the panel's lean back from upright, in radians. The game steers by its change since the
   start (`game_tilt_y`), so the same tip of the wrist is the same push however far back the
   panel is held, where the raw `az` fades to nothing as it lies flat (review). */
float game_pitch(int az, int down);
/* The forward/back steer, -1..1: full at GAME_PITCH_FULL past where it started. */
#define GAME_PITCH_FULL 0.30f /* radians, about 17 degrees: the lean's own full tilt */
float game_tilt_y(float pitch, float pitch0);

/* Seconds into the round, and the level it has reached (1 + one per 12 s). */
float game_play_s(const game_t *g, uint32_t now);
int game_level(const game_t *g, uint32_t now);
