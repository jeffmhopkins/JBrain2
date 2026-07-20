# Research Library — Build Plan

> **Status:** In progress · **Last verified:** 2026-07-20 · **Waves:** R1✅ R2✅ R3◻️

The owner-facing **browse surface** over the two `external`-corpus artifacts jerv
produces on its own turns — **deep-research reports** (`app.research_reports`, the
`deep_research` tool) and **video analyses** (`external.sources`, `analyze_stream` /
external-video ingestion). Both already persist server-side and are reachable to *jerv*
via corpus tools (`list_/search_/read_/show_/remove_research_report`, the
`search_external_video` family). This plan adds the **human's** door to the same corpus: a
card-launcher destination (`ResearchLibraryScreen`) that lets the owner **search, view, and
delete** what's been researched, without going through a jerv turn.

The GUI gate is settled: three interactive mocks were reviewed and **variant B — segmented
tabs** was locked (binding spec `docs/mocks/research-library/b-segmented-tabs.html`; the
reasoning lives in `docs/reference/DESIGN.md` §"Research Library"; A/C retained as the
record).

## Why this fits (the lean litmus)

Almost entirely reuse. The expensive layers — the two corpora, their RLS policies, and
the read/search/fetch/**delete** callables — already ship:

| Need | Reuse vs. net-new |
|---|---|
| List / search / fetch reports | **Reuse** `research_corpus.list_reports` / `search_reports` / `fetch_report`. |
| List / search / fetch videos | **Reuse** `corpus.list_corpus` / `search_corpus` / `fetch_transcript`. |
| Owner-initiated hard delete | **Reuse** `research_corpus.delete_report` / `corpus.delete_external_video` (the same callables the proposal executor already invokes). |
| RLS scope + DELETE grants | **Reuse** — both tables already `ENABLE`+`FORCE` RLS with a `has_domain_scope(domain_code)` policy and hold the `DELETE` grant (migrations 0133 / 0140). A full-owner HTTP context (`ctx_for(principal)`) satisfies it. **No migration, no new grant.** |
| Owner auth gate | **Reuse** `api/deps.owner_only`. |
| Report body render | **Reuse** the shared `<Markdown>` path (what an assistant turn and the `deep_research_report` view use). |
| Video render | **Reuse** the exported `VideoAnalysis` component (`components/VideoAnalysis.tsx`). |
| Screen shell / list / tabs / sheet / detail layer | **Reuse** `RunsScreen` + `SearchScreen` patterns, `.seg-row`, `Sheet`, the App-level stacked detail layer (`ListDetailScreen` model). |

**Net-new is thin:** one backend router (+ its tests), one frontend screen + one detail
layer + an action sheet (+ their tests), the API-client methods + mock fixtures, one
launcher tile, and the `App.tsx`/`Launcher.tsx` navigation wiring. **Zero new runtime
dependencies; zero migrations.**

## Settled decisions

1. **Variant B is binding.** Segmented Reports · Videos tabs, each a purpose-built list;
   search filters within the active tab; per-row `⋯` action sheet; tap-title opens a
   view-only detail layer. (GUI gate — done.)
2. **Direct owner-initiated delete, not a Proposal.** The proposal/executor path
   (`connectortools.py` ops `delete_research_report` / `delete_external_video`) exists
   because *jerv* is untrusted and must stage owner approval. The owner at the keyboard **is**
   the trusted executor, so the HTTP delete calls `delete_report` / `delete_external_video`
   directly under `ctx_for(principal)` (full owner). It uses the settled tap-again confirm +
   **deferred-commit undo**: the row is removed from the list immediately and the server
   `DELETE` fires only when the undo toast expires (undo cancels it), so undo is honest
   without needing to re-create a hard-deleted row.
3. **Owner-facing HTTP, not a widened jerv scope.** Reads pass `principal.id` to the corpus
   readers, which build the `external`-scoped context internally; a full-owner session
   already reaches the `external` domain, so no domain wiring rides the HTTP layer.
4. **Videos key on `video_id`; reports key on `id` (uuid).** `list_corpus` returns
   `video_id` (not the row uuid), and `fetch_transcript` is `video_id`-keyed, so the video
   endpoints key on `video_id` — the `DELETE` resolves the row `source_id` internally via
   `fetch_transcript`. This avoids touching the shipped `corpus.py` / its tests. (See Open
   decision 1 for the multi-provider caveat.)
5. **No thumbnail route in v1.** The list rows show a placeholder video disc (as the mock
   does), and the video detail embeds the YouTube player via `video_id` (`youtubeId`), with
   filmstrip frames rendered as time markers — the `VideoAnalysis` component already
   degrades to marker frames when no `thumbUrl` is supplied. A served-thumbnail route is a
   deferred nicety, not required by the mock.

## Architecture

### Backend — one router, thin wrappers (Wave R1)

`api/research_library.py` — `APIRouter(prefix="/research-library",
dependencies=[Depends(owner_only)])`, registered in `main.py`'s `include_router` block.
A small `ResearchLibrary` reader object is constructed in the lifespan startup (holding
`session_maker` + `embed_client`) and attached to `app.state`, so unit tests inject a fake
(the `run_reader` precedent). Endpoints (all owner-gated, route-ordered so literals precede
`/{id}`):

| Method + path | Calls | Notes |
|---|---|---|
| `GET /research-library/reports` | `list_reports(maker, limit, offset, principal_id)` | `{items:[ReportListOut], total}`; clamped pagination. |
| `GET /research-library/reports/search?q=` | `search_reports(maker, embedder, q, limit, principal_id)` | `{items:[ReportHitOut], degraded}`. |
| `GET /research-library/reports/{id}` | `fetch_report(maker, ref=id, principal_id)` | 404 when `None`; carries full `report_md`. |
| `DELETE /research-library/reports/{id}` | `delete_report(maker, ctx_for(principal), id)` | 204; idempotent. |
| `GET /research-library/videos` | `list_corpus(maker, limit, offset, principal_id)` | `{items:[VideoListOut], total}`. |
| `GET /research-library/videos/search?q=` | `search_corpus(maker, embedder, q, limit, principal_id)` | `{items:[VideoHitOut], degraded}`. |
| `GET /research-library/videos/{video_id}` | `fetch_transcript(maker, video_id, principal_id)` | 404 when `None`; full transcript + frames + summary. |
| `DELETE /research-library/videos/{video_id}` | resolve `source_id` via `fetch_transcript` → `delete_external_video(maker, ctx_for(principal), source_id)` | 204; idempotent (missing → 204). |

Pydantic `…Out` models are built from the existing frozen dataclasses (`LibraryReport`,
`ReportHit`, `ReportRecord`, `LibraryVideo`, `CorpusHit`, `ExternalTranscript`) — no field
the model doesn't need (e.g. the report list omits `report_md`). Accessors
`get_session_maker` / `get_embed_client` copy the `notes.py` / `analysis.py` patterns.

**RLS verification (Wave R1, red-team):** confirm a full-owner `ctx_for(principal)` both
*reads* and *deletes* rows in `app.research_reports` (domain `external`) **and**
`app.external_sources` (default domain `general`) — the video default `domain_code` differs
from reports', so the isolation assertion must prove the full-owner reach holds for both and
that a **narrowed** (non-owner / domain-scoped) principal is refused by `owner_only` before
RLS is even consulted.

### Frontend — screen, action sheet, detail layer (Waves R2–R3)

- **`screens/ResearchScreen.tsx` (R2)** — the list surface. A `.seg-row` **Reports ·
  Videos** control (DataScreen model) over a per-type list; live search (SearchScreen's
  250ms debounce + `seq`-guard) scoped to the active tab; report rows lead with the
  question + complexity badge + provenance chips, video rows with a placeholder thumb +
  channel + duration + frames + transcript-source; a per-row `⋯` opens
  **`ResearchActionSheet`** (a `Sheet`) listing the applicable actions (View · Open in jerv
  · Copy… · Download report / Open source · Delete). Delete uses the tap-again rose row +
  deferred-commit undo toast (RunsScreen `<output>` toast pattern).
- **`screens/ResearchDetailScreen.tsx` (R3)** — an App-level stacked layer (ListDetailScreen
  model: own `TopBar`, `{loading|error|done}`, id-keyed fetch with a `stale` guard,
  swipe-down-at-top to close). A report renders the provenance strip + `report_md` through
  `<Markdown>`; a video renders `<VideoAnalysis>` (embedding via `youtubeId=video_id`) with
  the summary + transcript. Copy (report/summary/transcript) and Download `.md` are wired
  here and shared with the list's action sheet.
- **Navigation wiring** — `Launcher.tsx` gains a **Research** tile under KNOWLEDGE and
  `"research"` in `LauncherTarget`; `App.tsx` gains `"research"` in `Card` + `SCREEN_TITLES`,
  a subscreen render branch for the list, and a `researchDetail` stacked-layer (state +
  render block + `closeTopLayer`/`overlayDepth` entries) for the detail.
- **"Open in jerv conversation"** — a report deep-links to its originating `session_id` when
  present; a video (no `session_id`) and a report whose session is gone open a **new** Full
  Brain conversation seeded with a reference to the item. The exact session-resume plumbing
  is an R3 investigation (Open decision 2).
- **API client + mock** — `api/client.ts` gains the eight methods + response interfaces;
  `api/mock.ts` gains fixtures and `mockFetch` branches with the DoD variants (empty /
  long / degraded-search / error / offline).

## Security & non-negotiables

- **#3 RLS / firewall.** Every read + delete runs on an RLS-scoped session; `owner_only`
  gates the router; the full-owner reach is proven for both tables and refusal proven for a
  non-owner (Wave R1 red-team). No new table → no new isolation test mandated, but the
  HTTP delete path is exercised end-to-end.
- **#1 data/instruction boundary.** `report_md` renders through the same escaped
  `<Markdown>` path the assistant turn uses (model-authored-over-escaped-findings, no
  model-authored URLs/scripts); the video card is data-only and builds its media source
  from `video_id`, never a payload URL (#9).
- **#7 wiki stays machine-written.** Untouched — this is a read/delete browse surface over
  jerv's own artifacts, mints no notes, writes no graph.
- **#5 tests same PR.** 80% backend / security-100%, real Postgres via testcontainers, LLM
  + embed faked (the `StaticEmbed` corpus-test fake); frontend Vitest.

## Testing

- **R1 backend** — unit (`test_research_library_api.py`, `test_runs_api.py` style): owner
  gating (401/403 unauth), response shapes + field selection, pagination clamp, search
  `degraded` pass-through, `404` on missing get, `204` on delete (+ idempotent missing).
  Integration (real PG, faked embed): persist → list → search → fetch → **delete** round-trip
  through `TestClient` for both corpora, plus the full-owner-reach / non-owner-refusal RLS
  assertions.
- **R2/R3 frontend** — Vitest + testing-library (`RunsScreen.test.tsx` style, `vi.spyOn(api,
  …)` or an injectable fetch prop): tab switch re-queries the right endpoint; live search
  debounce + latest-wins; the `⋯` menu renders exactly the applicable actions per type;
  delete removes the row + shows undo, undo restores it and cancels the deferred server call,
  toast-expiry commits the delete; empty / filtered-empty / error / offline states; the
  detail layer renders a report (Markdown) and a video (`VideoAnalysis`), and back closes it.

## Wave split (per `docs/reference/PROCESS.md`)

Each wave: local `ruff`+`pyright` / `biome`+`tsc` + unit tests green before merge; an
independent adversarial review per task and per wave (the R1 RLS/scope surface gets a
red-team pass); one PR per wave, CI green before merge.

- **Wave R1 — Backend HTTP API (red-team gated). ✅ LANDED (this branch).**
  `api/research_library.py` (8 endpoints) + the injectable `api/research_service.ResearchLibrary`
  reader on `app.state`, `main.py` wiring, the `…Out` models, unit
  (`tests/unit/test_research_library_api.py`, 10 cases — owner-gating, shapes, degraded
  pass-through, 404/204, video-`source_id` resolution, limit clamp) + integration
  (`tests/integration/test_research_library_api_pg.py` — real-Postgres owner-gated round-trip
  for both corpora). No migration/grant — both tables already carry the DELETE grant + the
  `external`-domain RLS (0133/0136/0140), and a full-owner `ctx_for` reaches + deletes both.
- **Wave R2 — Frontend browse + view + delete. ✅ LANDED (this branch).** `ResearchScreen`
  (`.seg-row` Reports/Videos tabs + as-you-type filter + per-type rows + the `⋯` action
  sheet with View + tap-again Delete), the deferred-commit undo, `ResearchDetailScreen` (the
  App-level stacked layer: a report via the shared `<Markdown>` + a provenance strip, a video
  via `<VideoAnalysis>`), the `api/client.ts` methods + interfaces, `api/mock.ts` fixtures,
  the launcher **Research** tile (`FlaskIcon`) + the `App.tsx` card/title/nav + stacked-layer
  wiring, the `.rl-*` styles (amber research accent), and Vitest coverage
  (`ResearchScreen.test.tsx`, `ResearchDetailScreen.test.tsx`). Verified end-to-end in mock
  mode (launcher → Research → tabs → detail). **Boundary note (scope deviation, PROCESS §):**
  the detail *view* landed here with the list (a coherent browse+view+delete commit) rather
  than in R3; the action sheet ships View + Delete, and R3 adds the remaining item actions.
- **Wave R3 — Item actions.** Add **Open in jerv conversation**, **Copy** (report / summary /
  transcript), **Download report (.md)**, and **Open source ↗** to the action sheet + detail,
  each shown only when applicable to the source; tests. Depends on R1 (detail endpoints) + R2
  (screen + detail).

R2 depends on R1; R3 depends on R1 + R2. Within R1 the reports and videos endpoint sets are
parallelizable; within R2/R3 the two type-lanes are parallelizable.

## Open decisions for the build

1. **Video key uniqueness.** `video_id` alone is not globally unique (`UNIQUE(provider,
   video_id)`); the endpoints key on `video_id` and `fetch_transcript` picks the newest
   match. Single-provider (YouTube) today makes this safe; if a second provider lands,
   promote the video key to `source_id` (the row uuid) by adding it to `LibraryVideo` +
   `list_corpus`. Recommend: ship `video_id`-keyed, revisit on a second provider.
2. **"Open in jerv conversation" semantics.** Deep-link-to-exact-session (report
   `session_id`) vs. always-seed-a-new-conversation. Recommend: deep-link when the session
   exists, else seed new; confirm the session-resume plumbing exists during R3 and degrade to
   opening the Sessions list if not.
3. **Served video thumbnails.** Deferred (Settled decision 5). Revisit if the marker-frame
   filmstrip reads as too bare on real videos.

## Deferred past v1

- **A served-thumbnail route** for external-video frames (Open decision 3).
- **Bulk delete / select mode** — variant C's paradigm; not in the chosen B. A follow-on if
  the library grows large enough to want it.
- **Re-run / refresh** an analysis from the library (re-analyze a video, re-run a report) —
  those are jerv-turn actions; the library links into jerv rather than re-implementing them.
