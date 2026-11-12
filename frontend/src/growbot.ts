// The words GrowBot's surfaces share — the screen, its sheets, the launcher tile and the Ops
// row — so a robot or a persona reads the same wherever the owner meets it (binding mocks
// docs/mocks/growbot-flash/b-growbot-screen.html and docs/mocks/growbot-personas/b-three-tabs.html,
// copy from their shared.js).

import type {
  GrowbotFace,
  GrowbotFailCode,
  GrowbotFlashJob,
  GrowbotMemory,
  GrowbotMemoryKind,
  GrowbotOverview,
  GrowbotPersona,
  GrowbotPhone,
  GrowbotRegion,
  GrowbotRobot,
  GrowbotSoul,
  GrowbotStatus,
} from "./api/client";

export type GrowbotLevel = "ok" | "warn" | "bad";

export function ago(iso: string, now = Date.now()): string {
  const s = (now - Date.parse(iso)) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const d = Math.round(s / 86400);
  return d === 1 ? "yesterday" : `${d} days ago`;
}

export function day(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

/** A robot's state in words beside its dot; colour is never the only signal. */
export function robotState(
  r: GrowbotRobot,
  serviceUp: boolean,
  now = Date.now(),
): { level: GrowbotLevel; text: string } {
  if (r.revoked_at) return { level: "bad", text: "key revoked — can't connect" };
  if (!serviceUp) return { level: "bad", text: "can't connect — growbot is down" };
  const seen = r.last_seen_at ? ago(r.last_seen_at, now) : null;
  if (r.connected) return { level: "ok", text: seen ? `connected · ${seen}` : "connected" };
  return {
    level: "warn",
    text: seen ? `not connected · last seen ${seen}` : "not connected · never seen",
  };
}

/** The key's age is its last mint: a rotation replaced the one made at first flash. */
export function keyMadeAt(r: GrowbotRobot): string {
  return r.rotated_at ?? r.created_at;
}

/** The launcher tile's second line: who wears the body, or that growbot is down. Without the
 *  personas (a box before G4) it falls back to who is connected. */
export function tileLine(
  s: GrowbotStatus,
  ov?: GrowbotOverview | null,
): { level: GrowbotLevel | null; word: string } | null {
  // Read on every launcher open, from a box that may predate GrowBot: anything that isn't the
  // status shape leaves the tile without a line rather than taking the launcher down.
  if (!s || !Array.isArray(s.robots)) return null;
  if (s.service === "down") return { level: "bad", word: "growbot is down" };
  const live = s.robots.filter((r) => !r.revoked_at);
  if (ov && Array.isArray(ov.personas) && Array.isArray(ov.body) && live.length > 0) {
    const worn = live.flatMap((r) => {
      const p = riderOf(ov, r.code);
      return p ? [{ r, p }] : [];
    });
    if (worn.length === 0) {
      return {
        level: null,
        word: `nobody is wearing ${live.length === 1 ? robotLabel(live[0]) : "a robot"}`,
      };
    }
    return {
      level: worn.some(({ r }) => r.connected) ? "ok" : null,
      word: worn.map(({ r, p }) => `${p.name} is wearing ${robotLabel(r)}`).join(", "),
    };
  }
  const on = s.robots.filter((r) => r.connected && !r.revoked_at);
  if (on.length === 0) return { level: null, word: "no robot connected" };
  return { level: "ok", word: `${on.map((r) => r.name || r.code).join(", ")} connected` };
}

export const PHASES: { id: keyof GrowbotFlashJob["phases"]; label: string }[] = [
  { id: "load", label: "Load MicroPython" },
  { id: "copy", label: "Copy its firmware and Wi-Fi" },
  { id: "join", label: "Join Wi-Fi and reach the box" },
];

export const PHASE_WORD: Record<GrowbotFlashJob["phases"]["load"], string> = {
  wait: "waiting",
  run: "running",
  done: "done",
  fail: "failed",
  skip: "already on it",
};

/** What happened and what to do next, per failure — never "see the log" (DESIGN.md Voice &
 *  microcopy). load_failed and wifi_failed are the mock's own words. */
export const FAIL_COPY: Record<GrowbotFailCode, { title: string; body: string }> = {
  load_failed: {
    title: "MicroPython did not load",
    body: "the Pico isn't in its USB loader. Unplug it, hold the white BOOTSEL button while plugging it back into the box, then flash again. Nothing on it was changed.",
  },
  not_bootsel: {
    title: "It wouldn't restart into its USB loader",
    body: "the box asked the Pico to, and it stayed as it was. Unplug it, hold the white BOOTSEL button while plugging it back into the box, then flash again. Nothing on it was changed.",
  },
  usb_gone: {
    title: "It left the box's USB partway through",
    body: "the Pico disconnected while it was being flashed. Check it is on a data cable and pushed fully in, plug it back into the box, then flash again.",
  },
  wrong_robot: {
    title: "That's a different robot",
    body: "the Pico on the cable has another pairing code than the robot you chose, so nothing was written and no key was made. Pick the robot that's plugged in, or add it as a new one.",
  },
  copy_failed: {
    title: "Its files didn't copy",
    body: "MicroPython is on it, but copying its firmware and Wi-Fi stopped partway. Unplug it, plug it back into the box, then flash again; the next try skips MicroPython.",
  },
  wifi_failed: {
    title: "Copied, but it couldn't join Wi-Fi",
    body: "none of its networks answered. Check they are 2.4 GHz and in range of the robot, then flash again; MicroPython is already on it, so the next try is quicker.",
  },
  box_unreached: {
    title: "It joined Wi-Fi but never reached the box",
    body: "it is on the network, and its call to the box didn't arrive. Check that network can reach the box — a guest network usually can't — then flash again.",
  },
  clock_unset: {
    title: "It joined Wi-Fi but couldn't set its clock",
    body: "it checks the box's certificate against the time, and the box didn't answer its time request. Run Ops → Update so the box answers it, then flash again.",
  },
  needs_reflash: {
    title: "This robot needs a reflash, not a new key",
    body: "it was set up without the box's certificate, so a new key alone would leave it trusting anyone. Tap Add a robot and pick this one to reflash it.",
  },
  timeout: {
    title: "It stopped answering",
    body: "the robot went quiet before it finished. Unplug it, plug it back into the box, then flash again.",
  },
  internal: {
    title: "The box hit a fault of its own",
    body: "nothing about the robot caused this. Flash again; if it keeps happening, restart api from Ops → Services.",
  },
};

function robotLabel(r: GrowbotRobot | undefined): string {
  return r ? r.name || r.code : "the robot";
}

/** Shown once per phone: the box's certificate note before the first Open GrowBot. */
export const CERT_NOTED_KEY = "jb.growbot.certNoted";

export function certNoted(): boolean {
  try {
    return localStorage.getItem(CERT_NOTED_KEY) === "1";
  } catch {
    return false;
  }
}

export function noteCert(): void {
  try {
    localStorage.setItem(CERT_NOTED_KEY, "1");
  } catch {
    // A private window keeps no note, so the next open explains the certificate again.
  }
}

export const CERT_URL = "/api/lan/root.crt";

// ===== Personas (wave G4: DESIGN.md "GrowBot — the desk robot's screen") =====

/** A robot's state for the Body tab, in the persona mock's words. */
export function bodyState(
  r: GrowbotRobot,
  serviceUp: boolean,
  now = Date.now(),
): { level: GrowbotLevel; text: string } {
  if (r.revoked_at) return { level: "bad", text: "key revoked — can't connect" };
  if (!serviceUp) return { level: "bad", text: "can't connect — growbot is down" };
  const seen = r.last_seen_at ? ago(r.last_seen_at, now) : null;
  if (r.connected) return { level: "ok", text: seen ? `on · seen ${seen}` : "on" };
  return { level: "warn", text: seen ? `off · last seen ${seen}` : "off · never seen" };
}

/** What the screen needs to name a place: which phone is this one (learned, see
 *  `learnThisPhone`), every phone's label, and the robots. */
export interface Places {
  thisPhone: string | null;
  phones: GrowbotPhone[];
  robots: GrowbotRobot[];
}

export function phoneName(id: string | null, pl: Places, cap = false): string {
  if (id && id === pl.thisPhone) return cap ? "This phone" : "this phone";
  return pl.phones.find((f) => f.id === id)?.label || (cap ? "A phone" : "a phone");
}

export function onPhone(id: string | null, pl: Places): string {
  return id && id === pl.thisPhone ? "on this phone" : `on ${phoneName(id, pl)}`;
}

export function robotName(code: string | null, pl: Places): string {
  const r = pl.robots.find((x) => x.code === code);
  return r ? r.name || r.code : code || "the robot";
}

/** Who wears robot `code`: the body list is the box's word, a persona's `wearing` its echo. */
export function riderOf(ov: GrowbotOverview, code: string): GrowbotPersona | null {
  const row = ov.body.find((b) => b.code === code);
  const pid = row ? row.persona : (ov.personas.find((p) => p.wearing === code)?.id ?? null);
  return pid ? (ov.personas.find((p) => p.id === pid) ?? null) : null;
}

/** Who wears robot `code`, from the personas alone (a sheet that has no overview to hand). */
export function riderOfPersonas(personas: GrowbotPersona[], code: string): GrowbotPersona | null {
  return personas.find((p) => p.wearing === code) ?? null;
}

export type WhereLevel = "violet" | "steel" | "ok" | "";

/** Where a persona is, in words beside its dot; colour is never the only signal. */
export function where(
  p: GrowbotPersona,
  pl: Places,
  now = Date.now(),
): { level: WhereLevel; text: string } {
  if (!p.phone) {
    if (!p.last_phone) return { level: "", text: "not open yet" };
    const when = p.opened_at ? ` ${ago(p.opened_at, now)}` : "";
    return { level: "", text: `not open · last ${onPhone(p.last_phone, pl)}${when}` };
  }
  if (p.wearing) {
    return {
      level: "violet",
      text: `${onPhone(p.phone, pl)} · wearing ${robotName(p.wearing, pl)}`,
    };
  }
  return {
    level: p.phone === pl.thisPhone ? "steel" : "ok",
    text: `alone ${onPhone(p.phone, pl)}`,
  };
}

export function initial(name: string): string {
  return (
    name
      .replace(/^[^\p{L}\p{N}]*/u, "")
      .charAt(0)
      .toUpperCase() || "?"
  );
}

/** What moved between two overviews, as the banner says it; null when nothing moved. The same
 *  words serve a move made here and one made on another phone, so both read alike. */
export function handover(prev: GrowbotOverview, next: GrowbotOverview, pl: Places): string | null {
  const before = new Map(prev.personas.map((p) => [p.id, p]));
  const out: string[] = [];
  const told = new Set<string>();
  const offNote = (code: string) => {
    const r = pl.robots.find((x) => x.code === code);
    return r && !r.connected && !r.revoked_at
      ? ` ${robotName(code, pl)} is off right now — its legs move once it's switched on and reaches the box.`
      : "";
  };
  const riderBefore = (code: string) => riderOf(prev, code)?.id ?? null;

  for (const p of next.personas) {
    const was = before.get(p.id);
    if (!was || !p.phone || was.phone === p.phone) continue;
    told.add(p.id);
    const wore = p.wearing && riderBefore(p.wearing) !== p.id ? p.wearing : null;
    const wearing = p.wearing ? robotName(p.wearing, pl) : "";
    const lead =
      p.phone === pl.thisPhone
        ? p.wearing
          ? `${p.name} is wearing ${wearing} on this phone.`
          : `Opened ${p.name} on this phone, alone.`
        : `${p.name} was opened on ${phoneName(p.phone, pl)}${p.wearing ? `, wearing ${wearing}` : ""}.`;
    const ev = [lead];
    for (const q of prev.personas) {
      const later = next.personas.find((x) => x.id === q.id);
      if (q.id === p.id || q.phone !== p.phone || !later || later.phone) continue;
      told.add(q.id);
      ev.push(
        `${q.name} closed ${onPhone(p.phone, pl)} to make room — it's kept on the box, with everything it remembers.`,
      );
    }
    if (was.phone) {
      ev.push(`It moved from ${phoneName(was.phone, pl)}, which now shows that it has moved.`);
    }
    // Opened here and nothing else moved: the launch's own toast already said so.
    if (p.phone === pl.thisPhone && ev.length === 1 && !wore) continue;
    out.push(ev.join(" ") + (wore ? offNote(wore) : ""));
  }

  for (const r of pl.robots) {
    const from = riderBefore(r.code);
    const to = riderOf(next, r.code);
    if (from === (to?.id ?? null)) continue;
    // A rider that is gone was deleted, and the delete said what it did to the body.
    if (from && !to && !next.personas.some((x) => x.id === from)) continue;
    const name = robotName(r.code, pl);
    const ev: string[] = [];
    if (to && !told.has(to.id)) {
      ev.push(`${to.name} is wearing ${name}${to.phone ? ` ${onPhone(to.phone, pl)}` : ""}.`);
    }
    const old = from ? next.personas.find((x) => x.id === from) : null;
    if (old?.phone) {
      ev.push(`${old.name} let go of ${name} and carries on alone ${onPhone(old.phone, pl)}.`);
    } else if (old && !told.has(old.id)) {
      ev.push(`${old.name} let go of ${name}.`);
    }
    if (!to) ev.push(`${name} stands still until a persona connects.`);
    if (to && !told.has(to.id)) ev.push(offNote(r.code).trim());
    const said = ev.filter(Boolean).join(" ");
    if (said) out.push(said);
  }
  return out.length ? out.join(" ") : null;
}

// "This phone": the creature runs on another origin, so the PWA never sees its phone id. It
// is learned instead — a launch here is remembered, and the next overview that shows that
// persona opened at or after the launch names the phone it opened on as this one.
export const THIS_PHONE_KEY = "growbot.thisPhone";
export const LAUNCHED_KEY = "growbot.launchedHere";
// The box's clock and this phone's can disagree by a little. A ticket lives 120 s, so a
// launch older than that (and the skew) never landed and is forgotten.
const SKEW_MS = 30_000;
const LAUNCH_TTL_MS = 120_000 + 2 * SKEW_MS;

function readStore(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStore(key: string, value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // A private window remembers nothing: phones are then named by their label.
  }
}

export function storedThisPhone(): string | null {
  return readStore(THIS_PHONE_KEY);
}

export function noteLaunchHere(persona: string, now = Date.now()): void {
  writeStore(LAUNCHED_KEY, JSON.stringify({ persona, at: now }));
}

/** Forget a launch here that won't land here: its mint failed, its tab was blocked, or a
 *  code for that persona went to another phone (which would be learned as this one). */
export function forgetLaunchHere(persona?: string): void {
  const raw = readStore(LAUNCHED_KEY);
  if (!raw) return;
  if (persona) {
    try {
      if ((JSON.parse(raw) as { persona?: unknown }).persona !== persona) return;
    } catch {
      // Unreadable: dropped.
    }
  }
  writeStore(LAUNCHED_KEY, null);
}

/** This phone's id, learned from `ov` when a launch here has since landed. */
export function learnThisPhone(ov: GrowbotOverview, now = Date.now()): string | null {
  const known = storedThisPhone();
  const raw = readStore(LAUNCHED_KEY);
  if (!raw) return known;
  let at = 0;
  let persona = "";
  try {
    const got = JSON.parse(raw) as { persona?: unknown; at?: unknown };
    persona = typeof got.persona === "string" ? got.persona : "";
    at = typeof got.at === "number" ? got.at : 0;
  } catch {
    // An unreadable note is dropped below.
  }
  if (!persona || now - at > LAUNCH_TTL_MS) {
    writeStore(LAUNCHED_KEY, null);
    return known;
  }
  const p = ov.personas.find((x) => x.id === persona);
  if (!p?.phone || !p.opened_at || Date.parse(p.opened_at) < at - SKEW_MS) return known;
  writeStore(THIS_PHONE_KEY, p.phone);
  writeStore(LAUNCHED_KEY, null);
  return p.phone;
}

// ---- the soul, in upstream's soul_format 3 regions ----

export const SAFETY_NOTE =
  "Child-safety rules are always added after the personality. They can't be edited here, in a soul file or in the creature.";

export interface RegionInfo {
  id: GrowbotRegion;
  label: string;
  what: string;
  kind: "text" | "json" | "meta";
  /** Who wins while it wears the body (plan §2b keeps models and legs with the robot). */
  robot?: (robot: string) => string;
  floor?: boolean;
}

export const REGIONS: RegionInfo[] = [
  {
    id: "persona",
    label: "Personality",
    what: "How it talks and what it cares about.",
    kind: "text",
    floor: true,
  },
  {
    id: "expression",
    label: "Expression",
    what: "Its face, sounds and little moves.",
    kind: "json",
  },
  {
    id: "meta",
    label: "Name and description",
    what: "What a soul file says about it.",
    kind: "meta",
  },
  {
    id: "dream",
    label: "Dreams",
    what: "How it looks back over each day and rewrites its own memory at night.",
    kind: "text",
  },
  {
    id: "harness",
    label: "Thinking",
    what: "Which model it asks for and how long it waits.",
    kind: "json",
    robot: (b) => `While it wears ${b}, the robot's own settings win over these.`,
  },
  {
    id: "body",
    label: "Body",
    what: "How it thinks its legs move.",
    kind: "json",
    robot: (b) => `While it wears ${b}, the robot's own settings win over these.`,
  },
];

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** A region as its editor holds it: the text, `name — description`, or pretty JSON. */
export function regionText(soul: GrowbotSoul, r: RegionInfo): string {
  if (r.id === "persona") return str(soul.persona?.constitution);
  if (r.id === "dream") return str(soul.dream?.system_prompt);
  if (r.id === "meta") {
    const d = str(soul.meta?.description);
    return d ? `${str(soul.meta?.name)} — ${d}` : str(soul.meta?.name);
  }
  const v = soul[r.id];
  return v && typeof v === "object" ? JSON.stringify(v, null, 2) : "";
}

export function clip(s: string, n: number): string {
  const one = s.replace(/\s+/g, " ").trim();
  return one.length > n ? `${one.slice(0, n - 1).trimEnd()}…` : one;
}

/** The faces a persona's creature can wear (growbot/patches/growbot-face.js), in the order
 *  the picker shows them. */
export const FACES: { id: GrowbotFace; name: string; what: string }[] = [
  { id: "lantern", name: "Lantern", what: "Two glowing eyes, no body." },
  { id: "ostrich", name: "Ostrich", what: "The panels' ostrich: crest, eyes and a ringed neck." },
  { id: "robot", name: "Robot", what: "Square robot eyes." },
  { id: "pixel", name: "Pixel", what: "The original pixel eyes." },
];

/** The creature's own memories. It writes them itself, as it goes and in its dreams; the owner
 *  may correct each one, and `up` is the soul key (and the box's region) it lives in. */
export const MEMORY = [
  { id: "identity", label: "Who it is", up: "identity" },
  { id: "bond", label: "Bond line", up: "identityCore" },
  { id: "scratch", label: "Right now", up: "scratch" },
  { id: "goals", label: "Wants", up: "goals" },
  { id: "log", label: "Diary", up: "log" },
] as const;

export type MemoryId = (typeof MEMORY)[number]["id"];

/** The creature's own caps on what it keeps (soul.js), which the box holds a correction to. */
export const MEMORY_CAPS = {
  identityMin: 20,
  identity: 1600,
  bond: 240,
  state: 90,
  rule: 90,
  rules: 24,
  want: 80,
  wants: 4,
  selfName: 32,
  line: 140,
} as const;

/** The name the creature calls itself, which is its own (upstream's `goals.selfName`). */
export function selfName(soul: GrowbotSoul, fallback: string): string {
  return str(soul.goals?.selfName) || fallback;
}

/** A want or a rule may be a sentence or a small object; either way, its words. */
export function words(v: unknown): string {
  if (typeof v === "string") return v;
  if (v && typeof v === "object") {
    const o = v as Record<string, unknown>;
    return str(o.text) || str(o.txt) || str(o.want) || str(o.rule) || "";
  }
  return "";
}

/** HH:MM, the time a row under its day's heading shows. */
export function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

/** The heading a dated row sits under: Today, Yesterday, or its day (local time). */
export function dayHeading(iso: string, now = new Date()): string {
  const d = new Date(iso);
  const start = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const back = Math.round((start(now) - start(d)) / 86_400_000);
  if (back === 0) return "Today";
  if (back === 1) return "Yesterday";
  // Named checkpoints are kept for good, so an older year says so and never merges with this one.
  return d.getFullYear() === now.getFullYear() ? day(iso) : `${day(iso)} ${d.getFullYear()}`;
}

/** Rows grouped under their day's heading, in the order given (newest first in, so out). */
export function byDay<T extends { created_at: string }>(
  rows: T[],
  now = new Date(),
): { heading: string; rows: T[] }[] {
  const out: { heading: string; rows: T[] }[] = [];
  for (const r of rows) {
    const heading = dayHeading(r.created_at, now);
    const last = out[out.length - 1];
    if (last && last.heading === heading) last.rows.push(r);
    else out.push({ heading, rows: [r] });
  }
  return out;
}

/** dd Mon, HH:MM — a checkpoint's date. */
export function when(iso: string): string {
  return `${day(iso)}, ${clock(iso)}`;
}

// A visual memory with no name still reads as what it is.
const KIND: Record<GrowbotMemoryKind, { label: string; that: string }> = {
  person: { label: "A person", that: "that person" },
  object: { label: "A thing", that: "that thing" },
  handwriting: { label: "Handwriting", that: "that handwriting" },
  scene: { label: "A scene", that: "that scene" },
  place: { label: "A place", that: "that place" },
};

/** A visual memory's label: its name, or what it is. */
export function memoryLabel(m: GrowbotMemory): string {
  return m.name.trim() || (KIND[m.kind] ?? KIND.object).label;
}

/** Its name in quotes, or "that person": the end of "<self name> forgot …". */
export function memoryForgot(m: GrowbotMemory): string {
  return m.name.trim() ? `“${m.name.trim()}”` : (KIND[m.kind] ?? KIND.object).that;
}

/** "3 visual memories" — a checkpoint's, or nothing when it keeps none. */
export function memoriesCount(n: number): string {
  return n > 0 ? `${n} visual memor${n === 1 ? "y" : "ies"}` : "";
}

// The names the brain log hides (GrowbotBrainLogSheet), kept on this phone only: they are the
// household's, so the box never stores them and the list is typed once per phone.
export const BRAIN_LOG_NAMES_KEY = "growbot.brainLogNames";

export function storedBrainLogNames(): string {
  return readStore(BRAIN_LOG_NAMES_KEY) ?? "";
}

export function storeBrainLogNames(names: string): void {
  writeStore(BRAIN_LOG_NAMES_KEY, names.trim() ? names : null);
}

/** "Quorra, Tamsin" → ["Quorra", "Tamsin"]: comma- or line-separated, blanks dropped. */
export function splitNames(names: string): string[] {
  return names
    .split(/[,\n]/)
    .map((n) => n.trim())
    .filter((n) => n.length >= 2)
    .slice(0, 20);
}
