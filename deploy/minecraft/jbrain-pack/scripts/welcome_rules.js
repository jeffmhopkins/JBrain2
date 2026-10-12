// Dave's welcome and the first-time story beats (docs/plans/MINECRAFT_BEDROCK_PLAN.md
// §DW1; canon: Bleepbloop_City notes/lore.md, "Dave, the crew and the Tricorder"), the
// parts free of @minecraft/* so node can test them; welcome.js does the playing.
import { LINES } from "./rules.js";

// Each beat is once per player per world; the world records which they have had.
export const BEAT = {
  welcome: "welcome",
  firstDeath: "first_death",
  firstEnderPearl: "first_ender_pearl",
  firstPowerPack: "first_power_pack",
  firstWormhole: "first_wormhole",
  firstReplicator: "first_replicator",
};

// Dave speaks every 3 s, the lore table's pace, so a line can be read before the next.
export const DAVE_EVERY = 60;
const DAVE_FROM = 160;

// The lore page's table, in ticks (20 a second). Only the gamertag and the crash-site
// pair change between players and worlds.
export const welcomeTimeline = (name, wreck) => [
  { at: 0, step: "dark" },
  { at: 50, step: "sparkles" },
  { at: 90, step: "whatsThis" },
  { at: 100, step: "tricorder" },
  { at: 120, step: "signal" },
  ...LINES.welcome(name, wreck).map((text, i) => ({ at: DAVE_FROM + i * DAVE_EVERY, step: "dave", text })),
];

// `/dave hello`: the welcome's Dave lines alone, at the welcome's pace, from now.
export const helloTimeline = (name, wreck) =>
  LINES.welcome(name, wreck).map((text, i) => ({ at: i * DAVE_EVERY, step: "dave", text }));

// Exactly "hello": anything longer is a real question and goes to Dave's backend.
export const isHello = (text) => text.trim().toLowerCase() === "hello";

// A player's recorded beats, from the world property; anything unreadable is none yet.
export const parseBeats = (raw) => {
  if (typeof raw !== "string") return [];
  try {
    const list = JSON.parse(raw);
    return Array.isArray(list) ? list.filter((b) => typeof b === "string") : [];
  } catch {
    return [];
  }
};

// The first-death lines. When a Power Pack saved their things, they are not where the
// player fell, so Dave never says they are.
export const firstDeathLines = (savedByPack) =>
  savedByPack ? LINES.firstDeath.slice(0, 1) : [...LINES.firstDeath];

// The first-Power-Pack lines a world has built: the keep-your-things field only with the
// keep switch on, saved places as transport beacons only with the transport switch on.
// None at all leaves the beat unheard, for a later pack once a switch is on.
export const powerPackLines = ({ keep, transport }) => [
  ...(keep ? [LINES.firstPowerPack.keep] : []),
  ...(transport ? [LINES.firstPowerPack.transport] : []),
];

// The wrapper's crash-site flag from `scriptevent jb:config {json}`: {wreck} when it
// says, {} when it doesn't (a world never told is a no-wreck world).
export const parseWreck = (message) => {
  const raw = JSON.parse(message);
  if (typeof raw !== "object" || raw === null) throw new Error("not an object");
  return typeof raw.wreck === "boolean" ? { wreck: raw.wreck } : {};
};
