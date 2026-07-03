---
name: read_labs
version: 1
permission: read
params:
  type: object
  properties:
    analyte:
      type: string
      description: The lab test to read (e.g. "platelet count", "hemoglobin"). Omit to list recent results across all analytes.
    since:
      type: string
      description: ISO date; only results collected on or after it. Optional.
    until:
      type: string
      description: ISO date; only results collected on or before it. Optional.
    abnormal_only:
      type: boolean
      description: Only results flagged high, low, abnormal, or critical. Defaults to false.
    trend:
      type: boolean
      description: For a single analyte, return the full time-series oldest-to-newest to describe how a value moved. Defaults to false (most-recent first).
    limit:
      type: integer
      description: Maximum results (default 20).
  required: []
---
List the owner's lab results from their imported medical records — analyte, value with unit,
reference range, the abnormal/critical flag, collection date, the performing lab, and the id of
the encounter each draw belongs to — for one analyte or across all. Set trend:true with an analyte
to get the ordered time-series.

The ordering provider is NOT on the individual result; it belongs to the encounter the draw was
part of — pass encounter_id to read_encounters to see who ordered it. Many patient-portal labs
have NO enclosing encounter: encounter_id is then empty and there is simply no ordering provider on
record — say so rather than inventing one.

These are the readings the record CONTAINS, not a diagnosis or an assessment of what they mean.
Report the numbers, their reference ranges, and their flags; do not infer a cause, a condition, or
a recommendation — that is not what this record is for. Each result cites its source note id (pass
it to read_note for the full report).

A corrected result supersedes the earlier value for the same draw: superseded readings are marked
"corrected — see current" and carry the id of the reading that replaced them. When a result is
superseded, still pending review, or preliminary, say so plainly rather than presenting the number
as current. Only results the current session is scoped to see are returned; under a non-health scope
this tool returns nothing.
