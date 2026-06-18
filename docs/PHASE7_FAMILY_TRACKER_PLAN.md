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
- **T8 — go-auth runs least-privilege.** go-auth's own DB role is RLS-enforced
  and subject-pin-aware (or reads only a purpose-built, pre-projected ACL table
  with no cross-subject inference). An isolation test proves go-auth's role cannot
  read another subject's `view_scope`/`location_fixes`.

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
Authz (topic ACL) is the same endpoint's superuser/acl checks, computed from
`view_scope` under a least-privilege RLS role (T8). One cred store, the shipped
L4 model reused verbatim, no reconciler invented to sync a dual store.
*(dynsec `$CONTROL` kick is retained only as the force-disconnect mechanism for
T5 revocation, not as the auth system.)*

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

### B8. WebView auth: **Keystore cred → short single-use token → HttpOnly cookie**
Device credential lives in the **Android Keystore** (hardware-backed,
non-exportable), **never in JS**. On dashboard open, native does a
challenge-response `/session/mint` (HMAC of a server nonce — long-lived secret
never sent) → **≤5-min single-use audience-scoped token** → the **exact first
load URL** Set-Cookies an **HttpOnly + Secure + SameSite=Strict** session cookie
(carrying `cred_epoch`) and 302s into the SPA (because `loadUrl(headers)` only
covers the first request). The WS upgrade authenticates the cookie **and** a
strict Origin allow-list. The session is bound to the authenticated subject and
its view-scope.

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
- **M5 — Forked APK.** `oss`+`gms` flavors; pairing screen (`MessageConfiguration`
  inject, `remoteConfiguration=true`); background-location + OEM walkthrough;
  FGS/boot hardening; remote mode switch (verify cmd JSON). Gate: instrumented
  security + one aggressive-OEM reliability pass.
- **M6 — FCM.** `fcm_token` registry (+ RLS test); content-free poke sender;
  view-scope-aware routing; dedupe; on-poke fetch-then-local-notify. Gate:
  **no-PII-in-payload** test, routing test, revoke-kills-token test.
- **M7 — Owner controls + ops.** Owner-only **add/remove member** + **revoke**
  (kills MQTT session + `cred_epoch` bump + membership tombstone); the **30-day
  member history cap** (B5); `view_audit`; **encryption at rest** (compensating
  control — no retention/gate/self-Leave per T7); Caddy L4 + SIGHUP-on-renewal
  hook; keystore backup/escrow runbook; pin-rotation runbook. *(No in-app Leave
  button; the Android-mandated tracking notification covers "you're being
  tracked.")*
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
| go-auth role launders cross-subject reads | least-privilege RLS-pinned go-auth DB role + isolation test (T8) |
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

## I. Hands-on-verify before/within build

OwnTracks fork: manifest `foregroundServiceType="location"` present; nav hook in
`ui/base` for the WebView tab; config-import confirmation dialog (suppress for
one-tap pairing); multi-filter `subTopic` parsing; exact `setConfiguration` cmd
JSON the fork honors + that `remoteConfiguration=true` enables it; Paho version /
reconnect behavior. Server: go-auth ↔ Mosquitto 2.x build + a custom SHA-256-hex
equality query against `principals`; dynsec `$CONTROL` kick as the force-
disconnect path; Caddy `layer4` SNI route for `:8883`; SIGHUP-on-renewal. FCM:
`onNewToken` ordering vs app auth-readiness (queue/retry); minimum Play Services
floor; collapse-key cap.
