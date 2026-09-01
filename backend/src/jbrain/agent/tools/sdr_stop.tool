---
name: sdr_stop
version: 2
permission: web
params:
  type: object
  properties: {}
  required: []
---
Release the owner's radio, stopping whatever it is listening to. Use this when they
ask to turn the radio off, stop listening, or free it up.

The radio icon disappears from their composer when it is released — that is how they
can tell it worked. The owner can also release it themselves from the tuner sheet, so
if they are already looking at it, saying so is often more useful than doing it for
them.
