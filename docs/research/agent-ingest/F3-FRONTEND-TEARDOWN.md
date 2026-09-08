> **Status:** Research · **Last verified:** 2026-09-08

# F3 — Frontend teardown & reuse map (agent ingest)

Research dossier for the proposed change: the deterministic note-analysis
pipeline (extract → Integrator → arbiter → apply) is **deleted**; a note becomes
turn 0 of an agent conversation. A tool-using local model reads the note, writes
the entity/predicate graph through tools when confident, and asks the owner
questions **in the conversation** when not. Questions accumulate in a silent
queue (no push). Scope: owner notes + attachments/OCR/media. Phone-only owner,
remote, no terminal. The DB is disposable.

This document is the **frontend** half: what dies, what gets rewritten, what is
reused verbatim, and where the state/data-flow model breaks.

**Method.** Every claim below was read out of the tree on branch
`claude/agent-predicate-db-redesign-xog54v`. Line numbers are as-read today; the
`Verified vs assumed` section (§8) separates what I confirmed in code from what
is a design proposal. No application code was modified.

---

## 0. The shape of the frontend, for orientation

- 110 test files, **2028 test cases**, run by `vitest run`
  (`frontend/package.json:31`). Lint is `biome check .`
  (`frontend/package.json:26`, `frontend/biome.json`), types are
  `tsc --noEmit` (`frontend/package.json:30`). Verified — those three are the
  frontend gates; there is no separate frontend coverage gate in
  `package.json`.
- Everything hangs off `frontend/src/App.tsx` (754 lines), which owns the
  overlay stack: note view `App.tsx:695-715`, entity page `App.tsx:717-728`,
  launcher cards `App.tsx:580-608` (Review at `App.tsx:595`).
- One React state tree, no router; layers are `useState` + a back-depth counter
  (`App.tsx:432-446`).
- `frontend/src/styles.css` is 26 458 lines, sectioned by comment banners
  (`grep -n '^/\* ====' src/styles.css`). Section boundaries matter for the
  teardown; they are cited per-surface below.

---

## 1. Teardown inventory — everything that touches the dying pipeline

Classification key: **DELETE** (file/block goes away entirely), **REWRITE**
(surface survives, contents change), **KEEP** (untouched, or a data-source swap
only).

### 1.1 The Note view and its Analysis tab

| Path:line | What it is | Verdict |
|---|---|---|
| `frontend/src/screens/NoteScreen.tsx:1-501` | The note layer: header, Note / Attachments / Analysis tabs | **REWRITE** |
| `NoteScreen.tsx:297` | `useState<"note"\|"attachments"\|"analysis">("analysis")` — **Analysis is the default tab** | **REWRITE** → default becomes the conversation tab |
| `NoteScreen.tsx:410-418` | The "Analysis" tab button | **REWRITE** → "Conversation" (+ an unanswered-question count pill, mirroring the Attachments count at `:406-408`) |
| `NoteScreen.tsx:449-457` | `<AnalysisTab noteId … onOpenEntity />` mount | **REWRITE** → `<NoteConversation noteId … />` |
| `NoteScreen.tsx:369` | `<IngestChip item={…} />` in the note header | **REWRITE** — see §1.2 |
| `NoteScreen.tsx:28-30` | `NoteViewSource.ingestState` / `.analyzed` | **REWRITE** — `analyzed: boolean` is a pipeline concept; a conversation has states, not a boolean (§6) |
| `NoteScreen.tsx:57-73` | `noteViewFromSearch` hardcodes `ingestState: null, analyzed: false` | **REWRITE** with the new state field |
| `NoteScreen.tsx:89-104` | `attachmentStatus()` — `indexing… / ocr queued… / text + description` | **KEEP** (attachments still OCR; the agent reads the extracts) |
| `NoteScreen.tsx:106-259` | `AttachmentsTab` — the canonical manifest | **KEEP** verbatim |
| `NoteScreen.tsx:460-498` | ⋯ sheet: edit / move domain / delete | **KEEP** |

**`frontend/src/components/AnalysisTab.tsx:1-615` — DELETE, whole file.** It is
the single largest concentration of dying concepts:

- `AnalysisTab.tsx:35-70` — `AnalysisState`, `SubjectGroup`, `groupBySubject()`,
  `VISIBLE_STATUSES = {active, pending_review}` (a fact-status vocabulary that
  disappears with the arbiter).
- `AnalysisTab.tsx:72-115` — `FactRow`: `predicate.qualifier → value`, tenure
  track, kind badge, status chip, confidence %.
- `AnalysisTab.tsx:404,431-510` — the **3 s analysis poller**, `gated` empty
  state, `rerunNote()` (`POST /notes/{id}/analyze`), `rerunImage()`.
- `AnalysisTab.tsx:512-542` — the four empty states ("analysis runs after
  indexing", "loading analysis…", "couldn't load analysis", "waiting on image
  analysis").
- `AnalysisTab.tsx:544-612` — title, tags, subject groups, the `Dates` temporal-
  token row, the Sources card.

**Salvage out of it before deleting** (three pieces are pipeline-agnostic and
worth lifting into the new surface rather than re-authoring):

- `AnalysisTab.tsx:117-213` — `imageSources()`, `audioSources()`,
  `AudioTranscriptSources` (the karaoke transcript card). **KEEP, relocate.**
  Attachments/OCR/media stay in scope, and the owner still needs to see what the
  agent read.
- `AnalysisTab.tsx:215-227,229-304` — `StageMark`, `SourceImageRow` (the
  `ocr ✓ · description ✓` stage line + in-place expansion). **KEEP, relocate.**
- `AnalysisTab.tsx:306-402` — `SourcesCard`. **REWRITE**: the per-source rows
  and the ⋯ "re-run image analysis" sheet survive; the footer's
  "analyzed <when> · <extractor>" provenance line (`:358-369`) and the "re-run
  analysis" button (`:370-378`) are pipeline artifacts — replaced by "the agent
  read these" + "ask the agent to re-read".

**Supporting modules:**

| Path:line | Verdict | Note |
|---|---|---|
| `frontend/src/analysis/bits.tsx:1-136` | **DELETE** | `KindBadge`, `EdgeValue`, `FactTenure`, `StatusChip`, `FactCitation` — all `FactOut`-shaped. `MarkedText` (`:87-103`) is the one exception: **KEEP**, it is a 15-line wrapper over `search/marks.ts` and is also used by `EntityScreen.tsx:11`. Move it to `search/marks` or `components/`. |
| `frontend/src/analysis/format.ts:1-290` | **SPLIT** | `edgePath` (`:144`), `factValue` (`:210`), `factSpan` (`:283`), `dedupeTokens` (`:264`) are fact-shaped → **DELETE**. `fmtTemporal` (`:230`), `fmtQuantity` (`:153`), `valueLabel` (`:205`), `fmtConfidence` (`:220`) are generic display helpers → **KEEP** (relocate to `frontend/src/format.ts`; `fmtTemporal` alone has 28 tests in `analysis/format.test.ts` and correct UTC-vs-local calendar handling worth preserving). |
| `frontend/src/components/ImageExtracts.tsx:1-240` | **KEEP** | `useImageExtracts`, `imageStages`, `ImageExpansion`, `OcrText`, `fmtBytes`. Attachment extraction is *upstream* of the agent, not part of the arbiter. `fmtBytes` is imported by `NoteScreen.tsx:11`. |
| `frontend/src/components/AudioTranscript.tsx` | **KEEP** | Also a registered tool-view dependency (`agent/views/registry.tsx:23`). |

### 1.2 Home-stream analysis/indexing chips and the poll they drive

| Path:line | What | Verdict |
|---|---|---|
| `frontend/src/notes/lifecycle.ts:1-54` | `lifecycleChip()` — the whole `indexing… → reading image(s)… → analyzing… → quiet` ladder, plus `awaitingImageCount()` | **REWRITE** |
| `lifecycle.ts:40-53` | precedence chain, incl. `if (source.analyzed) return null` | **REWRITE** — `analyzed` ceases to exist; the terminal state becomes "the conversation settled" or "N questions waiting" |
| `lifecycle.ts:34-38` | `awaitingImageCount` | **KEEP** — still gates "the agent hasn't seen the picture yet", used by `NoteScreen.tsx:127`, `AnalysisTab.tsx:490` |
| `frontend/src/components/Stream.tsx:27-33` | `IngestChip` wrapper | **KEEP shell, REWRITE labels** |
| `Stream.tsx:184-206` | the chips row: attachments, `pending sync`, `<IngestChip>` | **REWRITE** — this is where the note-conversation state chip lands (§6) |
| `frontend/src/notes/useNotes.ts:33-34` | `StreamItem.analyzed` | **REWRITE** → a conversation-state field |
| `useNotes.ts:81-87` | `inFlight()` — "a pending-tone chip keeps the fast poll alive" | **REWRITE** — see §3 |
| `useNotes.ts:74-79` | `IDLE_INTERVAL_MS = 30_000` / `ACTIVE_INTERVAL_MS = 2_500` | **REWRITE** — a minutes-long local-model pass makes 2.5 s polling wasteful (§3, §5) |
| `useNotes.ts:92-114` | `serverItem()` maps `note.ingest_state` / `note.analyzed` | **REWRITE** |
| `useNotes.ts:116-138` | `pendingItem()` sets `analyzed: false` for outbox rows | **REWRITE** |

### 1.3 The re-analyze affordances

Two exist, both die as *pipeline* controls:

1. **Note-level.** `AnalysisTab.tsx:370-378` (button) → `AnalysisTab.tsx:496-503`
   `rerunNote()` → `api.analyzeNote()` (`api/client.ts:3084-3086`,
   `POST /api/notes/{id}/analyze`, 202/409). **DELETE the endpoint call.** The
   successor is "reply in the conversation" or an explicit "re-read this note"
   turn — the agent is the only writer, so a re-run is just another turn.
2. **Per-image.** `AnalysisTab.tsx:382-399` (⋯ sheet) → `:505-510`
   `rerunImage()` → `extractsApi.analyze()` → `api.analyzeAttachment()`
   (`api/client.ts:2491-2494`). **KEEP.** This re-runs OCR/vision, which is
   upstream of the agent, not the arbiter. Its follow-on comment ("facts
   re-extract after", `AnalysisTab.tsx:396`) needs rewording.

### 1.4 Fact / entity rendering (outside the note view)

| Path:line | Verdict | Note |
|---|---|---|
| `frontend/src/screens/EntityScreen.tsx:1-368` | **REWRITE** | The hub survives as navigation; its internals are fact-shaped. `predHead()` (`:34-38`), `priorCount()` (`:42-44`), `isFormer()` (`:50-52`), `IRREALIS` (`:26`), `PredicateBlock` (`:54-101`) all read `EntityPredicate.current/.history` — the supersession chain the arbiter built. With the arbiter gone, "what supersedes what" is whatever the agent's write tool recorded. |
| `frontend/src/screens/EntityHistorySheet.tsx:1-…` | **REWRITE or DELETE** | The `N earlier →` revision rail. Only meaningful if the write tools keep a supersession chain. |
| `frontend/src/entities/kinds.tsx` | **KEEP** | `EntityTypeIcon` / `resolveEntityKind` — pure iconography, used by `SearchScreen.tsx:16`, `EntityListScreen`, `GraphScreen`, `review/grouping.ts:10`. |
| `frontend/src/screens/EntityListScreen.tsx:1-…` | **KEEP** | Browse rows over `GET /api/entities`; no fact shapes. |
| `frontend/src/screens/GraphScreen.tsx` (27 tests) | **KEEP** | Reads `GET /api/graph` / `neighbors`; edges are relationships, not `FactOut`. |
| `frontend/src/api/client.ts:1174-1234` | **REWRITE** | `FactKind`, `FactStatus`, `FactOut`, `AnalysisEntity`, `TemporalTokenOut`, `NoteAnalysis` — the wire contract of the dying pipeline. `FactOut.status`, `.confidence`, `.assertion`, `.pinned`, `.source_snippet` are all arbiter outputs. |
| `frontend/src/api/client.ts:1236-1250` | **REWRITE** | `EntityPredicate` (`current` + full `history` chain), `InboundEdge`, `EntityMention`. |
| `frontend/src/screens/wiki/CitationCard.tsx`, `ReferencesList.tsx` | **REWRITE** | They import fact shapes; the wiki is Phase 6 and out of this scope, but they will not typecheck through a `FactOut` change. Flagged so the change is not a surprise. |

### 1.5 Review inbox — the extraction-derived card kinds

The entire inbox exists to adjudicate what the pipeline could not.

- `frontend/src/api/client.ts:1363-1377` — the `ReviewKind` union. Of the 13
  kinds, **11 are extraction/arbiter artifacts** and die with it:
  `fact_conflict`, `attribute_collision`, `merge_proposal`, `ambiguous_mention`,
  `domain_promotion`, `low_confidence`, `low_confidence_inference`,
  `split_proposal`, `extraction_truncated`, `new_predicate`, `confirm_entity`.
  The two survivors are `wiki_contradiction` and `wiki_stale_claim` — filed by
  the **wiki linter** (Phase 6), not by the note pipeline. **Verified** by
  reading the union and the per-kind sequence table.
- `frontend/src/review/blocks/registry.ts:41-62` — the kind→block table.
  Eleven rows **DELETE**; the two `wiki_*` rows survive
  (`registry.ts:59,61`).
- `frontend/src/review/blocks/NewPredicateCard.tsx:1-109` — **DELETE**. Only
  caller is `review/blocks/Action.tsx:124`. Pure two-tier-predicate-registry UI
  (map to existing / keep as new / rename), the exact machinery
  `docs/reference/ENTITY_GRAPH_REFOCUS_PLAN.md` describes.
- `frontend/src/review/blocks/ClaimInference.tsx:1-312` — **DELETE**. The
  editable proposed-fact panel: predicate picker + value chip/enum + modality.
  Its whole reason to exist is "the extractor proposed a fact and you correct
  it in place"; in the new model the agent asks in prose instead.
- `frontend/src/review/blocks/ClaimDiff.tsx:1-25` — **DELETE**. Before→after
  value diff for collisions/conflicts.
- `frontend/src/review/blocks/Trace.tsx:1-145` + `review/payload.ts:19-28,78-148`
  — **DELETE**. `TraceStage` is literally documented as
  *"extraction → integration → arbiter"* (`payload.ts:20-21`). It cannot outlive
  the pipeline it traces.
- `frontend/src/review/blocks/ClaimNotice.tsx:1-13` — **DELETE**
  (`ambiguous_mention` only).
- `frontend/src/review/blocks/ClaimContradiction.tsx:1-138` — **KEEP**
  (`wiki_contradiction` only).
- `frontend/src/review/blocks/{Header,Action,Evidence,Footer}.tsx` — **REWRITE**.
  `Action.tsx:1-304` carries the per-kind fork; strip the `new_predicate` and
  inference branches, keep the generic accept/reject/correct rails. `Header.tsx`
  and `Evidence.tsx` are thin and generic.
- `frontend/src/review/payload.ts:170-227` `parsePayload()` — **REWRITE**. Half
  its fields (`predicate`, `qualifier`, `assertion`, `valueJson`, `enumValues`,
  `suggestions`, `predicateSuggestions`, `trace`) are extraction payload.
- `frontend/src/review/payload.ts:296-313` `correctionDraft()` — **REWRITE**;
  its lead-in strings name `ambiguous_mention` / `merge_proposal`.
- `frontend/src/review/grouping.ts:1-83` — **KEEP shape, REWRITE keys**. Groups
  the pending lane by subject entity, reading `entity_name / subject / name /
  entity_ref` off the payload (`:36-40`). Directly reusable for a
  **question queue** grouped by subject.
- `frontend/src/screens/ReviewScreen.tsx:1-575` — **REWRITE**. The split-inbox
  shell (lanes, list, selection, bulk bar, detail with prev/next, undo
  snackbar) is *exactly* the shape a silent question queue needs. What dies:
  `EDITABLE_FACT_KINDS` (`:35-38`), the `isInference` branch (`:56`), the
  inference predicate line (`:79`), the `editable` hoisted edit state (`:138`).
- `frontend/src/review/useReviewQueue.ts:1-230` — **REWRITE**. Two lanes,
  optimistic move, server-unwind undo. `resolve/correct/batch/reopen` map onto
  "answer / skip / answer-many" if the question queue keeps this controller.
- `frontend/src/api/client.ts:3331-3385` — the five review endpoints
  (`reviewQueue`, `reviewResolve`, `reviewResolveBatch`, `reviewFileCorrection`,
  `reviewReopen`, `reviewPredicateSuggestions`). `reviewPredicateSuggestions`
  (`:3381-3385`) is **DELETE** outright (predicate registry). The rest are
  **REWRITE** against the question-queue contract (§4).
- `frontend/src/components/Launcher.tsx:41,90,210,252,370-372` — the **Review**
  tile and its live count badge (polled via `api.reviewQueue()` at `:252`).
  **REWRITE**: this badge is the *only* ambient surface for "the agent is
  waiting on you", and the no-push constraint makes it load-bearing (§5).
- `frontend/src/App.tsx:595` — `{card === "review" && <ReviewScreen />}`.
  **REWRITE** (rename the card target).

### 1.6 Search result "fact badges" — **verified absent**

The mission brief anticipates fact badges on search results. There are none.
`frontend/src/screens/SearchScreen.tsx:1-232` renders exactly two card types:
`NoteResultCard` (`:173-204`) and `WikiResultCard` (`:208-231`). The badges
present are `TypeBadge` ("Note"/"Wiki", `:167-171`) and `MatchBadge`
(`semantic`/`keyword`/`both`, `:30-33`) — both **retrieval transparency**, not
facts. `SearchResult` carries `snippet`, `body_preview`, `attachment_count`,
`source_anchor` — no fact fields (`api/client.ts:2020-2050`).

**Verdict: KEEP, entire file.** The one coupling is
`NoteScreen.noteViewFromSearch()` (`NoteScreen.tsx:57-73`), which fabricates
`ingestState: null, analyzed: false` for a search-opened note — that changes
with the state model, not with search itself.

### 1.7 The mock API server

`frontend/src/api/mock.ts` (4777 lines) backs `npm run dev:mock`. Pipeline
routes to **REWRITE**: `/api/notes/{id}/analysis` (`mock.ts:3908`),
`/api/notes/{id}/analyze` (`mock.ts:3533`), `/api/review` (`mock.ts:4061`),
`/api/review/resolve-batch` (`mock.ts:4082`). **KEEP**: the attachment-extract
fixtures (`mock.ts:363,719,3591,3608`) — OCR survives.
`frontend/src/api/mock.test.ts` (26 tests) asserts against these routes.

### 1.8 CSS

Section banners in `frontend/src/styles.css` give clean cut lines:

| Range | Section | Verdict |
|---|---|---|
| `styles.css:5008-5415` | Note view layer | **KEEP** (tab strip needs one new label) |
| `styles.css:5416-5742` | Analysis tab — property-graph edges grouped by subject | **DELETE** (~327 lines) |
| `styles.css:5743-5914` | Sources card | **KEEP, relocate** (~172 lines) |
| `styles.css:5915-6140` | Entity page hub | **REWRITE** |
| `styles.css:6141-6226` | Entities browse | **KEEP** |
| `styles.css:6227-8107` | Review inbox (split inbox, list, bulk bar, detail, proposals, footer, undo) | **REWRITE** — ~1880 lines, the largest single block in play. The list/lane/detail/undo chrome is reusable for the question queue; the `claim:*` block styling is not. |
| `styles.css:8653-13235` | Full Brain agent surface | **KEEP** (~4580 lines — this is the reuse dividend) |
| `styles.css:13236-13474` | Inline approval card | **KEEP** |

Rough teardown: **~330 lines deleted outright, ~1900 rewritten, ~4800 reused**.

---

## 2. What is reusable for note conversations

The headline finding: **a note-rooted conversation is a Full Brain conversation
with a different root and a different entry point.** The transcript model, the
SSE reducer, the streaming client, tool-step rendering, entity chips, inline
approvals, and the status line are all root-agnostic today. The surgery is in
the *controller* (which session to open, and how it is addressed), not the
*view*.

### 2.1 Transcript model + streaming reducer — **reuse verbatim**

- `frontend/src/agent/transcript.ts:139-161` — `TranscriptMessage`
  (`role/text/tools/views/streaming/reasoning/thinking/verdict/attachments`).
  Carries no session/root concept at all. **Zero surgery.**
- `transcript.ts:80-127` — `ToolActivity` (`id/name/ok/args/summary/sources/
  webSources/proposal/entities/progress/textOffset/reasoningOffset`). A graph-
  write tool call lands here unmodified; `entities` (`:99`) already renders
  resolved entities as tappable chips.
- `transcript.ts:242-473` — `applyEvent()`, the pure SSE reducer (33 tests in
  `transcript.test.ts`). **Zero surgery.**
- `transcript.ts:474-…` — `endStream()`. **Zero surgery.**
- `frontend/src/agent/types.ts:48-272` — the `ChatEvent` union: `text_delta`,
  `reasoning_delta`, `tool_call`, `tool_result`, `tool_view`, `tool_progress`,
  `usage`, `done`, `run`, `verdict`, plus the `subagent_*` family. A
  note-conversation turn is the same union. **Zero surgery** — unless questions
  need a first-class event (§4, open question 2).

### 2.2 SSE / streaming client — **reuse verbatim**

- `frontend/src/agent/chat.ts:9-59` — `parseChatStream()`. Frame-boundary
  parsing, malformed-frame tolerance, and the reader-cancel discipline at
  `:29-46` (the leaked-socket fix). **Zero surgery.**
- `api/client.ts:3946-3962` — `api.chat()` (POST + `ReadableStream`, `X-Run-Id`
  header surfaced as a synthetic `run` event). **Zero surgery.**
- `api/client.ts:3964-3977` — `api.chatResume(runId, after)`. **Critical for
  this change**: a minutes-long local pass on a backgrounded phone will drop its
  socket routinely, and this is the existing reattach path.
- `api/client.ts:3979-4007` — `api.sessionLiveRun(sessionId)` → `{runId,
  snapshot, frameIndex}`. After a PWA reload this is how a still-running
  detached turn is found and its bubble reseeded. **This is the single most
  valuable existing asset for the latency problem** (§5).
- `api/client.ts:4009-4014` — `api.cancelChatRun()`.
- `useFullBrain.ts:123-124` — `RECONCILE_INTERVAL_MS = 3000`,
  `RECONCILE_TIMEOUT_MS = 3_720_000` (62 minutes). A dropped-stream recovery
  loop already tolerates a **one-hour** turn. Verified — the minutes-long
  first-pass concern is already engineered for.

### 2.3 The chat surface — **light surgery**

`frontend/src/agent/FullBrainSurface.tsx` (2038 lines) is a **pure view over
`fb: FullBrain`** (`:111-153` Props, `:155` component). It reads the controller
and renders; the composer is the omnibox, provided by `HomeScreen`. Reusing it
note-rooted means:

- `FullBrainSurface.tsx:663-1147` `Bubble` — user/assistant bubbles, live image
  previews, sub-agent fan, tool views, "Worked" disclosure, read-aloud, flags.
  **Zero surgery** for a note conversation.
- `FullBrainSurface.tsx:1195-1349` `ActivityLine` + `:1753-1861` `StepRow` —
  the collapsible per-step trace with arguments/result/raw rungs. A graph write
  (`write_fact`, `link_entity`, …) renders here for free **provided the tool is
  registered in `toolSummary.ts`** — see §2.6.
- `FullBrainSurface.tsx:1709-1751` `EntityChips` — the tappable entity pill,
  `onOpenEntity` all the way up through `HomeScreen.tsx:273` to
  `App.tsx:setEntityView`. **Zero surgery**; this is the entity-pill reuse the
  brief asks about.
- `FullBrainSurface.tsx:1927-1955` `SourceCard` — the cited-note card. In a
  note conversation the root note is *the* source; this card is how a
  cross-referenced note appears.
- `FullBrainSurface.tsx:452-650` `AgentStatusLine` — the "what it's doing right
  now" line, driven by `agent/status.ts:152-160` `agentStatus()`. **REWRITE the
  labels only**: `status.ts:45-56` `TOOL_LABELS` needs entries for the graph-
  write tools; `status.ts:83-105` `phaseStatus()` needs a new terminal state for
  "asked you a question" (today the terminal is `Answered · N tools used`,
  `:100-104`).
- **The surgery that is real**: `FullBrainSurface` is rendered *inline in the
  home body* (`HomeScreen.tsx:269-305`), gated on
  `seg.mode === "research" || "fullbrain"` (`HomeScreen.tsx:212`), with the
  omnibox as composer (`HomeScreen.tsx:336-405`). A note conversation lives
  **inside the note layer** (`App.tsx:695-715`), which has no omnibox. Either
  (a) the note conversation gets its own compact composer inside the note
  layer, or (b) opening a note's conversation flips home to a conversation mode
  rooted at that note. (a) preserves the note-as-container mental model; (b)
  reuses more. **Open question 1.**

### 2.4 The controller — **medium surgery**

`frontend/src/agent/useFullBrain.ts` (1161 lines) is the reuse decision point.

- `useFullBrain.ts:132-156` `FullBrainDeps` — 16 injected functions, with a
  `LIVE` default at `:157-176`. **Every dependency is already injectable**, so a
  note-rooted variant can be a different `deps` object with the same hook. This
  is the cheapest path.
- `useFullBrain.ts:89-95` — `ConvMode = "research" | "fullbrain"`,
  `MODE_AGENTS`, `NEW_AGENT`. A third mode (`"note"`) plus a `noteId` slot is
  the minimal change.
- `useFullBrain.ts:99-121` — `newSessionBody()` and `latestForMode()`. A note
  conversation is **not** "the latest session for a mode"; it is "the session
  whose root is note X". `latestForMode` (`:106-121`) must be replaced by a
  lookup by root, and `AgentSession` (`agent/types.ts:329-357`) needs a
  `root_note_id` (**assumed**, not present today).
- `useFullBrain.ts:248-341` `FullBrain` interface — 30 members. A note surface
  needs maybe 12 of them (`messages`, `busy`, `activeTurn`, `canSend`, `send`,
  `stop`, `usage`, `open`, `requestOpen`, `panel` can go). Options: (a) reuse
  whole and ignore the rest, (b) extract a `useConversation` core. (a) ships
  faster; (b) is the honest factoring given the note surface has no Sessions or
  Proposals panels.
- `useFullBrain.ts:178-214` `fromTurn()` — persisted turn → `TranscriptMessage`.
  **Zero surgery.**
- `useFullBrain.ts:216-246` `historyContent()` — the image-reference suffix with
  a documented **KV-prefix cache contract** with
  `backend/attachment_content.decorated_history_text`. Note-conversation turn 0
  carries the note body *and* attachment ids; this contract must be honored or
  every follow-up turn re-pays a ~35 s vision encode. Flagged as a real hazard.
- `useFullBrain.ts:654-…` `send()` — `:668` drops a send while `busy`; `:670-673`
  opens the sessions panel when there is no session. Both need note-rooted
  behaviour (there is always exactly one session; a queued answer must not be
  silently dropped while a first pass is still streaming — **open question 4**).

### 2.5 Inline approvals — **reuse, with a kind added**

`frontend/src/agent/InlineProposal.tsx:1-428` is the "act on a staged change **in
the conversation**" card the new model wants: per-leaf approve/decline/correct,
a double-tap Enact (`:105-120` arming), and a server-authored outcome sent back
as a follow-up turn.

- `InlineProposal.tsx:15-25` `INLINE_KINDS` — `correction, knowledge,
  appointment, merge, egress, remove-library-video`. A graph-write proposal is
  a new member (`knowledge` may already fit).
- `InlineProposal.tsx:27` `EDITABLE_OPS = {add_note, manage_appointment}` — the
  leaves whose text the owner corrects in place. A `write_fact` leaf wants the
  same treatment.
- Rendered at `FullBrainSurface.tsx:817-826`, falling back to `ProposalChip`
  for non-inline kinds. **Zero view surgery.**
- `frontend/src/agent/ProposalTree.tsx:1-195` (the panel tree) and
  `ProposalsPanel.tsx:1-63` (the list) — **KEEP**. `ProposalsPanel.tsx:9-18`
  `BADGE` needs a glyph if a new kind lands.

**Design tension worth naming.** The brief says the agent "writes the graph
through tools when confident, and asks questions when not". If it *writes*
directly, the proposal/enact machinery is bypassed and `InlineProposal` becomes
reuse-for-questions-only. If it *stages*, then `InlineProposal` is the answer
surface and the question queue is just the un-enacted proposal list. **Open
question 3.**

### 2.6 Tool-use rendering — reuse, with a registration duty

- `docs/mocks/assistant-flip-tooluse.html` is one of ~8 explored tool-use
  variants. The **shipped** pattern is the inline "Worked" disclosure
  (`FullBrainSurface.tsx:1-10` header comment; `ActivityLine` `:1195`, `StepRow`
  `:1753`), not the flip card. Verified — the flip mock is a rejected rival, not
  the implementation.
- `frontend/src/agent/toolSummary.ts:34-…` `STEP_LABELS` — ~140 tools mapped to
  friendly labels, plus `INLINE_ARGS`/`NO_INLINE` policies. **Every new graph-
  write tool MUST be registered here**, and this is enforced by a backend test:
  `backend/tests/unit/test_tool_step_polish.py` parses `toolSummary.ts` with
  regexes and fails if any `.tool` sidecar lacks a label + inline policy. A new
  ingest tool that skips it ships as a raw `snake_case` row and breaks backend
  CI, not frontend CI. **Cross-package gotcha — call it out in the build plan.**
- `frontend/src/agent/views/registry.tsx:1-…` `ToolView` — the fixed
  name→component map; an unknown `view` renders nothing. A structured
  "here's what I wrote to the graph" card would be a **new registered view**
  (data-only, per DESIGN.md invariant "model output never authors markup"), and
  by DESIGN.md `UI development process` a new card shape trips the **three-mock
  GUI gate**.

### 2.7 Agent canvas — **not relevant**

`docs/plans/AGENT_CANVAS_PLAN.md` is annotate/crop/draw over images
(`canvas`, `show_canvas`, `crop_regions`, `render_html`). Its only intersection
with note ingest is that OCR'd attachments already flow to a vision model. No
teardown, no reuse obligation. Listing it as "reusable for note conversations"
in the brief appears to be a misfile.

### 2.8 Everything else in `agent/` that comes free

`markdown.tsx` (safe React-node markdown, no innerHTML), `usePacedText.ts`
(typewriter reveal), `useReadAloud.ts` + `speakable.js` (read-aloud),
`tokenMeter.ts` (context meter), `SubagentFan.tsx`, `DeepResearchProgress.tsx`,
`glyphs.tsx`, `attachmentKind.ts`, `SessionsPanel.tsx`. All root-agnostic;
**zero surgery**, though `SessionsPanel` (1057 lines, 26 tests) may want to
*exclude* note-rooted sessions from the Chats picker so the owner's chat list
isn't flooded with one session per note. **Open question 5.**

---

## 3. State and data flow — how the PWA learns anything today, and what breaks

### 3.1 Today: polling, everywhere. No SSE for notes.

**Verified.** There is no push, no WebSocket, and no SSE for note state. The
only `EventSource` uses in the client are ops logs and host vitals
(`api/client.ts:3508,3524`). Note state is learned by two independent pollers:

1. **The stream poller** — `useNotes.ts:198-204`. Interval is
   `anyInFlight ? 2_500 : 30_000` (`:189,201`), where `inFlight` is *literally
   defined as "the row shows a pending-tone lifecycle chip"* (`:81-87`). Gated
   on `enabled && foreground && visible` (`:199`) — a backgrounded PWA polls
   nothing (`visibility.ts:55-59`). Each tick is `flushOutbox()` then
   `GET /api/notes?limit=100` (`:157-173`).
2. **The Analysis-tab poller** — `AnalysisTab.tsx:461-480`, 3 s
   (`AnalysisTab.tsx:404`), armed only while the tab is mounted, comparing
   `analyzed_at` against a pre-run snapshot, foreground-gated by
   `useForegroundRef()` (`:454,466`).

Plus a third for the ambient badge: `Launcher.tsx:247-270` polls
`api.reviewQueue()` while the launcher is open and foreground.

Optimistic updates exist only for **hide** (`useNotes.ts:273-282`) and for
**outbox appends** (`useNotes.ts:249-252`). Analysis is never optimistic.

### 3.2 What changes when the answer is "a conversation is in progress"

The current model is **binary and monotonic**: `analyzed` flips false→true, and
the chip vanishing *is* the completion signal. That is why a boolean and a poll
sufficed. The new model is neither binary nor monotonic — a conversation can go
`working → asked you something → working again → settled`, and it can bounce
back to "asked" after an answer.

Concretely:

- `StreamItem.analyzed: boolean` (`useNotes.ts:33-34`) must become a state
  enum + a count (§6). `inFlight()` (`:81-87`) then keys the fast poll on
  `state === "working"` only — a note **waiting on the owner** must NOT hold the
  2.5 s poll open, or every unanswered question pins the stream at 2.5 s
  forever. This is a real regression risk in a naive port.
- The poll cadences are wrong for the new latency profile. A local first pass
  taking minutes means 2.5 s ticks are ~100 wasted round-trips per note. Two
  options: (a) back the "working" cadence off to ~10 s, or (b) hang the stream
  off the existing chat SSE for the open conversation and leave the list poll
  idle. (b) is strictly better when a note view is open and irrelevant when it
  is not — the stream is a *list*, and there is no list-level stream today.
- The **backgrounded case is the interesting one and it is already handled by
  accident**: with no push and a foreground-gated poll, a phone in a pocket
  learns nothing. When the owner returns, `useForeground` re-runs the effect and
  fires an immediate sync (`useNotes.ts:198-204`, `visibility.ts:32,43-50` —
  which also listens to `pageshow`/`focus`/`online` because iOS standalone PWAs
  frequently deliver only `pageshow`). A conversation that finished, asked three
  questions, and went quiet while the phone slept simply shows its state on
  return. **That is the correct behaviour for a silent queue** and needs no new
  machinery.
- A **still-running** turn on return is the harder case, and the existing answer
  is `api.sessionLiveRun()` (`api/client.ts:3979-4007`) → reseed the bubble from
  `snapshot` → `chatResume(runId, frameIndex)`. This works today for Full Brain
  after a full PWA reload; it should be wired into the note conversation on
  mount, not reinvented.

### 3.3 The IndexedDB offline outbox

**Verified**: `frontend/src/notes/outbox.ts:53-92` is the **only** IndexedDB
usage in the frontend (`grep indexedDB src/` returns `outbox.ts:64,74` and
nothing else). DB `jbrain`, store `outbox`, keyPath `client_id`, version 1.
Everything else that persists uses `localStorage` (theme, font scale, tasks
viewed, band picks, read-aloud autoplay, …).

`flushOutbox` (`outbox.ts:102-161`) is serialized (`:99-108`), idempotent on
`client_id` (`:5-6`), persists per-attachment progress so a retry never
re-uploads (`:137-144`), drops 4xx permanently rather than wedging the queue
(`:150-156`), and stops on 5xx/network so order is preserved (`:157-158`). It is
driven from `useNotes.ts:159` (each sync), `:177-184` (a flush-only off-screen
retry), and `:209-214` (the `online` event, from anywhere in the app).

**A conversation started offline.** The note is queued locally with a
`client_id` and appears immediately as a `pending` row
(`useNotes.ts:249-252`, `pendingItem()` at `:116-138`, chip at
`Stream.tsx:203`). It has **no server id** (`StreamItem.id === null`), so:
`NoteScreen.tsx:342,373` hides the ⋯ menu, `AnalysisTab.tsx:512-514` shows
"analysis runs after indexing", and `Stream.tsx:63` disables the swipe rail.
The successor behaviour is the same shape: **turn 0 cannot exist until the note
has a server id**, so the conversation tab must render a distinct
`queued — the agent hasn't seen this yet` state, not an empty transcript. This
is a genuinely new state (§6) with no current analogue, because today "not
analyzed yet" and "not even uploaded yet" happen to render the same quiet line.

**An answer typed offline — this is the gap.** There is **no offline queue for
chat**. `useFullBrain.send()` (`:654-…`) uploads attachments then opens the SSE
stream directly; a network failure lands in the broad catch at `:873-878` and
settles the bubble as `stopped`/`error` via `endStream`. The typed text is
**gone from the controller** (the omnibox keeps its draft on an *attachment*
failure — `:685-689` throws `AttachmentUploadError` before touching the
transcript — but a mid-stream network drop has already appended the user
bubble). For a phone-only owner answering questions on the move, silently losing
an answer is the worst failure mode in this whole design.

Three options, in increasing cost:
1. **Do nothing** — answers require connectivity; the composer shows an error
   and keeps the text. Cheapest, and matches today's chat behaviour.
2. **Reuse the outbox store** — add a second object store (`answers`) to the
   same IDB, keyed by `client_id`, flushed by the same `online`/foreground
   hooks. The DB is at `version: 1` (`outbox.ts:64`) so this is an
   `onupgradeneeded` bump — cheap, and the flush discipline (serialized,
   idempotent, per-item progress, 4xx-drop) is already written and tested.
3. **Full offline conversation** — queue arbitrary turns, replay in order.
   Overkill; ordering against a server-side agent loop is not solvable client-
   side.

Option 2 is the recommendation, and it is the *reason the outbox is worth
studying* rather than a footnote.

---

## 4. API surface delta

### 4.1 Endpoints the frontend calls today that disappear

| Client method | Route | Fate |
|---|---|---|
| `api.noteAnalysis` (`client.ts:3077-3080`) | `GET /api/notes/{id}/analysis` | **GONE** — replaced by the conversation |
| `api.analyzeNote` (`client.ts:3084-3086`) | `POST /api/notes/{id}/analyze` | **GONE** — a re-read is a turn |
| `api.reviewPredicateSuggestions` (`client.ts:3381-3385`) | `GET /api/review/{id}/predicate-suggestions` | **GONE** — predicate registry |
| `api.reviewQueue` (`client.ts:3331-3334`) | `GET /api/review?status=` | **REPLACED** (question queue) |
| `api.reviewResolve` / `reviewResolveBatch` (`:3336-3360`) | `POST /api/review/{id}/resolve`, `/resolve-batch` | **REPLACED** |
| `api.reviewFileCorrection` (`:3362-3370`) | `POST /api/review/{id}/correction` | **REPLACED** — the correction channel becomes "reply in the conversation" |
| `api.reviewReopen` (`:3372-3378`) | `POST /api/review/{id}/reopen` | **UNCERTAIN** — "unwind a decision" only survives if the agent's writes are reversible |

**Survive unchanged**: `GET /api/entities`, `GET /api/entities/{id}`,
`/neighbors`, `/graph` (`client.ts:3089-3216`); `attachmentExtracts` /
`analyzeAttachment` (`client.ts:2484-2494`); all note CRUD
(`client.ts:2421-2467`); `search` (`:2462-2467`).

### 4.2 The replacement shape

Two viable framings, and the choice drives how much of `useFullBrain` is reused.

**A — note conversations ARE agent sessions** (recommended). No new
conversation endpoints at all; `AgentSession` (`agent/types.ts:329-357`) gains
`root_note_id: string | null`, and:

- *list*: `GET /api/sessions?root_note_id=…` — or a `notes` list field carrying
  the session id + state + open-question count, avoiding a second round-trip per
  stream row (**strongly preferred**: the stream renders up to 100 rows and must
  not fan out).
- *get*: `GET /api/sessions/{id}/transcript` — **exists today**
  (`client.ts:3918-3921`), returns `TranscriptTurn[]`, already replayed by
  `fromTurn()`.
- *post a reply / answer a question*: `POST /api/chat` — **exists today**
  (`client.ts:3946-3962`), streaming SSE. An answer to a queued question is a
  turn with a `question_id` discriminator, mirroring the existing
  `proposal_outcome` / `deferred_outcome` flags on `ChatRequest`
  (`agent/types.ts:412-421`).
- *stream turns*: `POST /api/chat` for a new turn;
  `GET /api/chat/runs/{id}/stream?after=N` to reattach (`client.ts:3964-3977`);
  `GET /api/chat/sessions/{id}/live-run` to find a detached run after reload
  (`client.ts:3979-4007`). **All three exist.**
- *the queue*: `GET /api/questions?status=open` →
  `{ items: [{ id, note_id, session_id, turn_index, text, subject?, asked_at,
  status }] }`, and `POST /api/questions/{id}/answer {text}` (or skip). Shaped
  to fit `useReviewQueue`'s two-lane optimistic controller
  (`review/useReviewQueue.ts:17-35`) and `review/grouping.ts:36-40`'s subject
  extraction.

**B — a parallel `/api/note-conversations` surface.** Cleaner conceptually,
but re-implements `chat`, `chatResume`, `live-run`, `cancel`, transcript
replay, and attachments — every one of which is already built, tested, and
hardened against dropped sockets. Not recommended.

**One thing genuinely new either way**: the stream list must carry per-note
conversation state cheaply. Today `NoteOut` carries `ingest_state` + `analyzed`
(`useNotes.ts:100-101`); the successor is `conversation_state` +
`open_question_count` on the same list payload.

---

## 5. Local-model latency in the UI

The first pass may take minutes. `useFullBrain.ts:124` already tolerates a
62-minute turn, and `AgentStatusLine` already distinguishes "loading the model"
from "thinking" (`status.ts:130-147`, `modelLoadStatus`) precisely because a
cold 120B load made the agent look hung. Both are directly applicable.

**What the note looks like in the stream meanwhile.** Today a mid-pipeline note
shows an amber `analyzing…` chip (`lifecycle.ts:53`) and holds the 2.5 s poll.
The successor should show a *calm working* chip and **not** hold a fast poll for
minutes. The `Stream.tsx:184-206` chips row is the render site.

**The no-push constraint bites here.** With questions in a silent queue and no
notification, the owner's only ambient signal is
`Launcher.tsx:370-372`'s tile badge — which polls only while the launcher is
**open** (`Launcher.tsx:248`). A question asked while the launcher is closed is
invisible until the owner opens the menu. Either the badge count moves to the
top bar / omnibox, or the stream row's own chip becomes the signal. Recommend
the latter: the note that asked the question is the honest place to say so.

### 5.1 Enumerated states of a note-conversation

| # | State | Trigger | Stream row (`Stream.tsx:184-206`) | Note-view conversation tab |
|---|---|---|---|---|
| 0 | `queued` | outbox row, no server id | `pending sync` chip (exists, `Stream.tsx:203`) | "queued — the agent hasn't seen this yet"; composer disabled |
| 1 | `indexing` | server has it, chunks/OCR not in | amber `indexing…` (exists, `lifecycle.ts:41-43`) | Sources card with per-stage spinners; transcript empty |
| 2 | `reading` | attachments still extracting | amber `reading image(s)…` (exists, `lifecycle.ts:49-52`) | as above, `awaitingImageCount` drives it |
| 3 | `working` | turn 0 streaming | calm chip, e.g. `thinking…`; **no fast poll** | live transcript + `AgentStatusLine` (`FullBrainSurface.tsx:452`) + Stop |
| 4 | `asked` | ≥1 unanswered question | **amber `1 question` / `N questions`** — the load-bearing new chip | question card(s) at the foot of the transcript, composer focused |
| 5 | `answered-working` | owner replied, agent re-running | same as `working` | live transcript; the answered question renders settled |
| 6 | `settled` | agent done, nothing asked | **no chip** (matches today's quiet end-state, `lifecycle.ts:48`) | full transcript + what it wrote |
| 7 | `stalled` | run errored / stopped / budget | rose `couldn't finish` (reuse `chip-failed`, `lifecycle.ts:27-29`) | settled bubble with `stopReason` (`status.ts:73-80` `STOP_LABELS`) + a retry |
| 8 | `detached` | app returned, a run is still live | as `working` | `sessionLiveRun` reseeds the bubble, `chatResume` picks up the stream |

States 0–2 and 6–7 already have chips. **4 is the only genuinely new one**, and
it is the one the whole design turns on. State 8 has no chip and needs none — it
is a client-side recovery, invisible by design.

**Two honesty rules carried over from DESIGN.md that apply directly:**
- *"absence of state chrome IS the calm state"* (`analysis/bits.tsx:68`) — a
  settled conversation shows nothing, like an analyzed note today.
- The model-load line outranks the turn status (`status.ts:117-129`) — during a
  minutes-long cold load the note must say *"Loading gpt-oss-120b… 43%"*, not
  *"Thinking it through"*. Reuse `modelLoad` plumbing:
  `HomeScreen.tsx:217,304` → `FullBrainSurface` prop `:147-152`.

---

## 6. Testing

Verified toolchain: `vitest run` + `biome check .` + `tsc --noEmit`
(`frontend/package.json:26,30,31`). No frontend coverage gate in
`package.json`; the 80% gate in `CLAUDE.md` #5 is backend.

### 6.1 Tests that die outright

| File | Lines | Tests | Fate |
|---|---|---|---|
| `src/components/AnalysisTab.test.tsx` | 515 | 13 | **DELETE** (its subject is deleted) |
| `src/notes/lifecycle.test.ts` | 93 | 12 | **REWRITE** — the chip ladder changes |
| `src/analysis/format.test.ts` | 234 | 28 | **SPLIT** — the `fmtTemporal`/`fmtQuantity`/`valueLabel` cases survive with the helpers; the `factValue`/`factSpan`/`dedupeTokens` cases die |
| `src/review/blocks/registry.test.ts` | 81 | 6 | **REWRITE** — asserts the kind→sequence table |
| `src/review/grouping.test.ts` | 100 | 10 | **REWRITE** — payload keys change |
| `src/screens/ReviewScreen.test.tsx` | 1229 | 35 | **REWRITE** — the largest single test casualty |
| `src/screens/NoteScreen.test.tsx` | 496 | 16 | **PARTIAL** — Attachments/⋯/swipe cases survive; the analysis-tab-default and lifecycle-chip cases change |
| `src/screens/EntityScreen.test.tsx` | 432 | 10 | **REWRITE** — every fixture is a `FactOut` with `status`/`confidence`/`valid_to` |
| `src/api/mock.test.ts` | 514 | 26 | **PARTIAL** — the `/analysis`, `/analyze`, `/review` route cases |
| `src/components/Stream.test.tsx` | 178 | 10 | **PARTIAL** — chip assertions |
| `src/notes/useNotes.test.tsx` | 170 | 5 | **PARTIAL** — poll-cadence assertions key on `analyzed` |
| `src/review/blocks/ClaimContradiction.test.tsx` | 42 | 5 | **KEEP** (wiki kind) |

Rough count: **~90 of 2028 tests deleted or rewritten**, concentrated in
`ReviewScreen.test.tsx` (35) and `AnalysisTab.test.tsx` (13).

### 6.2 Tests that must NOT be disturbed

`src/agent/` carries the reuse dividend and its tests are the safety net:
`FullBrainSurface.test.tsx` (2443 lines, 84 tests),
`views/registry.test.tsx` (1989, 77), `transcript.test.ts` (669, 33),
`SessionsPanel.test.tsx` (706, 26), `useFullBrain.test.tsx` (597, 17),
`InlineProposal.test.tsx` (310, 13), `toolSummary.test.ts` (160, 16),
`chat.test.ts` (121, 7), `FullBrainSurface.lifecycle.test.tsx` (307, 4).
**~277 tests.** If a note conversation reuses these components, this suite is
the regression gate proving the reuse was non-invasive — it should stay green
*without modification* through the whole teardown. Any diff here is a signal
that the surgery was deeper than planned.

### 6.3 What the new surfaces need

- **`notes/lifecycle.ts`** — a pure state→chip function, unit-tested like today
  (12 cases): one per state in §5.1, plus the precedence rule that `asked`
  outranks `working` and that `queued` (no server id) outranks everything.
- **`NoteConversation`** — mount states 0–8; a question card renders and its
  answer sends; a note with no server id disables the composer; `sessionLiveRun`
  reattach on mount; `chatResume` after a simulated socket drop.
- **The question queue screen** — lanes, grouping by subject, optimistic answer
  with rollback, empty lane copy. Largely a port of `ReviewScreen.test.tsx`'s
  structure with the fact-shaped fixtures removed.
- **`useNotes`** — the poll-cadence regression: a note in `asked` must **not**
  hold `ACTIVE_INTERVAL_MS`. This deserves an explicit test; it is the easiest
  bug to ship.
- **Offline answer** (if option 2 in §3.3): an answer typed offline persists
  across reload and flushes on `online`, mirroring `outbox.test.ts`.
- **`toolSummary.ts`** — every new graph-write tool needs a `STEP_LABELS` entry
  and an inline-arg policy, gated by `backend/tests/unit/test_tool_step_polish.py`.
  A frontend-only PR that adds tools will fail **backend** CI.
- **Mock server** — `mock.ts` needs a note-conversation route set, or
  `dev:mock` (the only way to drive the UI without a box) goes dark for the new
  surface.

---

## 7. Sequencing note (process constraint, not a design opinion)

`docs/reference/DESIGN.md:1069` ("UI development process") and its recurring
"settled in a three-way review" annotations make the mock-first gate binding for
new surfaces. Three things in this teardown are new shapes and will trip it:
(1) the note-conversation tab, (2) the question-queue screen, (3) the `asked`
stream chip. The Full Brain transcript, inline approvals, entity pills and tool
steps are all **already-settled** paradigms and reuse them without a new gate.
Planning the mock gate up front is cheaper than discovering it at merge.

---

## 8. Verified vs assumed

**Verified by reading the tree** (line numbers as cited): every file path,
line number, class name, endpoint, type, poll interval, test count and CSS
range in §0–§6; the absence of fact badges in search; IndexedDB being used only
by the note outbox; there being no SSE or push for note state; the
`test_tool_step_polish.py` cross-package gate; `chatResume` /
`sessionLiveRun` / `RECONCILE_TIMEOUT_MS` existing today; `assistant-flip-
tooluse.html` being a rejected rival rather than the shipped pattern; the 11-of-
13 review kinds being extraction artifacts.

**Assumed / proposed (not in the tree today):**
- `AgentSession.root_note_id` and a note-conversation session concept.
- `NoteOut.conversation_state` + `open_question_count` on the list payload.
- The `GET /api/questions` / `POST /api/questions/{id}/answer` shape in §4.2.
- The state names in §5.1 (`queued/indexing/reading/working/asked/
  answered-working/settled/stalled/detached`).
- That graph writes are reversible enough to keep an "unwind" affordance.
- That a question is a first-class row, not merely an un-enacted proposal leaf.
- The teardown line counts (~330 deleted / ~1900 rewritten / ~4800 reused) are
  measured from the CSS section banners, so they are accurate for CSS and
  estimated for TSX.

---

## Open questions for the owner

1. **Where does a note's conversation live?** Inside the note layer (its own
   compact composer, note-as-container), or does opening it flip home to a
   conversation mode rooted at that note (maximum reuse of the omnibox +
   `FullBrainSurface`, but the note stops being the container)? This decides
   whether `HomeScreen`'s composer plumbing is reused or duplicated.

2. **Is a question a first-class object, or just an assistant turn?** A
   first-class `question` row gives a cheap queue, a badge count, and a stable
   "answered/skipped" record. Treating it as "the last assistant turn ended with
   a question" is zero backend work but makes the queue a heuristic. The silent-
   queue requirement argues strongly for first-class.

3. **Does the agent write the graph directly, or stage a proposal the owner
   enacts?** The brief says "writes when confident", which bypasses
   `InlineProposal`. If it stages instead, the existing inline-approval card
   *is* the answer surface and the question queue is the un-enacted proposal
   list — a much smaller build. Which one?

4. **What happens to an answer typed while the first pass is still running?**
   Today `send()` drops a turn while `busy` (`useFullBrain.ts:668`). For a note
   conversation that could mean the owner's answer to question 1 is silently
   discarded while the agent works on question 2. Queue it, interrupt the run,
   or block the composer with a visible reason?

5. **Do note conversations appear in the Chats picker?** One session per note
   would flood `SessionsPanel` (which today buckets Research/Full Brain chats).
   Hide them entirely, give them their own bucket, or show only those with open
   questions?

6. **What is the ambient signal for a waiting question, given no push?** The
   Review tile badge only polls while the launcher is open
   (`Launcher.tsx:248`). Move the count to the top bar, to the omnibox, or rely
   solely on the stream row's chip?

7. **Does the entity page keep a revision history?** `EntityScreen`'s
   `N earlier →` rail and `EntityHistorySheet` exist because the arbiter built a
   supersession chain. If the agent's write tool doesn't record one, both are
   deleted, not rewritten — and the entity page becomes strictly current-value.

8. **Should the offline answer queue be built now?** Option 2 in §3.3 (a second
   IndexedDB store on the existing `jbrain` DB) is cheap and reuses a hardened
   flush loop. Losing a typed answer on a phone with poor signal is the worst
   plausible failure of this design; is it in scope for the first cut?
