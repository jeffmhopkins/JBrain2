---
name: read_encounters
version: 1
permission: read
params:
  type: object
  properties:
    encounter_id:
      type: string
      description: Expand one encounter in full — its providers, coded diagnoses, transfusion orders, and the transfer chain. Omit to list all admissions and visits.
    since:
      type: string
      description: ISO date; only encounters admitted on or after it. Optional.
    until:
      type: string
      description: ISO date; only encounters admitted on or before it. Optional.
    limit:
      type: integer
      description: Maximum encounters when listing (default 20).
  required: []
---
List or expand the owner's hospital admissions and clinical visits from their imported medical
records — class (inpatient / emergency / ambulatory / observation), facility, care unit, admit and
discharge dates, the derived length of stay, and discharge disposition. Pass an encounter_id to
expand one in full: the providers and their roles, the ICD-coded diagnoses, transfusion orders, and
the transfer chain — a stay that moved facilities reads as one continuous hospitalization.

This is the record of what happened and when — dates, places, people, coded diagnoses as they
appear in the source. Report them as written; leave any medical meaning to the owner and their
clinicians. Do not infer a diagnosis, a severity, or a recommendation from an admission.

Each encounter cites its source note id (pass it to read_note for the full report). Read-only:
encounters are projected from the owner's notes, so you don't write here. Only encounters the
current session is scoped to see are returned; under a non-health scope this tool returns nothing.
