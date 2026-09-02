// The APRS heard log, as the Radio screen reads it.
//
// Two facts, and keeping them together is the point: what was heard, and whether
// anything is receiving RIGHT NOW. A quiet packet channel and a dead receiver look
// identical in a list of rows, and this surface already refused that once — the tuner's
// signal meter was deleted for reading high on a dead channel
// (docs/mocks/aprs/README.md). So `logging` travels with the rows, never inferred from
// them.
//
// EVERY FIELD BELOW IS UNTRUSTED. These are transmissions from anyone with a radio in
// range, and a callsign is plain bytes that forge trivially. The UI renders them as
// quoted text and nothing here ever reaches a model as instructions
// (docs/plans/APRS_CONTROL_PLAN.md, the two trust tiers).

export interface AprsPacket {
  heard_at: string;
  frequency_hz: number;
  source: string;
  destination: string;
  path: string[];
  info: string;
}

export interface AprsLogState {
  /** True only while a session is holding the tuner to LOG. Not "there are rows". */
  logging: boolean;
  /** Whether the box could reach the radio at all. `logging: false` alone cannot say
   * whether the receiver is off or unreachable, and a dead receiver reading as a
   * switched-off one is exactly the confusion this surface exists to prevent. */
  reachable: boolean;
  frequency_hz: number | null;
  packets: AprsPacket[];
}

export interface AprsToggleResult {
  logging: boolean;
  /** False when the call was a no-op — it was already in the state asked for. */
  changed: boolean;
  frequency_hz?: number | null;
}

/** How stale the newest packet can be before the receiver reads as suspect rather than
 *  the channel as quiet. A packet frequency goes minutes between frames, so this is
 *  generous; what it rules out is a receiver that died hours ago looking healthy. */
export const STALE_AFTER_MS = 40 * 60 * 1000;

/** Health as the tab shows it: never a signal bar, only last-decode and rate. */
export function receiverHealth(state: AprsLogState, now: number = Date.now()) {
  // Unreachable outranks everything: with the box unable to ask, every other answer
  // here would be a guess dressed as a reading.
  if (!state.reachable) return { tone: "stale" as const, text: "the radio isn't reachable" };
  if (!state.logging) return { tone: "off" as const, text: "not logging" };
  const newest = state.packets[0];
  if (!newest) return { tone: "quiet" as const, text: "listening — nothing heard yet" };
  const age = now - new Date(newest.heard_at).getTime();
  if (!Number.isFinite(age)) {
    // An unparseable timestamp used to fall through to `live` with "heard NaNs ago" —
    // the most reassuring tone for the one state we understand least. In a control
    // whose entire job is telling a dead receiver from a quiet channel, the unknown
    // case belongs on the suspect side.
    return { tone: "stale" as const, text: "last decode unreadable" };
  }
  if (age > STALE_AFTER_MS) {
    return {
      tone: "stale" as const,
      text: `nothing for ${Math.round(age / 60000)} min`,
    };
  }
  return { tone: "live" as const, text: `heard ${relative(age)}` };
}

function relative(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s ago`;
  return `${Math.round(s / 60)}m ago`;
}

/** Packets per hour over what is loaded — the honest second half of "is it alive".
 *
 * Measured over the SAME window that decides staleness, and that shared threshold is
 * the point. Spanning oldest-to-now made the rate decay without ever reaching zero, so
 * a receiver that had heard nothing for 41 minutes read "26 pkt/hr" — busy — beside a
 * health line saying "nothing for 41 min": the two halves of one control contradicting
 * each other. Sharing the threshold makes that disagreement unrepresentable. */
export function decodeRate(state: AprsLogState, now: number = Date.now()): string {
  if (!state.logging || !state.reachable) return "—";
  const cutoff = now - STALE_AFTER_MS;
  const recent = state.packets.filter((p) => {
    const at = new Date(p.heard_at).getTime();
    return Number.isFinite(at) && at >= cutoff;
  });
  if (recent.length === 0) return "0 pkt/hr";
  return `${Math.round((recent.length / STALE_AFTER_MS) * 3_600_000)} pkt/hr`;
}

/** One of the owner's radio commands, as the tab summarises it. Read-only here: the
 * editor lives in Tasks, and two doors onto one object need care they do not repay. */
export interface AprsCommand {
  id: string;
  name: string;
  enabled: boolean;
  word: string | null;
  callsign: string | null;
  days: number[];
  from: string | null;
  until: string | null;
  /** Too many failed attempts: nothing fires until the owner clears it. */
  locked: boolean;
  last_at: string | null;
}

/** One attempt heard on the air. A REFUSAL is the row worth having — three of these
 * from an unknown station last Tuesday is a fact the owner needs to be able to find,
 * and a push notification does not keep. */
export interface AprsAttempt {
  heard_at: string;
  source: string;
  word: string;
  accepted: boolean;
  reason: string;
}

export interface AprsCommandState {
  commands: AprsCommand[];
  attempts: AprsAttempt[];
}

/** How the arming window reads on a card: "armed weekdays 06:00–09:00" (the mock's
 * phrasing). It answers "when is it LISTENING", never "when does it run". */
export function armedLabel(command: AprsCommand): string {
  if (!command.enabled) return "paused";
  if (command.locked) return "locked — too many failed attempts";
  if (!command.from || !command.until) return "armed always";
  return `armed ${daysLabel(command.days)} ${command.from}–${command.until}`;
}

const DAY_LETTERS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function daysLabel(days: number[]): string {
  if (days.length === 0 || days.length === 7) return "daily";
  const sorted = [...days].sort((a, b) => a - b);
  if (sorted.join() === "1,2,3,4,5") return "weekdays";
  return sorted.map((d) => DAY_LETTERS[d]).join(" ");
}
