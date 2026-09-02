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
  if (!state.logging) return { tone: "off" as const, text: "not logging" };
  const newest = state.packets[0];
  if (!newest) return { tone: "quiet" as const, text: "listening — nothing heard yet" };
  const age = now - new Date(newest.heard_at).getTime();
  if (age > STALE_AFTER_MS) {
    return { tone: "stale" as const, text: `nothing for ${Math.round(age / 60000)} min` };
  }
  return { tone: "live" as const, text: `heard ${relative(age)}` };
}

function relative(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s ago`;
  return `${Math.round(s / 60)}m ago`;
}

/** Packets per hour over what is loaded — the honest second half of "is it alive". */
export function decodeRate(state: AprsLogState, now: number = Date.now()): string {
  const oldest = state.packets.at(-1);
  if (!state.logging || !oldest || state.packets.length < 2) return "—";
  const spanMs = now - new Date(oldest.heard_at).getTime();
  if (spanMs <= 0) return "—";
  return `${Math.round((state.packets.length / spanMs) * 3_600_000)} pkt/hr`;
}
