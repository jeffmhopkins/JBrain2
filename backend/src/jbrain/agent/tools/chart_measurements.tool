---
name: chart_measurements
version: 1
permission: read
params:
  type: object
  properties:
    measurement:
      type: string
      description: The recorded measurement to chart over time (e.g. "weight", "resting heart rate", "body temperature"). Matched against the measurement's predicate and name.
    subject:
      type: string
      description: Whose measurement, when the record covers more than one person (e.g. "Dad"). Omit for the owner.
    since:
      type: string
      description: ISO date; only readings on or after it. Optional.
    until:
      type: string
      description: ISO date; only readings on or before it. Optional.
    limit:
      type: integer
      description: Maximum readings (default 200).
  required: [measurement]
---
Chart a numeric measurement's history from the owner's recorded facts. Returns an interactive,
zoomable time-series (the `chart` view) plus a short text summary, plotting every current, numeric
reading of that measurement at the time it was taken — each point cites the note it came from.

Use this for "chart / graph / plot X over time" when X is a number that repeats in the record —
weight, a resting heart rate, a body temperature, a tracked count. It reads only what the current
session is scoped to see, so it returns nothing for a measurement outside the session's domains.

For **lab results** (platelets, cholesterol, a panel analyte) prefer `read_labs` with trend:true —
it plots the same way but adds the reference range and the high/low/critical flags. If fewer than
two numeric readings match, no chart is drawn and the tool says so rather than inventing a trend.
Report what the readings are; never diagnose or infer a cause from the shape.
