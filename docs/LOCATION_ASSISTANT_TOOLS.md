# Location assistant — tool catalog (reference)

Candidate AI-assistant tools, inline UI components, and automations for the Phase 7
**location** domain, so the assistant can answer questions ("what was Jeff's battery
last night at Walmart?"), show maps inline ("map of Jeff over the last week"), and
manage places — plus the proactive/analytics ideas we deliberately parked.

Produced by a multi-researcher brainstorm + an independent red-team pass (3 clusters).
This is a **reference list** — only the ✅ items are slated to build now
(see `docs/LOCATION_ASSISTANT_PLAN.md`); 🟡/⛔ are recorded so we don't re-derive them.

## Status legend
- ✅ **DO** — build-ready, privacy-clean, no missing infrastructure.
- 🟡 **MAYBE** — valuable but needs a dependency, tuning, or an owner decision.
- ⛔ **DON'T** — firewall-crossing, surveillance-shaped, or an owner-deferred line.

## Cross-cutting invariants (apply to every location tool)
- **Full-owner only.** Location reads need `app.is_full_owner()`; a narrowed /
  `owner_scoped` / sub-agent session sees zero rows. Every tool refuses such a
  session like `geocode_forward` and says so in its `.tool` prose.
- **Names, not coordinates.** Tools return names / times / distances / render-only
  GeoJSON to the owner UI — never raw lat/lon into the model's text, never the
  `raw` jsonb / SSID metadata.
- **Notes are the sole source of truth (#7).** No tool writes a fact or the
  `place_geofence` mirror directly; place authoring is an owner-approved note.
- **Egress only via owner-approved Proposal (#9).** Anything off-box is staged.
- **Three shared enablers** gate many items: the **person⇄device link** (#1), the
  **stay-point/trip segmenter** (#14), and a **proactive delivery channel** (the
  chat is pull-only today — nothing can be *pushed*). A **Timescale continuous
  aggregate** is the one infra add for trend rollups.

## The catalog

| # | Tool | Category | Status | Gate / why |
|--|------|----------|--------|-----------|
| 1 | Person⇄device link | Foundation | ✅ | unlocks "who"; note-authored fact via `entities.subject_id` |
| 2 | Repo reads trio (nearest-fix / latest-place / dwells) | Foundation | ✅ | powers most reads |
| 3 | `location_map` view | Component | ✅ | gap-aware, downsampled, proxy tiles |
| 4 | `place_card` view | Component | ✅ | owner-gated visit stats |
| 5 | `where_was_i` / `where_is` | Core read | ✅ | place/address only |
| 6 | `location_query` (battery@Walmart) | Core read | ✅ | aggregate + place name |
| 7 | `location_history` (map of X) | Core read | ✅ | downsample + gap-split |
| 8 | `time_at_place` / nights-away | Core read | ✅ | local-calendar, clamped dwells |
| 9 | `find_when_at` | Core read | ✅ | fail-closed place resolve |
| 10 | `device_status` / `battery_lowwatch` | Core read | ✅ | read-only flags |
| 11 | `home_status` | Core read | ✅ | current state + freshness |
| 12 | `nearby_now` (KNN) | Core read | ✅ | bounded-radius KNN |
| 13 | `save_place` (geofence-from-here) | Core write | ✅ | stages owner place-note Proposal (#7) |
| 14 | Stay-point / trip segmenter | Analytics | ✅ | keystone; accuracy-gate + gap-split |
| 15 | `commute_summary` | Analytics | 🟡 | needs ≥N distinct days |
| 16 | Distance / time trends | Analytics | 🟡 | continuous-aggregate migration |
| 17 | `mode_of_transport` | Analytics | 🟡 | p50/p85 + glitch reject |
| 18 | Presence heatmap | Analytics | 🟡 | dwell-weighted, coarsened |
| 19 | Rhythm punchcard / first-last-seen | Analytics | 🟡 | "no data" ≠ "not present" |
| 20 | Place discovery + `geofence_from_pattern` | Analytics | 🟡 | DBSCAN; proposes note (#7) |
| 21 | `who_was_with` / co-location | Analytics | 🟡 | needs #1; sustained + gated |
| 22 | Unusual-day anomaly (pull) | Analytics | 🟡 | baseline/variance guard |
| 23 | Territory hull + coverage report | Analytics | 🟡 | pair with confidence |
| 24 | `note_location` | Cross-domain | 🟡 | display-only; dual-scope if health/finance |
| 25 | `errand_check` | Cross-domain | 🟡 | dual-scope + privacy gate |
| 26 | `note_here` | Cross-domain | 🟡 | fail-closed name, no coords |
| 27 | Place-arrival surfacer | Proactive | 🟡 | needs push channel |
| 28 | Reminders-by-place | Proactive | 🟡 | conditional "not home by 10pm" → ⛔ yet |
| 29 | Nightly/weekly digests | Proactive | ✅ | pull artifact, no push needed |
| 30 | Went-dark / low-batt / flapping | Proactive | 🟡 | flapping → radius-correction Proposal |
| 31 | App-open presence chip + tone | Proactive | ✅ | owner reads own location |
| 32 | Arrival briefing | Proactive | 🟡 | pull-only; push variant gated |
| 33 | `life_replay` | Wild | 🟡 | grounded; own-devices co-location only |
| 34 | Predictive departure nudge | Wild | ⛔ yet | push channel + opt-in + Proposal |
| 35 | Route clustering / CoG drift / wiki pages | Wild | ✅ (Phase 6) | rides the wiki, not built yet |
| 36 | `place_spend` (finance × location) | Cross-firewall | ⛔ | E2 forbids; needs sanctioned primitive |
| 37 | `health_place_correlation` | Cross-firewall | ⛔ | most-protected domain; near-permanent |
| 38 | Device provisioning via agent | Sensitive | ⛔ | keep owner-only UI |
| 39 | Proactive anomaly auto-alerts | Surveillance | ⛔ | keep anomaly pull-only |
| 40 | `create_location_note` (auto-author) | #7 line | ⛔ | events-only; the owner-deferred line |

**Tally:** ✅ 16 · 🟡 18 · ⛔ 6 (5 hard + 1 "yet"). **Build-now spine:** 1–14, 29, 31
(35 is ✅ but Phase-6-gated, so it is not in the build-now set).

## Per-tool notes (red-teamed)

Each line: the build approach + the single most important mitigation the red-team folded in.

### Foundations & components (✅)
- **1. Person⇄device link.** `device.yaml` gains an `operatedBy` ref→person predicate, authored only via an owner note (never the LLM); the Device entity hard-links to its OwnTracks subject through the existing `entities.subject_id`; `SqlDeviceRepo.linked_person(ctx, subject_id)` joins it under RLS. *Mitigation:* a note-authored fact, invisible to non-full-owner sessions — closes the "Jeff" gap without an LLM-writable human↔track join.
- **2. Repo reads trio.** `nearest_fix` (±max-gap, returns `gap_seconds`), `latest_place` (from current `geofence_state`), `dwells` (enter→exit pairing, clamps open intervals, drops non-positive). *Mitigation:* `nearest_fix` surfaces the gap so no tool reports a stale fix as a real position.
- **3. `location_map` view.** Register in `views/registry.tsx` reusing `leafletMap.ts` (tiles via `/api/tiles`); gap-aware trail + ~2k-point downsample. *Mitigation:* coordinates live only in render-only slots.
- **4. `place_card` view.** Name + mini-map + address + last-visited/visit-count + note-sourced entity chips; stats omitted when not full-owner. *Mitigation:* derived stats owner-gated; chips note-sourced (no invented neighbors).

### Core reads (✅)
- **5. `where_was_i` / `where_is`.** Resolve subject (default owner device via #1) → `nearest_fix`/`latest_place` → on-box reverse-geocode + nearest place → prose + single-pin map; flags stale fixes. *Mitigation:* place/address only; coords confined to the map slot.
- **6. `location_query`.** Resolve `place`→fence (mirror, else on-box geocode) → `fixes_within(... ST_DWithin)` → **aggregate** (count, battery min/last, mean accuracy) + place name + map. *Mitigation:* place owner-gated, radius/window clamped, answer is aggregate-with-name.
- **7. `location_history`.** `fixes(...limit)` → downsample + gap-split → summary + trail map. *Mitigation:* downsample/gap-split before the payload crosses the wire.
- **8. `time_at_place` / nights-away.** Sum clamped `dwells`; nights bucket Home-fence dwells by the owner's local civil date (DST-safe). *Mitigation:* validated dwell-pairing on a local calendar.
- **9. `find_when_at`.** Resolve place→fence (ambiguous → ask), last-visit (owner tz) + frequency over a capped window; "no recorded visits" when none. *Mitigation:* fail-closed fence resolution.
- **10. `device_status` / `battery_lowwatch`.** `device_activity` + `DeviceRepo.list` + #1 labels; staleness/low as enum flag tones in a table. *Mitigation:* strictly read-only — flags are computed, never persisted.
- **11. `home_status`.** Per-subject `latest_place` cross-checked against `device_activity` freshness, person-labeled. *Mitigation:* current state + freshness, so an old fix is never "is here now"; hardest full-owner gate.
- **12. `nearby_now`.** Bounded-radius `ST_DWithin`/`<->` KNN over fences + past-stay clusters, name+distance only. *Mitigation:* GiST-indexed bounded KNN — no whole-table scan, no coordinate dump.
- **13. `save_place`.** `mutate` tool, full-owner; resolves current position → **stages a place-note Proposal** (owner approves text + `{center,radiusMeters}`) → ingest → extraction → `project_place_geofences`. *Mitigation:* never a direct fact/mirror write (#7); no sub-agent can plant a place.

### Analytics (✅ keystone + 🟡 tier)
- **14. Stay-point/trip segmenter (✅ keystone).** `segments(...accuracy_gate, roaming_radius, min_dwell, max_gap)`: accuracy-gate, sessionize with `lag` distance + gap-split, label stays vs in-transit with confidence. *Mitigation:* gate + gap-split in the base CTE so dependents inherit clean segmentation.
- **15–23 (🟡).** commute_summary (≥N distinct days), distance/time trends (continuous aggregate), mode_of_transport (p50/p85 + glitch reject), presence heatmap (dwell-weighted, coarsened), rhythm punchcard ("no data"≠absence), place discovery + geofence_from_pattern (DBSCAN, proposes a note), who_was_with/co-location (sustained + gated, needs #1), unusual-day anomaly (baseline/variance guard, pull-only), territory hull + coverage report (pair every extent with confidence).

### Cross-domain (🟡)
- **24. `note_location`** (display-only; dual-scope if the note is health/finance). **25. `errand_check`** (dual-scope + privacy gate on health appointments; dwell-gated). **26. `note_here`** (fail-closed name-or-unknown, no coords).

### Proactive (✅ pull + 🟡 push-gated)
- **29. Nightly/weekly digests (✅).** SYSTEM_CTX scheduled `compile_place_digest` (seeded like 0064) reads geofence events → a pull artifact in the Locations tab. *Mitigation:* full-owner read, idempotent, only place names.
- **31. App-open presence chip + tone (✅).** On chat open, pull the owner's own latest place; optional coarse "currently at: <place>" in ephemeral session context. *Mitigation:* owner reading own location; never volunteers location into exportable output.
- **27, 28, 30, 32 (🟡).** Place-arrival surfacer, reminders-by-place, went-dark/low-batt/flapping (flapping proposes a radius-correction note), arrival briefing — all need the **proactive delivery channel** (or stay pull-only); the "not home by 10pm" conditional additionally needs an **absence detector** (⛔ yet).

### Wild
- **33. `life_replay` (🟡).** On-demand grounded day narrative, citation-backed, ephemeral; co-location limited to the owner's own devices. **34. Predictive departure nudge (⛔ yet).** Push + standing predictive watch — needs a push channel + per-routine opt-in + Proposal-staged outbound. **35. Route clustering / CoG drift / wiki place-pages (✅, Phase-6-gated).** Ride the wiki authoring path (#7), sequenced after the wiki lands.

### Don't (⛔)
- **36. `place_spend`** (finance × location) — E2 forbids fanning one firewall into another; needs an owner-sanctioned cross-domain read primitive first. **37. `health_place_correlation`** — strictly stronger; most-protected domain; near-permanent don't. **38. Device provisioning via the agent** — keep credential minting in the owner-only UI. **39. Proactive anomaly auto-alerts** — keep anomaly pull-only. **40. `create_location_note`** (auto-author from transitions) — the owner-deferred #7 line; transitions stay events-only.
