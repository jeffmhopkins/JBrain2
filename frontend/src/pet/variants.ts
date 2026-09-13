// The anti-boredom engine: weighted-random variant pools, per-variant cooldowns, and a
// repetition penalty.
//
// **Why this exists at all.** Loona ships 700-1000 expressions and reviewers still describe it
// going on a shelf, so variety is not a content problem — it is an engine problem. Vector layers
// five independent anti-repetition systems; three of them are cheap and are implemented here.
//
// **Where the boundary sits.** The *action* is server-authoritative — `jpet/` decides that a poke
// means "wiggle". Only the *variant* is chosen here, because which of several wiggles you get is
// presentation, not pet state. This surface never invents what the pet did.
//
// **The age-specific tension, deliberately resolved.** Preschoolers love repetition; they will
// ask for the burp a hundred times. So recency is suppressed WITHIN a gag — you get a different
// burp — and never applied to the gag itself. Nothing here can refuse an action.

export interface Variant {
  /** The action key the renderer plays (`rig.ts:ACTIONS`). */
  v: string;
  /** Relative selection weight before cooldowns are applied. */
  w: number;
  /** Milliseconds after this variant plays before it may be chosen again. */
  cd: number;
}

/** One entry per thing the child can trigger repeatedly. A pool of one is fine and is how an
 *  action opts out of variation. */
export const POOLS: Record<string, Variant[]> = {
  poke: [
    { v: "wiggle", w: 3, cd: 2500 },
    { v: "giggle", w: 3, cd: 2500 },
    { v: "boing", w: 2, cd: 4000 },
    { v: "blush", w: 1, cd: 9000 },
    { v: "sneeze", w: 1, cd: 20000 },
    // Rare on purpose. A child who sees something once in three weeks talks about it for a
    // month; Vector ships calendar-gated animations at very low weight for the same reason.
    { v: "hiccup", w: 0.4, cd: 45000 },
  ],
  fart: [
    { v: "fart", w: 3, cd: 0 },
    { v: "sneeze", w: 1, cd: 12000 },
  ],
  dance: [
    { v: "bop", w: 3, cd: 2000 },
    { v: "shimmy", w: 2, cd: 4000 },
    { v: "dance", w: 1, cd: 7000 },
  ],
};

/** Mutable selection memory. Kept outside the pool tables so the tables stay constant data the
 *  firmware can put in flash. */
export interface PoolMemory {
  /** `${pool}:${variant}` → the ms timestamp it last played. */
  lastPlayed: Record<string, number>;
  /** `pool` → when it last fired, for the repetition penalty. */
  lastFired: Record<string, number>;
  /** `pool` → how many times in the current rapid burst. */
  streak: Record<string, number>;
}

export function newMemory(): PoolMemory {
  return { lastPlayed: {}, lastFired: {}, streak: {} };
}

/** Choose a variant, honouring cooldowns. `rand` is injected so selection is testable and the
 *  firmware can use its own PRNG.
 *
 *  **It can never fail.** If every variant is cooling down, the full pool is used rather than
 *  returning nothing — a child who pokes the pet must always get a reaction, and a silent
 *  no-op is the one outcome that reads as "broken". */
export function pickVariant(
  pool: string,
  mem: PoolMemory,
  now: number,
  rand: () => number = Math.random,
): string {
  const opts = POOLS[pool];
  if (!opts || opts.length === 0) return pool;
  const live = opts.filter(
    (o) => now - (mem.lastPlayed[`${pool}:${o.v}`] ?? Number.NEGATIVE_INFINITY) > o.cd,
  );
  const from = live.length > 0 ? live : opts;
  const total = from.reduce((a, o) => a + o.w, 0);
  let r = rand() * total;
  for (const o of from) {
    r -= o.w;
    if (r <= 0) {
      mem.lastPlayed[`${pool}:${o.v}`] = now;
      return o.v;
    }
  }
  // Unreachable while every weight is positive; kept so a zero-weight table still answers.
  const last = from[from.length - 1] ?? opts[0];
  if (!last) return pool;
  mem.lastPlayed[`${pool}:${last.v}`] = now;
  return last.v;
}

/** How long a burst has to be quiet before the penalty resets. */
export const PENALTY_RESET_MS = 12_000;
/** The floor: a heavily-repeated action still moves, it just moves less. Never zero — a
 *  motionless response is indistinguishable from a broken one. */
export const PENALTY_FLOOR = 0.35;

/** Magnitude multiplier for this firing (Vector's `repetitionPenalty`). The tenth identical
 *  poke in a burst lands softer than the first; leave it alone for 12 s and it is fresh again. */
export function repetitionPenalty(pool: string, mem: PoolMemory, now: number): number {
  const gap = now - (mem.lastFired[pool] ?? Number.NEGATIVE_INFINITY);
  mem.lastFired[pool] = now;
  if (gap > PENALTY_RESET_MS) {
    mem.streak[pool] = 0;
    return 1;
  }
  mem.streak[pool] = (mem.streak[pool] ?? 0) + 1;
  return Math.max(PENALTY_FLOOR, 0.82 ** mem.streak[pool]);
}
