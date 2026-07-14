---
name: render_chart
version: 1
permission: read
params:
  type: object
  properties:
    title:
      type: string
      description: What the chart shows (e.g. "Coffee shops visited per month"). Used as the label.
    unit:
      type: string
      description: The y-axis unit (e.g. "lb", "$", "count"). Optional.
    kind:
      type: string
      enum: [line, area]
      description: Line (default) or filled area.
    points:
      type: array
      description: The series to plot, one object per point.
      items:
        type: object
        properties:
          x:
            type: string
            description: The point's date — an ISO date (YYYY-MM-DD, YYYY-MM, or YYYY) or epoch milliseconds.
          y:
            type: number
            description: The value at that date.
        required: [x, y]
  required: [title, points]
---
Plot a series of dated numbers you have already gathered, as an interactive, zoomable time-series
(the `chart` view). Give at least two points, each a date (x) and a number (y), oldest to newest.

Use this only for **general** figures you assembled yourself — a count you tallied, a public series
you looked up, a back-of-envelope projection. It renders exactly the numbers you pass, so those
numbers must come from something you actually read this turn; state where they came from in your
reply.

Do NOT use it to re-plot the owner's recorded measurements or lab results — those have grounded,
cited tools (`chart_measurements` for a tracked measurement, `read_labs` with trend:true for a lab
analyte) that plot straight from the record so each point traces to a note. This tool is the
fallback for numbers that live nowhere else, and its chart is labelled general.
