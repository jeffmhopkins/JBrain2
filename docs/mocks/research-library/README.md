# Research Library — launcher mockups (GUI gate)

Three interactive variants for a new card-launcher destination that surfaces
everything jerv has researched: **deep-research reports**
(`app.research_reports`) and **video analyses** (`external.sources`). Each mock
lets the owner **search**, **view**, and **delete** items. Open any file in a
browser; a dark/light toggle sits above the phone frame. All three read the same
fixtures (`items.js`) so the data is identical across layouts.

The surface lives in the `external`/research domain, so it takes the **amber**
research accent (read-only, per DESIGN.md §Principles 4). Reports carry an amber
type disc; video analyses carry a steel disc — the type axis, distinct from the
amber domain dot.

Every item carries an **action menu** with the options that apply to its source:
**Open in jerv conversation** (both), **Copy** (report + summary for a report;
summary + transcript for a video), **Download report (.md)** (reports only),
**Open source ↗** (videos with a URL), and **Delete** (tap-again confirm). In A/C
it opens from the ⋯ in the detail view (with an Open-in-jerv primary action
alongside); in B it opens from the ⋯ on each row.

Reports show their real provenance — `complexity` (simple / comparative / deep),
`sub_agents`, `rounds`, `sources`, and the `analyzed` / `revised` /
`coverage_limited` / `truncated` flags. Videos show `channel_name`,
`duration_s`, `frames`, and `transcript_source` (captions / whisper), plus a
LIVE badge for a stream.

| File | Variant | Layout | Interaction |
|---|---|---|---|
| `a-unified-feed.html` | **A — unified feed + swipe rail** | One newest-first stream mixing both types; type disc distinguishes them | Live search + type chips; **swipe a row left** for a delete rail (tap-again confirm); tap opens a detail sheet; undo toast |
| `b-segmented-tabs.html` | **B — segmented tabs** | A **Reports / Videos** segmented control, each list purpose-built for its type (reports lead with the question + provenance; videos lead with a thumbnail + channel + duration) | Search filters the active tab; **⋯** sheet with open / copy / delete (tap-again confirm); full-screen detail layer |
| `c-search-first.html` | **C — search-first + bulk select** | A hero search field over passage-first results grouped under REPORTS / VIDEO ANALYSIS headers, with `semantic` / `keyword` match badges (the Search paradigm) | Live search + scope chips; **Select** turns rows into checkboxes for **bulk delete** (review-inbox pattern); full-screen detail layer with its own delete |

All three reuse settled paradigms — the swipe rail (A), segmented control +
action sheet (B), and passage-first search + bulk-select bar (C) — so the review
is about which *composition* fits the library, not new primitives.

No decision is recorded yet: once the owner picks, the chosen pattern and the
reasoning go into `docs/reference/DESIGN.md` in the same PR (the UI-development
process, §2–3), and the rivals stay here as the record.
