# Location surface — mock gate (Phase 7, Wave 0)

Three interactive directions for the **Phase 7 location surface**, per the
PROCESS.md GUI gate. Pick one; the chosen mock becomes the binding spec and its
rationale is recorded in `DESIGN.md` + `docs/PHASE7_LOCATION_PLAN.md` when Wave 5
(the UI) implementation starts.

All three cover the **same three jobs** on one screen — **device management**
(provision / rotate / revoke per-device OwnTracks keys, last-seen + battery +
connection), a **geofence/place editor**, and **activity** (arrived/left
transitions + per-device last-seen). All honor the binding design system:
self-contained phone-framed HTML, dark-first with a working theme toggle,
**tokens-only colors with location = teal** (`--teal`, the Phase-7 domain accent),
outline SVG icons, bottom sheets for forms, center-style destructive actions.

They also encode the plan's load-bearing invariants so the chosen direction is
faithful to the firewall, not just pretty:

- **Owner-eyes only** — a privacy line states location is never shared with scoped
  links (L8); each device tracks only its own subject (L5 subject-pin).
- **No tiles leave the box** — the map direction (B) is **self-rendered** (CSS
  grid + pins), no third-party tile servers (L1); addresses show a "local geocode"
  tag (B10).
- **Device key shown once** — provisioning dialog mirrors `rotate_owner_key`
  (key shown once, amber warning) and shows the OwnTracks HTTP+Basic config (B5/B12).
- **Geofence editor files a place note** — every editor states it writes a
  **place/correction note → graph → projector**, never the mirror table directly
  (L10, non-negotiable #7).

| File | Direction | Primary surface | Best when |
|---|---|---|---|
| `location-a-tabbed-console.html` | **Tabbed console.** Segmented Devices / Places / Activity; one list at a time; rich device cards with fix counts. | Lists + cards | Management is the main job — you mostly add/rotate devices and tune fences; activity is a tab you check. Closest reuse of the Settings/Ops list paradigm. |
| `location-b-map-anchored.html` | **Map anchored.** A self-rendered schematic map on top (last-seen pins + dashed fence circles), pull-up list with Devices / Places / Activity tabs; edit a fence by tapping it on the map. | Spatial map | The spatial mental model matters — you think in *where*, want to see pins and fence overlaps at a glance, and edit geometry directly. |
| `location-c-timeline-feed.html` | **Timeline feed.** A chronological activity feed is the home surface (arrived/left, low-battery, with on-box addresses); a presence strip up top; devices + places live in bottom-sheet managers. | Activity feed | "What happened / where is everyone" is the daily question; provisioning and fences are occasional, so they recede into sheets. Reuses the assistant/Talk feed idiom. |

## What each tests / trade-offs

- **A** is the most legible for *operating* the slice (provision, rotate, revoke,
  list fences) and the least new UI to build, but location is abstract — no spatial
  view; "where is everyone" is a tab, not the headline.
- **B** makes geofences and presence spatially obvious and makes geometry editing
  direct, but a real (even schematic) map is the heaviest build, and the on-box
  no-tiles constraint means the map is approximate, not a real basemap — set
  expectations accordingly.
- **C** answers the daily question ("who's where, what just happened") with the
  least chrome and naturally surfaces honest status (battery, last-seen, stale),
  but buries device/fence management one tap deeper and has no spatial view.

## Decision

**Chosen: _[pending owner selection]_.** Once picked, the chosen file is refined
to `location-chosen-<name>.html`, the **teal** location token is recorded in
`DESIGN.md` (the domain-color table) and `frontend/src/styles/tokens.css`, and the
a/b/c files are retained as the decision record. Mock fixtures for
default / empty / error / offline / revoked-device states are part of Wave 5's DoD.
Live-tracking map visualization beyond the schematic is a named, deferred follow-on.
