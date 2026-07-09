---
name: weather_history
version: 1
permission: web
cost_class: standard
params:
  type: object
  properties:
    location:
      type: string
      description: The place, as a plain name — a city, "city, state", or a landmark (e.g. "Cocoa, FL", "Portland, Oregon", "Tokyo"). Omit it to use the owner's current location (resolved to the nearest city).
    start_date:
      type: string
      description: First day of the range, as a calendar date YYYY-MM-DD (e.g. "2023-07-01"). Must be in the past.
    end_date:
      type: string
      description: Last day of the range, as a calendar date YYYY-MM-DD (e.g. "2023-07-31"). Must be in the past and on or after start_date.
  required: [start_date, end_date]
---
Get PAST weather for a place and date range, with the heat index computed for you.
It fetches the hourly historical record (air temperature and humidity) and returns the
aggregates: average temperature, average high/low, average humidity, and — the reason to
use this tool — the heat index ("feels like"), given three ways: the average across all
hours, the **average daily peak** (the daytime feels-like figure people usually mean),
and the single hottest hour. It also counts how many days reached the NWS "Danger" band.

Use this for any question about weather in the past beyond about a week — climate
history, "how hot was last summer", trends across years, and especially **heat index over
time**. Per-year heat index is published nowhere and can't be found by searching; it has
to be computed from the hourly data, which is exactly what this tool does. So prefer it
over web_search/web_fetch for historical temperature, humidity, or heat-index questions.
(For the next few days ahead, use the `weather` forecast tool instead; this one is history
only.)

One call covers up to a year. For a multi-year question, call it **once per year** — e.g.
for "the last five Julys" make five calls, `2021-07-01…2021-07-31`, `2022-07-01…`, and so
on. Independent calls run together, and each year's averages come back separately so you
can lay them out side by side. Pass a `location` name for a specific place; omit it to use
where the owner is now (it uses the nearest city — it never sends their exact position).
