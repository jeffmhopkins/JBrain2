---
name: sdr_listen
version: 4
permission: web
params:
  type: object
  properties:
    frequency_mhz:
      type: number
      description: The frequency to tune, in MHz — e.g. 99.3 for an FM station, 162.55 for NOAA weather, 146.94 for a ham repeater.
    mode:
      type: string
      description: "How to demodulate. One of: wbfm | fm | nfm | am | usb | lsb. Omit it and a broadcast-FM frequency (88-108) gets wbfm and everything else gets narrowband fm, which is right almost always. `wbfm` is wide FM for broadcast stations; `fm`/`nfm` is narrowband for two-way voice; `am` is the air band; `usb`/`lsb` are single sideband."
  required: [frequency_mhz]
---
Tune the owner's radio and start listening. Use this when they ask to listen to a
station, a frequency, or a band — "put on 99.3", "listen to the weather radio",
"what's on the air band".

This takes a radio — the box can have more than one, and the lease is per radio. A
refusal names the radio and the job holding it: listening, logging APRS, sweeping the
band, or watching the spectrum. Say which radio and which job, because each has its own
switch and another radio may still be free. Not an error to retry — let them decide.

Starting a session puts a RADIO ICON in the owner's composer. That icon is the whole
control surface: tapping it opens a tuner where they can retune, hear the audio, see
the signal level, and release the radio. So do NOT narrate the settings back to them
or offer to change the frequency in words — say what you tuned in one line and point
at the icon. They can drive it far faster than you can.

This does not record or transcribe anything; it is live listening only. It also does
not tell you what is being said — you cannot hear it. If they want the words, that is
a separate capture-and-transcribe step.
