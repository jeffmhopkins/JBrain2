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

/** The rung of `ladder` closest to `wantHz`.
 *
 *  Snapping rather than rounding to a step: the ladder is not evenly spaced (AM runs
 *  8/6/4/3 kHz) and every rung is a filter design a test has measured, so a value
 *  between two of them is not a finer setting — it is one the box will refuse. */
export function nearestBandwidth(ladder: readonly number[], wantHz: number): number {
  if (ladder.length === 0) return 0;
  return ladder.reduce((best, rung) =>
    Math.abs(rung - wantHz) < Math.abs(best - wantHz) ? rung : best,
  );
}

/** The next rung in or out from `current`, for arrow keys. Returns null at the ends,
 *  so a held key stops rather than wrapping from narrowest round to widest — which on a
 *  radio is a jump from a filter that rejects everything to one that rejects nothing. */
export function stepBandwidth(
  ladder: readonly number[],
  current: number,
  direction: "narrower" | "wider",
): number | null {
  const at = ladder.indexOf(current);
  if (at < 0) return null;
  // The ladder is WIDEST FIRST, so narrower is forward through it.
  const next = direction === "narrower" ? at + 1 : at - 1;
  return next >= 0 && next < ladder.length ? (ladder[next] as number) : null;
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
  bandwidths_hz?: number[];
}): boolean {
  return (session.bandwidths_hz?.length ?? 0) > 1 && (session.bandwidth_hz ?? 0) > 0;
}
