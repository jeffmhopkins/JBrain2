# Location assistant — build plan (DO tools)

Implements the ✅ build-now spine from `docs/LOCATION_ASSISTANT_TOOLS.md`: items
**1–14, 29, 31** (35 is ✅ but Phase-6-gated, excluded). Goal: the assistant can
answer location questions, show maps inline, and manage places — owner-only, names
not coordinates, notes as the sole source of truth.

> Status: **DRAFT for red-team.** Waves are sized as one PR each.

## Binding constraints (every wave)
- All location reads run under a **full owner** session; every agent tool refuses a
  narrowed/`owner_scoped`/non-owner session via a shared `require_full_owner(ctx)`
  helper (mirrors `agent/geocodetools.py::_is_full_owner`) and says so in its prose.
- Tools return **names/times/distances/render-only GeoJSON**; coordinates live only
  in `ViewPayload.data` for the map components, never in model-facing text, never the
  `raw` jsonb/SSID.
- **No graph writes except owner-approved notes (#7).** `save_place` stages a
  Proposal; nothing writes `place_geofence`/facts directly.
- **Tests land with code:** 80% line, **security paths 100%** — every new repo method
  gets an RLS isolation test (narrowed/foreign session → zero rows); LLM faked.
- **The `.tool` sidecar treadmill:** each new sidecar must (a) bind a handler in
  `build_registry`, (b) be added to the shipped-set + digest pins in
  `tests/unit/test_agent_readtools.py`, (c) declare `domains: [location]` so a
  general scope doesn't see it, (d) refuse non-full-owner in the handler.

## Shared design decisions
- **Person⇄device link** uses the existing `entities.subject_id` column: a Device
  graph entity (kind=Device) carries `subject_id` = the OwnTracks subject, and an
  `operatedBy`→Person predicate; resolution is Person→Device entity→`subject_id`→
  fixes. No new table.
- **One new repo surface:** extend `SqlLocationRepo` with `nearest_fix`,
  `latest_place`, `dwells`, `fixes_within`, `nearby`, `home_roster`, `segments`.
- **Two new inline views:** `location_map`, `place_card` (registry.tsx), reusing
  `leafletMap.ts` + the `/api/tiles` proxy.
- **Coordinates-to-UI only:** a shared backend helper builds the `location_map`
  `ViewPayload` (downsampled, gap-split) so no handler hand-rolls coordinate text.

## Waves (one PR each)

### L1 — Foundations (backend)
- **#1 Person⇄device link:** add `operatedBy` (ref→person, functional, relationship)
  to `schema/defs/types/device.yaml`; ensure the Device entity's `subject_id` is set
  when an owner note names a device (extraction/projection hard-link, like
  Person→subject); `SqlDeviceRepo.linked_person(ctx, subject_id)` + reverse
  `subject_for_person(ctx, entity_id)`.
- **#2 Repo reads trio:** `nearest_fix(ctx, subject, at)` (±max-gap, returns
  `gap_seconds`), `latest_place(ctx)` (current `geofence_state`), `dwells(ctx,
  subject, place?, since, until)` (enter→exit pairing, clamps open, drops
  non-positive); shared `require_full_owner` + a `LocationToolRefusal`.
- **Tests:** RLS isolation per method; nearest-fix gap; dwell pairing (open enter,
  orphan exit, reorder); person-link extraction + the narrowed-session-sees-nothing
  view test. **No migration** (device.yaml is schema; predicate seed if required).
- **DoD:** the trio + link land with 100% security-path coverage; unblocks L2–L6.

### L2 — Simple read tools (agent)
- New `agent/locationtools.py` (`build_location_handlers`) wired into
  `build_registry`; sidecars **`where_is`/`where_was_i` (#5)**, **`device_status`
  (#10)**, **`home_status` (#11)**, **`nearby_now` (#12)**. Repo adds `nearby` (KNN
  `ST_DWithin`/`<->`) and `home_roster`.
- **Tests:** per-tool unit (owner-only refusal; field mapping; stale-fix flagged;
  ambiguous handled); update readtools pins + shipped set + `schemas_for` location
  visibility.

### L3 — Map, place card, history & query
- Register **`location_map` (#3)** + **`place_card` (#4)** in `views/registry.tsx`
  (vitest). Tools **`location_history` (#7)** and **`location_query` (#6)**; repo
  `fixes_within(ctx, subject, since, until, center, radius, limit)`; shared
  downsample+gap-split helper (pure, unit-tested) building the map `ViewPayload`.
- **Tests:** view vitest (gap → multiple polylines; unknown view → nothing; no
  coordinate caption); tool unit (clamp radius/window; aggregate answer for
  "battery at <place> last night"; owner-only).

### L4 — Dwell/time tools
- **`time_at_place` / nights-away (#8)** + **`find_when_at` (#9)** over `dwells`
  (#2), tz-aware via `ctx.timezone`; fail-closed place resolution (ambiguous → ask).
- **Tests:** dwell sums; nights-away local-calendar/DST; never-visited; ambiguous
  name asks; owner-only; pins/shipped-set.

### L5 — Segmenter (keystone)
- **#14 `segments()`** in `locations/segments.py` (accuracy-gate → sessionize with
  `lag` distance + max-gap split → stays vs in-transit + confidence); `GET
  /api/locations/segments` (owner-only). Bridge to the 🟡 analytics tier.
- **Tests (testcontainers):** jitter→one stay; gap splits trips; outlier spike→no
  phantom stay; cold-start→none; RLS isolation.

### L6 — save_place (write)
- **#13 `save_place`** `mutate` tool: resolve current position → **stage a
  place-note Proposal** (owner approves text + `{center,radiusMeters}`) → existing
  ingest→extraction→`project_place_geofences`.
- **Tests:** stages a Proposal (no direct fact/mirror write); approval → Place
  entity + geofence fact + mirror (integration); owner-only.

### L7 — Proactive (no push channel needed)
- **#29 digests:** SYSTEM_CTX scheduled `compile_place_digest` action (+ seed
  migration mirroring `0064`, + a read endpoint) → a pull artifact in the Locations
  tab. **#31 presence chip:** a presence read + an app-open chip (frontend), with a
  coarse "currently at <place>" available to the assistant's ephemeral context.
- **Tests:** scheduler seed resolves (no `DispatchResolutionError`); digest compile;
  presence-chip vitest; owner-only.

## Critical path & parallelism
`L1` first (everything depends on it) → **L2, L3, L4 parallel** → **L5, L6, L7
parallel**. Each wave is its own branch+PR, CI green before merge, stacked only when
necessary.

## Out of scope (recorded, not built here)
The 🟡 analytics/proactive tier (15–28, 30, 32, 33) — most need the segmenter (L5,
shipped here) plus a **proactive delivery channel** and/or tuning; the ⛔ items
(34, 36–40) stay parked. See the catalog for each.

## Open questions for the red-team
1. Does `operatedBy` need a `canonical_predicates` seed/migration, or is the
   `device.yaml` edit enough for the extractor to mint it?
2. Should L5 (segmenter) ship now (it's ✅ but only the 🟡 tier consumes it) or
   defer until the analytics are scheduled?
3. Is a per-location-tool owner gate enough, or do we want a single master
   "assistant location access" setting (off by default) wrapping the whole module?
4. `location_query`/`nearby_now` free-text place resolution: fence-mirror only, or
   allow on-box forward-geocode fallback (an exfil-shaped surface even on-box)?
