# Plans — active build plans

> **Status:** Living · **Last verified:** 2026-07-13

Active, multi-wave build plans (`Scheduled` / `In progress` / `Parked`, per
`../DOC_LIFECYCLE.md`). A plan archives to `../archive/` in the PR that lands its
last wave; proposed-but-unscheduled ideas live in `../proposed/`.

| Doc | Status |
|---|---|
| `EMR_IMPORT_PLAN.md` | **In progress** — EMR / medical-record import: multi-system EMR PDF exports normalized into cited, health-firewalled `measurement` + `event` facts, with `lab_results`/`encounters` projections and `read_labs`/`read_encounters` tools. W0 (gates + synthetic fixtures) done; W1–W5 open. |
| `PHASE6_WIKI_PLAN.md` | **In progress** — Phase 6 (Wiki). Waves A–C shipped (builder, citations, Talk); Wave D open (re-enable schedules, grounding-gate tuning, purge→rebuild). |
| `JCODE_SESSION_ISOLATION_PLAN.md` | **Parked** — per-session network namespace; the P0 substrate was reverted after the P1 spike. Kept for a future revisit. |
| `LLM_PROMPT_CACHE_PLAN.md` | **Scheduled** — cut on-box first-token latency by reusing the static jerv/curator prompt prefix. W1 (cache-stable prompt layout — move the volatile `now_block` to the tail) + W2 (llama-server `--cache-reuse`/slot flags); both open. |
| `STREAM_ANALYSIS_PLAN.md` | **In progress** — `analyze_stream`: jerv pulls frame(s) + optional whisper audio from a video **URL** (live or VOD) via yt-dlp + ffmpeg, reusing the `analyze_video` map→fuse→reduce core. W1 (stream sampler + yt-dlp dep) + W2 (shared reduce core + `analyze_stream` tool, wired to jerv) done; W3 (frontend stream card + docs + egress red-team) open. |
