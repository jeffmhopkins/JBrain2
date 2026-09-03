// The heard log as a ROSTER — who is out there, rather than what scrolled past.
//
// Binding spec: docs/mocks/aprs/e-stations.html. Stations first, most recently heard
// first; the time range and the type chips sit above the list, and inside a station the
// same two controls narrow that station's own traffic.
//
// The one thing worth restating here, because it is invisible in the shape: these rows
// are keyed on the TRUE sender, not on the AX.25 source. Measured on the owner's box,
// `source` held 6 values while 16 stations were transmitting — three quarters of the
// channel was one IGate relaying internet traffic, and a relayed frame's source names
// the relay. The backend does that derivation; this file just has to not undo it.
//
// EVERY FIELD BELOW IS UNTRUSTED. A callsign is plain bytes anyone with a transmitter
// can forge, so grouping by one is a convenience for reading a log and never a
// statement about who someone is (docs/plans/APRS_CONTROL_PLAN.md, the two trust tiers).

/** The four ranges the segmented control offers. Three NEST — "3 days" contains
 *  "1 day" — and `old` is the complement of a week: the one exclusive bucket, and the
 *  only one that can be empty while the others are full. */
export const WINDOWS = [
  { id: "1d", label: "1 day" },
  { id: "3d", label: "3 days" },
  { id: "1w", label: "1 week" },
  { id: "old", label: "Older" },
] as const;

export type WindowId = (typeof WINDOWS)[number]["id"];

/** The five buckets, in the order the chips show them. Fixed rather than derived from
 *  the response so the row does not reorder itself as traffic changes underneath it. */
export const KINDS = ["Position", "Message", "Weather", "Object", "Other"] as const;

export type Kind = (typeof KINDS)[number];

export interface AprsStation {
  call: string;
  packets: number;
  last_heard_at: string;
  kinds: string[];
  /** Came from the internet rather than off the air. */
  gated: boolean;
  /** Who put it on the air, when that is not who wrote it. */
  relay: string | null;
}

export interface AprsRoster {
  window: WindowId;
  /** Packets per NESTED range (1d/3d/1w), so the tabs say what widening would reveal
   *  before you widen. `old` is deliberately absent: counting the complement of a week
   *  means reading everything the box has ever heard, on every poll. */
  window_packets: Record<string, number>;
  /** Whether the archive holds anything older than a week at all. */
  has_older: boolean;
  /** How much, but only when `old` is the range being read — see `window_packets`. */
  older: number | null;
  /** The list hit its ceiling. A capped list that does not say so hides a station. */
  truncated: boolean;
  /** Rows in this range the classifier has not reached yet. Normally zero; non-zero
   *  means the roster is INCOMPLETE and has to say so rather than quietly listing
   *  fewer stations — the same rule this screen already follows for a dead receiver. */
  unclassified: number;
  /** STATIONS per kind, not packets: it is what the chip filters, and a chip reading 27
   *  beside a list of three stations would be lying about what pressing it does. */
  kind_stations: Record<string, number>;
  /** Stations in range BEFORE the chips narrow it, so the header can read "4 of 16". */
  stations_total: number;
  stations: AprsStation[];
}

export interface AprsStationPacket {
  /** The row's own identity. Keying on an array index remounts every row each time a
   *  poll prepends a frame — invisible while rows hold no state, fatal the moment one
   *  can expand, and already costing a screen-reader user their focus every 5 s. */
  id: string;
  heard_at: string;
  /** What the row's TITLE says: Telemetry, Status, Capabilities, Position (Mic-E)… */
  kind: string;
  /** The coarse stored kind the chips filter on. Deliberately different from `kind` —
   *  five chips are a control you can aim, and "Other" as a title says nothing. */
  bucket: string;
  gated: boolean;
  direct: boolean;
  /** The payload the station composed — for a relayed frame, what is INSIDE the
   *  wrapper. Showing the stored frame would print the transport on every line. */
  text: string;
  /** One line in the APP's words. Empty when the station's own text says it better —
   *  a status report is its own summary, and repeating it in our voice would present a
   *  stranger's sentence as ours. */
  summary: string;
  /** `[label, value]` pairs, in the order a reader wants them. */
  fields: [string, string][];
  /** The station's own free text, verbatim. Rendered as theirs, never as ours. */
  comment: string;
  /** The two symbol characters as transmitted, for the icon. Empty when none. */
  symbol: string;
  /** What could not be read, or what a reader must not assume. Shown, never swallowed. */
  warnings: string[];
  /** Who put it on the air, when that is not who wrote it. */
  relay: string | null;
  /** Direwolf's own 0-100 reading of how strong the transmission was, or null where
   *  nothing was measured. NULL IS NOT ZERO — a frame logged before the level was
   *  captured, or one whose reading could not be paired, has no measurement, and
   *  showing it as weak would invent the one fact on this row that is not
   *  self-declared. */
  audio_level: number | null;
  /** The position as NUMBERS, for working out how far away the station is. Null for a
   *  frame that carried none. The `Position` field is the one to print. */
  lat: number | null;
  lon: number | null;
  /** The frame as heard — the only place a "gated via N4TDX" claim becomes checkable,
   *  because the row deliberately shows the inner payload rather than the wrapper. */
  frame: { source: string; destination: string; path: string[] };
}

export interface AprsStationDetail {
  call: string;
  /** All time, not the range: it is what says whether an empty range means quiet or
   *  means new. */
  packets_total: number;
  last_heard_at: string;
  gated: boolean;
  relay: string | null;
  window: WindowId;
  /** Scoped to this station, and exact for every range including `old`: the origin index
   *  bounds it to one station's rows, so the archive count is cheap here. */
  window_packets: Record<string, number>;
  has_older: boolean;
  older: number | null;
  /** PACKETS per kind here, unlike the roster's station counts — inside one station the
   *  chips narrow its traffic. */
  kind_packets: Record<string, number>;
  packets: AprsStationPacket[];
}

/** A callsign without its SSID. The truck is -9 and the handheld is -7; they are one
 *  operator, and an owner who typed the bare call in Settings meant both. */
export function baseCall(call: string): string {
  return (call || "").trim().toUpperCase().split("-")[0] ?? "";
}

/** Whether a station is the owner's, under the callsign they saved in Settings. An
 *  empty setting matches nothing — it must never make every station "mine". */
export function isMine(call: string, owner: string | null): boolean {
  const mine = baseCall(owner || "");
  return mine !== "" && baseCall(call) === mine;
}

/** The owner's own stations first, everything else in the order the server sent (most
 *  recently heard). Stable within each group, so the recency order survives the pin. */
export function pinMine(stations: AprsStation[], owner: string | null): AprsStation[] {
  if (!baseCall(owner || "")) return stations;
  return [
    ...stations.filter((s) => isMine(s.call, owner)),
    ...stations.filter((s) => !isMine(s.call, owner)),
  ];
}

/** How a station reached us, in the words the roster uses. `gated` without a named
 *  relay happens when the frame claimed an origin that was not callsign-shaped and the
 *  backend filed it under the transmitter instead — there is no second station to name. */
export function arrival(station: { gated: boolean; relay: string | null }): string {
  if (!station.gated) return "heard on RF";
  return station.relay ? `gated via ${station.relay}` : "gated from the internet";
}

/** Elapsed time as the roster states it: "22s", "4m", "3h", "2d".
 *
 * Rounded to one unit on purpose — the question a roster answers is "who is out there
 * right now", and to that question "3h" and "3h 12m" are the same answer. */
export function ago(iso: string, now: number = Date.now()): string {
  const at = new Date(iso).getTime();
  if (!Number.isFinite(at)) return "";
  const s = Math.max(0, Math.round((now - at) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

/** The chips to show, and their counts — the kinds present, plus any that are SELECTED.
 *
 * A chip row of five where the range only holds objects is four dead controls, and a
 * single chip is a control with nothing to choose between, so both collapse to nothing.
 *
 * The `selected` half is what keeps the row escapable. A kind carried into a station that
 * the station never sends has a count of zero, so without this the chip vanishes — and
 * the owner is left with an empty list, a message telling him to clear a type filter, and
 * no filter on screen to clear. */
export function chipsFor(
  counts: Record<string, number>,
  selected: readonly string[] = [],
): { kind: Kind; count: number }[] {
  const present = KINDS.filter((k) => (counts[k] || 0) > 0 || selected.includes(k)).map((k) => ({
    kind: k,
    count: counts[k] ?? 0,
  }));
  return present.length > 1 || selected.length > 0 ? present : [];
}

/** The list header's count: "4 of 16" while chips are narrowing it, "16" otherwise. */
export function shownLabel(shown: number, total: number, filtered: boolean): string {
  return filtered && total !== shown ? `${shown} of ${total}` : `${shown}`;
}
