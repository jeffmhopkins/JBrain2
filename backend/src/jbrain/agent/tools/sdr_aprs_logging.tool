---
name: sdr_aprs_logging
version: 2
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

APRS logging TAKES A RADIO for packets — that radio can't be listened to until logging
is off, but another radio, if there is one, stays free. A refusal names the radio and
the job holding it; that is not an error, so say which and let the owner decide. Turning
logging off frees that radio only; it never stops a listening session the owner started.

Safe to repeat: turning it on when it is already on succeeds and changes nothing.
Always report back the state you actually ended up in, which is what this returns —
never say it worked without reading that.

This does not tell you what was heard. Use `aprs_recent` for that.
