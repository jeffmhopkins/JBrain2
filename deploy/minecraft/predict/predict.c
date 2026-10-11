// jbrain-predict: a world's surface predicted from its seed alone (plan §M8, "satellite").
//
// Since 1.18 Bedrock places biomes and terrain from the same noise as Java for the same
// seed, so cubiomes (Java's generator, reimplemented) predicts ground nobody has been to.
// Heights are approximate (a few blocks, more under trees); biomes match the real world
// but for edge jitter. The map draws the real chunks over this wherever they exist.
//
//   jbrain-predict SEED DIM X0 Z0 N STEP
//
// writes N×N little-endian int16 pairs (height above the dimension floor, biome id),
// row by row, sampling every STEP blocks from block (X0, Z0). Biome ids are cubiomes'
// (Java's); the caller translates them. Only the Overworld has heights: the Nether and
// the End report a fixed one (the Nether's is its bedrock roof, what a real map shows).
#include "generator.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

static int floordiv4(int v) { return v >> 2; }

// The biome AT the surface: biomes are 3D since 1.18, and the height map's own ids come
// from below it (a cave biome under a beach, checked against a real 1.26 world).
static int surface_biome(const Generator *g, int x, int y, int z) {
    return getBiomeAt(g, 4, x >> 2, (y - 1) >> 2, z >> 2);
}

static void put(int16_t h, int16_t id) {
    int16_t out[2] = {h, id};
    fwrite(out, sizeof out[0], 2, stdout);
}

int main(int argc, char **argv) {
    if (argc != 7) {
        fprintf(stderr, "usage: jbrain-predict SEED DIM X0 Z0 N STEP\n");
        return 2;
    }
    uint64_t seed = (uint64_t)strtoll(argv[1], NULL, 10);
    int dim = atoi(argv[2]), x0 = atoi(argv[3]), z0 = atoi(argv[4]);
    int n = atoi(argv[5]), step = atoi(argv[6]);
    if (n <= 0 || n > 1024 || step <= 0 || step > 64 || (dim != 0 && dim != 1 && dim != 2))
        return 2;
    int cdim = dim == 0 ? DIM_OVERWORLD : dim == 1 ? DIM_NETHER : DIM_END;

    Generator g;
    setupGenerator(&g, MC_1_21, 0);
    applySeed(&g, cdim, seed);

    if (cdim != DIM_OVERWORLD) {
        for (int j = 0; j < n; j++)
            for (int i = 0; i < n; i++)
                put(128, getBiomeAt(&g, 4, floordiv4(x0 + i * step), 16,
                                   floordiv4(z0 + j * step)));
        return 0;
    }

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
