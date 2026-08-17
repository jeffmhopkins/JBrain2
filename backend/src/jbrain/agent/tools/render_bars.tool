---
name: render_bars
version: 5
permission: read
params:
  type: object
  properties:
    title:
      type: string
      description: What the bars show (e.g. "Coffee shops visited by neighborhood"). Used as the label.
    unit:
      type: string
      description: What each value counts (e.g. "notes", "$", "count"). Optional.
    stacked:
      type: boolean
      description: For multi-series data, stack the series into one bar per category (totals) instead of side-by-side (comparison). Default false. Ignored for a single series.
    categories:
      type: array
      description: The category labels, in the order to display them — one bar (or bar group) per label.
      items:
        type: string
    series:
      type: array
      description: One or more named series. A single series draws one bar per category; several series draw grouped or stacked bars. Give at most six.
      items:
        type: object
        properties:
          name:
            type: string
            description: The series label, shown in the legend.
          values:
            type: array
            description: One number per category, in the same order as `categories` (a missing value counts as 0).
            items:
              type: number
        required: [name, values]
  required: [title, categories, series]
examples:
  - {title: "Notes by tag", unit: "notes", categories: ["work", "home", "health"], series: [{name: "count", values: [12, 7, 3]}]}
---
Render a categorical breakdown or ranking you have already gathered as an interactive bar graph
(the `bar_chart` view): one bar per category, with a Chart / Table / Stats card. Give the category
labels and one or more named series of numbers, one value per category. A single series draws one
bar each; several series draw grouped (side-by-side) or, with `stacked: true`, stacked bars.

Use this for "plot / graph / chart this by Y", "how many X by Y", "compare X across Y", "which Y has
the most X" — counts per month, notes by tag, visits by place, a this-vs-last comparison. When the
owner says "plot / graph / chart / visualize this data", use this or `render_chart`, never
`generate_image` (that draws a picture, not a data plot). Prefer `render_bars` over `render_chart`
when the x-axis is a set of categories rather than a time line; for a value trending over dates (one
or several lines) use `render_chart` — the time-series twin, which you hold alongside this one.

It renders exactly the numbers you pass, so this is a **general** artifact for figures you
assembled yourself — a tally you counted, a public breakdown you looked up. Those numbers must come
from something you actually read this turn; say where in your reply. The bar card the owner sees
shows every category and value, so do NOT list the bars back in prose — give the takeaway (the
ranking, the standout) and say where the numbers came from. Never use it to re-plot the owner's
recorded measurements or lab results: if the grounded chart/lab tools are in your tool list, use
those instead (they trace each value to a note); if they are not, you cannot read those records at
all — say so rather than plotting numbers from somewhere else. If there aren't at least two
categories (or two series), no graph is drawn and the tool says so rather than inventing one.
