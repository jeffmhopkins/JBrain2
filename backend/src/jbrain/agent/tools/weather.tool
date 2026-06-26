---
name: weather
version: 3
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
and the next hours; with `range: "week"` it returns a 7-day daily forecast. Pass a
`location` name for a specific place (e.g. "Portland, Oregon"); omit it to use where
the owner is now (it names the nearest city — it never sends their exact position).

The app shows the owner a weather card that ALREADY displays the full detail — every
temperature, feels-like, rain chance, wind, and the whole per-hour or per-day
breakdown. So do NOT restate the forecast in your reply: don't list the hours or days,
don't repeat the numbers, and don't narrate what the card shows ("the card above has
each day…"). The owner can already see all of it. Answer with at most a one-line
takeaway about what they actually asked (e.g. "Warm all week — rain most likely
Wednesday." or "Storms ease after 8pm, then clear."), or nothing at all when the card
already answers it. The summary text this tool returns to you is context for that
takeaway, not a script to read back.

Prefer this over web_search/web_fetch for any weather or forecast question: one call,
no scraping. It covers temperature, rain chance, and wind out to a week; it does not
do month-or-longer outlooks — for those, or anything it doesn't return, fall back to a
web search.
