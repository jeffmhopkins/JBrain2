/** Where this radio can honestly be pointed, and why not, in one place.
 *
 *  **This is a bug class, not a fact.** `jbrain/sdr/tuner.py` exists because "the radio
 *  tunes 24-1766 MHz" is the R820T2 TUNER's range, and the box also reaches 0.1-14.4 MHz
 *  with that tuner powered down and the ADC fed straight from the antenna. Every copy of
 *  the tuner floor that leaks into a UI refuses something the box can do — and it has now
 *  leaked twice: once into the tuner controls (fixed there, with a comment saying so) and
 *  once into the band sheet, which read the tuner floor off the API and refused a manual
 *  4.625 MHz while the picture behind it was drawing 4.393-5.417 MHz from a band button.
 *
 *  So the rule lives here and both screens ask it. Mirrored from `listen.aliased_refusal`,
 *  which is where it is ENFORCED; this is the reading that catches it under the owner's
 *  thumb rather than as a round trip.
 */

/** The floor of everything the radio reaches, either path — `tuner.TUNABLE_MIN_MHZ`.
 *  NOT the tuner's floor, which is `TUNER_MIN_MHZ` below and is a different thing. */
export const MIN_MHZ = 0.1;
export const MAX_MHZ = 1766;

/** Below `TUNER_MIN_MHZ` the R820T2 is powered down and the RTL2832U's ADC is fed
 *  straight from the antenna at `ADC_RATE_MHZ`, so the honest range down there stops at
 *  half of that. 14.4-24 MHz is the SECOND Nyquist zone: ask for 18.1 and the radio
 *  hands back 10.7, mirrored, while reporting a healthy session at the frequency that
 *  was typed. Refused rather than warned — there is nothing in the audio or the picture
 *  to tell the owner they are somewhere else. */
export const NYQUIST_MHZ = 14.4;
export const TUNER_MIN_MHZ = 24;
export const ADC_RATE_MHZ = 28.8;

/** Why the radio cannot honestly be tuned to `mhzValue`, or null.
 *
 *  Both the steppers and the typed field ask it: stepping up from 14.35 MHz walks into
 *  the same zone as typing 18.1, and a guard on only one of them is a guard on neither. */
export function whyNotTunable(mhzValue: number): string | null {
  if (!Number.isFinite(mhzValue) || mhzValue < MIN_MHZ || mhzValue > MAX_MHZ) {
    return `This radio tunes ${MIN_MHZ}-${MAX_MHZ} MHz.`;
  }
  if (mhzValue > NYQUIST_MHZ && mhzValue < TUNER_MIN_MHZ) {
    const image = (ADC_RATE_MHZ - mhzValue).toFixed(3);
    return `Nothing between ${NYQUIST_MHZ} and ${TUNER_MIN_MHZ} MHz: down here the radio bypasses its tuner and samples at ${ADC_RATE_MHZ} MHz, so you would hear ${image} MHz instead.`;
  }
  return null;
}

/** Why a SPAN cannot honestly be drawn, or null.
 *
 *  A picture is a RANGE, and checking it is not checking a point twice. Three ways it can
 *  be wrong and only the first two are edges:
 *
 *  1. An edge past what the radio reaches at all.
 *  2. An edge inside the aliasing hole.
 *  3. **Neither edge in the hole, and the span straddling it anyway** — 12 to 28 MHz has
 *     two perfectly legal edges and a middle drawn from a mirror of somewhere else, with
 *     nothing on screen to say which part is which.
 *
 *  A centre-only check misses all three in one direction or another; an edges-only check
 *  misses the third. */
export function whyNotSpannable(startMhz: number, stopMhz: number): string | null {
  if (!Number.isFinite(startMhz) || !Number.isFinite(stopMhz) || stopMhz <= startMhz) {
    return "A picture needs a width.";
  }
  const edge = whyNotTunable(startMhz) ?? whyNotTunable(stopMhz);
  if (edge !== null) return edge;
  if (startMhz < TUNER_MIN_MHZ && stopMhz > NYQUIST_MHZ) {
    return `A picture cannot cross ${NYQUIST_MHZ}-${TUNER_MIN_MHZ} MHz: the radio changes signal path there, and the middle of this one would be a mirror of somewhere else. Ask for a piece below ${NYQUIST_MHZ} or above ${TUNER_MIN_MHZ}.`;
  }
  return null;
}
