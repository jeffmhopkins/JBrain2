# Workflow interval editor — GUI gate mocks

The Workflow surface (`AutomationsScreen`) lets the owner edit a **nightly
sweep's** schedule with the Tasks day/time/repeat editor, but **reconcilers**
(the sub-hourly sweeps in the "every few minutes" group) have no editor — they
are locked to their seeded interval. The backend already accepts a cadence
change: `PUT /api/ops/schedules/{id}` with `schedule_kind:"interval"` and a
positive `interval_seconds` (`ScheduleBody`, `api/ops.py`). This is the missing
**front end** for editing a reconciler's minute/hourly cadence.

**Decision (owner, GUI gate): B · Number + unit is the binding spec.** The
unchosen mocks (A stepper, C preset chips) were removed per mock-first
discipline; `interval-b-number-unit.html` is the spec implementation follows.

These mocks explored **how the owner enters a sub-day interval**. All shared:

- Add an **Interval** mode to the editor and ungate the "Edit schedule" button
  for reconcilers (`isEditableSchedule`).
- Keep an **On demand** option (pause; run only via Run now).
- **Do not** offer daily/weekly wall-clock repeat for reconcilers — that would
  silently downgrade a 5-minute sweep to once a day. (Nightly sweeps keep the
  existing day/time editor; this is purely the reconciler path.)
- Show a live cadence preview and an **amber guard** when the gap reaches an
  hour or more (new items wait that long to be processed).
- Save as `{ schedule_kind:"interval", interval_seconds }`.

## The chosen option

- **B · Number + unit** (`interval-b-number-unit.html`, chosen) — a numeric
  field + a unit select ("Every [5] [minutes]"), with quick chips and inline
  validation. Most precise/flexible; needs the keyboard and a validity guard.

Considered and rejected: A · Stepper (curated −/＋ ladder; least precise for an
arbitrary value) and C · Preset chips (grid + Custom…; most vertical space).

## Implementation notes (binding)

- Ungate `isEditableSchedule` so reconcilers (`group === "reconcile"`) get the
  "Edit schedule" button, alongside the existing nightly sweeps.
- The reconciler editor offers **Interval** and **On demand** only — never the
  daily/weekly wall-clock repeat (that would downgrade a sub-day sweep). Nightly
  sweeps keep their existing on_demand/once/repeat editor unchanged.
- Interval saves as `{ schedule_kind:"interval", interval_seconds }` (minutes×60
  or hours×3600); reject a non-positive / non-integer value before save.
- Implementation follows the wave process (`docs/reference/PROCESS.md`).
