---
name: weather
version: 2
permission: web
params:
  type: object
  properties:
    location:
      type: string
      description: The place to check, as a plain name — a city, "city, state", or a landmark (e.g. "Cocoa, FL", "Portland, Oregon", "Tokyo"). Omit it to use the owner's current location (resolved to the nearest city).
    range:
      type: string
      enum: [today, week]
      description: How far ahead to forecast. "today" (default) gives current conditions plus the next-24h hourly detail; "week" gives a 7-day daily outlook (each day's high/low, sky, rain chance, wind).
  required: []
---
Get the weather for a place. With `range: "today"` (the default) it returns the
conditions now (temperature, feels-like, sky, wind, humidity), today's high and low,
and the next hours; with `range: "week"` it returns a 7-day daily forecast. Either
way the app shows a weather card carrying the detail, so you don't need to repeat
every hour or day in your reply — summarize what the owner asked for (e.g. "this
afternoon", "the week ahead") and let the card do the rest. Pass a `location` name
for a specific place (e.g. "Portland, Oregon"); omit it to use where the owner is now
(it names the nearest city — it never sends their exact position). Prefer this over
web_search/web_fetch for any weather or forecast question: one call, no scraping. It
covers temperature, rain chance, and wind out to a week; it does not do month-or-
longer outlooks — for those, or anything it doesn't return, fall back to a web search.
