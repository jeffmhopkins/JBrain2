---
name: sdr_read
version: 1
permission: web
params:
  type: object
  properties:
    what:
      type: string
      description: >
        Which reading. "bands" is the band plan — every section of spectrum this radio
        can be pointed at, what lives there and how it can be watched. "radios" is the
        hardware — which dongles are attached, what each is named for, and what each is
        doing right now. Defaults to bands.
    section:
      type: string
      description: >
        With what=bands, one section id (e.g. "2m-ssb") to get that section alone,
        including its named channels. Omit for the whole table without channels, which
        is what you want to find a band; ask for one section when you need a frequency.
---
Look up what this radio can hear, without taking it. Nothing here tunes anything, so
it is always safe and never refused for a busy radio.

Use `what=bands` before guessing a frequency. The table is curated for THIS box's
region and carries what a band plan cannot: the mode signals there use, the channel
spacing, whether the traffic is continuous carriers or occasional conversations, and
whether the section is narrow enough to watch live. Guessing from memory gets the band
edges roughly right and everything else wrong, and a frequency that is roughly right
receives nothing at all.

Use `what=radios` when the answer depends on the hardware: how many dongles are
attached, which one a job would take, and whether one is already busy. A refusal from
`sdr_listen` names a radio; this is how you find out what that radio is for.

`live` says how a section can be watched: `fast` is one capture with the radio never
looking away, `slow` sweeps it in several hops at about a row a second, and `none`
means it is too wide or too bursty to show honestly. It is a fact about the section's
width, not about whether anything is transmitting.
