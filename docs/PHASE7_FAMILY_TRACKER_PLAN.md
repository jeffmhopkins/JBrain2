# JBrain2 — Phase 7+ Family Location Tracker — Hybrid Build Plan
### App name: **JBrain360** (forked OwnTracks · MQTT · view-scope)

> **Provenance.** Synthesized from three independent research sweeps (OwnTracks
> fork internals; MQTT broker/server/ACL; Android-native/security), three
> independent implementation plans (client-weighted, server-weighted,
> security/integration-weighted), three adversarial red-team passes (one per
> plan), and a dedicated FCM design slice. Every BLOCKER/MAJOR finding is folded
> in. **Plan doc only — no code lands from this file.**

> **Relationship to `PHASE7_LOCATION_PLAN.md`.** This is the explicitly-named
> Phase 7 follow-on: *"scoped capability tokens beyond device keys"* and
> *"live-tracking map."* It **extends** the shipped location slice (invariants
> L1–L10, `location_fixes` hypertable, `device_context()` RLS, geofence-at-ingest,
> the Phase-5 workflow engine) — it does not replace or fork any of it.

---

## Scope

Turn the shipped single-owner location-ingest slice into a **family-scale
tracker**: forked OwnTracks Android app → MQTT/TLS → self-hosted Mosquitto →
a backend consumer that lands fixes in the **existing `location_fixes`
hypertable** → an embedded WebView dashboard (devices / timeline / map) whose
viewers see **only** what a per-device **view-scope** permits, with both
**live** (broker-pushed) and **history** (Timescale-queried) paths, **remote
mode switching** over MQTT, and **on-device notifications** via MQTT (while
connected) plus **FCM** (wake-from-Doze backstop). Sideloaded, self-signed APK;
pairing-code onboarding.

**Out of scope (v1):** iOS; multi-household/tenant; payload E2E encryption;
UnifiedPush/ntfy for de-Googled phones (**v1.1**); editing view-scope from the
phone (owner-only via the JBrain2 API); waypoint-authoring UI in the fork.

---

## A. Binding invariants (extend L1–L10; tracker invariants T1–T8)

Security-adjacent invariants are **100% coverage**. Each new table ships an RLS
isolation test (non-negotiable #3).

- **T1 — One store of location truth.** `app.location_fixes` (TimescaleDB +
  PostGIS, B3 subject-pinned RLS, proven across chunks) is the **sole** system of
  record for location history. The OwnTracks **Recorder is not used as a store or
  a read path** — it would be an unauthenticated, RLS-bypassing second source of
  truth. (If ever used, it is air-gapped, synthetic-data, dev-only and deleted
  before any milestone touching real data.) History is served by **RLS-scoped
  Timescale queries**, never by Recorder's API.
- **T2 — One device identity across transports.** A device is a single
  `principals.kind='device_key'` row (256-bit key, **saltless SHA-256-hex**,
  index-equality lookup, kind-filtered, `revoked_at IS NULL` — the shipped L4
  model). MQTT authenticates through **mosquitto-go-auth pointed at an internal
  JBrain2 auth endpoint** that runs the existing device-key check against
  `principals`. **No second credential store** (no dynsec client passwords, no new
  `pw_hash` column). Revocation flows through the existing `revoked_at` filter and
  kills both HTTP and MQTT access.
- **T3 — Family-sees-family authz lives in Postgres RLS.** Configuration is a
  **single flat family group** *(owner decision)*: every member sees every other
  member; non-members see nothing. The owner's only action is **add/remove a
  member** — there is no per-pair setup. Under the hood this still **extends B3
  inside the database**: the `location_fixes` read policy gains
  `OR app.viewer_may_see(current_setting('app.subject_id'), subject_id)`, backed
  by group membership (`view_scope`). The broker ACL, the live fan-out filter, the
  history query, and FCM routing are all **projections of this one RLS decision** —
  never the authority. The base subject-pin policy is **never loosened**; a
  gateway/broker bug therefore cannot leak cross-subject data the DB itself
  forbids. **The group is the security floor** — it keeps non-family out (a leaked
  URL/credential sees nothing), not a config burden on the owner.
- **T4 — Geofence-at-ingest moves to the MQTT consumer.** L5a (inline geofence
  detection under `device_context()` with the subject GUC set, emitting
  transitions into the Phase-5 workflow engine) runs **in the MQTT ingest
  consumer**, subject-pinned to the authenticated device's `subject_id` (derived
  from the principal, **never** from the MQTT topic — `subTopic` is client-side
  only, never trusted for authz, L9).
- **T5 — Deny-by-default, re-checked, promptly revocable.** Every live and
  history request re-evaluates view-scope against Postgres (no trusting the
  client). go-auth's ACL cache TTL is **0 on the live path**; revocation
  **force-disconnects** the live MQTT session (not just future ACL checks) and
  bumps a `cred_epoch` that invalidates dashboard cookies for an instant 401.
- **T6 — L1 holds for every new path.** Family-view live + history are
  **deliberate, consented, logged** location egress: `view_scope` is the consent,
  `view_audit` is the log. The public broker port carries **only** device-authn'd
  ingest and scope-checked fan-out — never an open subscribe. **FCM pokes carry
  no domain data** (content-free rotating nonce only), so they are **exempt from
  EgressGuard** (no location leaves the box on the wire) but **audited** (Google
  still learns poke metadata). Putting any location/subject/place field in an FCM
  payload is a true L1 egress and is **forbidden** — enforced by a test, not a
  convention.
- **T7 — Owner is the sole authority; membership is owner-only.** *(Owner
  decision.)* The owner adds/removes family members unilaterally; there is **no
  affirmative-consent gate** and **no in-app self-Leave** (a member opts out only
  by uninstalling, which surfaces as a device going offline). The residual
  safeguards — kept **on by default** — are the **Android-mandated persistent
  "tracking active" notification** (not optional; the OS requires it for
  background location, so nobody is tracked invisibly) and the **who-saw-whom
  access audit** (`view_audit`, server-side, incl. live + history + poke). Because
  consent-gating, self-Leave, **and** retention are all off, the breach/insider
  exposure surface is larger, so **encryption-at-rest, the access audit, and
  prompt owner-initiated revocation (T5) are mandatory compensating controls**,
  not optional.
- **T8 — the MQTT auth/ACL endpoint runs least-privilege.** *(Updated by spike:
  go-auth uses its HTTP backend, so it has NO database role — auth/ACL is decided
  in our `/internal/mqtt-auth` + `/internal/mqtt-acl` endpoints.)* Those endpoints
  evaluate under a dedicated, least-privilege RLS-scoped context (the existing
  `login` auth-context for the credential lookup; a purpose-built scoped session
  for the membership/ACL check) that reads only group membership and cannot infer
  cross-subject location. An isolation test proves the ACL endpoint cannot be
  driven to authorize a topic outside the caller's `view_scope`.

---

## B. Resolved decisions (the fork-in-the-road calls, with rationale)

### B1. Transport: **MQTT (Mosquitto 2.x)**, reconciled with shipped HTTP ingest
The shipped phones ingest over **HTTP `/pub`** (OwnTracks HTTP mode, HTTP Basic).
MQTT is **added**, not silently swapped. The MQTT broker feeds a **backend
consumer** that calls the *existing* ingest path under `device_context()`, so
`location_fixes`, L5a geofence detection, dedup (L6), and the workflow-engine
emission are all preserved. **HTTP `/pub` is kept as a permanent supported
fallback** *(owner decision)*: MQTT is primary, HTTP is a per-device fallback that
survives a broker outage and gives easy rollback. Both transports share the one
ingest core, so the marginal cost is only the thin transport edge — not a second
copy of the sensitive logic.

### B2. Broker auth/ACL: **mosquitto-go-auth → JBrain2, NOT Dynamic Security**
Unanimous red-team finding. dynsec would stand up a **second credential store**
(`dynamic-security.json` client passwords) that cannot be a "projection" of
Postgres (Postgres holds device-key *hashes*, not broker passwords). go-auth's
HTTP backend authenticates each MQTT connect against an internal
`/internal/mqtt-auth` endpoint that reuses the shipped fail-closed device-key
check against `principals` (SHA-256-hex equality, kind + `revoked_at` filter).
Authz (topic ACL) is the same service's `/internal/mqtt-acl` check, computed from
`view_scope` membership under a least-privilege RLS session (T8). One cred store,
the shipped L4 model reused verbatim, no reconciler invented to sync a dual store.
**Spike-confirmed + pinned:** the Postgres backend *can't* compare a bare hex
SHA-256, so the **HTTP backend** is the mechanism (it forwards `clientid` too).
`mosquitto-go-auth` is archived (2025-06) and only the **Mosquitto 2.0.x** plugin
ABI is evidenced → **pin Mosquitto 2.0.21 + go-auth final master by digest**; the
plugin is a dumb forwarder, so all credential/ACL logic lives in our maintained
endpoints. *(Force-disconnect on revoke (T5): go-auth ACL re-check with cache
TTL→0 halts new delivery; a hard kick of an idle subscriber uses a broker
management action — settled in M3.)*

### B3. View-scope: **a Postgres table that extends B3**, projected outward
`view_scope` is the single writable source of truth (RLS-scoped). It is consulted
**inside** the `location_fixes` RLS policy via `app.viewer_may_see(...)` (T3). The
broker ACL and history/live paths are projections; the **projection-equivalence
test asserts equivalence to the RLS decision** (not merely broker-set ==
gateway-set).

### B4. Live feed: **backend-proxied WebSocket, NOT direct broker-WS**
Unanimous. loc-api holds the broker subscription(s) and re-broadcasts to browsers
over its **own authenticated WSS** after applying view-scope **per message**. No
MQTT creds in browser JS; broker stays private; one enforcement + audit point.
To shrink blast radius, prefer **per-subject backend subscriptions gated by
current scope** over one god-subscription; the fan-out filter re-reads scope per
message (or short TTL) so revocation is near-immediate, and the **live path writes
`view_audit` too** (not just history).

### B5. History: **RLS-scoped Timescale queries** (Recorder dropped, T1)
The timeline tab queries `location_fixes` through a `device_context`/view-scoped
session; the DB enforces the subject-pin + family-group scope. No `X-Limit`
header discipline, no Recorder. **Member apps are history-capped at 30 days**
*(owner decision)*: the member dashboard may only request the trailing 30-day
window (enforced server-side, not client-trusted — an out-of-window request is
clamped/rejected). The **owner has the full, uncapped history** (plus everything
else in JBrain2). Same map / devices / timeline tabs; the member view simply
doesn't go back as far.

### B6. Notifications: **MQTT-while-connected + FCM wake-from-Doze**, deduped
- **MQTT** delivers live events to a foreground/connected viewer; the app builds
  a **local** notification on-device.
- **FCM** is a **content-free doorbell** (data-only, HIGH priority, rotating
  nonce) that wakes a backgrounded/dozing viewer so it opens the pinned channel
  and **fetches the real event from the box**, composing the notification string
  **on-device** (T6).
- **Dedupe:** the server pokes a viewer only if it is **not** currently holding a
  live connection for that subject; the on-device fetch is **idempotent on event
  id**, so a redundant poke shows nothing. (Also avoids burning FCM's
  HIGH-priority budget on suppressed-notification messages.)

### B7. Client: **fork `owntracks/android`, minimal additive diff, two flavors**
- **`gms`** flavor: `firebase-messaging` (for FCM) **without** Google Maps
  (map lives in the WebView). For stock-Android family phones.
- **`oss`** flavor: no Google; FCM degraded to live-channel-only;
  UnifiedPush/ntfy is the **v1.1** path. Keep the flavor seam now so adding a
  UnifiedPush sender later is additive.
- Keep **Eclipse Paho** for v1 (battle-tested in OwnTracks); Paho replacement is a
  tracked post-v1 risk.

### B8. WebView auth: **Keystore cred → `/session/mint` → HttpOnly cookie**
Device credential lives in the **Android Keystore** (hardware-backed,
non-exportable), **never in JS**. On dashboard open, native POSTs the device key
to `/session/mint` **over TLS** — the *same* credential the device already
presents on the MQTT / OwnTracks path, verified against the shipped kind-filtered
device lookup (saltless SHA-256, `revoked_at IS NULL`) — and the response
Set-Cookies an **HttpOnly + Secure + SameSite=Strict** session cookie bound to
the authenticated subject and its view-scope. The WS upgrade authenticates the
cookie **and** a strict Origin allow-list.

> **Owner decision (2026-06-18): direct-key over TLS, not HMAC challenge-response.**
> The original draft mooted an HMAC-of-nonce so the long-lived secret is *never
> sent*, but the shipped `principals` table stores only a one-way saltless
> SHA-256 hash — HMAC verification would require a *new* recoverable per-device
> secret (extra column, encrypted at rest, rotation). The key already transits
> TLS on the MQTT/OwnTracks path, so direct presentation adds no new exposure and
> reuses `authenticate_device` verbatim. `cred_epoch`-carrying cookies + instant
> revocation remain an M7 owner-controls concern; M4a mints a long-lived
> device-principal session whose cookie is invalidated by revoking the principal.

---

## C. Data model (all tables `domain_id=location`, FORCE RLS, isolation test each)

Reuses shipped `subjects`, `principals` (`kind IN owner|capability_token|
device_key`), `location_fixes`, `place_geofence`, `geofence_state`.

```
pairing_code(code PK[160-bit], device_principal_id FK, subject_id, mode,
             expires_at[<=15m], redeemed_at, created_at)      -- one-time, TTL'd
family_group(id PK, name, created_at)                         -- one group for v1
view_scope(group_id FK, member_subject_id, member_device, joined_at,
           added_by[owner], PK(group_id, member_subject_id, member_device))
                              -- group MEMBERSHIP; owner-only add/remove (T3/T7)
view_audit(id PK, viewer_principal_id, target_subject_id, path[live|history|poke],
           triggering_event_id, at)                          -- who-saw/was-poked-about-whom
fcm_token(id PK, device_principal_id FK, token, platform, created_at,
          last_seen_at, revoked_at)                          -- bound to principal
```

- **Flat-group model:** `view_scope` is **group membership**, not per-pair grants.
  Two members of the same group may see each other; that *is* the policy. (The
  table keeps the per-row shape so asymmetric scopes remain possible later without
  a migration, but v1 configures exactly one mutual family group.)
- **`app.viewer_may_see(viewer_subject, target_subject)`** SQL helper (SECURITY
  DEFINER) = "both subjects share a `family_group`." Consulted by the extended
  `location_fixes` policy (T3). Default deny.
- **Member history cap (B5):** the member dashboard/API enforces a **30-day**
  trailing window server-side; the owner is uncapped. Enforced in the query layer
  on top of the RLS scope (a member request for older rows is clamped/rejected),
  not trusted to the client.
- **B3 extension** on `location_fixes` USING/WITH CHECK:
  `app.has_domain_scope('location') AND (app.is_full_owner()
   OR subject_id::text = current_setting('app.subject_id', true)
   OR app.viewer_may_see(current_setting('app.subject_id', true), subject_id))`.
  The first two terms are **unchanged** from shipped B3; only the `viewer_may_see`
  term is new — base policy not loosened.
- **`view_scope` RLS:** a viewer reads its own grant rows; a **target** may read
  rows where it is the target (powers "who can see me") via a distinct
  target-scoped policy; full owner reads all; everyone else zero.
- **`fcm_token` RLS:** a device reads/writes only its own token rows.
- Retention: **no auto-purge** *(owner decision)* — all history is kept. Storage
  is a non-issue at family scale (5 devices ≈ tens of MB to ~1.3 GB/yr
  uncompressed, far less with Timescale columnar compression on cold chunks).
  Because retention is therefore **not** a breach mitigation, the compensating
  controls in T7 (encryption at rest, `view_audit`, prompt revocation) are
  load-bearing.

---

## D. Component topology

```
[Forked OwnTracks gms/oss APK] --MQTT/TLS 8883--> [Caddy: L4/SNI for :8883,
   - Keystore device cred                          HTTPS for /dash /api /session,
   - locked WebView -> /dash (HTTPS) ---------------- wss /live]
   - FirebaseMessagingService (gms)                       |
                                                          v
                              [Mosquitto 2.x + mosquitto-go-auth] --auth/acl HTTP-->
                                 |  (1883 internal, 8883 TLS,           [loc-api /internal/mqtt-auth
                                 |   9001 ws internal -> loc-api)        -> principals + view_scope (RLS)]
                                 v
                         [loc-api  (router in the JBrain2 FastAPI image)]
                           - MQTT ingest consumer -> existing ingest -> location_fixes
                             (device_context, subject-pin, L5a geofence-at-ingest)
                           - history: RLS-scoped Timescale queries (NO Recorder)
                           - live: per-subject scoped subs -> authed wss fan-out + view_audit
                           - issuer: pairing redeem, session mint, fcm token registry
                           - FCM v1 sender (content-free poke; service-acct secret via storage abstraction)
                                 |
                                 v
                  [Postgres (RLS): pairing_code, view_scope, view_audit, fcm_token,
                                   + shipped principals/subjects/location_fixes/geofence]
        [Photon geocoder on internal:true net — unchanged, L1]
```
Recorder and any unscoped frontend are **omitted**. The Firebase service-account
JSON is a secret loaded via the storage/secret abstraction (non-negotiable #2).
`scripts/dev-setup.sh` gains Mosquitto + go-auth + the Firebase Android dep +
service-account bootstrap (non-negotiable #8).

---

## E. Milestones (thin walking-skeleton first; each independently security-testable)

- **M0 — Secure spine.** Caddy + Mosquitto (TLS, go-auth → `/internal/mqtt-auth`
  against `principals`, **anonymous rejected, deny-by-default**) + one
  hand-provisioned device key. Gate: in-scope publish authenticates; out-of-scope
  subscribe receives **nothing**; TLS pin works. *No app, no UI.*
- **M1 — MQTT ingest bridge.** Consumer lands fixes in `location_fixes` under
  `device_context()`; **L5a geofence-at-ingest moved here**; HTTP `/pub` still
  live. Gate: parity with HTTP ingest; geofence transitions still fire into the
  workflow engine; L3 chunk-isolation re-proven for the new path.
- **M2 — Pairing + view-scope.** `pairing_code`/`view_scope`/`view_audit` tables
  + RLS isolation tests; redeem mints/binds the device principal; **B3 extension**
  (`viewer_may_see`) lands. Gate: per-table isolation + pairing-abuse suites
  green; family-sees-family enforced **by the DB**.
- **M3 — History (RLS Timescale) + proxied live WS.** Per-subject scoped subs →
  authed wss; live + history both deny-by-default, re-checked, audited. Gate:
  projection-equivalence-to-RLS test; mid-session revocation stops live delivery
  within bound.
- **M4 — Dashboard SPA + WebView auth.** `/session/mint` → cookie; WS upgrade
  cookie+Origin; lockdown; SPKI pin + backup. Gate: WebView-auth security tests.
  Sliced: **M4a** session mint (device key → member cookie) ✓; **M4b** member
  reads (positions + presence, RLS-scoped, 30-day cap) ✓; **M4c** per-place
  `place_share` opt-in → member shared-fence overlay + shared/visible-subject
  timeline ✓; **M4d-1** member live-WS (owner+member on one socket, per-fix
  view-scope filter, CSWSH Origin allow-list) ✓; **M4d-2** the member SPA at /dash
  — separate Vite entry, session gate (B8 cookie, key stays native), Devices /
  Timeline / Map (Leaflet trail + shared fences + live WS) ✓. **M4 complete.**
- **M5 — Forked APK.** `oss`+`gms` flavors; pairing screen (`MessageConfiguration`
  inject, `remoteConfiguration=true`); background-location + OEM walkthrough;
  FGS/boot hardening; remote mode switch (verify cmd JSON). Gate: instrumented
  security + one aggressive-OEM reliability pass.
  Sliced: **M5a** the `android/` app skeleton — a locked-down WebView host
  (`DashboardActivity`) loading the server's `/dash`, a unit-tested `DashboardConfig`
  URL helper, the Gradle build + a CI `android` job (SDK setup → `assembleDebug` +
  JVM unit tests; on-device/emulator tests need KVM, out of CI scope) ✓; **M5b**
  native `/session/mint` → injected cookie: a MockWebServer-tested `SessionMinter`,
  a Keystore-backed `CredentialStore` (EncryptedSharedPreferences), and a
  `SessionLauncher` that reads the key → mints → loads /dash, self-healing a revoked
  key to pairing ✓; **M5c** pairing-code redeem: a MockWebServer-tested
  `PairingClient` (redeem → device key = OwnTracks config `password` + the config),
  a `PairingCoordinator` that persists both, and a native `PairingActivity` the
  launcher routes to and re-launches from once paired ✓; **M5d-1** WebView lockdown
  hardening — a JVM-tested same-origin `NavigationPolicy` + `LockedWebViewClient`
  that refuses any off-origin navigation, plus mixed-content NEVER_ALLOW, file-URL
  access off, and no JS bridge ✓; **M5d-2** location publishing (owner chose the
  minimal-publisher path over forking OwnTracks): a JVM-tested `LocationReport`
  (OwnTracks `_type:location` encode) + `LocationPublisher` (POST `/api/owntracks`,
  device key as Basic password, 401→clear, 429→back off) and a framework
  `LocationService` (foreground, LocationManager) the dashboard starts after auth;
  doze/aggressive-OEM reliability is a deferred hardening pass ✓. **M5 complete.**
- **M6 — FCM.** `fcm_token` registry (+ RLS test); content-free poke sender;
  view-scope-aware routing; dedupe; on-poke fetch-then-local-notify. Gate:
  **no-PII-in-payload** test, routing test, revoke-kills-token test.
  Sliced: **M6a** the `fcm_token` registry — migration 0075 (device-scoped RLS:
  a device manages only its own token, owner/system reads all), `SqlFcmTokenRepo`
  (register upsert / delete / active-only `tokens_for_subjects`), the member
  PUT/DELETE `/fcm-token` endpoints, and the RLS isolation test incl.
  revoke-drops-token ✓; **M6b** the content-free poke sender + view-scope routing:
  a PII-free `fcm_message` (data-only, no `notification` block) + `FcmNotifier`
  (HTTP v1, per-token, survives a bad token) + `NullNotifier`, and a `PushRouter`
  that pokes `visible_subjects(X)`-minus-X's active de-duped tokens. Gates: no-PII
  payload, co-members-only routing, revoke-drops-out ✓; **M6c-backend** the live
  trigger — `detect_transitions`/`ingest_location` thread an optional notifier (HTTP
  + MQTT paths), so a confirmed crossing fires one content-free poke to the family
  group; `app.state.push_notifier` is None until FCM creds are configured at deploy.
  Proven against PostGIS (a crossing pokes co-members; None notifier is a safe
  no-op) ✓. **M6 server-side complete.** *Deferred to Android hardening:* the FCM
  receiver (gms flavor + Firebase project) → fetch-then-local-notify, and wiring
  `FcmNotifier` + its service-account OAuth provider at deploy.
- **M7 — Owner controls + ops.** Owner-only **add/remove member** + **revoke**
  (kills MQTT session + `cred_epoch` bump + membership tombstone); the **30-day
  member history cap** (B5); `view_audit`; **encryption at rest** (compensating
  control — no retention/gate/self-Leave per T7); Caddy L4 + SIGHUP-on-renewal
  hook; keystore backup/escrow runbook; pin-rotation runbook. *(No in-app Leave
  button; the Android-mandated tracking notification covers "you're being
  tracked.")*
  Sliced (the 30-day cap [M4b] + `view_audit` [M3a] already shipped): **M7a** owner
  family-membership management — `SqlFamilyRepo` (get-or-create the v1 group,
  add/remove/list members) + owner `GET/POST/DELETE /api/family/members`; no new
  table (owner-only `view_scope` RLS is the barrier). Proven against PostGIS:
  add→mutual `viewer_may_see`, remove→ends, writes owner-only, add idempotent ✓;
  **M7b** member revoke — the per-publish MQTT ACL now re-checks revocation (a
  revoked device is denied even on its own namespace, killing the live session
  within bound, not just at the next connect), and owner `POST
  /api/family/members/{id}/revoke` tombstones the device principal AND drops it
  from the family. The instant dashboard-cookie 401 already falls out of the
  session lookup's `revoked_at` filter, so no `cred_epoch` column is needed for
  revoke; `cred_epoch`'s distinct value (invalidating sessions on key *rotation*,
  where the principal stays active) is a separate refinement, deferred ✓;
  **M7c** encryption-at-rest + the Caddy/keystore/pin-rotation runbooks.
- **v1.1 — UnifiedPush/ntfy** for de-Googled phones (additive sender behind the
  push-backend-agnostic interface).

Rollout: single owner test phone end-to-end → **one** family member at
**self-only** scope → owner provisions scope at discretion (no gate, T7) →
expand one device at a time, watching the who-saw-whom audit and per-device
last-seen (OEM-killer telemetry).

---

## F. Owner decisions — RESOLVED

1. **Transport:** **Keep both permanently.** MQTT primary; HTTP `/pub` a permanent
   per-device fallback (broker-outage insurance, easy rollback). Shared ingest
   core → low marginal cost. (B1, T-transport.)
2. **Retention:** **No auto-purge — keep everything.** Storage is a non-issue at
   family scale. Retention is *not* a mitigation, so encryption-at-rest +
   `view_audit` + prompt revocation are load-bearing. (C-retention, T7.)
3. **FCM flavor:** **Accept the `gms` flavor** (`firebase-messaging`, no Google
   Maps). `oss` stays pure (zero Google); UnifiedPush/ntfy deferred to **v1.1**
   for de-Googled phones. (B7.)
4. **Membership & consent:** **Owner is the sole gatekeeper.** One **flat family
   group** (everyone sees everyone); owner-only add/remove; **no consent gate** and
   **no in-app self-Leave** (members opt out by uninstalling). The
   Android-mandated tracking notification + `view_audit` are the residual
   safeguards. (T3, T7.)
   - **Member app history is capped at 30 days**; the owner is uncapped. (B5.)
5. **Branding:** **App name = JBrain360.** New `applicationId` (e.g.
   `org.jbrain.jbrain360`) + custom name/icon to avoid collision with stock
   OwnTracks; ship the EPL-1.0 license + the source of modified files with each
   distributed APK. (B7.)

---

## G. Testing (repo norms: 80% backend / security paths 100% / real Postgres via
testcontainers / external services faked / isolation test per new table)

- **RLS isolation** per new table (`pairing_code`, `view_scope`, `view_audit`,
  `fcm_token`), incl. the target-scoped "who can see me" read and the go-auth
  least-privilege role (T8).
- **Family-group enforcement, both paths:** live — a member receives other
  group members, receives **zero** for a non-member (and the negative
  `publishClientReceive`/ACL-miss case); history — a request for a non-member →
  **403**, their rows never returned; non-member device → sees nothing (never
  "all").
- **Member history cap:** a member request for fixes older than **30 days** is
  clamped/rejected server-side; the owner (full) session is uncapped — both proven.
- **Projection-equivalence to the RLS decision** (broker ACL ≡ history query ≡
  live filter ≡ `viewer_may_see`).
- **Revocation:** kills live MQTT session within bound; `cred_epoch` bump → instant
  401; `fcm_token` invalidated; re-pair required.
- **FCM:** **no location/PII in any payload** (property/fuzz over the serializer);
  view-scope-aware routing; dedupe vs live; revoke-kills-token (fail-closed).
- **Pairing abuse:** one-time (409 reuse), TTL (410), rate-limit (429),
  bound-device (403), redemption fails-closed (no orphan creds).
- **Owner controls + audit (residual safeguards, T7):** every live + history +
  poke access writes a `view_audit` row; **owner-only** add/remove member +
  revoke tombstones membership and drops the live session within bound. (No
  consent-gate and no self-Leave tests — owner is sole authority per T7.)
- **L1:** family-view paths are the only new location egress; FCM send carries no
  domain data; broker public port rejects open subscribe.
- **Client (instrumented):** Keystore cred never in JS/logs; WS upgrade Origin +
  cookie; TLS pin rejects MITM; mint token single-use/short-TTL; FGS survives
  overnight + boot on an aggressive-OEM device.

---

## H. Top risks

| Risk | Mitigation |
|---|---|
| Live-path residual exposure (broker ACL is the async copy) | go-auth ACL cache TTL→0 on live; per-message scope re-check; force-disconnect on revoke; per-subject (not god) subscriptions |
| MQTT auth/ACL endpoint launders cross-subject reads (go-auth HTTP backend, no DB role) | least-privilege RLS-scoped `/internal/mqtt-acl` + isolation test (T8) |
| Archived go-auth plugin breaks on a Mosquitto upgrade | pin Mosquitto 2.0.21 + go-auth final by digest; plugin is a dumb forwarder (logic in our endpoint); EMQX native-HTTP-auth is the escape hatch if needed |
| Two stores of location truth | T1: Recorder dropped; `location_fixes` sole source; history via Timescale |
| Second credential store / mislabeled "projection" | T2/B2: go-auth against `principals`; no dynsec passwords; no new hash column |
| Geofence transitions silently stop on MQTT | T4: L5a moved into the MQTT consumer under `device_context()` |
| FCM leaks location to Google / lockscreen | T6: data-only content-free nonce; on-device string composition; no-PII test |
| FCM HIGH-priority downgrade (suppressed notifications) | presence-dedupe (don't send) over send-then-suppress |
| OEM battery killers stop the FGS (#1 field risk) | onboarding + re-runnable OEM "reliability checkup"; staleness badge; never imply real-time |
| Self-signed keystore loss = no updates | redundant offline encrypted backup/escrow; CI-injected; runbook |
| SPKI pin bricks stale installs on cert rotation | ship backup pin N releases ahead; rotation runbook; soft-fail min-version |
| `remoteConfiguration=true` is a remote-control surface | restrict cmd-topic publish via go-auth ACL; audit every config push; `monitoring:2` flood-bounded |
| Domestic-abuse / insider misuse (heightened: no consent gate, no self-Leave, no retention, per owner T7) | residual safeguards mandatory — Android-mandated persistent tracking notification + who-saw-whom `view_audit` (live + history + poke) + prompt owner-initiated revocation + encryption at rest + 30-day member history cap |

---

## I. Verify-spike — RESOLVED (read against real source)

A spike read the shipped JBrain2 auth code and the current `owntracks/android`
source. Results:

**Confirmed / settled:**
- **MQTT auth = go-auth HTTP backend → our `/internal/mqtt-auth` endpoint** *(owner
  decision: Mosquitto + go-auth, pinned)*. go-auth's Postgres backend cannot
  compare a bare hex SHA-256, but its **HTTP backend** POSTs `{username, password,
  clientid}` to our endpoint, where we run the **shipped** `hash_key()` (incl.
  `normalize_key`) + `find_active_device_principal_by_key_hash()` and return
  allow/deny. One credential store; the plugin holds **zero** credential logic
  (dumb forwarder). ACL checks POST `{username, clientid, topic, acc}` to our
  `/internal/mqtt-acl`, evaluated against `view_scope` under RLS. **Pin Mosquitto
  2.0.21 + go-auth final master by digest** (go-auth archived 2025-06; only 2.0.x
  ABI is evidenced — `dev-setup.sh` pins both).
- **Ingest posture confirmed:** `device_context(principal_id, subject_id)` exists
  verbatim (`db/session.py`) — `principal_kind="device_key"`, subject-pinned,
  `domain_scopes=("location",)`. The MQTT consumer ingests under it; pairing mints
  via `create_principal(kind="device_key", …)`.
- **Remote mode switch:** `setConfiguration` cmd verified in source; `monitoring`
  ∈ {-1 Quiet, 0 Manual, 1 Significant, 2 Move}; gated by `remoteConfiguration`
  (**default false** → paired config ships it `true`) + `cmd` (default true).
- **`subTopic` multi-filter:** supported (space-separated). **Config one-tap
  pairing:** feasible by calling `saveConfiguration()` directly, bypassing
  `LoadActivity`'s apply-button preview. **WebView:** none exists; add an Activity
  + a `DrawerProvider.kt` drawer entry (nav is a drawer, `MapActivity` is the main
  entry).
- **MQTT TLS edge:** simplest is to **expose Mosquitto `:8883` directly** with its
  own cert; nginx-`stream` `ssl_preread` is the rock-solid fallback; Caddy
  `layer4` SNI works but needs a non-standard `xcaddy` build (deprioritized).

**Corrections to earlier assumptions:**
- **FGS type is `connectedDevice`, not `location`.** OwnTracks' `BackgroundService`
  declares `foregroundServiceType="connectedDevice"` (+ `FOREGROUND_SERVICE_
  CONNECTED_DEVICE`). We **inherit** this working choice (Play policy is moot —
  we sideload). Any "must declare location FGS" assumption is dropped.
- **`cmd`-topic restriction:** when `subTopic` is non-default, commands are only
  accepted on `receivedCommandsTopic` — the paired config must align them.

**Remaining build-time checks (not blockers):** Paho version / reconnect behavior;
the force-disconnect-on-revoke mechanism (go-auth ACL re-check with cache TTL→0
stops *new* delivery; a hard kick of an idle subscriber needs a broker management
action — settle in M3); FCM `google-services` Gradle plugin presence +
`onNewToken`-vs-auth-readiness ordering + min Play Services floor + collapse-key
cap (M5/M6).

---

## J. Future phase — quantified-self + usage collectors (post-M7)

Discussed and deferred. Two **phone-side, polled/batched** collectors (a periodic
worker, not a live stream) that reuse every JBrain360 rail — the transport, the
go-auth identity, the shared ingest core, `device_context`, and the firewall
domains. Owner-decided reach:

- **Usage / screen-time → everyone (you + others).** The *same* collector runs on
  every JBrain360 phone, person-attributed. Visibility falls out of RLS: the full
  owner sees all, each person sees their own, and cross-person visibility is the
  **M2 view-scope family group**. So the owner's own usage is quantified-self and
  others' is monitored — one collector, one domain, RLS decides who sees what.
- **Health → owner only.** Runs solely on the owner's phone; the owner is subject
  AND viewer, so there is **no view-scope and no consent** — pure quantified-self.

### J1. Health (Health Connect)
On-box read via `androidx.health.connect` (steps, distance, heart rate, sleep,
activity). **Granular per-metric** read permissions + the **background-read**
permission (Android 14+/Health Connect) so it syncs without the app open;
incremental **change-token** sync on a periodic WorkManager job. Lands in the
shipped **`health` firewall domain** (Timescale fits the time-series). No off-box
path — a wearable-cloud pull (Fitbit/Garmin) would be egress via the EgressGuard
Proposal path and is out of scope.

### J2. Usage / screen-time (UsageStatsManager)
`UsageStatsManager` behind the **`PACKAGE_USAGE_STATS`** special-access permission
— a **manual per-device grant** (Settings → Usage access, deep-linked in
onboarding). **Not Accessibility** (that triggers the Play-Protect hard-block on a
sideloaded app — a line we do not cross), so the depth is screen on/off,
screen-time totals, and per-app foreground time. Lands in a **new low-sensitivity
`usage` domain** (distinct from `health` — screen time isn't medical), which
extends the same `viewer_may_see` view-scope the location domain uses.

### J3. Shared modeling note
Both are **person-attributed**, unlike location's **device-attribution** — so add a
**device→person link** (this phone belongs to this person-subject) and attribute
health/usage to the person. Each new domain table ships its RLS isolation test.

### J4. Privacy (heightened: monitoring others)
Usage-of-others is parental-control territory — a normal guardian use for minors,
the documented abuse pattern for another adult. The owner is sole gatekeeper (T7),
but the residual safeguards carry more weight per added data class: the
Android-mandated tracking notification + the who-saw-whom `view_audit` remain the
floor. `PACKAGE_USAGE_STATS` requiring a manual on-device grant is itself an
inherent transparency checkpoint — it cannot be enabled remotely or silently.

### J5. Roadmap
- **M8a — Health (owner-only):** standalone; independent of view-scope, so it can
  land any time after the location MVP.
- **M8b — Usage (everyone):** depends on **M2** (view-scope/family group); follows
  M2–M7. Adds the usage collector + the `usage` domain + the device→person link.
