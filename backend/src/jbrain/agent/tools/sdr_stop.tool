---
name: sdr_stop
version: 3
permission: web
params:
  type: object
  properties: {}
  required: []
---
Release the radio the owner is LISTENING on. Use this when they ask to turn the radio
off, stop listening, or free it up.

It stops listening only: a radio logging APRS, sweeping the band or watching the
spectrum has its own switch, so if nothing was listening this comes back naming what is
holding a radio — ask which they want turned off rather than guessing.

The radio icon disappears from their composer when it is released — that is how they
can tell it worked. They can also release it from the tuner sheet, so if they are
already looking at it, saying so is often more useful than doing it for them.
