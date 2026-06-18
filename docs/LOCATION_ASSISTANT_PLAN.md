# Location assistant — build plan (DO tools)

Implements the ✅ build-now spine from `docs/LOCATION_ASSISTANT_TOOLS.md`: items
**1–13, 29, 31** (14/segmenter deferred — see below; 35 is ✅ but Phase-6-gated,
excluded). Goal: the assistant can answer location questions, show maps inline, and
manage places — owner-only, names not coordinates, notes as the sole source of truth.

> Status: **v2 — red-teamed.** Three independent passes (architecture/dependency,
> repo/RLS-firewall, delivery/testing) folded in. Waves are sized as one PR each.
> Changes from the draft are flagged inline as **[RT]**.

## What the red-team changed (executive summary)
- **#1 person⇄device link is net-new machinery, not "use the existing column."**
  `entities.subject_id` is set in exactly one place today (the bespoke owner "Me"
  hard-link); every Device entity is born `subject_id=None`, and OwnTracks subjects
  are minted on a disjoint path with no back-reference. The link must be an
  **owner-only deterministic binding**, never LLM-set. **[RT P0]**
- **The full-owner firewall does NOT cover `app.events` or `place_geofence` reads** —
  both are gated only by `has_domain_scope`, which passes for *any* owner (including a
  narrowed `owner_scoped` session) and even a non-owner holding the `location` scope.
  So `require_full_owner` in the handler is the **primary** barrier for any method
  touching those tables — RLS will not save you. **[RT P0]**
- **`require_full_owner` becomes a registration-time wrapper**, not per-handler
  discipline, plus a default-off master "assistant location access" setting. **[RT P1]**
- **L5 (segmenter) is deferred** — nothing in the build-now spine consumes it. **[RT P1]**
- **`save_place` moves earlier** — it only needs L1, not the segmenter. **[RT P1]**
- **L7 splits** into L7a (digest, **compute-on-read, no table, no migration, no
  scheduler**) and L7b (presence chip). **[RT]**
- **L2→L3→L4 serialize** (shared `build_registry` + `test_agent_readtools.py` hotspot
  make true parallelism a guaranteed merge conflict). **[RT]**
- **No migrations in this plan.** `operatedBy` is a `device.yaml` edit; the digest is
  compute-on-read; `save_place` rides the existing projection. **[RT]**

## Binding constraints (every wave)
- **Full-owner gate is the primary barrier, not a backstop.** Every agent tool refuses
  a narrowed/`owner_scoped`/non-owner session via a shared `require_full_owner(ctx)`
  helper (mirrors `agent/geocodetools.py::_is_full_owner`) **applied as a wrapper at
  registration** in `build_location_handlers`, so a new sidecar *cannot forget it*.
  This is load-bearing because two of the tables these tools read
  (`app.events`, `place_geofence`) are only `has_domain_scope`-gated in RLS — a missed
  handler check there is a real leak, not a harmless empty result. **[RT]**
- **A default-off master setting** ("assistant location access") wraps the whole
  module as a second layer (OQ#3 answer: yes, both gate + setting). **[RT]**
- Tools return **names/times/distances/render-only GeoJSON**; coordinates live only in
  `ViewPayload.data` for the map components, never in model-facing text, never the
  `raw` jsonb/SSID.
- **No graph writes except owner-approved notes (#7).** `save_place` stages a Proposal;
  nothing writes `place_geofence`/facts directly.
- **Tests land with code:** 80% line, **security paths 100%**. Every new repo method
  gets an RLS isolation test (narrowed/foreign session → zero rows); LLM faked.
  **Additionally [RT]:** for any method touching `app.events`/`place_geofence`, add an
  explicit `NARROWED_LOCATION` (owner + `owner_scoped` + `location` scope) → zero-rows
  test, and **backfill the missing regression** for the existing `timeline()`/`places()`
  (today only a wrong-*domain* test exists, not a narrowed-but-location-scoped one).
- **The `.tool` sidecar treadmill (counted) [RT]:** each new sidecar must (a) bind a
  handler in `build_registry`, (b) add a `pins` entry *with a computed sha256 digest*
  **and** a `shipped`-set entry in `tests/unit/test_agent_readtools.py` (the
  `on_disk == set(pins)` guard at ~line 661 fails CI the moment an unpinned `.tool`
  lands), (c) declare `domains: [location]`, (d) be covered by the `schemas_for`
  visibility split (~lines 470–507). **9 new sidecars total** across the plan (see each
  wave); `where_is`/`where_was_i` is **two** tools, not one.

## Shared design decisions
- **Person⇄device link is new machinery [RT].** Today nothing binds a graph Device
  entity to its operational `subjects(kind='device')` row: `entities.subject_id` is set
  only by the "Me" hard-link (`analysis/entities.py`), every Device is born
  `subject_id=None`, and OwnTracks subjects are minted disjointly
  (`devices/repo.py`, stamped at `api/owntracks.py`). The plan adds an **owner-only
  deterministic binding** (a UI affordance or a reconciler that matches an
  `operatedBy`+device-name fact to a `subjects(kind='device')` row and writes
  `subject_id` under a full-owner/SYSTEM_CTX session) — **never LLM-set**, because a
  display-name match is spoofable and is exactly the human↔track join we refuse to
  automate. Resolution is then Person→Device entity→`subject_id`→fixes, fail-closed
  (an unbound Device yields *zero* fixes).
- **One new repo surface:** extend `SqlLocationRepo` with `nearest_fix`,
  `latest_place`, `dwells`, `fixes_within`, `nearby`, `home_roster`. (`segments` is
  deferred with L5.) All seven are new; `fixes_within` is a windowed/spatial variant of
  the existing `fixes`.
- **Two new inline views:** `location_map`, `place_card` in
  `frontend/src/agent/views/registry.tsx` — the **first** leaflet-dependent tool-views
  (the file has only pure-DOM views today). They reuse `leafletMap.ts` + the
  `/api/tiles` proxy, and their vitest must `vi.mock("./leafletMap")` like
  `LocationScreen.test.tsx:15` and **assert the downsampled/gap-split GeoJSON handed to
  the mock**, not rendered tiles (jsdom has no layout engine). **[RT]**
- **Coordinates-to-UI only:** a shared backend helper builds the `location_map`
  `ViewPayload` (downsampled, gap-split) so no handler hand-rolls coordinate text.

## Waves (one PR each)

### L1 — Foundations: person⇄device binding + repo reads (backend)
- **#1 Person⇄device link [rewritten per RT P0]:** add `operatedBy` (ref→person,
  functional, relationship) to `schema/defs/types/device.yaml` (**no migration** — see
  OQ#1). Build the **owner-only deterministic binding** that writes a Device entity's
  `entities.subject_id` under a full-owner/SYSTEM_CTX session from an owner note +
  device-name match (never the LLM). Add `SqlDeviceRepo.linked_person(ctx, subject_id)`
  + reverse `subject_for_person(ctx, entity_id)` (neither exists today).
- **#2 Repo reads trio:** `nearest_fix(ctx, subject, at)` (±max-gap, returns
  `gap_seconds`), `latest_place(ctx)` (current `geofence_state` — strict RLS), `dwells(
  ctx, subject, place?, since, until)` (enter→exit pairing, clamps open, drops
  non-positive). Shared `require_full_owner` + a `LocationToolRefusal`.
- **Tests:** RLS isolation per method; **fail-closed "unbound Device → zero fixes"**
  binding test; nearest-fix gap; dwell pairing (open enter, orphan exit, reorder);
  binding is owner-only / never LLM-set. **No migration.**
- **DoD:** the binding + trio land with 100% security-path coverage; unblocks L2–L6.

### L2 — Simple read tools (agent) — *start of the serialized L2→L3→L4 chain* [RT]
- New `agent/locationtools.py` (`build_location_handlers`, with the **registration-time
  `require_full_owner` wrapper**) wired into `build_registry`; sidecars
  **`where_is`/`where_was_i` (#5, two tools)**, **`device_status` (#10)**,
  **`home_status` (#11)**, **`nearby_now` (#12)**. Repo adds `nearby` (KNN
  `ST_DWithin`/`<->`) and `home_roster`.
- **This wave performs the one-time `schemas_for` flip [RT]:** the
  `schemas_for({"location"})` assertion must now equal `shipped`, and
  `schemas_for({"general"})` must *exclude* all location tools — make this coordinated
  edit here so L3/L4 don't re-litigate it.
- **Sidecars: 4 files (5 tools).** **Tests:** per-tool unit (owner-only refusal; field
  mapping; stale-fix flagged; ambiguous handled); `home_status`/`nearby_now` place-name
  resolution touches `place_geofence`/`events` → add the `NARROWED_LOCATION`→zero test
  there; update readtools pins + shipped set + the `schemas_for` split.

### L3 — Map, place card, history & query — *serialized after L2*
- Register **`location_map` (#3)** + **`place_card` (#4)** in
  `frontend/src/agent/views/registry.tsx` (vitest with the leaflet `vi.mock`). Tools
  **`location_history` (#7)** and **`location_query` (#6)**; repo `fixes_within(ctx,
  subject, since, until, center, radius, limit)`; shared downsample+gap-split helper
  (pure, unit-tested) building the map `ViewPayload`.
- **Sidecars: 2 files.** **Tests:** view vitest against the mock payload (gap →
  multiple polylines; unknown view → nothing; no coordinate caption); tool unit (clamp
  radius/window; aggregate answer for "battery at <place> last night"; place-name
  resolution → `NARROWED_LOCATION`→zero where it reads `place_geofence`/`events`;
  owner-only).

### L4 — Dwell/time tools — *serialized after L3*
- **`time_at_place` / nights-away (#8)** + **`find_when_at` (#9)** over `dwells` (#2),
  tz-aware via `ctx.timezone`; fail-closed place resolution (ambiguous → ask).
- **Sidecars: 2 files.** **Tests:** dwell sums; nights-away local-calendar/DST;
  never-visited; ambiguous name asks; owner-only; pins/shipped-set.

### L5 — Segmenter (DEFERRED to the analytics phase) [RT P1 / OQ#2]
- **#14 `segments()`** has **no consumer in the build-now spine** — its consumers are
  the 🟡 analytics tier (commute, trends, mode, heatmap, discovery), all out of scope
  here. Shipping it now means carrying a tested, RLS-covered surface with zero callers
  and risks tuning drift (accuracy-gate / roaming-radius) before anything exercises it.
  **Defer** to the analytics phase, where its first real consumer can validate the
  parameters against actual queries. (Pure compute over `location_fixes`; no table.)

### L6 — save_place (write) — *after the L2→L3→L4 chain; only depends on L1* [RT P1]
- **#13 `save_place`** `mutate` tool: resolve current position → **stage a place-note
  Proposal** (owner approves text + `{center,radiusMeters}`) → existing
  ingest→extraction→`project_place_geofences` (Place-only projection already exists).
  Does **not** need the segmenter.
- **Sidecars: 1 file.** **Tests:** stages a Proposal (no direct fact/mirror write);
  approval → Place entity + geofence fact + mirror (integration); owner-only. Heaviest
  single wave — keep it stacked, not parallel.

### L7a — Nightly/weekly digest (compute-on-read) [RT — discrepancy resolved]
- **#29 digest, reframed:** **no scheduled action, no new table, no seed migration.**
  The red-team confirmed `runs`/`run_steps` store only enqueue metadata — a scheduled
  `compile_place_digest` would have nowhere to write its output. So compute the rollup
  **on read** from `app.events` under a full-owner ctx, served by an **owner-only
  `GET /api/locations/digest`** endpoint, rendered as a pull artifact in the Locations
  tab. Because `app.events` is only `has_domain_scope`-gated, the endpoint's full-owner
  guard is the real barrier — assert it.
- **Tests:** digest compile (place names only, idempotent); endpoint full-owner gate +
  `NARROWED_LOCATION`→403/empty; Locations-tab render vitest.

### L7b — App-open presence chip [RT — mechanism named]
- **#31 presence:** a presence read + an app-open chip (frontend). The coarse
  "currently at <place>" reaches the assistant as a **data-framed `UserMessage`
  prepended in `api/agent.py::_conversation` (~line 239)**, mirroring the Loop-2 skills
  block — **not** the system prompt (`run_stream` hardcodes `system=SYSTEM_PROMPT` at
  `loop.py:347`, so a system-prompt injection would silently no-op in streaming), and
  owner-gated. Keeps the data/instruction boundary the skills block respects.
- **Tests:** presence-chip vitest; the injected line is data-framed + owner-gated +
  absent for a narrowed session; never volunteers location into exportable output.

## Critical path & realistic sequencing [RT]
`L1` first (everything depends on it) → **serialized `L2 → L3 → L4`** (they share
`build_registry` + `test_agent_readtools.py`; parallel branches would conflict on the
`shipped` set + `pins` dict on every merge and re-break the `on_disk == set(pins)`
guard) → **L6** → **{L7a, L7b}** (independent of each other). **L5 is deferred.**
Each wave is its own branch+PR, CI green before merge, stacked where the shared test
file forces it. **~7 PRs** (L1, L2, L3, L4, L6, L7a, L7b).

## Out of scope (recorded, not built here)
The 🟡 analytics/proactive tier (14–28, 30, 32, 33) — most need the **segmenter (L5,
now deferred here)** plus a **proactive delivery channel** and/or tuning; the ⛔ items
(34, 36–40) stay parked. See the catalog for each.

## Open questions — answered by the red-team
1. **`operatedBy` migration?** **No.** Adding it to `device.yaml` suffices: facts are
   minted with the model-emitted predicate as free text; `canonical_predicates` is a
   derived projection the `sync_predicates` job keeps in sync from the YAML registry
   (the existing `spouse`/`worksFor` relationship predicates have no hand-written seed
   and follow the identical zero-migration pattern). `device.yaml` already has
   `allow_open_predicates: true`; `is_functional` honors the registry flag.
2. **Ship L5 (segmenter) now?** **Defer.** No build-now consumer; its consumers are the
   deferred 🟡 analytics tier.
3. **Per-tool gate vs. master setting?** **Both.** Make `require_full_owner` a
   registration-time wrapper (un-forgettable) *and* add a default-off "assistant
   location access" master setting.
4. **Free-text place resolution exfil?** **Keep on-box forward-geocode, fence-mirror
   first.** Resolve `place`→fence-mirror, fall back to on-box forward-geocode only on a
   miss, route it through the same `require_full_owner` wrapper already proven for
   `geocode_forward`, and stage **no** Proposal (consistent with the existing
   reverse/forward tools; Photon runs on a no-egress network, so on-box it is a read,
   not an egress).
