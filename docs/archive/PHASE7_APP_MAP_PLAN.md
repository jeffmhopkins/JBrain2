# JBrain360 app — full-screen live-map build plan (Phase 7)

> **Status:** Shipped 2026-07 · \`MemberDashboard.tsx\` + \`api/member.py\` coords

Rebuilds the **member dashboard** (`/dash`, served into the Android app's WebView)
from the Devices/Timeline/Map tab shell into a **full-screen live map with floating
glass chrome**. Binding spec: `docs/mocks/app-live-map.html` (owner-approved;
recorded in `DESIGN.md` → "JBrain360 app — member live-map surface"). Executed under
`docs/PROCESS.md` (per-wave PR, independent adversarial review, red-team for any
firewall-touching wave).

## What it is

A Leaflet basemap (the same-origin `/api/tiles` proxy — no third-party tile host)
fills the screen. Floating over it: a **person switcher** (Everyone + each family
member, with live/stale presence dots), a **Trail/Heat** toggle, a **1–7 day** range
slider, map controls, and an expandable **"last actions"** card (arrived/left
transitions, names + times only). Selecting a person recenters the map and swaps the
overlay + card; Everyone shows all current pins + a roster card.

## Load-bearing invariants (a member surface — red-team every firewall-touching wave)

- **Family scope only.** A member (device-key) session sees **its own subject + its
  family group** — `app.viewer_may_see` / `app.visible_subjects`. No read may return
  a non-visible subject's roster row, coordinate, trail, or transition. Every new or
  widened read needs an **RLS isolation test** (CLAUDE.md non-negotiable #3).
- **Tiles never leave the box.** The basemap is Leaflet over `/api/tiles` (the proxy
  fixed to serve any session). No external tile host.
- **Names + times only in prose.** The last-actions card/timeline shows place names
  and times — never a raw lat/lon in text. Coordinates exist only as map geometry.
- **30-day retention cap** stays server-enforced (member.py clamps `since/until`).
- **Owner-approved spec is binding.** Build to the mock; deviations are
  critical-decision escalations.

## Reuse map (from the grounding inventory — most of this already exists)

| Need | Status | Source |
|---|---|---|
| Roster + presence (`/api/member/roster`) | ✅ exists | `member.py`, `member_roster()` |
| Per-subject trail over `[since,until]` (`/api/member/positions`) | ✅ exists | `member.py` (30-day capped) |
| Transitions / last actions (`/api/member/timeline`) | ✅ exists | `member.py`, visibility-scoped |
| Shared fences (`/api/member/places`) | ✅ exists | `member.py` |
| Family-scoped live WS (`/api/locations/live`) | ✅ exists | `liveSocket.ts` (server-scoped) |
| RLS visibility (`viewer_may_see`/`visible_subjects`) | ✅ exists | `viewscope.py` |
| Leaflet trail **and** heat (`leaflet.heat`) | ✅ exists | `leafletMap.ts` (`mode: trail\|heat`) |
| **Latest coordinate per visible subject** (Everyone pins + switcher) | ❌ **new** | extend `member_roster` |

So the only backend gap is the **current coordinate per visible subject**. The roster
query already LEFT JOINs each subject's latest fix (for `last_seen`/battery); Wave 1
adds `lat`/`lon`/`captured_at` to that same row. Everything else is frontend.

## Waves

**Wave 0 — GUI gate + plan (docs).** The approved mock lands in `docs/mocks/`, the
decision in `DESIGN.md`, this plan. *(This PR.)*

**Wave 1 — backend: roster coordinates.** Extend `member_roster` / `MemberSubject`
(repo + API out model + TS type) with the latest `lat`/`lon`/`captured_at` for each
visible subject, so the map can pin everyone without N round-trips. **Firewall wave**
— an RLS isolation test proving the coordinate is scoped to `visible_subjects` (a
non-family subject's position never appears), plus the per-wave red-team. One backend
PR.

**Wave 2 — frontend: full-screen shell + switcher + pins.** Rewrite
`MemberDashboard` into the full-screen Leaflet map; the floating **person switcher**
(Everyone + members, presence dots) driven by the roster (now with coords) → current
pins; the floating bottom **status card** (selected person's presence + battery) /
**roster** in Everyone mode; the floating glass chrome + tokens (extend
`tokens.css`/`styles.css` per the mock). API client + **mock fixtures** for every
state (default, empty, error). Reuses `leafletMap.ts` + `liveSocket.ts`. One
frontend PR.

**Wave 3 — frontend: overlays, range, last-actions.** The **Trail/Heat** toggle and
**1–7 day** range driving `/api/member/positions` → `leafletMap` overlays
(client-side heat as today); the expandable **last-actions** card from
`/api/member/timeline` filtered to the selected person (Today / Yesterday / N-days
groups); live-socket extension of the current person's trail/pin. Mock fixtures +
component tests for switching, range, mode, and card-expand. One frontend PR.

Each wave: local `ruff`/`pyright` or `biome`/`tsc` + unit tests before integration;
an independent reviewer reads the wave diff (red-team on Wave 1); one PR; CI green;
merge; proceed.

## Out of scope (carried)

- In-app camera **QR scanner** (separate Android slice).
- **Batched** upload (the offline queue shipped; batching deferred).
- Owner-side place editing from the member surface (members see shared fences only).
- Per-subject server-side timeline filter — client-side filter is sufficient at the
  500-row cap; revisit only if it bites.
