---
name: weather
version: 1
permission: web
params:
  type: object
  properties:
    location:
      type: string
      description: The place to check, as a plain name — a city, "city, state", or a landmark (e.g. "Cocoa, FL", "Tokyo"). Omit it to use the owner's current location (resolved to the nearest city).
  required: []
---
Get the current weather and an hourly forecast for a place. Returns the conditions
now (temperature, feels-like, sky, wind, humidity), today's high and low, and the
next hours — and the app shows a weather card with the hourly strip, so you don't
need to repeat every hour in your reply; summarize what the owner asked for (e.g.
"now through midnight") and let the card carry the detail. Pass a `location` name
for a specific place; omit it to use where the owner is now (it names the nearest
city — it never sends their exact position). Prefer this over web_search/web_fetch
for any weather or forecast question: one call, no scraping. It covers temperature,
rain chance, and wind; for anything it doesn't return, fall back to a web search.
