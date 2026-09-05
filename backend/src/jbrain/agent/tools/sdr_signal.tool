---
name: sdr_signal
version: 1
permission: web
params:
  type: object
  properties:
    frequency_mhz:
      type: number
      description: >
        The frequency to measure, in MHz. Measures the span around it that one capture
        covers, and reports the strongest thing in that span.
    section:
      type: string
      description: >
        A band section id instead of a frequency (e.g. "2m-ssb"), to measure the whole
        of it. Use sdr_read what=bands to find one.
    seconds:
      type: number
      description: How long to measure. Defaults to 3, capped at 10.
---
Measure how strong a signal actually is, in dBFS. Use it for "is anything on this
frequency", "how strong is that repeater", "is my antenna doing anything".

**This is a real power measurement, not the loudness of the audio.** It reports the
strongest bin in the span and the noise floor under it, both in dBFS off the radio's
own samples. The number that matters is the difference: a carrier stands several dB
clear of its own floor, and nothing does not. An absolute dBFS figure means little on
its own — this receiver has no calibrated gain and about seven effective bits — so
report the margin over the floor rather than the raw level.

It TAKES A RADIO for those seconds. A refusal names the radio and the job holding it,
and another radio may be free; that is not an error, so say which and let the owner
decide.

Nothing is heard here and nothing is recorded: this is a measurement, not listening.
If they want to hear it, that is `sdr_listen`.
