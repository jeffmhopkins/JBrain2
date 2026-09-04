// One way to print a frequency — and a bandwidth — everywhere.
//
// Three decimals, always. Not decoration: 144.390 and 146.940 are how the bands are
// SPOKEN and written, and the channel spacing on most of the narrowband plan is 5 kHz
// — so a second decimal is a digit short of naming a channel. A rule that dropped to
// two above 100 MHz (which a mock proposed, and which read fine on a broadcast dial)
// would print the APRS frequency as 144.39.
//
// Here rather than in one of the radio modules because four files had a private copy of
// this, and the fourth one disagreed with the other three.

/** A frequency in Hz as MHz, to three decimals. */
export function mhz(hz: number): string {
  return (hz / 1_000_000).toFixed(3);
}

/** A bandwidth in Hz as kHz. One decimal below 100 kHz, none above.
 *
 *  The decimal is not cosmetic. A bin width is not what the caller ASKED for: rtl_power
 *  grants the largest power-of-two division of its per-hop bandwidth no coarser than
 *  the request, so asking for 25 kHz across the FM dial returns 19531 Hz. Rounded to
 *  "20" that reads as a round number somebody chose, and the owner is left wondering why
 *  their 25 became 20; "19.5" reads as what it is — a figure the radio picked. */
export function khz(hz: number): string {
  const at = hz / 1000;
  return at >= 100 ? at.toFixed(0) : at.toFixed(1);
}
