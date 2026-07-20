# Plans — active build plans

> **Status:** Living · **Last verified:** 2026-07-19

Active, multi-wave build plans (`Scheduled` / `In progress` / `Parked`, per
`../DOC_LIFECYCLE.md`). A plan archives to `../archive/` in the PR that lands its
last wave; proposed-but-unscheduled ideas live in `../proposed/`.

| Doc | Status |
|---|---|
| `EMR_IMPORT_PLAN.md` | **In progress** — EMR / medical-record import: multi-system EMR PDF exports normalized into cited, health-firewalled `measurement` + `event` facts, with `lab_results`/`encounters` projections and `read_labs`/`read_encounters` tools. W0 (gates + synthetic fixtures) done; W1–W5 open. |
| `PHASE6_WIKI_PLAN.md` | **In progress** — Phase 6 (Wiki). Waves A–C shipped (builder, citations, Talk); Wave D open (re-enable schedules, grounding-gate tuning, purge→rebuild). |
| `JCODE_SESSION_ISOLATION_PLAN.md` | **Parked** — per-session network namespace; the P0 substrate was reverted after the P1 spike. Kept for a future revisit. |
| `DEEP_RESEARCH_TOOL_PLAN.md` | **In progress** — a dedicated jerv-only `deep_research` tool: a bounded plan→gather→reflect→one-refill→synthesize→critique/revise run over the web-sandboxed sub-agent fan, with a complexity skip matrix and tiered source-quality corroboration (both borrowed from `kyuz0/deep-research-agent`). All three waves landed on-branch (D1 spine, D2 refill + critique, D3 the `deep_research_report` tool-view + jerv steering) with full backend + frontend unit suites; the D3 mock-gate sign-off and on-box budget tuning remain. |
| `LLM_PROMPT_CACHE_PLAN.md` | **Scheduled** — cut on-box first-token latency by reusing the static jerv/curator prompt prefix. W1 (cache-stable prompt layout — move the volatile `now_block` to the tail) + W2 (llama-server `--cache-reuse`/slot flags); both open. |
| `EXTERNAL_VIDEO_INGESTION_PLAN.md` | **In progress** — analysed YouTube videos → an isolated, embedded, searchable corpus (never the knowledge graph). Waves A–B shipped (migrations 0133–0134, timeline windower, `embed_external_source`, `analyze_stream` write-through, the `search_external_video` + `check_channel` jerv tools); Wave C (a recurring Jerv Task for scheduling) open. |
| `RESEARCH_LIBRARY_PLAN.md` | **Scheduled** — the owner-facing browse surface over jerv's two `external`-corpus artifacts (deep-research reports + video analyses): a card-launcher `ResearchLibraryScreen` (locked GUI variant B — segmented Reports/Videos tabs) to search, view, and delete them over a net-new owner-gated HTTP API that reuses the existing corpus read/search/fetch/delete callables (no migration). Waves R1 (backend API) ◻️, R2 (list surface) ◻️, R3 (detail layer + actions) ◻️. |
| `VIDEO_IMAGE_TOOLS_PLAN.md` | **In progress** — give jerv eyes on a specific still: `grab_frame` (persist a frame from a video URL/attachment at time T, optional inline `question`), `fetch_image` (per-hop-SSRF-guarded, validated web-image fetch), a 2..N-source widening of `analyze_image` (+ a `compare_images` sidecar) that always emits an owner-visible side-by-side, and a `show: false` flag to suppress the analyze-video/stream card on intermediate steps. Reuses `generated_images` via a nullable `provenance` column. Reconciled with a four-lens review. V0 (the `single`-mode `seek` fix) shipped on-branch; V1–V6 open. |
