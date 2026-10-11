// jbrain-predict: a world's surface predicted from its seed alone (plan §M8, "satellite").
//
//   jbrain-predict SEED DIM X0 Z0 N STEP
//
// writes N×N little-endian int16 pairs (height above the dimension floor, biome id),
// row by row, sampling every STEP blocks from block (X0, Z0).
//
// Overworld: since 1.18 Bedrock places biomes and terrain from the same noise as Java for
// the same seed, so cubiomes (Java's generator, reimplemented) predicts it. Heights are
// approximate (a few blocks, more under trees); biomes match the real world but for edge
// jitter. Its ids are cubiomes' (Java's); the caller translates them.
//
// Nether and End: Bedrock seeds their noise differently from Java (a Mersenne Twister on
// the seed's low 32 bits, not java.util.Random), so cubiomes' Java answers are wrong
// there. The seeding below is ported from Reed A. Cartwright's Bedrock fork of cubiomes
// (github.com/reedacartwright/cubiomes, branch `bedrock`, MIT, the same licence text as
// cubiomes), driving upstream cubiomes' own noise, voronoi and End terrain code. Both emit
// Bedrock ids directly:
// - Nether: biome only; the height is the bedrock roof (128), which is what a real map of
//   the Nether shows.
// - End: biome 9 and the real terrain height; END_VOID (-1) where no terrain is, which
//   the caller draws as nothing.
//
// The map draws the real chunks over all of this wherever they exist.
#include "generator.h"

#include <math.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

enum { NETHER_ROOF = 128, END_BIOME = 9, END_VOID = -1 };

static int floordiv4(int v) { return v >> 2; }

static int floordiv8(int v) { return v >> 3; }

static void put(int16_t h, int16_t id) {
    int16_t out[2] = {h, id};
    fwrite(out, sizeof out[0], 2, stdout);
}

// --- Overworld -------------------------------------------------------------------------

// The biome AT the surface: biomes are 3D since 1.18, and the height map's own ids come
// from below it (a cave biome under a beach, checked against a real 1.26 world).
static int surface_biome(const Generator *g, int x, int y, int z) {
    return getBiomeAt(g, 4, x >> 2, (y - 1) >> 2, z >> 2);
}

static int overworld(uint64_t seed, int x0, int z0, int n, int step) {
    Generator g;
    setupGenerator(&g, MC_1_21, 0);
    applySeed(&g, DIM_OVERWORLD, seed);
    SurfaceNoise sn;
    initSurfaceNoise(&sn, DIM_OVERWORLD, seed);
    if (step > 4) {
        for (int j = 0; j < n; j++)
            for (int i = 0; i < n; i++) {
                int x = x0 + i * step, z = z0 + j * step;
                float y;
                mapApproxHeight(&y, NULL, &g, &sn, floordiv4(x), floordiv4(z), 1, 1);
                put((int16_t)(y + 64), (int16_t)surface_biome(&g, x, (int)y, z));
            }
        return 0;
    }
    // At or finer than the 4-block grid heights come on: compute that grid once (one cell of
    // margin) and interpolate, so close-up relief is smooth rather than 4-block steps.
    int qx = floordiv4(x0), qz = floordiv4(z0);
    int qw = floordiv4(x0 + (n - 1) * step) - qx + 2, qh = floordiv4(z0 + (n - 1) * step) - qz + 2;
    float *ys = malloc(sizeof(float) * qw * qh);
    int *ids = malloc(sizeof(int) * qw * qh);
    if (!ys || !ids || mapApproxHeight(ys, NULL, &g, &sn, qx, qz, qw, qh))
        return 1;
    for (int cz = 0; cz < qh; cz++)
        for (int cx = 0; cx < qw; cx++)
            ids[cz * qw + cx] =
                surface_biome(&g, (qx + cx) * 4, (int)ys[cz * qw + cx], (qz + cz) * 4);
    for (int j = 0; j < n; j++)
        for (int i = 0; i < n; i++) {
            int bx = x0 + i * step - qx * 4, bz = z0 + j * step - qz * 4;
            int cx = bx / 4, cz = bz / 4;
            float fx = (bx % 4) / 4.0f, fz = (bz % 4) / 4.0f;
            float a = ys[cz * qw + cx], b = ys[cz * qw + cx + 1];
            float c = ys[(cz + 1) * qw + cx], d = ys[(cz + 1) * qw + cx + 1];
            float y = (a * (1 - fx) + b * fx) * (1 - fz) + (c * (1 - fx) + d * fx) * fz;
            put((int16_t)(y + 64), (int16_t)ids[cz * qw + cx]);
        }
    free(ys);
    free(ids);
    return 0;
}

// --- Bedrock's noise seeding (Nether and End) ------------------------------------------

typedef struct {
    uint32_t s[624];
    int i;
} Mt;

static void mt_seed(Mt *m, uint32_t seed) {
    m->s[0] = seed;
    for (int k = 1; k < 624; k++)
        m->s[k] = 1812433253u * (m->s[k - 1] ^ (m->s[k - 1] >> 30)) + (uint32_t)k;
    m->i = 624;
}

static uint32_t mt_next(Mt *m) {
    if (m->i >= 624) {
        for (int k = 0; k < 624; k++) {
            uint32_t y = (m->s[k] & 0x80000000u) | (m->s[(k + 1) % 624] & 0x7fffffffu);
            m->s[k] = m->s[(k + 397) % 624] ^ (y >> 1) ^ ((y & 1) ? 0x9908b0dfu : 0);
        }
        m->i = 0;
    }
    uint32_t y = m->s[m->i++];
    y ^= y >> 11;
    y ^= (y << 7) & 0x9d2c5680u;
    y ^= (y << 15) & 0xefc60000u;
    return y ^ (y >> 18);
}

// Bedrock draws each perlin's offsets as floats: the rounding is what makes a prediction
// match it rather than drift away from it.
static double mt_float(Mt *m) { return (double)(float)(mt_next(m) / 4294967296.0); }

static void mt_perlin(Mt *m, PerlinNoise *p) {
    p->a = mt_float(m) * 256.0;
    p->b = mt_float(m) * 256.0;
    p->c = mt_float(m) * 256.0;
    p->amplitude = p->lacunarity = 1.0;
    for (int k = 0; k < 256; k++)
        p->d[k] = (uint8_t)k;
    for (int k = 0; k < 256; k++) {
        int j = (int)(mt_next(m) % (uint32_t)(256 - k)) + k;
        uint8_t t = p->d[k];
        p->d[k] = p->d[j];
        p->d[j] = t;
    }
    p->d[256] = p->d[0];
    double i2 = floor(p->b), d2 = p->b - i2;
    p->h2 = (uint8_t)(int)i2;
    p->d2 = d2;
    p->t2 = d2 * d2 * d2 * (d2 * (d2 * 6 - 15) + 10);
}

// Unlike Java, Bedrock initialises only the octaves it uses: no skipping ahead for the
// absent ones.
static void mt_octaves(Mt *m, OctaveNoise *o, PerlinNoise *oct, int omin, int len) {
    double amp = 1.0 / ((1LL << len) - 1.0), lac = pow(2.0, omin + len - 1);
    for (int k = 0; k < len; k++, amp *= 2, lac *= 0.5) {
        mt_perlin(m, &oct[k]);
        oct[k].amplitude = amp;
        oct[k].lacunarity = lac;
    }
    o->octaves = oct;
    o->octcnt = len;
}

// --- Nether ----------------------------------------------------------------------------

typedef struct {
    DoublePerlinNoise temp, humid;
    PerlinNoise oct[8];
    Layer voronoi;
} Nether;

static void nether_seed(Nether *nn, uint64_t seed) {
    Mt m;
    // (10/6)·(len+1)/(len+2) for two octaves, where Java's is len/(len+1).
    const double amp = (10.0 / 6.0) * 3.0 / 4.0;
    mt_seed(&m, (uint32_t)seed);
    nn->temp.amplitude = amp;
    mt_octaves(&m, &nn->temp.octA, nn->oct + 0, -7, 2);
    mt_octaves(&m, &nn->temp.octB, nn->oct + 2, -7, 2);
    mt_seed(&m, (uint32_t)(seed + 1));
    nn->humid.amplitude = amp;
    mt_octaves(&m, &nn->humid.octA, nn->oct + 4, -7, 2);
    mt_octaves(&m, &nn->humid.octB, nn->oct + 6, -7, 2);
    // The block-scale jitter is seeded by the FULL 64-bit seed (low 32 bits alone: 99.5%
    // of columns right instead of 99.99%).
    memset(&nn->voronoi, 0, sizeof nn->voronoi);
    nn->voronoi.layerSalt = getLayerSalt(10);
    setLayerSeed(&nn->voronoi, seed);
}

// Bedrock's Nether is 2D: the biome of a 4×4 cell is read at y = 0.
static int nether_cell(const Nether *nn, int qx, int qz) {
    static const float points[5][4] = {
        {0, 0, 0, 8},                  // nether_wastes
        {0, -0.5f, 0, 178},            // soulsand_valley
        {0.4f, 0, 0, 179},             // crimson_forest
        {0, 0.5f, 0.375f * 0.375f, 180},  // warped_forest
        {-0.5f, 0, 0.175f * 0.175f, 181}, // basalt_deltas
    };
    float t = (float)sampleDoublePerlin(&nn->temp, qx, 0, qz);
    float h = (float)sampleDoublePerlin(&nn->humid, qx, 0, qz);
    int best = 0;
    float dmin = 0;
    for (int k = 0; k < 5; k++) {
        float dx = points[k][0] - t, dy = points[k][1] - h;
        float d = dx * dx + dy * dy + points[k][2];
        if (k == 0 || d < dmin) {
            dmin = d;
            best = k;
        }
    }
    return (int)points[best][3];
}

// A w×h block area at (x, z), through the 1.14 voronoi, into the start of `out`, which
// first holds the cell grid and so needs room for both.
static int nether_area(const Nether *nn, int *out, int x, int z, int w, int h) {
    int vx = x - 2, vz = z - 2, px = vx >> 2, pz = vz >> 2;
    int pw = ((vx + w) >> 2) - px + 2, ph = ((vz + h) >> 2) - pz + 2;
    for (int j = 0; j < ph; j++)
        for (int i = 0; i < pw; i++)
            out[j * pw + i] = nether_cell(nn, px + i, pz + j);
    return mapVoronoi114(&nn->voronoi, out, x, z, w, h);
}

static int nether(uint64_t seed, int x0, int z0, int n, int step) {
    Nether nn;
    nether_seed(&nn, seed);
    if (step == 1) {
        int cells = (n / 4 + 3) * (n / 4 + 3);
        int *out = malloc(sizeof(int) * (cells + n * n));
        if (!out || nether_area(&nn, out, x0, z0, n, n))
            return 1;
        for (int k = 0; k < n * n; k++)
            put(NETHER_ROOF, (int16_t)out[k]);
        free(out);
        return 0;
    }
    // Sparse samples each need only the few cells around them.
    int out[16];
    for (int j = 0; j < n; j++)
        for (int i = 0; i < n; i++) {
            if (nether_area(&nn, out, x0 + i * step, z0 + j * step, 1, 1))
                return 1;
            put(NETHER_ROOF, (int16_t)out[0]);
        }
    return 0;
}

// --- End -------------------------------------------------------------------------------

// Bedrock's island noise comes off the same twister as the terrain noise, after the
// terrain's 40 perlins (16+16+8 octaves) at 259 draws each: 3 offsets and 256 shuffle
// swaps. Java's End skips 17,292 (66 × 262). Found by testing against a real 1.26 world
// (plan §M8); the fork's Java-style 17,292 predicts no better than chance.
enum { END_ISLAND_SKIP = 10360 };
// Terrain is solid only between noise cells 2 and 18 (y 8 to 72): cubiomes'
// mapEndSurfaceHeight scans the same band.
enum { END_Y0 = 2, END_Y1 = 18, END_YN = END_Y1 - END_Y0 + 1 };

typedef struct {
    EndNoise en;
    SurfaceNoise sn;
    // Outer-island sizes on the island grid (2 terrain cells per step): 0 for none.
    uint8_t *isle;
    int ix, iz, iw, ih;
    // The noise column of each 8-block terrain cell a tile reads, NULL for the rest.
    const double **col;
    int cx, cz, cw, ch;
} End;

static void end_seed(End *e, uint64_t seed) {
    Mt m;
    mt_seed(&m, (uint32_t)seed);
    for (int k = 0; k < END_ISLAND_SKIP; k++)
        mt_next(&m);
    mt_perlin(&m, &e->en.perlin);
    e->en.mc = MC_1_21;
    mt_seed(&m, (uint32_t)seed);
    mt_octaves(&m, &e->sn.octmin, e->sn.oct + 0, -15, 16);
    mt_octaves(&m, &e->sn.octmax, e->sn.oct + 16, -15, 16);
    mt_octaves(&m, &e->sn.octmain, e->sn.oct + 32, -7, 8);
    e->sn.xzScale = 2.0;
    e->sn.yScale = 1.0;
    e->sn.xzFactor = 80.0;
    e->sn.yFactor = 160.0;
}

// cubiomes' getEndHeightNoise evaluates 625 simplex samples per column, but neighbouring
// columns share almost all of them; a tile's columns read them from one grid instead.
static int end_isle_grid(End *e) {
    int range = 12;
    int hx0 = e->cx / 2 - range - 1, hz0 = e->cz / 2 - range - 1;
    int hx1 = (e->cx + e->cw) / 2 + range + 1, hz1 = (e->cz + e->ch) / 2 + range + 1;
    e->ix = hx0, e->iz = hz0, e->iw = hx1 - hx0 + 1, e->ih = hz1 - hz0 + 1;
    e->isle = calloc((size_t)e->iw * e->ih, 1);
    if (!e->isle)
        return 1;
    for (int j = 0; j < e->ih; j++)
        for (int i = 0; i < e->iw; i++) {
            int64_t rx = hx0 + i, rz = hz0 + j;
            if (rx * rx + rz * rz > 4096 && sampleSimplex2D(&e->en.perlin, rx, rz) < -0.9f)
                e->isle[j * e->iw + i] = (uint8_t)(
                    (unsigned)(fabsf((float)rx) * 3439.0f + fabsf((float)rz) * 147.0f) % 13 + 9);
        }
    return 0;
}

// getEndHeightNoise over the cached grid, operation for operation.
static float end_height_noise(const End *e, int x, int z) {
    int hx = x / 2, hz = z / 2, oddx = x % 2, oddz = z % 2;
    int64_t h = 64 * ((int64_t)x * x + (int64_t)z * z);
    for (int j = -12; j <= 12; j++)
        for (int i = -12; i <= 12; i++) {
            int64_t v = e->isle[(hz + j - e->iz) * e->iw + (hx + i - e->ix)];
            if (v) {
                int64_t rx = oddx - i * 2, rz = oddz - j * 2;
                int64_t noise = (rx * rx + rz * rz) * v * v;
                if (noise < h)
                    h = noise;
            }
        }
    float ret = 100 - sqrtf((float)h);
    return ret < -100 ? -100 : ret > 80 ? 80 : ret;
}

// sampleNoiseColumnEnd's column over the solid band, for terrain cell (x, z).
static void end_column(const End *e, double *c, int x, int z) {
    uint64_t rsq = (uint64_t)x * x + (uint64_t)z * z;
    if ((int)rsq < 0) { // Java-derived far-out ring of void, kept as cubiomes has it
        for (int y = 0; y < END_YN; y++)
            c[y] = -1.0;
        return;
    }
    double depth = end_height_noise(e, x, z) - 8.0f;
    for (int y = END_Y0; y <= END_Y1; y++) {
        double upper = (32 + 46 - y) / 64.0, lower = (y - 1) / 7.0;
        upper = upper > 1 ? 1 : upper;
        lower = lower > 1 ? 1 : lower < 0 ? 0 : lower;
        double v = sampleSurfaceNoiseBetween(&e->sn, x, y, z, -128, +128) + depth;
        v = lerp(upper, -3000, v);
        c[y - END_Y0] = lerp(lower, -30, v);
    }
}

// The columns a sample at block (x, z) interpolates between: one on a cell corner, which
// every sample of the wide zooms is, else up to four.
static void end_corners(const End *e, int x, int z, int *at) {
    int i = floordiv8(x) - e->cx, j = floordiv8(z) - e->cz;
    int dx = (x & 7) != 0, dz = (z & 7) != 0;
    at[0] = j * e->cw + i;
    at[1] = at[0] + dx;
    at[2] = at[0] + dz * e->cw;
    at[3] = at[2] + dx;
}

// getSurfaceHeight: the top solid block, or END_VOID.
static int end_surface(const End *e, int x, int z) {
    int at[4];
    end_corners(e, x, z, at);
    const double *c00 = e->col[at[0]], *c10 = e->col[at[1]];
    const double *c01 = e->col[at[2]], *c11 = e->col[at[3]];
    double dx = (x & 7) / 8.0, dz = (z & 7) / 8.0;
    for (int cy = END_Y1 - 1; cy >= END_Y0; cy--) {
        int k = cy - END_Y0;
        for (int y = 3; y >= 0; y--) {
            double v = lerp3(y / 4.0, dx, dz, c00[k], c00[k + 1], c10[k], c10[k + 1], c01[k],
                             c01[k + 1], c11[k], c11[k + 1]);
            if (v > 0)
                return cy * 4 + y;
        }
    }
    return END_VOID;
}

// Each column is ~40 perlin samples per cell of height, so a wide-zoom tile's 65k of
// them are seconds of work: split them across the cores.
typedef struct {
    const End *e;
    const int *cells;
    double *store;
    int count, from, every;
} EndWork;

static void *end_worker(void *arg) {
    const EndWork *w = arg;
    for (int k = w->from; k < w->count; k += w->every) {
        int i = w->cells[k] % w->e->cw, j = w->cells[k] / w->e->cw;
        end_column(w->e, w->store + (size_t)k * END_YN, w->e->cx + i, w->e->cz + j);
    }
    return NULL;
}

static int end_area(uint64_t seed, int x0, int z0, int n, int step) {
    static End e;
    end_seed(&e, seed);
    e.cx = floordiv8(x0), e.cz = floordiv8(z0);
    e.cw = floordiv8(x0 + (n - 1) * step) - e.cx + 2;
    e.ch = floordiv8(z0 + (n - 1) * step) - e.cz + 2;
    size_t cells = (size_t)e.cw * e.ch;
    e.col = calloc(cells, sizeof *e.col);
    uint8_t *need = calloc(cells, 1);
    int *list = malloc(sizeof(int) * cells);
    if (!e.col || !need || !list || end_isle_grid(&e))
        return 1;
    for (int j = 0; j < n; j++)
        for (int i = 0; i < n; i++) {
            int at[4];
            end_corners(&e, x0 + i * step, z0 + j * step, at);
            for (int k = 0; k < 4; k++)
                need[at[k]] = 1;
        }
    int count = 0;
    for (size_t k = 0; k < cells; k++)
        if (need[k])
            list[count++] = (int)k;
    double *store = malloc(sizeof(double) * END_YN * (size_t)count);
    if (!store)
        return 1;
    long cores = sysconf(_SC_NPROCESSORS_ONLN);
    int threads = cores < 1 ? 1 : cores > 16 ? 16 : (int)cores;
    pthread_t tid[16];
    EndWork work[16];
    for (int t = 0; t < threads; t++) {
        work[t] = (EndWork){&e, list, store, count, t, threads};
        if (pthread_create(&tid[t], NULL, end_worker, &work[t]))
            return 1;
    }
    for (int t = 0; t < threads; t++)
        pthread_join(tid[t], NULL);
    for (int k = 0; k < count; k++)
        e.col[list[k]] = store + (size_t)k * END_YN;
    for (int j = 0; j < n; j++)
        for (int i = 0; i < n; i++) {
            int y = end_surface(&e, x0 + i * step, z0 + j * step);
            // Real chunks record the first air above the top block; match them.
            put((int16_t)(y == END_VOID ? END_VOID : y + 1), END_BIOME);
        }
    return 0; // the process exits: the memory goes with it
}

int main(int argc, char **argv) {
    if (argc != 7) {
        fprintf(stderr, "usage: jbrain-predict SEED DIM X0 Z0 N STEP\n");
        return 2;
    }
    uint64_t seed = (uint64_t)strtoll(argv[1], NULL, 10);
    int dim = atoi(argv[2]), x0 = atoi(argv[3]), z0 = atoi(argv[4]);
    int n = atoi(argv[5]), step = atoi(argv[6]);
    if (n <= 0 || n > 1024 || step <= 0 || step > 64)
        return 2;
    switch (dim) {
    case 0:
        return overworld(seed, x0, z0, n, step);
    case 1:
        return nether(seed, x0, z0, n, step);
    case 2:
        return end_area(seed, x0, z0, n, step);
    default:
        return 2;
    }
}
