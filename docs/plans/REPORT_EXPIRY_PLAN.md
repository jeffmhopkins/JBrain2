# Report Expiry (TTL) & Per-Run Dedup Keys

> **Status:** In progress · **Last verified:** 2026-08-09 · **Waves:** W1✅ W2✅ W3✅ W4◻️(deferred, GUI-gated)

The research library (`app.research_reports`, migration 0140) keeps every persisted
report forever. That is right for a candidate profile you'll revisit, but wrong for a
`daily_news` briefing (REPORT_PRESET_PLAN.md) — a throwaway you want to hear on the
drive in and forget by next week. This plan adds a **general, opt-in expiry (TTL)** to
any report, and fixes the **dedup collision** that would otherwise stop a fixed-question
daily preset from keeping more than one day at a time.

The two are coupled: retention is pointless if every day's `daily_news` run overwrites
the last. Both are needed to "keep the last 7 days of briefings, then auto-clean".

## Background — the two mechanisms this rides on

- **Dedup key.** `persist_report` (`external/research_corpus.py`) upserts on
  `(question_hash, tool)` (migration 0148), where `question_hash =
  sha256(lower(collapse_ws(question)))`. A re-run of the *same question by the same
  tool* replaces the older row (`created_at = now()`, newest wins). So a preset with a
  **fixed** `question` (today's `daily_news`) can only ever hold ONE row — every morning
  clobbers yesterday. Making each day's question distinct (a date in it) yields one row
  per day.
- **Nightly sweeps.** The workflow scheduler (`workflow/scheduler.py`) already runs
  in-code maintenance actions (`PURGE_ACTION`, the reconcilers, `GEOFENCE_SWEEP_ACTION`)
  as `ActionSpec`s composed into the worker registry, fired by a migration-seeded
  schedule → trigger → pipeline under `queue.SYSTEM_CTX`, each returning a work-count so
  an idle fire's run is reaped (`REAPABLE_IDLE_SWEEPS`). Report expiry is one more such
  sweep — no new execution machinery.

## Design decisions

1. **Retention is a preset field, and (stretch) a per-call arg.** A preset declares
   `retention_days: <int>` (general — any template can opt in; `candidate_profile` won't,
   `daily_news` will). This is the primary surface the owner asked for ("usable in other
   research templates"). A per-call `retention_days` on the `deep_research`/`deep_produce`
   tools (W1 stretch) makes any ad-hoc run expirable too; kept optional to avoid scope
   creep.
2. **`expires_at`, not a TTL column.** Store an absolute `expires_at timestamptz NULL`
   on the row: `NULL` = keep forever (every existing report and every non-retention run,
   untouched); a timestamp = eligible for the sweep. Stamped at persist as `now() +
   retention_days`, and refreshed on every upsert so a re-run rolls the clock forward.
   Absolute-instant is simpler for the sweep (`WHERE expires_at < now()`) and survives a
   changed retention value.
3. **Hard delete on expiry.** A `research_reports` row is self-contained (no chunk/blob
   children — unlike the video corpus), so the sweep `DELETE`s outright, mirroring
   `delete_report`. No soft-delete state to carry.
4. **Dedup fix = an auto-injected run-date variable.** The engine injects a `today`
   variable (the run date, formatted for speech, e.g. "Friday, August ninth") into every
   preset render **at the call site** (`deep_research._run_preset`), so `research_presets`
   stays pure and clockless. A caller-supplied `today` overrides it. `daily_news`'s
   question becomes `Daily news briefing for {{today}} — …`, so it stays a **zero-arg,
   one-call** preset AND produces a distinct row per day. (Rejected: making `date` a required
   caller variable — it breaks the one-call ergonomics and an automated Routine could
   forget it, failing the render.)
5. **Sweep identity.** Runs under `SYSTEM_CTX` like every other nightly sweep. Resolved in
   W2: `SYSTEM_CTX` is an owner-kind context (full cross-domain), which already INSERTs on
   persist and sees every row in the RLS firewall test, so it DELETEs `external`-domain
   reports too — no separate context needed. The W2 integration test exercises the default
   `SYSTEM_CTX` delete path.

## Waves

### W1 — Retention plumbing (data + persist + preset field) ✅
- **Migration:** add `expires_at timestamptz NULL` to `app.research_reports`, plus a
  partial index `WHERE expires_at IS NOT NULL` for the sweep's due-scan. No backfill
  (NULL = keep, the correct default for every existing row).
- **`research_presets.py`:** add optional `retention_days: int | None` to `Preset` +
  `RenderedPreset`; parse/validate in `_coerce_preset` (a positive int or absent);
  carry through `render_preset`. No `{{var}}` expansion — it's a scalar policy field.
- **`deep_research.py`:** `_run_preset` passes `rp.retention_days` down `_run` →
  `_persist` → `persist_report`. (Stretch: read an optional `retention_days` tool arg in
  the non-preset `research()`/`deep_produce` paths.)
- **`persist_report`:** new `retention_days: int | None` param; on INSERT and on the
  ON CONFLICT UPDATE, set `expires_at = now() + make_interval(days => :retention_days)`
  when provided, else `NULL`.
- **Tests:** preset loader parses/validates `retention_days`; a bad value refuses at
  load; render carries it; an integration PG test asserts `persist_report` stamps
  `expires_at ≈ now + N days` and refreshes it on re-run, and leaves it NULL without
  retention.

### W2 — The expiry sweep ✅
- **`external/research_corpus.py`:** `expire_reports(maker, *, now=None) -> int` —
  `DELETE FROM app.research_reports WHERE expires_at IS NOT NULL AND expires_at < :now`
  on an RLS-scoped session, returning the deleted count. `now` injectable for a frozen
  test clock (matches the scheduler's N3 discipline).
- **`workflow/scheduler.py`:** `EXPIRE_RESEARCH_REPORTS_ACTION` (in-code `ActionSpec`,
  `category="maintenance"`, `cost_class="cheap"`, like `PURGE_ACTION`) + an
  `expire_research_reports_handler` returning the count; add the kind to
  `REAPABLE_IDLE_SWEEPS` so a night that expires nothing leaves no run-log noise.
- **`worker.py`:** compose the new spec into the build registry (alongside the other
  in-code sweeps).
- **Migration:** seed the nightly schedule + trigger + pipeline for it (mirror the
  geofence-sweep seed).
- **Tests:** sweep deletes expired rows, keeps unexpired + NULL; idempotent (a second
  fire deletes nothing); RLS — the sweep's context can delete, and the deletion respects
  scope; registry bijection still holds (the in-code spec is not in the `app.actions`
  seed); the handler is reapable at count 0.

### W3 — daily_news dedup fix + retention ✅
- **`deep_research.py`:** inject the `today` run-date variable into the preset render
  (decision 4).
- **`daily_news.preset`:** question → `Daily news briefing — {{today}}`; add
  `retention_days: 7`; weave `{{today}}` into the "Good Morning" section so the spoken
  greeting names the day. Stays zero-arg for the caller.
- **`deep_research.tool`:** note daily_news is auto-dated and kept 7 days (a deliberate
  tool-description edit → version bump + `test_sidecars_pinned_to_their_versions` pin
  update, the CI guard).
- **Docs:** reconcile REPORT_PRESET_PLAN.md (daily_news now dated + retained) and flip
  this plan's W-status.
- **Tests:** render injects `today`; two runs on different dates produce different
  `question_hash` (distinct rows), so 7 days coexist; same date de-dups in place.

### W4 — Surface expiry + "keep this" (DEFERRED — GUI gate) ◻️
Owner-facing polish, held because it changes a **GUI surface** (PROCESS.md GUI gate:
three interactive mock artifacts chosen before build):
- `LibraryReport` gains `expires_at`; the library listing shows "expires in N days".
- A **keep** action (clear `expires_at` → keep forever), as an owner-context function
  (like `rename_report`) + a jerv tool + a library button.
Scoped separately after W1–W3 land; the data plumbing (W1's column) already supports it.

## Non-negotiables checklist (CLAUDE.md)
- **#3 RLS:** the sweep runs on an RLS-scoped session; W2 carries an RLS test for the
  delete path. No new table (a column on an already-RLS'd table), so no new isolation
  suite, but the delete-scope test is mandatory.
- **#5 tests same PR:** each wave lands its tests; real Postgres via testcontainers for
  the persist + sweep paths; no LLM calls involved.
- **#6 branch + PR, CI green:** one PR per wave (or a W1–W3 bundle if small), on the
  designated branch, green before merge.
- **#9 docs travel:** this plan's status flips per wave; REPORT_PRESET_PLAN.md and
  `deep_research.tool` reconciled in W3; `Last verified` bumped.
