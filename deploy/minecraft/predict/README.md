# predict — `jbrain-predict`, the map's "satellite" layer

A small C program that predicts a world's surface from its **seed alone**. The map uses it
to draw ground nobody has explored yet: the owner's "satellite from orbit". Real chunks
are drawn over it wherever they exist (`../mapping.py`).

```
jbrain-predict SEED DIM X0 Z0 N STEP
```

It writes N × N little-endian `int16` pairs (height above the dimension's floor, biome id),
row by row, sampling every STEP blocks from block (X0, Z0). `DIM` is 0 for the Overworld,
1 for the Nether and 2 for the End. `mapping.Predictor` runs it and translates the output.

## How it predicts

- **Overworld:** since 1.18, Bedrock generates biomes and terrain from the same noise as
  Java for the same seed, so [cubiomes](https://github.com/Cubitect/cubiomes) (a
  reimplementation of Java's generator) predicts it. Biomes match except for jitter at
  their edges. Heights are within a few blocks (more under trees). The ids are Java's;
  `mapping.py` translates them.
- **Nether and End:** Bedrock seeds these differently from Java (a Mersenne Twister on the
  seed's low 32 bits), so the seeding is ported from Reed A. Cartwright's Bedrock fork
  ([reedacartwright/cubiomes](https://github.com/reedacartwright/cubiomes), branch
  `bedrock`), driving upstream cubiomes' noise. Both emit Bedrock ids directly. The Nether
  gives biome only, with its height as the bedrock roof (128). The End gives biome 9 and
  its real height, or `END_VOID` (-1) where there is no ground.

Structures are not predicted here: Bedrock places them differently from Java. The map's
Structures overlay asks the running server instead (`../structure_sweep.py`).

## Building

The image builds it in its own stage (`deploy/Dockerfile.minecraft`) against cubiomes at a
pinned `CUBIOMES_COMMIT`, so the runtime image gets the binary and no compiler. At build
time the image checks it against spots a real 1.26 server generated from a known seed. A
prediction that drifted fails the build.

On a workstation, `scripts/dev-setup.sh` builds it into `~/.cache/jbrain/jbrain-predict`
(rebuilt whenever `predict.c` or the pin changes). Point `MC_PREDICT_BIN` at it to render
real predicted tiles. The tests use a fake instead, so they don't need it.

**The cubiomes commit is pinned in two places**, the Dockerfile and `dev-setup.sh`, and
they must match.

## Licence

cubiomes is MIT (Cubitect). The Bedrock fork's LICENSE is identical to it, byte for byte,
so the image's `licenses/cubiomes.LICENSE` covers both.
