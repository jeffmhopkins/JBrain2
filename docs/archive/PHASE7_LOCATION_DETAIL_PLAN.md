# JBrain360 — high-detail, low-battery location trails (Phase 7)

> **Status:** Shipped 2026-07 · \`android/.../SamplingPolicy.kt\` + batched array ingest

Raises the member tracker from sparse, jittery trails to **Life360-grade detail**
without adopting Google Play Services. Executed under `docs/PROCESS.md` (per-wave PR,
independent adversarial review, red-team for any ingest/firewall-touching wave) and
the `CLAUDE.md` non-negotiables. No new runtime dependency — explicitly **no GMS**.

## Why

The trail draws as a star-burst of sparse, scattered points (see the owner report:
a stationary phone spraying lines over a block). Two root causes, confirmed by
tracing the pipeline and comparing to OwnTracks' source:

1. **We sample too slowly.** `LocationService` requests `GPS_PROVIDER` with a
   `30 s` interval **and** a `25 m` displacement filter (whichever is slower wins),
   so a moving device yields a point every ~500–800 m on the highway. OwnTracks gets
   detail from its **Move mode (~10 s)**; Life360 from motion-adaptive sampling.
2. **Nothing de-jitters.** Every stored fix is drawn and connected. The backend
   already gates geofence detection at `ACCURACY_GATE_M = 100`
   (`locations/geofence.py`) but the **trail render** applies no gate, so wide-radius
   indoor fixes get drawn. OwnTracks ships `ignoreInaccurateLocations`; we have the
   constant but never apply it to display or collection.

Detail comes from **faster, motion-adaptive sampling + an accuracy filter**;
**batching** is the efficiency trick that keeps dense sampling battery-affordable.
None of this needs Play Services.

## Provider decision (settled)

Use the **framework fused provider**, not the GMS client:

- `LocationManager.FUSED_PROVIDER` on **API ≥ 31** (on a Pixel/GMS phone this is
  backed by Google's own fusion — smoothed fixes, no SDK link), falling back to
  `GPS_PROVIDER` below 31 and on devices that don't supply a fused provider.
- We forgo the GMS-only extras (`setMaxWaitTime` hardware batching, Activity
  Recognition) and reproduce them at the app layer: a motion state machine for
  cadence, and an app-level batch drain of the offline queue.

This keeps the app's "minimal, no Play Services" posture and works on every
family phone (Pixel or otherwise), degrading to GPS where fusion is absent.

## Load-bearing invariants (ingest path — red-team Waves 1 & 3)

- **Subject is code-set from the authenticated principal (L9).** A batch ingest
  MUST still derive the subject from the device principal, never the payload — one
  device key can only write its own subject's fixes. Array ingest is a new shape,
  not a new scope; an RLS/auth test proves a batch can't smuggle a foreign subject.
- **Idempotent ingest.** Dedup stays a no-op on retry (a paused batch re-POSTs).
- **No new runtime dependency / no GMS.** Zero-new-dep goal is hard here.
- **Battery floor is non-negotiable.** Dense cadence applies **only while moving**;
  a stationary phone falls back to the heartbeat. The foreground-service contract
  (notification + `FOREGROUND_SERVICE_LOCATION`) is unchanged.
- **Degrade, never drop.** The disk queue still backfills in order with real
  capture times; a batch failure pauses and retries the same batch.
- **Tiles/coords invariants from `PHASE7_APP_MAP_PLAN.md` carry** (family scope,
  names+times in prose). This plan touches collection + ingest + render, not scope.

## Reuse map (most of the scaffolding exists)

| Need | Status | Source |
|---|---|---|
| Offline FIFO queue (disk, bounded, backfills) | ✅ exists | `FixQueue` / `FileFixQueue` |
| Drain-with-pause-on-failure | ✅ exists | `LocationUploader` |
| Pure JVM-tested report encode/decode | ✅ exists | `LocationReport` |
| Foreground service + heartbeat watchdog | ✅ exists | `LocationService` |
| Shared ingest core (HTTP + MQTT feed it) | ✅ exists | `locations/ingest.py` |
| Accuracy gate **constant + concept** | ✅ exists | `geofence.py` (`ACCURACY_GATE_M`) |
| Per-fix accuracy stored (`accuracy_m`) | ✅ exists | `models/location.py`, fixes/positions APIs |
| Single-fix HTTP ingest (`/api/owntracks`) | ⚠️ single only | `api/owntracks.py` — **extend to accept an array** |
| Motion-adaptive cadence | ❌ **new** | a pure `SamplingPolicy` state machine |
| Device-side accuracy filter | ❌ **new** | gate before enqueue |
| Batched (array) upload | ❌ **new** | extend `LocationUploader` + `LocationPublisher` |
| Trail render accuracy gate | ❌ **new** | `leafletMap.ts` (drop `accuracy_m > gate`) |

## Waves

**Wave 0 — Plan (docs). _(this PR)_** This document; a pointer from `docs/README.md`.
**No GUI gate:** this changes collection/ingest/render, not a GUI *surface* — no new
control or layout (the trail simply renders cleaner and denser). The §"Settled
decisions" below are owner-signed-off; Wave 1 may begin on merge.

**Wave 1 — Backend batch ingest + render de-jitter (no APK).** Two independent,
quickly-shippable wins, one PR:
- Extend `POST /api/owntracks` to accept **either** a single `_type:location` object
  **or a JSON array** of them; ingest each via the existing core, subject still
  code-set from the principal; size the rate limiter for a batch (consume per-fix,
  raise burst). Back-compatible — OwnTracks' single-object post still works.
- Apply the **100 m accuracy gate to trail rendering** in `leafletMap.ts` (drop
  `accuracy_m > ACCURACY_GATE` before building the polyline/heat; keep null-accuracy
  fixes). Owner map + member map both benefit (shared module).
- **Red-team wave** (ingest shape change): an auth/RLS test that a batch cannot write
  another subject's fixes, and a malformed-element-in-array test (one bad element
  ⇒ 422 or skip, never a partial-trust write). One backend+frontend PR.

**Wave 2 — Android: fused provider + motion-adaptive sampling (APK).**
- A pure, JVM-unit-tested **`SamplingPolicy`**: given recent fixes + elapsed time it
  decides the desired `(intervalMs, displacementM)` and the motion state
  (`MOVING` ⇄ `STATIONARY`) from displacement — no activity-recognition API. Moving
  ≈ `5 s` / `8 m`; stationary ⇒ the existing heartbeat. Hysteresis so a parked phone
  doesn't flap.
- A pure **accuracy filter** (`ignoreInaccurateLocations`-style): drop `acc > 50 m`
  before enqueue, so only good fixes are ever stored.
- `LocationService` requests `FUSED_PROVIDER` (API ≥ 31) / `GPS_PROVIDER` fallback,
  and **re-requests** updates when `SamplingPolicy` changes the target cadence.
- Raise `FixQueue` `CAP` to `5000` (≈ 7 h of moving fixes at 5 s) for the denser
  cadence. One Android PR; lands in the **same APK release** as Wave 3.

**Wave 3 — Android: batched upload (APK, depends on Wave 1).**
- Extend `LocationUploader` to **drain the queue as one array POST** (peek up to `N`,
  POST the array, remove `N` on success; pause keeps the batch). Flush trigger: every
  `N` points or `T` seconds of accumulation, plus the existing connectivity-return
  flush. `LocationPublisher.publish` gains an array body to `/api/owntracks`.
- JVM tests (MockWebServer/fake publisher): batch drain, partial pause/retry order,
  single-fix path still works. One Android PR; **same APK version bump** as Wave 2,
  so the owner installs once.

**Wave 4 — (optional, carried) server-side trail downsample.** Dense trails make
large windows heavy. Add distance/Douglas-Peucker downsampling in the
fixes/positions read for big ranges (keeps payload + render snappy; the live tail
stays full-resolution). No APK. Build only if the dense data actually bites.

Each wave: local `ruff`/`pyright` or `biome`/`tsc` (or the Android JVM unit tests) +
the task tests before integration; an independent reviewer reads the wave diff
(red-team Waves 1 & 3); one PR; CI green; merge; proceed.

## Settled decisions (owner-signed-off)

1. **Moving cadence — `5 s / 8 m`** (denser than OwnTracks' 10 s, near Life360);
   stationary always relaxes to the 15-min heartbeat.
2. **Accuracy gates — device `≤ 50 m` + render `100 m`.** Only good fixes are
   stored (drop `acc > 50 m` before enqueue); the trail additionally drops
   `> 100 m` at draw time (matches the geofence gate).
3. **APK packaging — one version bump** for Waves 2 + 3 (separate PRs, single
   install).
4. **Queue CAP — `5000`** (≈ 7 h of moving fixes at 5 s).

## Out of scope (carried)

- **GMS Fused / Activity Recognition** (Play Services) — explicitly excluded.
- **Map-matching / road-snapping** the trail — a larger, separate effort.
- In-app **QR scanner** and other Android slices.
- Aggressive-OEM doze/battery-killer hardening — the deliberate later pass noted in
  `LocationService`.
