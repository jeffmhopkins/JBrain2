// The pet's procedural face — the six shipped `EMOTIONS` as lid geometry.
//
// **Why this is a module and not markup.** The Wall renders emotion as a chest-badge colour
// plus the width of one horizontal mouth bar (`deploy/wall/pet.html`), so `curious` and
// `sleepy` are pixel-identical apart from hue, `happy` and `excited` differ by 0.06 of a bar,
// and in any *creature* form emotion is invisible entirely. Emotion is therefore net-new work,
// settled in `docs/reference/DESIGN.md` → "Room endpoint — the robot pet's body": it is carried
// by **lid geometry, whole-face motion and timing, never colour**.
//
// Everything here is pure and framework-free ON PURPOSE. The ESP32-S3 panel will run the same
// model in C, so this file is the reference implementation: ~17 tweened floats, after
// Cozmo/Vector's 43-float `ProceduralFace`. Ekman's set is reachable from lid **y** and lid
// **angle** alone; **asymmetry between the two eyes** is the entire signal for curious and silly.

/** One eye's geometry. `uy`/`ly` are lid coverage 0-1; `ua` is the upper lid's tilt in degrees;
 *  `lb` bends the lower lid into the Duchenne cheek-raise that makes "happy" read. */
export interface EyeParams {
  uy: number;
  ua: number;
  ly: number;
  lb: number;
  sx: number;
  sy: number;
}

export type MouthShape = "smile" | "open" | "small" | "flat" | "tongue" | "o";

export interface FaceParams {
  l: EyeParams;
  r: EyeParams;
  /** Whole-face transform contribution: vertical scale and tilt. */
  face: { sy: number; ang: number };
  /** Transition-speed multiplier. The SAME shape at a different speed reads as a different
   *  feeling — the cheapest expressive channel there is, so it is part of the emotion. */
  rate: number;
  mouth: MouthShape;
}

/** The six emotions the server actually ships (`jpet/service.py:EMOTIONS`). */
export const EMOTIONS = ["happy", "excited", "curious", "sleepy", "silly", "scared"] as const;
export type Emotion = (typeof EMOTIONS)[number];

/** Not in `EMOTIONS` — the gag face. 4-5 year olds read a pratfall as funny when the character
 *  looks **bewildered**, and as *not* funny when it looks pained or smug, so every gag resolves
 *  to this and holds it. The hold is the punchline. */
export type FaceKey = Emotion | "bewildered";

const MIRROR = Symbol("mirror");
type Spec = {
  l: EyeParams;
  r: EyeParams | typeof MIRROR;
  face: { sy: number; ang: number };
  rate: number;
  mouth: MouthShape;
};

// Values follow pycozmo's shipped `Expressive Eyes` set, mapped onto our six.
const SPECS: Record<FaceKey, Spec> = {
  happy: {
    l: { uy: 0, ua: 0, ly: 0.4, lb: 0.4, sx: 1, sy: 1 },
    r: MIRROR,
    face: { sy: 1.0, ang: 0 },
    rate: 1.0,
    mouth: "smile",
  },
  excited: {
    l: { uy: 0, ua: 0, ly: 0.3, lb: 0.2, sx: 1.1, sy: 1.15 },
    r: MIRROR,
    face: { sy: 1.05, ang: 0 },
    rate: 1.7,
    mouth: "open",
  },
  // Asymmetric on purpose: one eye half-lidded, one wide, plus a head tilt. Cozmo encodes
  // confusion and skepticism the same way, and it costs nothing.
  curious: {
    l: { uy: 0.3, ua: -10, ly: 0.2, lb: 0.2, sx: 1, sy: 1 },
    r: { uy: 0.05, ua: 12, ly: 0, lb: 0, sx: 1.08, sy: 1.08 },
    face: { sy: 1, ang: -7 },
    rate: 0.8,
    mouth: "small",
  },
  sleepy: {
    l: { uy: 0.48, ua: 4, ly: 0.45, lb: 0, sx: 1, sy: 0.9 },
    r: MIRROR,
    face: { sy: 0.95, ang: 0 },
    rate: 0.35,
    mouth: "flat",
  },
  silly: {
    l: { uy: 0.1, ua: 0, ly: 0.1, lb: 0.3, sx: 1.25, sy: 1.25 },
    r: { uy: 0.42, ua: -14, ly: 0.05, lb: 0, sx: 0.8, sy: 0.75 },
    face: { sy: 1, ang: 9 },
    rate: 1.5,
    mouth: "tongue",
  },
  scared: {
    l: { uy: 0, ua: 26, ly: 0.35, lb: 0.1, sx: 1.2, sy: 1.3 },
    r: MIRROR,
    face: { sy: 1.02, ang: 0 },
    rate: 1.9,
    mouth: "o",
  },
  bewildered: {
    l: { uy: 0.05, ua: -16, ly: 0, lb: 0, sx: 1.3, sy: 1.35 },
    r: { uy: 0.3, ua: 18, ly: 0.15, lb: 0.15, sx: 0.95, sy: 1.1 },
    face: { sy: 1, ang: -11 },
    rate: 1.4,
    mouth: "o",
  },
};

/** True when the server handed us a face we know how to draw. Server enums drift; a screen
 *  that throws on an unknown emotion would blank the pet, so callers fall back to `happy`. */
export function isFaceKey(v: string | null | undefined): v is FaceKey {
  return v != null && v in SPECS;
}

/** Resolve an emotion to drawable parameters. Mirroring is applied here rather than stored, so
 *  a symmetric emotion is one edit and an asymmetric one is explicit about both eyes. */
export function resolveFace(key: FaceKey): FaceParams {
  const spec = SPECS[key];
  const r = spec.r === MIRROR ? { ...spec.l, ua: -spec.l.ua } : { ...spec.r };
  return { l: { ...spec.l }, r, face: { ...spec.face }, rate: spec.rate, mouth: spec.mouth };
}

/** Tween by halving: `cur = (cur + target) / 2` each frame. ~90% of the way in 66 ms at 50 fps
 *  with a natural ease-out, in one add and one shift — this is what makes a pose system read as
 *  an animation system, and it is cheap enough for the panel. */
export function approach(cur: number, target: number, rate: number): number {
  const k = Math.min(1, 0.5 * rate);
  return cur + (target - cur) * k;
}
