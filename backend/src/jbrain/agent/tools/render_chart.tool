---
name: render_chart
version: 2
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
      description: A SINGLE line to plot, one object per point. Use this OR `series`, not both.
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
    series:
      type: array
      description: SEVERAL named lines to overlay on one chart, one per object — use this instead of `points` when the data has more than one line (e.g. launches per month split by vehicle). Give at most six.
      items:
        type: object
        properties:
          name:
            type: string
            description: The line's label, shown in the legend.
          points:
            type: array
            description: This line's points, one per object, oldest to newest.
            items:
              type: object
              properties:
                x:
                  type: string
                  description: The point's date — an ISO date or epoch milliseconds.
                y:
                  type: number
                  description: The value at that date.
              required: [x, y]
        required: [name, points]
  required: [title]
---
Plot dated numbers you have already gathered as an interactive, zoomable time-series (the `chart`
view). This is the tool for **"plot / graph / chart this over time"** when the x-axis is a date or
time. Give a single line via `points` (each a date `x` and a number `y`, oldest to newest), or
**several lines** via `series` — one `{name, points}` per line — to overlay them on one chart with a
legend (e.g. a metric split by category, this-year-vs-last, launches per month by vehicle). Need at
least two points overall.

When the x-axis is a set of **categories** rather than a time line (counts by tag, a ranking, a
this-vs-that breakdown), reach for `render_bars` instead — a bar graph. Either way, when the owner
says "plot / graph / chart / visualize this data", use one of these chart tools; do **not** use
`generate_image` (that draws a picture, not a data plot).

Use `render_chart` only for **general** figures you assembled yourself — a count you tallied, a
public series you looked up, a projection. It renders exactly the numbers you pass, so those numbers
must come from something you actually read this turn; state where they came from in your reply. Do
NOT use it to re-plot the owner's recorded measurements or lab results — those have grounded, cited
tools (`chart_measurements` for a tracked measurement, `read_labs` with trend:true for a lab
analyte) that plot straight from the record so each point traces to a note.
