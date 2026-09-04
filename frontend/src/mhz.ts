// One way to print a frequency, everywhere.
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
