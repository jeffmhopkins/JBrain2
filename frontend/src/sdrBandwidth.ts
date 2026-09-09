/** The filter-width control's arithmetic, away from the components that draw it.
 *
 *  Binding spec: `docs/mocks/bandwidth/d-mode-width-draggable.html`. Three ways into one
 *  value — the mode button's popover, a drag on the tuning view's shaded box, and arrow
 *  keys on its handles — so the conversion between "a width" and "where its edges sit"
 *  has to be one function rather than one per input.
 *
 *  **The ladder is never hardcoded here.** It arrives on the session as `bandwidths_hz`
 *  (deploy/sdr/demod.py `BANDWIDTH_HZ`). A copy in the PWA would go on offering widths a
 *  redeployed box had stopped accepting, and the refusal would reach the owner as a 400
 *  they cannot act on.
 */

/** Where an SSB passband starts, relative to the suppressed carrier.
 *
 *  Mirrors `demod.SSB_LOW_HZ`, and it is the one number here that has to: the drag
 *  converts a pointer position into a WIDTH, and on SSB that subtraction needs the low
 *  edge. Wrong by a little and every dragged SSB width is wrong by the same little. */
export const SSB_LOW_HZ = 300;

/** Where a mode's passband sits, as offsets from the tuned frequency.
 *
 *  One-sided on SSB, which is the whole reason this is a function of the MODE and not
 *  half the width doubled: usb hears +300..+3400 and lsb -3400..-300, so shading a
 *  symmetric box would cover half the sideband the demodulator rejects. */
export function bandwidthEdges(
  mode: string,
  bandwidthHz: number,
): { lowHz: number; highHz: number } {
  if (mode === "usb") return { lowHz: SSB_LOW_HZ, highHz: SSB_LOW_HZ + bandwidthHz };
  if (mode === "lsb") return { lowHz: -(SSB_LOW_HZ + bandwidthHz), highHz: -SSB_LOW_HZ };
  return { lowHz: -bandwidthHz / 2, highHz: bandwidthHz / 2 };
}

/** The width a pointer at `offsetHz` from the dial is asking for, before snapping.
 *
 *  Symmetric modes move both edges together, so an edge `x` Hz out means a width of
 *  `2x`. SSB moves only its outer edge, so the width is the distance past `SSB_LOW_HZ`.
 *  Either way the sign is discarded: dragging the lower handle of an AM filter and
 *  dragging the upper one are the same request. */
export function widthFromOffset(mode: string, offsetHz: number): number {
  const away = Math.abs(offsetHz);
  if (mode === "usb" || mode === "lsb") return away - SSB_LOW_HZ;
  return away * 2;
}

/** How coarsely a DRAG lands: whole kilohertz.
 *
 *  Not the box's own grid, which is 100 Hz — that finer step exists so the named
 *  presets stay reachable (SSB's 2.4 and NFM's 12.5 are neither of them whole
 *  kilohertz). A finger on a phone cannot place 100 Hz on a 32 kHz picture, and letting
 *  it try produces a width that reads as a typo. Whole kilohertz is what the owner asked
 *  for, and every value it produces is one the box accepts. */
export const DRAG_STEP_HZ = 1000;

/** The width a drag to `wantHz` should commit, snapped to `DRAG_STEP_HZ` and held
 *  inside `[minHz, maxHz]`.
 *
 *  Clamped HERE and only here: the drag is a position, and a finger past the end of the
 *  picture is asking for the end of the range rather than for a refusal. Everywhere
 *  further in — the API, the session, the demodulator — a width outside the range is an
 *  error, because there it means a caller got it wrong rather than a thumb slipped. */
export function snapBandwidth(wantHz: number, minHz: number, maxHz: number): number {
  const stepped = Math.round(wantHz / DRAG_STEP_HZ) * DRAG_STEP_HZ;
  return Math.min(Math.max(stepped, minHz), maxHz);
}

/** One kilohertz in or out from `current`, for arrow keys. Returns null at the ends, so
 *  a held key stops rather than wrapping from narrowest round to widest — which on a
 *  radio is a jump from a filter that rejects everything to one that rejects nothing.
 *
 *  A kilohertz rather than the next preset: the arrows and the drag are the same
 *  gesture at different resolutions, and a key that jumped 2 kHz while the drag moved 1
 *  would make the two disagree about what "narrower" means. */
export function stepBandwidth(
  current: number,
  direction: "narrower" | "wider",
  minHz: number,
  maxHz: number,
): number | null {
  // From an off-grid width — NFM's 12.5k and SSB's 3.1k are both real presets and
  // neither is a whole kilohertz — the first press lands on the nearest grid point IN
  // THE DIRECTION PRESSED. Adding the step and then rounding would skip one: 12.5k
  // pressed wider gives 13.5k, which rounds to 14k, and 13k is never reachable by key.
  const onGrid = current % DRAG_STEP_HZ === 0;
  const next =
    direction === "narrower"
      ? onGrid
        ? current - DRAG_STEP_HZ
        : Math.floor(current / DRAG_STEP_HZ) * DRAG_STEP_HZ
      : onGrid
        ? current + DRAG_STEP_HZ
        : Math.ceil(current / DRAG_STEP_HZ) * DRAG_STEP_HZ;
  if (next < minHz || next > maxHz || next === current) return null;
  return next;
}

/** A width as the control shows it: "8k", "12.5k", "180k".
 *
 *  Terse because it sits under a three-letter mode name in a 48px button. The kHz is
 *  implied by the k, and a rung is never small enough for the unit to be ambiguous. */
export function bandwidthLabel(hz: number): string {
  const khz = hz / 1000;
  return `${Number.isInteger(khz) ? khz : khz.toFixed(1)}k`;
}

/** The same width said in full, for a label a screen reader reads aloud and for the
 *  popover's heading — where there is room and "8k" would be a crossword clue. */
export function bandwidthSpoken(hz: number): string {
  const khz = hz / 1000;
  return `${Number.isInteger(khz) ? khz : khz.toFixed(1)} kHz`;
}

/** Whether this session can have its filter changed at all.
 *
 *  A single-rung ladder means the mode is fixed (wide FM: narrowing a 180 kHz station
 *  clips the deviation, which distorts rather than cleans). An empty or absent one means
 *  either a spectrum stare, which has no channel, or a sidecar older than the control —
 *  and both should draw nothing rather than a control that cannot work. */
export function bandwidthAdjustable(session: {
  bandwidth_hz?: number;
  bandwidth_min_hz?: number;
  bandwidth_max_hz?: number;
}): boolean {
  // A range with room in it, not a ladder with rungs: wide FM reports min === max, and
  // a sidecar older than the control reports neither.
  const low = session.bandwidth_min_hz ?? 0;
  const high = session.bandwidth_max_hz ?? 0;
  return high > low && (session.bandwidth_hz ?? 0) > 0;
}

/** The passband the picture should DRAW, or null to let the incoming row speak.
 *
 *  **A retune takes about 100 ms and the rows keep coming throughout**, every one of
 *  them still carrying the old passband. Drawing from the row across that window puts
 *  the shaded box and the label at the old width while the handles the finger moved sit
 *  at the new one — the picture contradicting itself at exactly the moment the owner is
 *  watching it to see whether the drag worked. MEASURED on the box: the mode button read
 *  `8k` and the shading still spanned 16 kHz.
 *
 *  So the width the owner chose wins until the row agrees with it — and only until, so
 *  a width the box REFUSED is not drawn forever. When the session settles back on the
 *  old width, the drag state clears, `shownHz` becomes that old width again, the row
 *  already agrees, and the override evaporates on its own. */
export function pendingPassband(
  mode: string,
  shownHz: number,
  rowPassbandHz: number,
): { lowHz: number; highHz: number } | null {
  if (shownHz <= 0) return null;
  const edges = bandwidthEdges(mode, shownHz);
  // A hertz of slack: the row's width is a float that has been through a wire.
  return Math.abs(rowPassbandHz - (edges.highHz - edges.lowHz)) > 1 ? edges : null;
}
