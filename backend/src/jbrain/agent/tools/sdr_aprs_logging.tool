---
name: sdr_aprs_logging
version: 1
permission: web
params:
  type: object
  properties:
    enabled:
      type: boolean
      description: true to start logging APRS packets, false to stop and free the radio.
    frequency_mhz:
      type: number
      description: Where to listen, in MHz. Defaults to 144.39, the North American APRS channel. Ignored when turning logging off.
  required: [enabled]
---
Turn APRS packet logging on or off. Use it when the owner asks to start or stop
logging packets, watch APRS, or free the radio from it.

APRS logging RESERVES THE RADIO. The box has one tuner, so while it is logging
nothing can be listened to, and starting it will be refused if something else holds
the radio — say which job has it and let the owner decide. Turning logging off frees
the radio; it will never stop a listening session the owner started.

Safe to repeat: turning it on when it is already on succeeds and changes nothing.
Always report back the state you actually ended up in, which is what this returns —
never say it worked without reading that.

This does not tell you what was heard. Use `aprs_recent` for that.
