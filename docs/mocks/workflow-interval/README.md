# Workflow interval editor — GUI gate mocks

The Workflow surface (`AutomationsScreen`) lets the owner edit a **nightly
sweep's** schedule with the Tasks day/time/repeat editor, but **reconcilers**
(the sub-hourly sweeps in the "every few minutes" group) have no editor — they
are locked to their seeded interval. The backend already accepts a cadence
change: `PUT /api/ops/schedules/{id}` with `schedule_kind:"interval"` and a
positive `interval_seconds` (`ScheduleBody`, `api/ops.py`). This is the missing
**front end** for editing a reconciler's minute/hourly cadence.

These three mocks explore **how the owner enters a sub-day interval**. All three:

- Add an **Interval** mode to the editor and ungate the "Edit schedule" button
  for reconcilers (`isEditableSchedule`).
- Keep an **On demand** option (pause; run only via Run now).
- **Do not** offer daily/weekly wall-clock repeat for reconcilers — that would
  silently downgrade a 5-minute sweep to once a day. (Nightly sweeps keep the
  existing day/time editor; this is purely the reconciler path.)
- Show a live cadence preview and an **amber guard** when the gap reaches an
  hour or more (new items wait that long to be processed).
- Save as `{ schedule_kind:"interval", interval_seconds }`.

## The three options

- **A · Stepper** (`interval-a-stepper.html`) — a big −/＋ stepper over a
  curated ladder of intervals (1/2/3/5/10/15/20/30/45 min, 1/2/3/4/6/8/12 h)
  with a Minutes/Hours unit toggle. One-thumb, no keyboard, hard to enter a
  nonsense value; least precise for an arbitrary number.
- **B · Number + unit** (`interval-b-number-unit.html`) — a numeric field +
  a unit select ("Every [5] [minutes]"), with quick chips and inline
  validation. Most precise/flexible; needs the keyboard and a validity guard.
- **C · Preset chips** (`interval-c-preset-chips.html`) — a grid of common
  cadences (5/15/30 min, 1/6/12 h) plus a **Custom…** row that reveals a
  number+unit. Fastest for the common case, still allows arbitrary values;
  takes the most vertical space.

## Decision

The chosen mock becomes the binding spec and the others are removed (mock-first
discipline, `docs/DESIGN.md`). Implementation then follows the wave process
(`docs/PROCESS.md`).
