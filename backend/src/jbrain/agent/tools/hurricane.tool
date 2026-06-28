---
name: hurricane
version: 1
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

The app shows the owner a hurricane card that ALREADY displays the storm's vitals,
its distance, and its bearing. So do NOT restate those numbers in your reply — answer
with at most a one-line takeaway about what they actually asked (e.g. "Nothing close —
the nearest storm is over 600 miles away." or "Hurricane Elena is the nearest, about
215 miles to your southwest and moving away."), or nothing at all when the card
already answers it.

IMPORTANT — what this tool does and does NOT cover. It reports storm POSITION and
INTENSITY only. It does NOT return official watches or warnings, evacuation orders,
storm-surge heights, rainfall totals, or the local timing of wind/surge/rain for the
place. Never invent or imply those — do not say a place is "under a hurricane
warning", "should evacuate", or "will see N feet of surge" based on this tool. If the
owner asks about watches/warnings, surge, rainfall, or local impact timing, say those
aren't in this card and point them to official NWS/NHC advisories (weather.gov,
hurricanes.gov) or local emergency management.

When there are no active storms, it says so plainly. Prefer this over
web_search/web_fetch for "is there a hurricane near X / how far / how strong / which
way is it moving" — one call, no scraping. For watches, warnings, surge, rainfall, or
forecasts beyond current position and strength, fall back to a web search.
