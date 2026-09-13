// The pet's body rig — limb poses per action, and the whole-figure transform.
//
// The owner chose **"small body"** over eyes-only and eyes+mouth
// (`docs/reference/DESIGN.md`). The reasoning matters for anyone editing this file: the
// *emotions* never needed a body — `face.ts` carries those — but the **gags do**. Dance,
// wave, jump and above all peekaboo need arms. So a body is only worth its pixels if the
// actions use it, which is what this module is.
//
// Pure, and in the same figure-space the panel will use, so the firmware port is a
// transcription rather than a redesign.

/** Degrees, 0 = hanging straight down; negative swings forward/up. `handsUp` 0-1 drives
 *  peekaboo, where the hands are aimed at the eyes rather than posed by angle (see `draw.ts`). */
export interface RigPose {
  armL: number;
  armR: number;
  legL: number;
  legR: number;
  lean: number;
  handsUp: number;
}

/** What the whole figure does: offset, scale, tilt, and any one-shot extra the renderer draws. */
export interface FigurePose {
  ox: number;
  oy: number;
  sx: number;
  sy: number;
  ang: number;
  extra: "" | "puff" | "blush";
}

export interface ActionSpec {
  /** Milliseconds the action runs for. */
  dur: number;
  /** The face it wears while running. Gags resolve to `bewildered`. */
  face: string;
  gag?: boolean;
}

/** Actions this surface can play. A superset of the server's canned scripts, because the
 *  variant pools (`variants.ts`) expand one poke into several distinguishable reactions. */
export const ACTIONS: Record<string, ActionSpec> = {
  wiggle: { dur: 1100, face: "happy" },
  giggle: { dur: 1300, face: "happy" },
  boing: { dur: 900, face: "excited" },
  blush: { dur: 1600, face: "happy" },
  sneeze: { dur: 1200, face: "bewildered" },
  hiccup: { dur: 900, face: "bewildered" },
  dance: { dur: 2600, face: "excited" },
  bop: { dur: 2200, face: "excited" },
  shimmy: { dur: 2400, face: "excited" },
  spin: { dur: 1400, face: "excited" },
  jump: { dur: 900, face: "excited" },
  wave: { dur: 1400, face: "happy" },
  nod: { dur: 900, face: "happy" },
  beep: { dur: 800, face: "silly" },
  sing: { dur: 2400, face: "happy" },
  hide: { dur: 2400, face: "silly" },
  fart: { dur: 1500, face: "bewildered", gag: true },
  burp: { dur: 1400, face: "bewildered", gag: true },
  sleep: { dur: 2000, face: "sleepy" },
  idle: { dur: 0, face: "happy" },
};

/** Limb pose for an action at progress `p` (0-1), scaled by `mag` (the repetition penalty —
 *  the tenth rapid poke moves less than the first). `t` drives the idle sway that runs
 *  underneath everything: a character that is ever perfectly still reads as dead. */
export function rigFor(action: string | null, p: number, mag: number, t: number): RigPose {
  const sway = Math.sin(t / 1400) * 4;
  const pose: RigPose = {
    armL: 12 + sway,
    armR: -12 - sway,
    legL: 4,
    legR: -4,
    lean: 0,
    handsUp: 0,
  };
  if (!action) return pose;
  switch (action) {
    case "wave":
      pose.armR = -150 + Math.sin(p * Math.PI * 6) * 26 * mag;
      break;
    case "dance":
    case "bop":
    case "shimmy": {
      const q = Math.sin(p * Math.PI * 6) * mag;
      pose.armL = 40 + q * 55;
      pose.armR = -40 + q * 55;
      pose.legL = 10 + q * 10;
      pose.legR = -10 + q * 10;
      break;
    }
    case "jump":
    case "boing": {
      const q = Math.sin(p * Math.PI);
      pose.armL = 120 * q;
      pose.armR = -120 * q;
      pose.legL = 30 * q;
      pose.legR = -30 * q;
      break;
    }
    case "wiggle":
    case "giggle": {
      const q = Math.sin(p * Math.PI * 9) * mag;
      pose.armL = 25 + q * 30;
      pose.armR = -25 + q * 30;
      break;
    }
    case "sing":
      pose.armL = 70;
      pose.armR = -70;
      break;
    case "sleep":
      pose.armL = 6;
      pose.armR = -6;
      pose.legL = 2;
      pose.legR = -2;
      break;
    case "fart":
      if (p > 0.12 && p < 0.72) {
        pose.armL = 95;
        pose.armR = -95;
        pose.lean = 8 * mag;
        pose.legL = 26;
      }
      break;
    case "burp":
      if (p > 0.12 && p < 0.6) {
        pose.armR = -160; // hand to the mouth
        pose.lean = -6 * mag;
      }
      break;
    case "sneeze":
    case "hiccup":
      if (p > 0.35 && p < 0.6) {
        pose.armL = 140;
        pose.armR = -140;
      }
      break;
    case "hide":
      // PEEKABOO, and the fix for a shipped dud: on the Wall `hide` renders identically to
      // `sit` — it walks to a corner and squats with nothing occluding it. With hands, hide
      // can actually hide, and peekaboo is squarely on-target for a four-year-old.
      pose.handsUp = p < 0.18 ? p / 0.18 : p < 0.72 ? 1 : Math.max(0, 1 - (p - 0.72) / 0.28);
      pose.armL = 168;
      pose.armR = -168;
      break;
  }
  return pose;
}

/** The whole-figure transform. Comic actions come from squashing and tilting the WHOLE
 *  character, not from new artwork — which is why the panel can afford them. */
export function figureFor(
  action: string | null,
  p: number,
  mag: number,
  t: number,
  faceTilt: number,
  lean: number,
): FigurePose {
  const breathe = Math.sin(t / 1400) * 0.018 + 1;
  const f: FigurePose = { ox: 0, oy: 0, sx: 1, sy: 1, ang: faceTilt + lean, extra: "" };
  const spec = action ? ACTIONS[action] : undefined;
  if (action && spec) {
    switch (action) {
      case "wiggle":
        f.ang += Math.sin(p * Math.PI * 8) * 11 * mag;
        break;
      case "giggle":
        f.oy += Math.abs(Math.sin(p * Math.PI * 7)) * -13 * mag;
        f.ang += Math.sin(p * Math.PI * 9) * 6 * mag;
        break;
      case "boing": {
        const q = Math.sin(p * Math.PI * 3);
        f.sy += q * 0.18 * mag;
        f.sx -= q * 0.11 * mag;
        f.oy -= Math.abs(q) * 18 * mag;
        break;
      }
      case "blush":
        f.extra = "blush";
        break;
      case "dance":
      case "bop":
      case "shimmy":
        f.ox += Math.sin(p * Math.PI * 6) * 22 * mag;
        f.ang += Math.sin(p * Math.PI * 6) * 9 * mag;
        break;
      case "spin":
        f.ang += p * 720;
        break;
      case "jump":
        // The lift is applied OUTSIDE the figure scale, so keep it modest or the tallest
        // silhouette (cat ears) leaves the top of a 448 px panel at the peak.
        f.oy -= Math.abs(Math.sin(p * Math.PI)) * 32 * mag;
        break;
      case "nod":
        f.oy += Math.sin(p * Math.PI * 5) * 12 * mag;
        break;
      case "sneeze":
      case "hiccup":
        if (p < 0.35) {
          f.sx += 0.05;
          f.sy -= 0.04;
        } else if (p < 0.5) {
          f.sx -= 0.17;
          f.sy += 0.21;
          f.oy -= 12;
        }
        break;
      case "hide":
        f.sy -= Math.min(0.16, p * 0.4);
        f.oy += Math.min(26, p * 70);
        break;
      case "sleep":
        f.sy -= Math.min(0.1, p * 0.26);
        f.oy += Math.min(16, p * 36);
        break;
    }
    if (spec.gag) {
      // The gag skeleton: anticipation → 2-4 frames of act → LONG HOLD → settle. The hold is
      // the joke; the act is only the setup, so it is short and the hold is half the duration.
      if (p < 0.12) {
        f.sx += 0.05;
        f.sy -= 0.04;
      } else if (p < 0.22) {
        f.sx -= 0.22 * mag;
        f.sy += 0.26 * mag;
        f.oy -= 9;
        f.extra = "puff";
      } else if (p < 0.72) {
        f.extra = "puff";
      }
    }
  }
  f.sx *= breathe;
  f.sy *= 2 - breathe;
  return f;
}
