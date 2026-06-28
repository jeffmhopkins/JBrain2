---
name: hurricane
version: 2
permission: web
params:
  type: object
  properties:
    location:
      type: string
      description: The place to check, as a plain name — a city, "city, state", or a landmark (e.g. "Tampa, FL", "New Orleans", "San Juan"). Omit it to use the owner's current location (resolved to the nearest city).
  required: []
---
Check for an active tropical cyclone (hurricane, tropical storm, or depression) near
a place. It looks up the National Hurricane Center's live list of active storms
worldwide and returns the one NEAREST the place, with its name, classification and
Saffir-Simpson category, maximum sustained winds, central pressure, how it's moving,
and how far away it is and in which direction. Pass a `location` name for a specific
place; omit it to use where the owner is now (it names the nearest city — it never
sends their exact position).

The app shows the owner a hurricane card with several tabs that ALREADY display the
detail: the storm's vitals and distance/bearing, its forecast TRACK and cone, and —
where the place is inside U.S. National Weather Service coverage (the U.S. and its
territories) — the OFFICIAL watch/warning in effect plus a local wind/rain TIMELINE
and an IMPACT summary (peak wind/gusts, rainfall, a banded storm-surge estimate, and
approximate timing). So do NOT restate those numbers in your reply — answer with at
most a one-line takeaway about what the owner actually asked (e.g. "Tampa is under a
Hurricane Warning — Elena's core passes nearby early Thursday." or "Nothing close — the
nearest storm is over 600 miles away."), or nothing at all when the card answers it.

Honesty boundary — bind tightly to what the card actually carries:
- Official watches/warnings come from the NWS and ONLY exist when the place is in U.S.
  coverage. If the card shows no alert (or the place is outside U.S. coverage), do NOT
  claim or imply any watch/warning is in effect.
- Storm surge is a BANDED estimate ("up to N ft"), never a precise depth. Rainfall and
  the wind/surge arrival + impact TIMING are APPROXIMATE, derived from the local
  forecast — not official onset products.
- NEVER tell the owner to evacuate or that they must leave based on this card.
  Evacuation decisions follow official orders from local emergency management; point
  them there (and to weather.gov / hurricanes.gov) for anything authoritative.
- Outside U.S. coverage the card shows only the storm and its forecast track — say so
  rather than implying alerts or local impacts you don't have.

When there are no active storms, it says so plainly. Prefer this over
web_search/web_fetch for "is there a hurricane near X / how far / how strong / which
way is it moving / is my area under a warning". For anything authoritative — official
evacuation orders, precise surge, long-range outlooks — fall back to a web search or
defer to official advisories.
