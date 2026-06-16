# Track G · Temporal & recurrence modeling

**Status:** Phase-1 fan-out brief (greenfield, first-principles). Designs the *ideal*
temporal model for facts, then reconciles against §4 invariants. No implementation is
read or patched; concrete schemas are strawman targets for synthesis.

**Scope of this track:** the *time* dimension of a fact — bitemporal validity, date
*precision*, the "former / ended / ongoing / current" semantics, *recurrence* (rrule-style
with exceptions/overrides), how a human *edits time* soundly, how vague/unknown dates are
represented **without inventing data and without placeholder glyphs**, and how temporal
state drives **current-value** computation and interacts with **supersession**.

Out of scope (consumed, not designed here): the full fact IR (Track A), storage/graph
mechanics (Track B), the edit-operation algebra surface (Track C), prompt elicitation
(Track D), review UX (Track E), RLS (Track F). This brief defines the *temporal slice* of
the shared contract and the temporal subset of the edit algebra, with explicit seams to
those tracks.

---

## 0. External grounding (best practice the design leans on)

- **Bitemporal modeling** — two independent timelines: **valid time** (when a fact is true
  in the world) and **transaction/reported time** (when the system learned/recorded it).
  Standard guidance: never derive business time from system time; keep both as first-class
  half-open intervals; system-time columns participate in history-table uniqueness.
  ([Valid time](https://en.wikipedia.org/wiki/Valid_time),
  [Transaction time](https://en.wikipedia.org/wiki/Transaction_time),
  [IBM DB2 temporal best practices](https://public.dhe.ibm.com/ps/products/db2/info/devWorks_pdfs/Best_practices/DB2BP_Temporal_Data_Management_1012.pdf))
- **Allen's interval algebra** — 13 jointly-exhaustive, pairwise-disjoint relations between
  two intervals (precedes/meets/overlaps/starts/during/finishes/equals + converses). The
  vocabulary for classifying *temporal conflicts* between competing facts and for deciding
  supersession vs. coexistence.
  ([Allen's interval algebra](https://en.wikipedia.org/wiki/Allen%27s_interval_algebra))
- **iCalendar RRULE / RFC 5545** — recurrence as `RRULE` (FREQ/INTERVAL/UNTIL/COUNT/BYxxx/
  WKST), `RDATE` (extra instances), `EXDATE` (excluded instances), and `RECURRENCE-ID`
  (a per-instance override). The recurrence set = (RRULE ∪ RDATE) − EXDATE, with overrides
  keyed by RECURRENCE-ID. We adopt this model verbatim rather than inventing one.
  ([RFC 5545](https://datatracker.ietf.org/doc/html/rfc5545),
  [RRULE 3.3.10](https://icalendar.org/iCalendar-RFC-5545/3-3-10-recurrence-rule.html),
  [EXDATE 3.8.5.1](https://icalendar.org/iCalendar-RFC-5545/3-8-5-1-exception-date-times.html),
  [RDATE 3.8.5.2](https://icalendar.org/iCalendar-RFC-5545/3-8-5-2-recurrence-date-times.html))
- **EDTF (ISO 8601-2 Extended Date/Time Format, Level 1)** — a *string* grammar for
  uncertain/approximate/unspecified dates: `?` uncertain, `~` approximate, `%` both,
  `X` unspecified digit (`19XX`, `1999-01-XX`), open/unknown interval ends (`/..`, `../`),
  and reduced precision by truncation (`2026`, `2026-06`). We borrow the *semantics* and a
  small subset of the grammar — but store **structured fields**, not raw EDTF strings (see
  §3, §10-R4).
  ([EDTF Level 1, LoC](https://id.loc.gov/datatypes/edtf/EDTF-level1.html),
  [LoC EDTF](https://www.loc.gov/standards/datetime/edtf.html))

---

## 1. Proposal (the headline)

**A fact carries a single, self-describing `temporal` object** with three orthogonal
concerns, plus the bitemporal envelope:

1. **Validity** — a *half-open valid-time interval* `[valid_from, valid_to)` over the world
   timeline, where each endpoint is a **structured instant** (value + precision + certainty),
   and either endpoint may be **open** (unbounded) or **unknown** (bounded-but-not-known).
   These two open/unknown notions are *different* and the design keeps them distinct.
2. **Lifecycle status** — a *derived-but-storable* enum `{ ongoing, current, ended, former,
   scheduled, unknown }` computed from the interval + "now", with an explicit
   **`ended_without_date`** capability so "she no longer works there, date unknown" is
   first-class (a closed-but-unknown upper bound), never a fabricated date.
3. **Recurrence** — an optional RFC-5545 recurrence (`rrule` + `rdates` + `exdates` +
   per-instance `overrides`) attached to event/appointment-kind facts. The base
   `[valid_from, valid_to)` then describes the **recurrence window**, and the recurrence
   expands instances *lazily* (never materialized as N facts).

**Bitemporal envelope** (orthogonal to the above, lives on the storage row not the value):
`reported_at` (when the source asserted it / note timestamp), `recorded_at` (system
transaction time), and a transaction-time interval `[tx_from, tx_to)` for the row version.
Valid-time edits create **new tx versions**; nothing is destructively overwritten.

**Five design commitments that fall out of this:**

- **C1 — Precision is a property of an *instant*, not of the fact.** `valid_from` can be
  day-precise while `valid_to` is year-precise. Precision is `{ instant, day, month, year,
  decade, era, unknown }`.
- **C2 — "Unknown bound" ≠ "open bound" ≠ "missing field".** Three distinct states, three
  distinct renderings, zero placeholder glyphs (§3).
- **C3 — Current-value is *computed* from temporal state + supersession lattice**, not stored
  as a boolean flag that can drift. A materialized `is_current` may be cached but is always
  re-derivable (§4).
- **C4 — Recurrence is rrule-native and lazy.** Exceptions/overrides follow RFC 5545
  exactly; we never expand a daily medication into 3650 fact rows.
- **C5 — Every temporal edit is a typed, reversible operation** over the temporal object
  (set/clear bound, set precision, mark former, set/clear recurrence, add/remove exception),
  audited with provenance, applied machine-side to preserve doctrine #7 (§5).

---

## 2. Concrete temporal & recurrence schema

### 2.1 The `temporal` value object (JSON, lives in the fact IR / value payload)

```jsonc
{
  "temporal": {
    "schema_version": "g-temporal/1",

    // ---- VALIDITY (valid-time, half-open [from, to) ) ----
    "valid_from": {
      "instant": "2019-09-01",   // ISO 8601, truncated to its precision; null if unbounded/unknown
      "precision": "month",      // instant|day|month|year|decade|era|unknown
      "certainty": "asserted",   // asserted|approximate|uncertain|inferred
      "bound": "closed"          // closed | open | unknown   (see §2.2)
    },
    "valid_to": {
      "instant": null,
      "precision": "unknown",
      "certainty": "asserted",
      "bound": "open"            // ongoing: no end
    },

    // ---- LIFECYCLE (derived; stored as cache + reason) ----
    "status": "ongoing",         // ongoing|current|ended|former|scheduled|unknown (see §2.4)
    "status_reason": "valid_to.bound=open && valid_from<=now",

    // ---- RECURRENCE (optional; RFC 5545) ----
    "recurrence": null           // see §2.3
  }
}
```

For a **closed-but-unknown end** ("former employer, left date unknown"):

```jsonc
"valid_to": { "instant": null, "precision": "unknown", "certainty": "asserted", "bound": "unknown" }
// status => "former". The interval IS closed (the fact is no longer true) but the
// endpoint instant is genuinely not known. This is the "ended without an end date" case.
```

### 2.2 The `bound` trichotomy (the heart of C2)

| `bound`   | Meaning                                   | `instant`        | Renders as (example)                |
|-----------|-------------------------------------------|------------------|-------------------------------------|
| `closed`  | endpoint is known                         | a date string    | "since Sep 2019" / "until 2021"     |
| `open`    | endpoint genuinely does not exist (yet)   | `null`           | "since 2019" (no end) / "ongoing"   |
| `unknown` | endpoint exists but value is not known    | `null`           | "former" / "started, date unknown"  |

This is the single most important modeling decision in the track. Conflating these three is
exactly what produces fabricated dates and the hated `— → 2026` rendering. See §3 and §6.

### 2.3 Recurrence object (RFC 5545 subset, normalized to JSON)

```jsonc
"recurrence": {
  "rrule": "FREQ=WEEKLY;BYDAY=TU,TH;INTERVAL=1;UNTIL=2026-12-31",
  "dtstart": "2026-01-06",          // anchor instant for the rule (the "first" occurrence)
  "rdates":  ["2026-07-04"],        // extra one-off instances (union)
  "exdates": ["2026-09-08"],        // excluded instances (subtraction; takes precedence)
  "overrides": [                    // per-instance edits, keyed by recurrence_id (RFC RECURRENCE-ID)
    {
      "recurrence_id": "2026-03-17",      // which instance this overrides (original instant)
      "patch": {                          // sparse: only changed temporal/value fields
        "valid_from": { "instant": "2026-03-18", "precision": "day", "bound": "closed" },
        "note": "moved to Wed that week"
      }
    }
  ],
  "tz": "America/Los_Angeles",      // IANA tz; null => floating/date-only
  "count_cap": 730                  // safety cap for expansion (see §10-R6)
}
```

The realized instance set for a window `[a, b)` is
`expand(rrule, dtstart, window=[a,b)) ∪ rdates) − exdates`, then each surviving instant is
patched by any matching `override`. **Lazy:** expansion happens at query time for a bounded
window only; we never persist the expansion.

### 2.4 `status` derivation (pure function of temporal + now)

```
status(temporal, now):
  if recurrence != null:                      -> "recurring"   (further classified per-instance)
  vf, vt = valid_from, valid_to
  if vf.bound==closed and vf.instant > now:    -> "scheduled"   (future-dated; not yet live)
  if vt.bound==open:                            -> "ongoing"     (live, no end)
  if vt.bound==unknown:                         -> "former"      (ended, end date unknown)
  if vt.bound==closed and vt.instant > now:     -> "current"     (live, has a future end)
  if vt.bound==closed and vt.instant <= now:    -> "ended"       (past, end date known)
  if vf.bound==unknown and vt.bound==open:      -> "ongoing"     (start unknown, still true)
  else                                          -> "unknown"
```

`current` vs `ongoing`: **ongoing** = no end exists; **current** = an end exists but is in
the future (e.g. a lease running until 2027). Both are "live now". `former` and `ended` are
both "no longer true" — they differ only in whether the end *date* is known.

### 2.5 Postgres columns (storage projection — Track B owns the table, this is the temporal slice)

```sql
-- On the fact-version row (bitemporal: one row per (fact, tx-version)):

-- valid-time, structured per endpoint
valid_from_instant     timestamptz,                 -- nullable; truncated-to-precision lower bound
valid_from_precision   temporal_precision NOT NULL, -- enum: instant|day|month|year|decade|era|unknown
valid_from_certainty   temporal_certainty NOT NULL DEFAULT 'asserted',
valid_from_bound       temporal_bound     NOT NULL,  -- enum: closed|open|unknown

valid_to_instant       timestamptz,
valid_to_precision     temporal_precision NOT NULL,
valid_to_certainty     temporal_certainty NOT NULL DEFAULT 'asserted',
valid_to_bound         temporal_bound     NOT NULL,

-- a Postgres range used ONLY for indexing/overlap queries; derived, never authoritative.
-- open/unknown upper bound both map to "no upper bound" here; the bound enums disambiguate.
valid_range            tstzrange GENERATED ALWAYS AS (
                         tstzrange(
                           valid_from_instant,
                           CASE WHEN valid_to_bound='closed' THEN valid_to_instant ELSE NULL END,
                           '[)')
                       ) STORED,

-- lifecycle cache (always re-derivable from the four bound/instant pairs + now)
status                 temporal_status,             -- cache; recomputed on read-as-of or write
status_recomputed_at   timestamptz,

-- recurrence (NULL for non-recurring). JSONB keeps the rrule/rdates/exdates/overrides blob.
recurrence             jsonb,                       -- validated against the §2.3 shape; NULL or well-formed

-- bitemporal envelope (transaction time + reported time)
reported_at            timestamptz NOT NULL,        -- when the SOURCE asserted it (note time)
recorded_at            timestamptz NOT NULL DEFAULT now(),
tx_from                timestamptz NOT NULL DEFAULT now(),
tx_to                  timestamptz NOT NULL DEFAULT 'infinity',  -- closed when superseded by a new version

-- indexes
-- GiST on valid_range for Allen-relation / overlap queries (§6)
-- partial index WHERE tx_to='infinity' for "current version" reads
-- index on (subject, predicate, status) WHERE tx_to='infinity' for current-value (§4)
```

`temporal_precision`, `temporal_certainty`, `temporal_bound`, `temporal_status` are Postgres
enums (new types → new RLS isolation test obligation per §4 invariant, even though they're
type definitions; the table they sit on carries the test).

---

## 3. The vague / unknown-date representation rule (binding)

**Rule G-VAGUE.** *We store what is known at the precision it is known, mark the rest, and
let rendering choose words — never glyphs.*

Three sub-rules:

- **G-VAGUE-1 (precision, not padding).** A date known only to the year is stored as
  `instant="2019", precision="year"`. We **do not** pad to `2019-01-01`. The value's
  precision is authoritative; comparisons and rendering both honor it. ("June 2019" →
  `2019-06`, `precision=month`.)
- **G-VAGUE-2 (bound trichotomy, §2.2).** "No end" (`open`), "end exists but unknown"
  (`unknown`), and "endpoint known" (`closed`) are three states. An unknown endpoint stores
  `instant=null` and is **never** invented, defaulted to "now", or copied from another field.
- **G-VAGUE-3 (words, not placeholders).** Rendering maps temporal state to **prose**, and
  there is **no glyph fallback** (`—`, `?`, blank arrows). The user's specific complaint —
  `— → 2026` — is structurally impossible because an open/unknown lower bound never produces
  a dash-arrow; it produces a word.

**Rendering table (illustrative, owned downstream but constrained here):**

| temporal state                                                   | render                          |
|------------------------------------------------------------------|---------------------------------|
| from=closed(2019-09 month), to=open                              | "since September 2019"          |
| from=closed(2019 year), to=open                                  | "since 2019"                    |
| from=open/unknown, to=open                                       | "ongoing"                       |
| from=closed(2019), to=unknown                                    | "former (since 2019)"           |
| from=unknown, to=closed(2021)                                    | "until 2021"                    |
| from=closed(2019), to=closed(2021)                               | "2019–2021"                     |
| from=unknown, to=unknown, but status=former                      | "former" / "no longer"          |
| precision=era ("childhood")                                      | "in childhood" / "as a child"   |
| precision=unknown, no bounds                                     | (omit the time clause entirely) |

If *nothing* temporal is known, the time clause is **omitted**, not rendered as empty. A fact
with no useful temporal information renders as a plain present-tense statement.

**Approximate vs uncertain (EDTF-borrowed).** `certainty=approximate` ("around 2015")
renders "around 2015"; `certainty=uncertain` ("maybe 2015") renders "possibly 2015";
`certainty=inferred` ("must have been after X") renders "by inference, …" and is flagged for
review. None of these change the stored `instant`/`precision`; they color the rendering and
gate confidence.

---

## 4. Current-value & supersession interaction

**Definition.** The **current value** of a (subject, predicate) — for a *functional*
(single-valued) predicate — is the fact version that is (a) `tx_to = infinity` (latest
recorded version), (b) **valid now** per its temporal state, and (c) **not superseded** by a
later-validity fact in the supersession lattice. For a **set-valued** predicate, "current
value" is the *set* of all such facts (no supersession collapse; see Track C / wishlist §9).

**Current-value algorithm (functional predicate):**

```
current(subject, predicate, now):
  candidates = facts WHERE subject, predicate, tx_to='infinity', status in {ongoing,current,scheduled?}
  live = [f for f in candidates if valid_now(f, now)]      # ongoing OR current OR (scheduled & policy)
  if live is empty: return None                            # nothing true now (all former/ended/future)
  # supersession: among live facts, the one with the latest known valid_from wins;
  # ties broken by reported_at (later report wins), then confidence, then recorded_at.
  return argmax(live, key=(valid_from_sortkey, reported_at, confidence, recorded_at))
```

**`valid_now`** must respect precision and the bound trichotomy:
- `from.bound=closed`: live requires `from.instant <= now` *at from's precision*
  (a year-precise `2026` is "started" once we are anywhere in 2026 — or, by policy, at the
  *start* of the precision window; we choose **start-of-window for from, end-of-window for
  to**, the most inclusive reading, and surface the ambiguity to review when it matters).
- `to.bound=open`: always still-live.
- `to.bound=unknown`: **not** live (it ended; we just don't know when) → `former` is excluded
  from current-value. This is the subtle, important rule: *a fact that became former without
  an end date is correctly excluded from "what's true now," with no fabricated date.*
- `to.bound=closed`: live iff `to.instant > now` (end-of-precision-window for `to`).

**Supersession ≠ deletion.** Superseding writes a *new fact version* and, where the old fact
should be retired, sets the old fact's `valid_to.bound`:
- If the new fact provides an explicit start (`new.valid_from`), the old fact's `valid_to`
  becomes `closed(new.valid_from)` — **abutment** (Allen `meets`): "lived in Austin until
  2021, then Denver from 2021."
- If the new fact's start is unknown, the old fact's `valid_to.bound` becomes `unknown`
  ("former") — we do **not** fabricate a boundary.
- The superseded version retains `tx_to` history; nothing is destroyed. Reopen/undo restores
  the prior `valid_to`.

**Interaction summary:** supersession edits **valid-time** of the *prior* fact (closing or
marking-former its interval) and creates a *new tx version* of the new fact. Both timelines
move; current-value is then a pure read over the post-edit lattice. Because `is_current` is
derived (C3), there is no flag to forget to flip.

---

## 5. Human-edit semantics for time (the typed operations)

All temporal edits are **typed operations** over the `temporal` object, applied
**machine-side** as audited correction operations (this is the doctrine-#7 reconciliation
for the temporal slice: the human expresses *intent* via a structured op; the system mutates
the record; the wiki re-renders — humans never hand-edit prose or raw dates). Each op is
reversible (stores the pre-image) and provenance-stamped.

| Op                        | Args                                              | Effect                                                                 | Reverse |
|---------------------------|---------------------------------------------------|------------------------------------------------------------------------|---------|
| `set_bound`               | endpoint∈{from,to}, instant, precision, certainty | sets that endpoint to `closed` with given value                        | restore pre-image |
| `clear_bound`            | endpoint, mode∈{open,unknown}                     | sets `instant=null`, `bound=open|unknown` (the §2.2 trichotomy)        | restore pre-image |
| `set_precision`           | endpoint, precision                               | re-precision *without* changing the stored instant's known digits      | restore pre-image |
| `mark_former`             | (optional end instant)                            | if end given → `set_bound(to, …)`; else → `clear_bound(to, unknown)`   | restore pre-image |
| `mark_ongoing`            | —                                                 | `clear_bound(to, open)` (un-former: it's true again / never ended)     | restore pre-image |
| `set_recurrence`          | rrule, dtstart, tz                                | attaches/replaces the recurrence object                                | restore pre-image |
| `clear_recurrence`        | —                                                 | removes recurrence (fact becomes a single interval)                    | restore pre-image |
| `add_exception`           | recurrence_id (instant)                           | append to `exdates`                                                     | remove it |
| `add_extra_occurrence`    | instant                                           | append to `rdates`                                                      | remove it |
| `override_occurrence`     | recurrence_id, patch                              | append/replace an `overrides[]` entry                                   | restore prior override |
| `correct_reported_time`   | reported_at                                       | fixes the bitemporal *reported* timestamp (e.g. backdated note)        | restore pre-image |

**Soundness rules enforced on every op (deterministic backstops, Track D-adjacent):**

- **S1 — ordering:** if both bounds `closed`, require `from.instant <= to.instant` at the
  coarser of the two precisions; reject otherwise with a precise error (no silent swap).
- **S2 — no fabrication:** `mark_former` with no date **must** produce `bound=unknown`, never
  a default like "now". Setting an instant requires the human to actually supply digits.
- **S3 — precision monotonicity on re-precision:** `set_precision` may *coarsen* freely
  (drop digits) but *refining* (year→day) requires new digits — it cannot invent them.
- **S4 — bitemporal immutability:** editing valid-time **never** rewrites a prior tx version;
  it appends a new version (`tx_to` of the old set to `now()`, new row with `tx_from=now()`).
  `reported_at`/`recorded_at` are corrected only via `correct_reported_time`, itself versioned.
- **S5 — recurrence/interval coherence:** `set_recurrence` requires `valid_from` (the window
  start) to be `closed`; `UNTIL`/`COUNT` in the rrule must agree with `valid_to` (the window
  end) or the op normalizes one from the other and flags it.
- **S6 — firewall safety (defer to Track F):** a temporal edit alone never moves domain; but
  if a `correct_reported_time` or override carries a provenance span, that span must stay in
  the fact's domain. Noted as a seam, not solved here.

**Edit affordances map directly to wishlist §2.5:** set/clear `valid_from`/`valid_to`
(`set_bound`/`clear_bound`), mark former/ended/ongoing (`mark_former`/`mark_ongoing`),
precision (`set_precision`), recurrence (`set_recurrence`/exceptions), reported/captured time
(`correct_reported_time`). Every §2.5 capability has exactly one op.

---

## 6. Edge-case walkthrough

**E1 — Open interval, no end ("works at Acme").**
`from=closed(2019-09,month), to=open`. status=`ongoing`. Renders "since September 2019".
Counts as current-value. No dash, no fabricated end. ✔

**E2 — Former without an end date ("used to work at Acme").**
`from=closed(2019,year), to=unknown`. status=`former`. `valid_now=false` → **excluded** from
current-value. Renders "former (since 2019)". The classic failure (inventing an end date or
printing `2019 → —`) is structurally prevented: `unknown` bound carries `instant=null` and a
word-rendering. ✔

**E3 — Era precision ("learned to swim as a child").**
`from={instant:null or a coarse decade, precision:era, bound:closed-ish}`. We model `era` as
a *named coarse window* (childhood/teens/"the 90s") with optional decade anchor. Comparisons
treat it as a wide interval; rendering says "as a child". For Allen relations, an era
interval is wide and usually `during`-relates other facts. ✔

**E4 — Conflicting intervals (Allen).** Two facts for a functional predicate
(home = Austin `[2018,2021)`, home = Denver `[2020,open)`). They **overlap** (Allen
`overlaps`), which is illegal for a functional predicate. Resolution policy:
(a) detect via `valid_range` GiST overlap on (subject, functional-predicate);
(b) classify the Allen relation; (c) for `overlaps`/`during`/`equals` raise a **temporal
conflict review item** (not auto-resolve); for `meets`/`before` coexist as a timeline. The
human resolves via `set_bound`/`mark_former` (e.g. close Austin at 2020). Allen relations
become the *vocabulary of the conflict explanation* shown in review. ✔

**E5 — Recurring + exception ("PT every Tue/Thu, skip Sep 8, moved Mar 17→18").**
One fact, `recurrence` per §2.3: `rrule=FREQ=WEEKLY;BYDAY=TU,TH`, `exdates=[2026-09-08]`,
`overrides=[{recurrence_id:2026-03-17, patch:{valid_from:2026-03-18}}]`. Query a week window →
expand → subtract exdate → apply override. Never 100s of rows. Editing one instance =
`override_occurrence`; cancelling one = `add_exception`; adding a bonus session = `rdates`. ✔

**E6 — Scheduled / future fact ("dentist on 2026-08-01").**
`from=closed(2026-08-01,day), to=closed(2026-08-01,day)` (instant-precision appointment),
status=`scheduled` (from > now). Excluded from "true now" but listed in upcoming. After the
date passes, status recomputes to `ended`. ✔

**E7 — Backdated note (bitemporal divergence).** A note written 2026-06 says "I moved in
2021." `valid_from=closed(2021)`, `reported_at=2026-06`, `recorded_at=now`. Asking "what did
we believe in 2025?" reads tx-versions with `tx_from <= 2025`; this version (recorded 2026)
is correctly *absent* — the bitemporal split answers it. ✔

**E8 — Correction supersedes ("actually it was 2020 not 2021").** `set_bound(from, 2020)`
appends a new tx version; the 2021 version keeps `tx_to=now()` and is reversible. Valid-time
changed, transaction-time recorded the correction, audit intact. ✔

**E9 — Re-precision both ways.** "It was the 90s" → later "actually 1994": `set_precision`
can't refine without digits, so the human supplies `set_bound(from, 1994, day?)` — coarse→
fine requires new data (S3). Conversely "1994" → "sometime in the 90s" coarsens freely. ✔

**E10 — Set-valued, no supersession ("has worked at Acme and Globex").** Both facts live
(`open` ends) and **coexist**; current-value returns the *set*. Adding Globex is `add`, not a
supersede-overwrite — the temporal model defers cardinality to Track C but provides the
"both ongoing" representation that makes coexistence correct. ✔

**E11 — Recurrence ends ("PT through Dec 2026").** rrule `UNTIL=2026-12-31` must equal
`valid_to=closed(2026-12-31)` (S5). After Dec 2026 the whole fact's status → `ended`; no
further instances expand. ✔

---

## 7. Reconciliation with §4 invariants

- **Bitemporal model** — *central* to the design: valid-time interval + `reported_at` +
  `recorded_at`/`tx_[from,to)` are all first-class and independent. ✔
- **Audit & reversibility** — every temporal op stores a pre-image and appends a tx version;
  reopen/undo restores. `is_current` is derived so there's no hidden mutable flag. ✔
- **Storage abstraction / LLM-adapter** — temporal validators are deterministic (no LLM in
  the edit path); extraction emits the `temporal` object *through* the IR/adapter (Track A/D).
  No raw-path or SDK use introduced. ✔
- **RLS firewalls** — temporal edits don't move domain; the new enums sit on a domained table
  whose RLS isolation test must cover temporal columns. Provenance on `correct_reported_time`
  must stay in-domain (seam to Track F). ✔
- **Machine-written wiki (#7)** — humans never edit dates as prose; they emit typed temporal
  ops that the machine applies and the wiki re-renders. The doctrine holds with **no change
  needed** for the temporal slice (cf. Track C's broader argument). ✔

---

## 8. Risks & open questions (for the red-team)

**R1 (Sev-2 candidate) — precision-aware comparison semantics.** "Is `2019` < `2019-06`?"
and the start-of-window/end-of-window convention (§4) are subtle; a wrong convention silently
mis-classifies current-value at year boundaries. *Open:* lock the convention (proposed:
from→start-of-window, to→end-of-window) and write exhaustive boundary tests. Does any
predicate need the opposite?

**R2 (Sev-2) — `former`/`unknown-end` excluded from current-value.** Correct, but if
extraction over-uses `mark_former`/`unknown` it could wrongly drop live facts. *Open:* what's
the default when prose is ambiguous ("worked at Acme" past tense) — `ended`(when?) vs
`former`(unknown) vs `ongoing`? Proposed default: tense → `former/unknown` only on explicit
past-tense cessation cues, else `ongoing`; surface low-confidence to review.

**R3 (Sev-2) — recurrence expansion cost & unbounded rrules.** A `FREQ=DAILY` with no
`UNTIL/COUNT` over an open window could explode. Mitigation: `count_cap`, mandatory bounded
query windows, reject unbounded high-frequency rules without a cap. *Open:* is lazy expansion
fast enough for "show this month" across many recurring facts? Index strategy?

**R4 (Sev-3) — store structured vs. EDTF string.** We chose structured fields (queryable,
RLS-friendly) over raw EDTF strings (compact, standard). *Open:* do we need a *lossless* EDTF
round-trip for import/export? Proposed: derive EDTF on output, parse on input, but persist
structured.

**R5 (Sev-2) — bound trichotomy comprehension.** Will the **LLM** reliably distinguish
`open` from `unknown` at extraction (the whole anti-fabrication thesis rests on it)? *Open:*
Track D must prove the model picks `unknown` (not a guessed date, not `open`) for "used to."
Backstop: default ambiguous cessation to `unknown` (safe: excluded from current-value, never
fabricates).

**R6 (Sev-2) — Allen conflict auto-resolution boundary.** Which Allen relations may
auto-resolve (`meets`→abut) vs. must-review (`overlaps`/`during`)? Aggressive auto-abutment
could fabricate a boundary. Proposed: only `meets`/`before` coexist silently; everything else
reviews. *Open:* validate against real overlap cases.

**R7 (Sev-3) — recurring + bitemporal + override interaction.** An override edits one
instance's valid-time; does that spawn a tx version of the *whole* recurring fact or a
sub-version? Proposed: the recurrence object is part of the fact value, so any override = a
new tx version of the fact (coarse but simple & reversible). *Open:* is per-instance tx
history ever needed?

**R8 (Sev-2) — timezone & floating dates.** Day/month/year-precise facts are usually
*floating* (no tz); appointments are zoned. Mixing them in `tstzrange` (which needs a tz)
risks off-by-one at boundaries. Proposed: store floating dates at UTC-midnight *with* a
`floating` flag and compare at precision granularity, not instant. *Open:* confirm DST/tz
edge handling for recurrence (RRULE UNTIL is UTC).

---

## 9. Seams to other tracks (explicit handoffs)

- **Track A (IR):** the `temporal` object is a sub-object of the fact value; A owns its
  placement and the value-typing around it.
- **Track B (storage):** owns the fact-version table; this brief supplies the temporal
  columns, enums, `valid_range` generated column, and index obligations.
- **Track C (corrections):** the §5 ops are the *temporal subset* of C's edit algebra; C
  owns reversibility/audit framing and the functional-vs-set distinction that current-value
  (§4) depends on.
- **Track D (prompt):** must elicit the bound trichotomy and precision reliably (R2, R5) and
  supply deterministic backstops S1–S5.
- **Track F (RLS):** owns the isolation test for the temporal columns and the provenance-
  domain check on `correct_reported_time` (S6).

---

## 10. Summary of binding decisions

1. **`temporal` object** = structured `valid_from`/`valid_to` endpoints (instant + precision
   + certainty + **bound trichotomy**) + derived `status` + optional RFC-5545 `recurrence` +
   bitemporal envelope (`reported_at`/`recorded_at`/`tx_[from,to)`).
2. **Bound trichotomy** (`closed`/`open`/`unknown`) is the core anti-fabrication mechanism;
   `unknown` end = "former without a date," excluded from current-value, rendered as a word.
3. **Precision per endpoint**, stored at known granularity, never padded; rendering is prose-
   only (no `—`/glyphs) — directly killing the `— → 2026` complaint.
4. **Current-value is derived** from temporal state + supersession lattice; supersession edits
   the prior fact's valid-time (abut or mark-former) and creates a new tx version.
5. **Recurrence is rrule-native and lazy**, with RDATE/EXDATE/RECURRENCE-ID overrides.
6. **All temporal edits are typed, reversible, machine-applied ops** (§5), preserving doctrine
   #7 for the temporal slice with no doctrine change.
