# JBrain2 — Phase 7 (GPS / Location Ingestion) — Hybrid Build Plan

> **Provenance.** Synthesized from four independent plans (vertical-slice,
> foundation-first, threat-model-first, process-conformance) and three adversarial
> red-team passes (Postgres/Timescale/RLS; auth/privacy/egress; process/completeness).
> Every BLOCKER/MAJOR finding is folded in. **Plan doc only — no code lands from
> this file.**

## Scope
The **location slice** of ROADMAP Phase 7: OwnTracks ingestion with per-device
keys, a TimescaleDB+PostGIS location hypertable, geofence-transition events into
the Phase-5 workflow engine, local-first geocoding, and the privacy enforcement
location forces. Out of scope (named follow-ons): scoped capability tokens beyond
device keys, guided-intake links, lab-report extraction, live-tracking map,
auto-authored location notes.

## Roadmap sequencing (owner-decided)
`docs/ROADMAP.md` lists **Phase 6 (Wiki) as Planned and preceding Phase 7**;
CLAUDE.md calls Phase 6 "the next frontier." Phase 7 has **no hard code
dependency** on the wiki. **Owner decision: proceed with the Phase 7 location slice
now**, recorded as a deliberate deviation from roadmap order (PROCESS.md scope
deviation, signed off).

---

## A. Binding invariants (extend CLAUDE.md, DEVELOPMENT.md, the engine's E1–E8)
Security-adjacent invariants are **100% coverage**.

- **L1 — Location never leaves the box** without an explicit, consented, logged
  Proposal. Local Photon geocoding is **not egress** (no Proposal); the geocoder
  container sits on a docker network marked `internal: true` so it has **no route
  off-box** (enforced, not asserted). The only egress path is the out-of-extract
  external-geocoder Connector through the existing `EgressGuard` + `egress_executor`
  + `connector_log`, default off.
- **L2 — Every new location table is RLS-scoped and ships an isolation test**
  (owner-all / device-own-only / other-domain+unscoped-zero / WITH CHECK blocks
  cross-domain & cross-subject insert), **including across hypertable chunks (L3)**.
- **L3 — FORCE RLS holds across hypertable chunks.** Proven, not assumed: a
  scoped session reading a chunk-spanning range returns only permitted rows, AND
  the app role cannot reach `_timescaledb_internal` chunk relations directly.
- **L4 — The device-key path is fail-closed and physically separate from the
  owner path.** A device authenticates by HTTP Basic (device id : device key);
  the key is SHA-256-hashed and matched by a **kind-filtered** lookup
  (`kind='device_key' AND revoked_at IS NULL`); unknown/revoked/wrong-kind → 401,
  writes nothing. No owner cookie reaches the ingest endpoint; no device key
  reaches owner endpoints. (Security argument is **256-bit key entropy + SHA-256 +
  revocation filter** — *not* constant-time compare; the lookup is index equality.)
- **L5 — Device sessions are subject-bound, never owner-laundered.** Ingest runs
  under a new `device_context()` (`principal_kind='device_key'`, `subject_id=<device
  subject>`, `domain_scopes=('location',)`), **not** `narrowed_context()` (which
  hardcodes `principal_kind="owner"` and would grant all-subject access). RLS pins
  a device to its own subject.
- **L5a — Geofence work never relies on a device-stamped *job* to read fixes.**
  `worker.py` runs every stamped job under `narrowed_context(principal_id,
  domain_code)`, which hardcodes `principal_kind="owner", owner_scoped=True` and
  sets **no** `subject_id` — so a device-stamped pipeline job is both
  owner-laundered AND would read **zero** rows under the B3 subject-pin (empty
  `app.subject_id`). Therefore: (a) **geofence detection is inline at ingest under
  `device_context()`** (where the subject GUC is set) — this is the only place raw
  fixes/state are read per-subject; (b) **downstream pipeline actions consume the
  event payload (opaque IDs + state), never raw `location_fixes` by subject-pin**;
  (c) the **`geofence_sweep` reconciler is a scheduled full-owner system job**
  (`is_full_owner()` true → reads all subjects to reconcile), never a device-stamped
  job. If a future action genuinely needs subject-scoped fix reads inside a job, the
  E1 scope stamp + `narrowed_context` must be extended to carry `subject_id` — called
  out as an explicit engine change, deferred until needed.
- **L6 — Ingest is idempotent, rate-bounded, and clock-sane.** Replays are no-ops
  (natural-key `ON CONFLICT DO NOTHING`); a per-device token bucket caps write rate
  (429); `captured_at` is bounded to a sane window around the server-set
  `received_at` (far-future rejected, far-past flagged).
- **L7 — Transitions are debounced** (N confirming fixes; exit buffer = radius +
  margin) and **emitted only on a real state change**; low-accuracy fixes are gated
  out of detection.
- **L8 — Note-location fields are owner-eyes metadata, stripped at the
  serialization boundary.** This is the **sole** defense (coordinates live on
  `notes` across *all* domains; the domain RLS firewall does **not** strip a column
  on a same-domain readable row). Two layers (B11): the **HTTP note API** gates on
  `kind=='owner'` (request principals are never `owner_scoped`); the **agent/tool
  layer** — the only place owner-narrowed sessions read notes — gates on
  `kind=='owner' AND NOT owner_scoped`. Applied at **every** note-serializing path
  in both layers.
- **L9 — Device-supplied fields are DATA, never instruction or scope.** `_type`,
  `ssid`, `tid`, `lat/lon/tst`, and the verbatim `raw` payload are untrusted data;
  the `subject_id`/`domain_code` stamp is code-set, never device-set. `raw` is
  size-capped, field-allowlisted, and never rendered/forwarded.
- **L10 — Geometry has one source of truth.** The canonical geofence geometry is
  the note-sourced `place.yaml` `geofence` predicate (graph fact). `app.place_geofence`
  is a **derived, non-authoritative spatial read-model** projected *from* the graph,
  never edited directly (honors non-negotiable #7).

---

## B. Key decisions (every brief gap + every red-team finding resolved)

### B1. PostGIS + storage shape
`CREATE EXTENSION IF NOT EXISTS postgis` in its **own** migration (0054).
`location_fixes` stores raw `latitude`/`longitude` doubles **as source of truth**
plus a **STORED generated** geography:
`geog geography(Point,4326) GENERATED ALWAYS AS
(ST_SetSRID(ST_MakePoint(longitude,latitude),4326)::geography) STORED`.
*(Red-team: the generated expression is IMMUTABLE-safe and does NOT conflict with
`ON CONFLICT`; reject the repo-computed approach — drift risk. Note arg order is
`(longitude, latitude)` = (X, Y); a flipped order silently breaks every spatial
predicate — pinned by a round-trip test.)*

### B2. Hypertable × RLS (highest-stakes)
`create_hypertable('app.location_fixes', by_range('captured_at'))`. **Migration
order:** `CREATE TABLE` → unique + GiST/btree indexes → `create_hypertable` →
`ENABLE` + `FORCE ROW LEVEL SECURITY` → policy → `GRANT`. Chunk inheritance is a
**proven Wave-1 DoD** (L3), not assumed; `jbrain_app` is granted nothing on
`_timescaledb_internal`. Fallback if FORCE doesn't propagate: a non-hypertable
RLS-native partitioned table (escalate).

### B3. RLS policy shape (resolves the cross-plan contradiction)
Location is the most sensitive domain, and device writers are **non-owner**
principals, so neither the bare `has_domain_scope` policy nor the
`is_owner() AND has_domain_scope` policy is correct alone. Adopt a **domain +
subject-pin** policy on `location_fixes` / `place_geofence` / `geofence_state`:

```
USING (
  app.has_domain_scope(domain_code)                      -- domain firewall (honors owner_scoped)
  AND ( app.is_full_owner()                              -- full (non-narrowed) owner: all subjects
        OR subject_id::text = current_setting('app.subject_id', true) )  -- device/narrowed: own subject only
)
WITH CHECK ( same )
```

New SQL helper `app.is_full_owner()` = `principal_kind='owner' AND
coalesce(current_setting('app.owner_scoped',true),'false') <> 'true'`.
Result: a **full owner session** reads/writes all subjects; a **device session**
(`device_key`, subject-bound, location-scoped) reads/writes **only its own
subject**; a **non-owner capability token** or **owner-narrowed agent** scoped
elsewhere reads **zero**. *(Prerequisite: the `app.subject_id` GUC is already set
by `scoped_session` from `SessionContext.subject_id`; the device context must
populate it — see B4.)*

### B4. Auth plumbing prerequisites (missing from ALL four plans — explicit tasks)
- `PrincipalInfo` gains **`subject_id`** and **`owner_scoped`** (`auth/service.py`).
- `auth/repo.py`: new **`find_active_device_principal_by_key_hash`** (filters
  `kind='device_key' AND revoked_at IS NULL`, returns `subject_id`); extend
  `create_principal` to accept `subject_id`.
- New **`device_context()`** in `db/session.py` (NOT `narrowed_context`):
  `SessionContext(principal_id, principal_kind='device_key', subject_id=...,
  domain_scopes=('location',))`.
- **Kind constraints already exist** — migration 0001 constrains
  `subjects.kind CHECK (kind IN ('person','device'))` and `principals.kind CHECK
  (kind IN ('owner','capability_token','device_key'))`. So device kinds are already
  permitted; the task is to **verify** this covers our use (it does) and **not** add
  a duplicate CHECK. No new constraint migration needed.

### B5. Device-key HTTP auth
HTTP Basic (`username`=device id/label for the audit label only — **never trusted
for authz**; `password`=device key). **`X-Limit-U`/`X-Limit-D` are labels only,
never an auth fallback.** Kind-filtered lookup (B4). **401 on auth failure**
(only *post-auth* transient errors return the always-200 OwnTracks contract, after
the fix is durably written). Separate `current_device_principal` DI dependency,
physically distinct from `current_principal` (L4).

### B6. Geofence detection placement
**Inline at ingest** (low-latency transitions) **plus** a scheduled
`geofence_sweep` reconciler (self-healing backstop via `scheduler_tick`). One
**pure** evaluator (`ST_DWithin(geog, center, radius_m)` for circles; **`ST_Covers`
(polygon_geog, geog)`** for polygons — never `ST_Contains`, unsupported on
geography; both operands geography/4326), hysteresis (L7), accuracy gate, shared by
both paths and unit-tested once.

### B7. Transitions → events only (default)
Emit `location.geofence_transition` (LIVE dispatch; no hardcoded shadow twin —
tested directly; emitted with the **device principal_id**). **Note:** the device's
firewall to `location` is enforced by the **RLS subject-pin (B3) + the trigger's E2
`_accepts_domain` gate**, *not* by the dispatcher's `authorize_domain` (E1) — E1
only checks the domain is valid and the stamp constructs (it does not narrow by
principal kind; per its own comment "the single owner is entitled to every real
domain"). Do not characterize E1 as the device-narrowing tooth. Auto-authoring
location **notes/facts is deferred and owner-escalated** (`create_location_note`,
default **off**); if ever enabled it routes through the integrator → arbiter
(`active/pending_review`), never a direct graph write (non-negotiable #7).

### B8. Event payload forwarding (resolves the global-frozenset BLOCKER)
Do **not** widen the module-level `FORWARD_KEYS` frozenset. Add a **per-trigger
`forward_keys`** field (on `TriggerFilter`, which is `extra="forbid"` — default
`["note_id"]` to preserve the existing shadow-diff baseline). This is **not just a
field add**: `diff_pipeline` today reads the module-level frozenset and does not
receive the `TriggerFilter`, so its signature (and `resolve_event`'s call site)
must be **threaded to pass the trigger's `forward_keys` through**. The geofence
trigger forwards **opaque IDs + `transition`/`state` only — never `lat/lon`**. (The
new event has no hardcoded twin, so `compute_diff` treats it as informational —
no baseline breakage, provided the default stays `["note_id"]`.) Tests:
`forward_keys_no_coord_leak`; E2 `_accepts_domain` fail-closed for the new event;
seeded pipeline resolves (no `DispatchResolutionError`).

### B9. Geometry source of truth (resolves the BLOCKER vs non-negotiable #7)
The `place.yaml` `geofence` predicate (graph fact, note-sourced) is canonical.
`app.place_geofence` is a **derived spatial read-model** projected from the graph
by a hook in the fact-apply path (`analysis/persist.py
IntegrationRunLog.persist`). The UI "geofence editor" writes a **correction/place
note** → graph → projector → mirror; it **never** writes the mirror table directly.
**Staleness — the projector must fire on the full predicate lifecycle, not just
apply:** geofence-predicate **retraction/supersession** (`analysis/supersession.py`,
`analysis/purge.py`) must delete/disable the corresponding mirror row, otherwise the
mirror drifts. As a backstop, the `geofence_sweep` reconciler rebuilds the mirror
from the graph. `geofence_state` (per-(subject,fence) hysteresis) is genuinely
operational. Test: applying then superseding a geofence fact leaves no stale mirror
row.

### B10. Geocoder
**Photon** (lighter than full Nominatim) on an **opt-in** compose profile
(`profiles: ["geocoder"]`), `mem_limit`, named volume, regional OSM extract
(owner-chosen), `networks: [internal]` with the network marked **`internal: true`**
(no off-box route), `JBRAIN_GEOCODER_URL`, mirroring `embed`. `dev-setup.sh`
pre-pulls under the profile (non-neg #8); CI never runs the profile (geocode HTTP
faked). **`geocode_reverse` (lat/lon→address) is the safe default; `geocode_forward`
(free-text `query`) is owner-only** (a free-text slot is an exfil channel the
ParamSpec key-allowlist cannot constrain). **Local results are not cached in the DB**
(Photon is the cache); the **external** fallback caches the address (PII) in
`connector_cache` (owner + location RLS — documented as PII). Coordinates in an
egress **Proposal preview** are by-design and owner-only (carve-out from the
"no coords persisted" invariant).

### B11. Note-location privacy (resolves the app-layer leak)
Coordinates live on `notes` across **all** domains, so the domain RLS firewall does
not strip them — **app-layer stripping is the sole defense.** The risk splits across
two layers that must be handled distinctly:
- **HTTP note API (`api/notes.py`):** request principals come via `current_principal`
  and are **never `owner_scoped`** (`ctx_for` defaults it False), so at this layer the
  correct gate is simply **`kind=='owner'`**. Audit every HTTP emitter of the
  structured fields: `api/notes.py` (`note_out`, list+detail), `notes/service.py`;
  confirmed-clean-but-verify `api/search.py` (returns snippets, not the columns),
  `api/feed.py` (ICS venue strings), `api/appointments.py` (free-text venue),
  `api/lists.py` (list-item DTOs). Regression test: non-owner token → coordinates
  `None`; owner → present.
- **Agent/tool layer (the real owner-narrowed case):** owner-narrowed
  (`owner_scoped=True`) sessions exist only for agent/tool reads
  (`analysis/graph_context.py` and any note-reading agent tool), **not** the HTTP
  API. The `NOT owner_scoped` gate (and the location-visibility decision) belongs
  **here** — a health-scoped agent session must not receive a note's GPS. Enumerate
  the agent note-reading tools and strip there; regression test uses an
  `owner_scoped=True` session at the tool layer, not the API. **Note (current
  state):** the agent `read_note` tool's `format_note` does not serialize
  coordinates today, so this leak is **latent/forward-looking** — the gate is
  defense-in-depth against a future emitter, and its test asserts coords stay absent.
  The only **live** coordinate emitter is the HTTP `note_out`.

Prerequisite: `PrincipalInfo`/`SessionContext` surface `owner_scoped` to the
serialization decision (B4).

### B12. Provisioning / UI / color
Owner-only `api/devices.py`: `POST /devices` (create `Subject(kind='device')` +
`Principal(kind='device_key', subject_id=...)`, key shown **once**), rotate
(=revoke+create), revoke (`revoked_at`), list (no secrets). UI behind the **Wave-0
three-mock GUI gate** → `docs/mocks/`; location domain color **teal** in
`tokens.css` (no raw hex). Live map deferred.

### Open decisions
**Owner-decided:** proceed now (sequencing); **full RLS subject-pin** for
cross-subject isolation (the default specified in B3). **Wave 0 constants — locked
defaults (tunable later via the settings store):** geofence debounce = **2
confirming fixes**; exit buffer = **radius + 50 m**; accuracy gate = **drop fixes
with accuracy_m > 100 m** from detection; per-device rate cap = **60 fixes/min**
(429 over); OSM extract region = **owner-chosen at install**; `create_location_note`
= **off**.

### Wave 1 — as-built refinements (discovered in implementation)
- **Test image aligned to prod (owner-decided):** the integration suite runs
  `timescale/timescaledb-ha:pg17` (was `pgvector/pgvector:pg16`, which lacked
  Timescale + PostGIS). Migration `0054` creates both `timescaledb` and `postgis`
  extensions per-database so fresh test clones / deploys have them.
- **No `geoalchemy2` dependency:** geometry is migration-owned and queried via raw
  `ST_*` SQL on the RLS-scoped session; ORM models map only scalar columns — so
  dev-setup needs no change (it already pre-pulls the timescale image; non-neg #8 met).
- **Hypertable key:** a hypertable forbids a single-column PK omitting the partition
  column, so `location_fixes` uses composite PK `(id, captured_at)` plus the
  natural-key UNIQUE `(subject_id, captured_at, latitude, longitude)`.
- **`place_geofence` read policy** guards the subject-less "all devices" branch to
  `principal_kind='device_key'`, so a non-device location-scoped token cannot read
  fence geometry; writes are full-owner/system only.

---

## C. Data model (migrations from 0054; each RLS-isolation-tested)
Models in `backend/src/jbrain/models/location.py`. Every table carries
`domain_code text NOT NULL REFERENCES app.domains(code)` (always `'location'`),
`subject_id`, and the RLS quartet (ENABLE/FORCE + B3 policy + GRANT).

| Migration | Object | Notes |
|---|---|---|
| **0054_postgis** | `CREATE EXTENSION IF NOT EXISTS postgis` | First-in-repo; downgrade is a **no-op / guarded `DROP EXTENSION IF EXISTS`** only after dependents drop (revision chain handles order). Superuser, like 0003. |
| **0055_location_fixes** | `app.location_fixes` (**hypertable**) | raw `latitude/longitude` doubles + generated `geog` (B1); `subject_id`, `principal_id` (device), `captured_at` (=`tst`), `received_at`, `accuracy_m`, `altitude_m`, `velocity`, `course`, `battery`, `connection`, `tid`, `raw jsonb` (size-capped, allowlisted, L9). `create_hypertable(by_range('captured_at'))`; GiST on `geog`; btree `(subject_id, captured_at desc)`; **unique `(subject_id, captured_at, latitude, longitude)`** → `ON CONFLICT DO NOTHING` (dedups verbatim retries). B2 order; B3 policy; **L3 chunk-RLS proof**. |
| **0056_place_geofence** | `app.place_geofence` (derived read-model, L10) | `place_entity_id`, `subject_id` (nullable=all-devices), `center geography(Point,4326)`, `radius_m`, `polygon geography(Polygon,4326)`, `enabled`; CHECK exactly one of (center+radius)/polygon; GiST. Projected from the graph predicate, never edited directly. |
| **0057_geofence_state** | `app.geofence_state` | PK `(subject_id, place_geofence_id)`; `state` (`inside/outside/unknown`), `confirming_fixes`, `since`, `last_fix_at`. Operational hysteresis state. |
| **0058_seed_geofence_workflow** | trigger + pipeline + schedule | Seeded **in the same PR as the registered `geofence_sweep` ActionSpec** (else `DispatchResolutionError` every tick); pipeline-resolves test required. |

Fixes are DB rows, never the blob store (non-neg #2); `raw` is a column, not a file.

---

## D. Waves (PROCESS.md: worktrees, per-task + per-wave gates, one PR per wave)

Wave boundaries chosen for reviewability and parallelism. Every wave: per-task
independent adversarial review (reviewer ≠ builder); **per-wave red-team mandatory
for W1–W4** (all touch RLS/firewall/auth/egress); one PR opened only after both
gates clean; CI green (ruff/pyright or biome/tsc; testcontainers real Postgres;
80% line / **security-100%**; `.tool` digest pins; `dev-setup.sh` + `docker-compose`
currency). LLM faked; clocks injected.

### Wave 0 — Gates & decisions (no code, no PR)
GUI mock gate (3 interactive mocks for device-mgmt + geofence editor + last-seen →
owner choice → `docs/mocks/` + teal token spec); owner sign-off on the open
decisions and the Phase-6 sequencing deviation. **DoD:** chosen mock recorded;
decisions logged.

### Wave 1 — Spatial + identity foundation (security bedrock)
- Migrations 0054–0057 (C); `models/location.py`; `geoalchemy2` dep
  (pyproject + dev-setup, non-neg #8).
- Auth plumbing prerequisites (B4): `PrincipalInfo.subject_id/owner_scoped`,
  `find_active_device_principal_by_key_hash`, `create_principal(subject_id)`,
  `device_context()`. (Kind CHECKs already exist in 0001 — verify, do not duplicate.)
- `app.is_full_owner()` helper + B3 policy on all three tables.
- **Tests (RLS 100%):** isolation per table (owner-all / device-own-only /
  cross-subject denied / other-domain+unscoped zero / WITH CHECK blocks
  cross-domain+cross-subject insert); **multi-chunk FORCE proof + direct-chunk
  denial** on `location_fixes`; geog `ST_X/ST_Y` round-trip.
- **DoD:** migrations up+down clean on `timescaledb-ha:pg17`; L2/L3/L5 proven.
  *Red-team mandatory.*

### Wave 2 — Device-key auth + provisioning + note-privacy regression
- `authenticate_device` (kind-filtered, fail-closed); `current_device_principal`
  DI (Basic only; X-Limit labels-only; 401 on fail); `api/devices.py` owner-only
  provision/rotate/revoke/list (key once).
- Note-location stripping at both layers (B11): HTTP emitters gated `kind=='owner'`;
  agent/tool note readers gated `kind=='owner' AND NOT owner_scoped`.
- **Tests (security 100%):** good/unknown/revoked/wrong-kind/owner-key-as-device/
  device-key-on-owner-route; provisioning + rotate-revokes-old + revoke-blocks-auth;
  HTTP stripping (non-owner → `None`); **agent-layer stripping with an
  `owner_scoped=True` session**; audit-all-emitters regression. *Red-team mandatory.*

### Wave 3 — OwnTracks ingest + geofence brain
- `api/ingest.py POST /pub` (`OwnTracksLocation` pydantic; clock-skew bound;
  per-device token-bucket rate limit → 429; `ON CONFLICT DO NOTHING`; always-200
  post-auth / 401 pre-auth; `raw` size-cap + allowlist). Ingest under
  `device_context()` (L5).
- `place_geofence` **projector** wired into the fact-apply path (L10).
- Pure `location/geofence.py` (B6); inline detection + `geofence_state` RMW; emit
  `location.geofence_transition` (B7); **per-trigger `forward_keys`** mechanism
  (B8); `geofence_sweep` ActionSpec + `0058` seed.
- **Tests:** detector unit (enter/exit/hysteresis/flap-suppression/accuracy-gate);
  ingest→event integration (LIVE enqueue + run-log under narrowed location scope);
  E2 fail-closed; `forward_keys_no_coord_leak`; sweep reconcile; idempotency;
  clock-skew; rate-limit 429; pipeline-resolves. **Geofence event emission tested
  by plain integration tests — NOT the scenario harness** (no LLM extraction
  involved); reserve the harness for the deferred transition→note flow.
  *Red-team mandatory.*

### Wave 4 — Local-first geocoding + egress containment
- Photon compose service (opt-in profile; `internal: true` no-egress network;
  `JBRAIN_GEOCODER_URL`; dev-setup pre-pull, non-neg #8). `.tool` sidecars
  (digest-pinned) for `geocode_reverse` (default) and `geocode_forward`
  (owner-only). External fallback `Connector` via EgressGuard + Proposal (default
  off); local = not egress; no local DB cache.
- **Tests:** local reverse/forward stages **zero** Proposals + geocoder has **no
  off-box route** (asserted); external requires Proposal; `build_egress` rejects
  undeclared param; external-geocode cache RLS (location+owner). *Red-team mandatory.*

### Wave 5 — Location UI (post Wave-0 gate; may overlap W4)
- Device management, geofence editor (**writes place/correction notes → graph →
  projector**, never the mirror table directly), last-seen/history on fixtures;
  teal token finalized; live map deferred.
- **Tests:** owner-only RLS-scoped reads (non-owner 403; stripping reaffirmed on
  any new read DTO); frontend vitest; biome/tsc/build.

**Critical path:** W0 → W1 → W2 → W3 → W4; W5 overlaps W4 after the mock gate.

---

## E. Residual risks (named, not silently accepted)
- **DB backups bypass RLS** (superuser-level): the `location_fixes` table is the
  most sensitive in the system → require backup-at-rest encryption + restricted
  dump access (ops, not code; flag to owner).
- **A valid-but-compromised device key can manufacture plausible fixes**
  (spoofing): mitigated by hysteresis + accuracy gate + events-only (no silent
  graph writes), not eliminated. Rotation/revocation is the response.
- **`raw jsonb` / SSID-BSSID** are location-revealing: allowlisted, size-capped,
  never rendered/forwarded.
- **Cross-subject isolation depth** is an owner decision (Wave 0); the plan ships
  the full RLS subject-pin by default.

## F. Exit (ties to ROADMAP "phones report location continuously")
A provisioned phone, configured with its per-device key over HTTP Basic, posts
OwnTracks fixes to `/pub`; each fix is durably stored in the RLS-scoped,
subject-pinned `location_fixes` hypertable (idempotent, rate-bounded, clock-sane);
crossing a geofence (geometry projected from a note-sourced Place predicate) emits
a debounced `location.geofence_transition` that drives the Phase-5 engine to a
logged `runs` row under a narrowed location scope; reverse geocoding runs locally
with no egress; note location metadata is invisible to every non-full-owner
principal; and every new table proves domain **and** cross-subject isolation,
including across hypertable chunks.
