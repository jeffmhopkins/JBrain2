# JBrain2 — GUI Design System

Binding reference for all UI work. Derived from the owner-supplied JBrain v1
reference screens (dark composer, knowledge hub, calendar, medical entry).
Components use **tokens only** — no raw hex values outside the token sheet.

## Principles

1. **Phone-first, one-thumb.** Primary actions live in the bottom half of the
   screen. Touch targets ≥ 44px. Bottom nav is the spine.
2. **Minimal / utilitarian.** Near-monochrome surfaces; color is *information*
   (state, domain), never decoration. No gradients, no glass, no shadows
   heavier than a hairline border.
3. **Comfortable density.** Generous padding and type sizes; fewer things per
   screen, each easily hittable. Data-dense surfaces (logs, lab tables,
   location history) may use the compact variants noted below.
4. **Color codes the domain.** Accents are muted and contextual: an active
   medical surface tints rose, research tints amber, general/info tints
   steel blue. The accent tells you *where you are and what kind of data
   you're touching*.
5. **Honest status, always visible.** Connectivity, sync, and server state are
   surfaced persistently (status dot + banner), never hidden behind a tap.
   The app must feel trustworthy about what it is and isn't doing.

## Theming

Dual theme, dark-first. Implementation:

- All colors are CSS custom properties on `:root`, overridden by
  `[data-theme="light"]`. Components reference tokens only.
- Default follows `prefers-color-scheme`; a Settings option overrides it
  (`system | dark | light`), persisted locally and (later) as a user setting.
- The PWA `theme-color` meta updates with the active theme.
- **Text size**: every type token is `calc(px × var(--font-scale))`; a
  Settings "Text size" control (65 / 75 / 90 / 100%) sets the scale,
  persisted locally. **Default is 75%** of the drawn px values (settled in
  Phase 1 polish — the doc's sizes read large on real devices).

## Color tokens

### Neutrals (dark / light)

| Token | Dark | Light | Use |
|---|---|---|---|
| `--bg` | `#0E0F11` | `#F7F7F5` | App background |
| `--surface` | `#17181B` | `#FFFFFF` | Cards, tiles, composer |
| `--surface-2` | `#1E2024` | `#F0F0EE` | Nested surfaces, inputs, inactive segments |
| `--border` | `#26282C` | `#E2E2DF` | Hairline borders (1px) |
| `--text` | `#E6E7E9` | `#1A1B1E` | Primary text |
| `--text-2` | `#9A9DA3` | `#5C5F66` | Secondary text, descriptions |
| `--text-3` | `#5C5F66` | `#9A9DA3` | Muted: placeholders, disabled, out-of-month days |

### Accents (identical across themes, tuned for both)

Muted, desaturated pastels — never saturated/neon. Each has a `-tint`
(translucent background for active segments, badges, banners).

| Token | Value | Tint | Meaning |
|---|---|---|---|
| `--steel` | `#7FA7C9` | 13% alpha | Brand (wordmark dot), Full Brain mode, links, focus ring, info |
| `--green` | `#8FBC9A` | 13% alpha | Entry mode / "saved", success, healthy |
| `--amber` | `#C9A36A` | 13% alpha | Research mode (read-only), pending/in-progress, warnings |
| `--rose` | `#CF8A8F` | 13% alpha | Medical domain, errors, destructive |
| `--violet` | `#A493C9` | 13% alpha | Financial domain |
| `--location` | `#6FB6B1` | 13% alpha | Location domain (teal) — map trail/fence/start, location tool-views |

Semantic aliases: `--ok: var(--green)`, `--warn: var(--amber)`,
`--danger: var(--rose)`, `--info: var(--steel)`, `--location: #6FB6B1`. The
location domain's color is **teal `--location` (`#6FB6B1`)** — settled by the
L3 location-assistant GUI gate (owner chose Option B + the teal location accent;
see `docs/mocks/location-views/README.md`). It is distinct from the five mode
accents and shares the teal hue with the MedicalProcedure entity disc (a
type-axis use, not a domain one — the two axes are independent). The inline
location tool-views (`location_map`/`place_card`) and their Leaflet trail/fence/
start markers ride this token (the steel `loc-lf-*` classes on the full-screen
map are unchanged unless separately re-decided).

**Mode/domain coding rule** (settled in the Phase 1 omnibox review):
green=entry/save, amber=research/read-only, steel=full-brain/agent,
rose=medical, violet=financial. A surface's active segment, status dot,
send button, and section markers all take its mode color — you can *see*
which mode and firewall you're inside.

### Entity-type accents

A separate axis from domain color: the entity-type icon disc is tinted by the
entity's *type*, while domain still rides its own dot on the same row. The five
accents above are reused where a type maps naturally; five muted tones plus a
neutral slate fill the rest, all in the same desaturated register so the disc
never out-shouts the chrome. `Entity.kind` is free text — anything outside this
set normalizes to **Thing**.

| Type | Token | Value | Type | Token | Value |
|---|---|---|---|---|---|
| Person | `--steel` | `#7FA7C9` | Animal | `--sage` | `#A8BD7E` |
| Organization | `--violet` | `#A493C9` | CreativeWork | `--rose` | `#CF8A8F` |
| Place | `--green` | `#8FBC9A` | MedicalCondition | `--terracotta` | `#D0917F` |
| Event | `--amber` | `#C9A36A` | MedicalProcedure | `--teal` | `#6FB6B1` |
| Product | `--periwinkle` | `#8F9FD0` | Drug | `--orchid` | `#C98AB4` |
| | | | Thing | `--slate` | `#9AA0A8` |

The disc is `color-mix(in srgb, <accent> 16%, transparent)` background with the
accent as the glyph color — one tint formula, no per-type `-tint` tokens.

## Typography

- System font stack: `system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`.
- Scale: 12 (micro/labels) · 14 (secondary) · 16 (body, inputs) · 18 (card
  titles) · 22 (screen titles) · 28 (wordmark/hero). Line-height 1.4.
- Weights: 400 body, 500 titles/buttons, 700 wordmark only.
- Section headers (e.g. KNOWLEDGE, AUTHORING): 12px, uppercase, letter-spacing
  0.08em, `--text-3`.
- The wordmark is `JBrain` + a `--accent` period: **JBrain.**

## Spacing & shape

- 4px base unit. Common steps: 4 / 8 / 12 / 16 / 24 / 32.
- Screen gutter 16px; card padding 16px; grid gap 12px.
- Radii: 16px cards/composer/tiles, 12px inputs/segments, 999px pills/dots.
- Borders: 1px `--border` on every raised surface; no drop shadows in dark,
  optional `0 1px 2px rgba(0,0,0,.06)` in light.
- Touch targets ≥ 44×44px; compact-variant rows may reduce to 36px height but
  never shrink tap areas below 44px including padding.

## Core components

**Top bar** — wordmark (or back chevron + screen title) left; right cluster:
status dot, notifications bell, mute, quick-action bolt. Height 56px.

**Status banner** — full-width strip under the top bar for connectivity/sync
problems: `--rose` text on rose-tint background, e.g. *"Browser online, but
JBrain server unreachable — retrying…"*. Auto-dismisses on recovery. Never
use modals for connectivity.

**Status dot** — 8px circle: green=healthy, amber=degraded/retrying,
rose=error, `--text-3`=unknown. Appears in top bar and composer footer.

**Segmented control** — pill row on `--surface-2`; inactive segments
transparent with `--text-2` label + icon; active segment gets the
*context-appropriate* accent tint background with accent icon and `--text`
label (Entry=accent, Research=amber, Medical=rose, Financial=green).

**Card / tile** — `--surface`, 16px radius, hairline border. Hub tiles:
3-column grid, outline icon top, 16px/500 title, 12px `--text-2` description.

**Composer** — the signature surface: card with mode segments on top, large
16px placeholder body, footer row with status dot + context microcopy left
(*"Files to notes/medical/ · PDFs staged."*) and paperclip + send icons right.

**Buttons** — primary: accent-tint background, accent text (no solid fills);
secondary: `--surface-2` + border; destructive: rose-tint + rose text;
ghost: text-only. All 12px radius, 44px min height. Destructive actions get
an inline confirm (button morphs to "Tap again to confirm") — `window.confirm`
is a Phase-0 placeholder to be replaced.

**Inputs** — `--surface-2` fill, hairline border, 12px radius, 16px text;
focus = 2px `--accent` ring (`:focus-visible` only). Selects match.

**Lists** — full-bleed rows inside cards, 1px `--border` separators, 44px+
rows; leading icon optional, trailing chevron for navigation.

**Badges** — 12px text on the relevant tint, pill radius (e.g. `running`,
`healthy` in the Ops screen).

**Meters** — 6px pill-radius track on `--surface-2`; fill is `--ok`, turning
`--warn` above 80% and `--danger` above 92%. Always paired with a text
value — the bar is a glance aid, never the only encoding.

**Status-card grids** — glanceable per-item status (Ops containers and
similar) uses **half-width cards** in a 2-column grid, not full-width rows
(settled in Phase 1 polish); names/images truncate with ellipsis rather
than wrapping.

**Ops Data card** (settled in a three-way review — inline card won over a
backup-vault list and a guided transfer sheet): a "Data" section with two
inline buttons. **Export backup** runs a supervisor one-shot that bundles
the database dump + blob files + manifest into one `.jbrain.tar`, then the
browser downloads it. **Import backup…** picks a file, shows
`name · size`, and arms a rose tap-again confirm that names the
consequence ("current data is overwritten"); a safety backup is taken
first, the stack restarts mid-import (the card tolerates the api being
unreachable, like Server update), and success offers **Reload app**.
**Reset DB** (right of Import, danger-styled) is a testing convenience
with the same double-press confirm — tap arms "Tap again — erases ALL
notes and data" (3s auto-disarm) — that takes a safety backup first, then
truncates all content data (notes, attachments, chunks, jobs, the entity
graph, facts, review items, analyses) and empties the blob volume while
auth/identity, domains, and llm_usage telemetry survive; the worker
restarts and success offers **Reload app**.
Progress is phased text + the one-shot's log tail, matching the update
card — no fake progress bars.

**Calendar** — Day/Week/Month/List segments; month grid with hairline cell
borders, out-of-month days in `--text-3`, today = accent ring around the day
number; selected-day panel below with `+ Add` (accent link) and `Open day →`.

**Home stream** (settled in the Phase 2 home review): home is NOT an
infinite timeline — it shows the **last 2 days** of notes with an
"older notes live in Search" pill above. The stream area is
**mode-scoped**: Entry shows the note stream; Research / Full Brain show
that mode's **conversation cards** (title, last-message preview, time,
mode dot) — tapping one descends the tree into the conversation layer;
typing in those modes always starts a NEW conversation. With no
conversations yet, the mode shows a one-line empty state.
**Swiping a note bubble left** slides it to reveal an
**icon action rail — Delete · Edit · Hide** (settled in the entry-mode swipe
review; **Move domain** was dropped from the rail to the note-view ⋯ menu and
**Hide** added — three 64px buttons, RAIL_WIDTH 192, each an outline icon over
a lowercase label). **Edit** opens the full-screen **focused-writer** editor (— settled in the
Phase 2 edit review against two rival designs: chrome fades to a whisper
context line (domain dot · date) with a quiet ✕; the note is the screen at
`--fs-editor` (20px-scale) with 1.7 line-height and a 38em measure, steel
caret/selection; the thumb bar holds live `words · chars` (+ amber
`· unsaved`) and a 44px **done** button — surface-2 until savable, then
green-tint per the green=save rule — riding above the keyboard; dirty ✕
arms an inline rose "discard edits?" that auto-disarms in 3s or on typing;
saving PATCHes the body and re-triggers ingestion; the editor also owns
**attachment management** — a paperclip in the thumb bar adds files, chips
above the bar list them with a tap-again rose remove; adds/removals apply
immediately to the note, independent of the text's done/cancel). **Delete**
uses an inline tap-again confirm (the button arms to a filled-rose state).
**Hide** removes the note from the home stream **without deleting it** — a
persisted per-note `hidden_at` flag (not a local view filter), so it survives
reload and syncs across devices; the note's chunks are untouched, so it stays
in Search and openable from there. Hiding offers a single **undo** toast
(green=save rule does not apply — undo links steel); there is **no persistent
hidden tray** and **no swipe-right gesture**. Hide/unhide are dedicated
endpoints (`POST /notes/{id}/hide|unhide`), never a PATCH, so visibility
toggles never re-ingest. Tapping a bubble opens the note sheet.

**Capture location** (settled in the Phase 2 review): a Settings toggle,
**on by default** (browser permission prompt on first launch; denial just
means location-less notes). While on, the app keeps a warm geolocation fix
and attaches lat/lng/accuracy to a note at send **only if the fix is under
2 minutes old** — capture is never blocked or delayed waiting for GPS.
Note-location is owner-eyes metadata: Phase 7 scoped tokens never receive
location fields, regardless of the note's domain.

**Image analysis** (Settings): a segmented control, **ocr only | full
analysis**, default **full**. Full = verbatim transcription plus a salient
description (objects, people, context, relationships visible — the text the
fact pipeline mines); ocr only skips the description call. This is the
**first server-synced setting** (GET/PUT `/api/settings` over
`app.settings`, owner-only RLS) because the worker reads it per job — theme
and text size deliberately stay device-local for now. Either way, capture
never waits: vision runs after sync.

**Note view** (settled in the Phase 2 review; Attachments tab settled in a
later three-way review — **manifest** won over gallery and inline-viewer
designs): entry-stream bubbles clamp at **3 lines**; tapping opens the
**note view layer** (slide-up tree level, swipe-down back) with a
**Note / Attachments / Analysis tab split**:

- *Note tab*: full markdown body only. No attachment chrome (files live
  in their own tab) and no action buttons — note actions live in a
  **⋯ menu right-aligned on the domain/date line** (same affordance as
  the attachment rows' ⋯; kept out of the top bar, which stays
  navigation-only) opening the shared bottom sheet with **edit**
  (amber-tint), **move domain**, and **delete** (rose, tap-again confirm
  "tap again — deletes this note"); the ⋯ hides for not-yet-synced
  outbox notes.
- *Attachments tab* — the **canonical attachment manager** (the editor
  keeps its quick paperclip for capture-time adds). The tab label carries a
  count pill. Layout is a **manifest**: a one-line summary
  (`N files · total size · how many searchable / indexing / awaiting ocr`),
  then one bordered card of rows — type icon, filename,
  `size · media type` meta line, and a **pipeline status chip** derived
  client-side (`indexing…` amber while the note's ingest is pending,
  `text extracted` green-tint for text/PDF, `ocr queued…` amber for an
  image whose vision cache is empty, `text extracted (ocr)` for an
  OCR-only image, `text + description` once full analysis also cached a
  description). Each row ends in a 44px `⋯` that opens the shared
  bottom sheet with **open** (new tab) and **remove** — remove uses the
  tap-again confirm and spells out the consequence ("removes file + its
  extracted text"). The card's last row is a steel **add files** row
  (multi-select) with the hint "pdfs and images become searchable";
  adds/removals apply immediately and re-trigger ingestion.

  **Image extracts moved out** (settled twice: first a three-way review
  chose inline expansion in the manifest [mock C]; then the Sources-card
  review [decided: **variant B** of three mockups] relocated viewing +
  the analyze re-run to the **Analysis tab's Sources card**): Attachments
  is a **pure manifest** again. The status chips stay; rows are **inert**
  — no caret, no tap expansion, no pdf-hint line; the per-file ⋯ sheet
  (open / remove) is untouched.
- *Analysis tab* (lights up by phase): generated title + 3-6 tags (P3 —
  pre-P3 the header shows only domain + date, **no title fallback**);
  salient facts with kind badges (measurement/state/event/preference),
  status chips (active / pending-review / **pinned**) and confidence;
  entity chips → entity pages; wiki backlinks → articles (P6). At the
  bottom, the **Sources card** (settled review — variant B, "sources
  provenance card") frames analysis as a pipeline:
  - A **note-text row** (char count, always ✓), then **one row per image
    attachment** with a per-stage status line (`ocr ✓ · description ✓`;
    amber spinners for in-flight stages, `queued` while a stage waits on
    OCR, `skipped` when the mode is ocr only).
  - Image rows carry a disclosure caret and **unfold in place** — a small
    thumbnail strip with `open full image →`, the verbatim OCR in a quiet
    monospace inset (clamped ~6 lines, "show all N lines" grows in place,
    `[illegible]` rendered muted-italic and never reworded), the
    description beneath when present with tool/confidence micro-meta and
    the "mined for facts in analysis" provenance line
    (`ocr · xai:grok-4.3 · 70%`). A row lacking a description in ocr mode
    reads *"no description — image analysis is set to ocr only."*
    Extracts are fetched eagerly when the card mounts (the stage line
    needs them up front).
  - Each image row's **⋯ opens the shared bottom sheet** with **re-run
    image analysis** — an on-demand full analysis for THAT attachment
    regardless of the global mode; in flight the row reads a calm
    *"analyzing image…"* and the fresh result polls in without reopening
    the note.
  - The **card footer unifies provenance with the note-level re-run**:
    the "analyzed Jun 11 · xai:grok-4.3" line (the former provenance
    foot — it has exactly one home, this card) next to a steel **re-run
    analysis** button (`POST /notes/{id}/analyze`, 202; a 409 means a
    run is already in flight and reads the same). After posting, the tab
    polls the analysis (~3s, cleared on unmount/tab switch) until
    analyzed_at moves, then swaps the fresh result in.
  - **Gated empty state**: the backend gates analysis on image extracts,
    so when analyzed_at is null and ≥1 image still lacks extracts the
    facts area is absent with the quiet line *"waiting on image analysis
    — facts extract once every source below is in."*, the Sources card
    renders mid-flight (per-stage spinners/pending), and the footer's
    re-run is disabled (*"analysis waits here — runs automatically when
    every source is in."*). Plain not-analyzed (no images outstanding)
    keeps the existing quiet line. With no images at all, an analyzed
    note's card collapses to the note-text row + footer.

  Gating makes the lifecycle-chip sequence **truly one-way** — indexing…
  → reading image(s)… → analyzing… → quiet. `analyzed` suppresses the
  chip ahead of the awaiting-images check (the backend's analyze-anyway
  paths can leave an image without extracts forever), and a note-level
  re-run flips analyzed back to false — the chip resumes at "analyzing…"
  without re-indexing.

Search results and stream taps open the same surface — this *is* the
former "note sheet", upgraded.

**Analysis tab + entity pages** (settled in the Phase 3 three-way review —
**graph-forward** won over a dense dossier and soft cards): the analysis
tab renders facts as **literal property-graph edges grouped by subject
node** (`me.blood_pressure → 128/82 mmHg`,
`appt:patel-follow-up.scheduled_time → Sep 2026 ±`), predicate paths in
monospace; subject headers double as entity navigation. Tapping a fact
cites back to the **highlighted source words**. The **entity page is a
hub**: centered node with kind/alias/domain meta, current facts as
outbound edges, inbound edges from other entities, provisional state
marked. **The page is current-only [decided: declutter]**: each property
shows its live value (a `pending_review` value stays on the page — it needs
the owner), and prior **once-true superseded** values collapse behind a
quiet `N earlier →` disclosure that opens that property's **revision
timeline rail** (each dot a supersession link citing its note) in the
shared `Sheet`. Muting stale values inline only dimmed them while keeping
their full footprint, so a multi-revision entity clogged; the rail is the
same settled paradigm, just relocated off the default view. **Retracted**
facts (machine extraction errors — never true) are excluded from the value
view entirely (audit-only, a later opt-in surface), never shown beside
once-true history. Correction is never a direct edit —
facts route to **review / pin** with tap-again confirms; the pipeline owns
the data. Temper the raw notation toward the lowercase-calm voice during
implementation (the chosen mock's `~provisional`/`.96` chrome reads too
developer-facing — keep paths, soften the meta). The launcher's **Entities
tile** opens a browse list of the graph — the search screen's live filter
input plus kind chips over standard list rows in a card, each row opening
the entity page — pure reuse of settled paradigms, so it shipped without a
variant review.

**Former / past relationships — the interval timeline** (settled in a three-way
review — **variant C** won over an inline "former" chip [A] and a current/
previously section split [B]; reference mock:
`docs/mocks/legacy-links-c-interval-timeline.html`). A relationship/state is
**current** only when it is `active` **and** open (`valid_to IS NULL`); a closed
interval (`valid_to` set) is **former**, even when nothing replaced it (the
two-axis model — `docs/research/legacy-links-handling.md` §3.1). A former edge
stays **visible on the default view** (it is not superseded history to hide
behind the `N earlier →` rail), rendered with a compact **validity track** under
the edge: a `--green` open span to **now** for the current value, a faded/dashed
`--slate` span for a former one — and **bounds the note never gave stay vague**
(an undated "used to" reads `former`/`ended ≤ <capture>` at era precision, never
an invented date). Tapping the row opens that property's **revision rail** in the
shared `Sheet` — the same settled history paradigm — where each dot cites its
note (so source citation lives in the rail, not a separate inline expansion).
Concurrent former values with no stated order are **co-equal** (neither
supersedes the other); the rail lists them without implying a sequence. A closed
relationship has **no derived inverse** (so a former `worksFor → X` never shows
`X employs Me`).

**Review inbox** (resettled in review — the **split inbox** won over the
original one-at-a-time triage: you couldn't move between items, and a
proposal that was only *reject*-able was a dead end): a segmented filter
**pending · deferred · decided** with live count pills splits the screen
into three lanes, and the list is **browsable** — every item in a lane is
listed (kind badge, domain dot, one-line summary, confidence badge,
when), not metered out one card at a time. A **select** toggle turns rows
into checkboxes with a contextual bulk bar (**defer all · approve all**),
and a one-tap **"approve N high-confidence"** suggestion clears the easy
volume; bulk actions resolve through one batch call and raise the same
undo. Tapping a row **pushes a detail** view (back to inbox + **N of M** +
**prev/next** chevrons, so you move between items without returning to the
list). The detail leads with the proposal: a **before→after value diff**
for collisions/conflicts (struck `current` over green `from this note`),
a **proposed-fact panel** (the `predicate → value` edge it would write,
rendered exactly as the entity page) for **every fact-bearing card** — a
low-confidence inference hold, and (beside the before→after diff) a fact
conflict or attribute collision — so it's clear what the decision records, and
that fact is **editable in place** (*correct in place*,
docs/mocks/review-inference-c-correct-in-place.html): the predicate via a
weighted picker (the canonicals nearest the proposed relation, plus free
entry), the value as a free-text chip→input or a member picker for a **typed
(closed-enum) predicate** like `gender → {male, female, unknown}` whose members
ride on the card payload, and the modality (the assertion stance). Deciding
unchanged records the pick (an inference's *approve*, a conflict's chosen
side); an edit flips the primary to *approve correction*, which files a
correction note (the #7 channel — the wiki stays machine-written) instead of
the footer's *correct it* detour (dropped for every editable fact card,
replaced by the inline edit). Or a what-happens panel for the rest;
then a one-line rationale, a
confidence badge, the **cited evidence** snippet (provenance), and the
**proposals to choose among** as stacked buttons (destructive ones —
splits, `distinct_from` — keep the armed tap-again). Two universal escape
hatches sit in the footer — **defer** (park for later) and **talk it
over** (hand to the assistant) — so *reject is never the only way out*:
the ambiguous-mention case that used to advertise only reject now always
offers defer and talk-it-over beside it. Every decision raises an **undo
snackbar** (undo is the server's own unwind — clean for a parked item, a
reopened tombstone for a real decision). Item kinds unchanged: fact
conflicts, attribute collisions, merge proposals, ambiguous mentions,
domain promotions, low-confidence extractions, splits.

**Deferred & decided lanes** (**reopen = full unwind** [decided]): the
**deferred** lane lists parked items (a *defer* or a *talk-it-over*, the
latter tagged **with assistant**); its detail offers **resume**, a clean
re-queue to pending with no tombstone — parking is not a decision. The
**decided** lane is the reverse-chronological log: each row carries **what
was decided in plain language** (the chosen option's own copy), dismissed
rows muted. Its detail shows the cited evidence, the **proposals that were
offered with the chosen one marked**, and an amber **reopen** (armed
tap-again) whose consequence text **names the unwind** per kind. Reopening
returns the item to pending (count pills update) and reverses the
resolution's recorded graph effects; the decided row stays behind as a
**struck-through "reopened" tombstone**. The one permanent exception is a
rejected merge: the `distinct_from` edge survives by doctrine. Empty lanes
read as one calm `--text-2` sentence each.

*Edit model:* "approve with edits" has two shapes, neither of which writes
the graph by hand (honoring non-negotiable #7 — facts aren't edited
directly). **Choose-among-proposals** picks among the values the pipeline
already proposed. **Correct in place** edits the proposed fact's predicate,
value, or modality directly on the card — available on **every fact-bearing
card** (inference holds, fact conflicts, attribute collisions), since each now
carries its structured proposed fact in the payload; an edit files the same
**correction note** rather than a verbatim pick, so the inline editor *is* the
correction channel for these kinds (their footer *correct it* is dropped).
For the kinds that carry no editable fact (merges, ambiguous mentions, domain
moves, …), **correct it** opens a composer that files the human's
fix as a real **correction note** (the #7 channel) in the item's domain and
resolves the item as *corrected*; the pipeline applies it when it processes
that note (**re-adjudicate**, never a hand-written fact), so the wiki stays
machine-written and the value lands once extraction runs — reopening keeps
the note (it's the human's own). The planned third mode, **talk it over
with the assistant**, is the conversational version of the same — the
assistant drafts that correction-note body from your intent; until that
handoff is wired the footer affordance parks the item for the assistant.
Whether a human may ever pin a typed value directly, short-circuiting the
pipeline, stays the open #7 decision.

*Detail composition: the block registry [decided].* The review detail is
**assembled from a sequence of typed, reusable blocks**, not a per-kind
conditional screen — so a new review kind is "declare a block sequence", not
"add a branch". The vocabulary is `header`, `claim:{inference,diff,notice}`,
`trace`, `action`, `evidence`, plus a lane-driven `footer` appended to every
detail. A `kind → block-sequence` table (`frontend/src/review/blocks/registry`)
declares each kind's blocks in a canonical order (e.g. a collision is
`header · trace · claim:diff · action · evidence`; an inference is
`header · claim:inference · trace · action · evidence`); listed blocks
**self-gate** — they render nothing when their payload data is absent — so a
sequence can be generous and reads as the kind's intent. The polymorphic
`action` block carries the per-lane fork (pending controls / decided record /
deferred park) and the per-kind controls (collision choices, inference
approve-reject, new_predicate map/keep/rename); the inference's edit state is
hoisted to the detail so `claim:inference` (the editable proposed-fact panel)
and `action` (the approve button that flips to *approve correction* on an
edit) share it. **The block-to-kind mapping is frontend-only** [decided]: it is
derived from `kind` + payload-field presence, leaving the backend display
contract (`display.py`, which emits card fields, not layout) and its tests
untouched — layout iterates without a wire migration. A future kind that needs
an ordering `kind` can't express may add an optional `payload.blocks` the
frontend prefers; until then the table is the single source. (Rejected:
backend-declared block sequences — couples the Python display contract to a
React layout vocabulary for no present gain.)

**Search** (settled in the Phase 2 review; input mode revised on-device):
**live as-you-type** — results update per keystroke behind a 250ms
debounce, stale responses sequence-guarded, previous results stay visible
while the next query is in flight; enter / the Search button forces an
immediate run. **Passage-first results** — the matched chunk is the hero text with
`--amber-tint` highlight marks, the source note is a one-line context row
beneath; domain-colored dot + date in the head; every result carries its
**match badge** (`semantic` steel-tint / `keyword` surface-2) — retrieval
transparency is a feature, not debug chrome. Domain filter chips under the
search bar. Degraded mode shows the amber "keyword-only results — semantic
search recovering…" banner (never an error page). Tapping a result opens
the **note sheet** — a minimal full-note view (body, attachments, metadata)
as a slide-up layer; swipe down returns to results. The omnibox Research /
Full Brain modes drive agent conversations; passage search lives behind the
Search tile.

**Empty states** — one `--text-2` sentence with the action inline: *"Nothing
scheduled — tap to add."* No illustrations.

**Toasts** — bottom-anchored above the nav, `--surface-2`, auto-dismiss 4s,
single action max.

## Motion

Fast and physical: 120–180ms ease-out for state changes; segment/theme
changes crossfade ≤150ms; no springs, no parallax. Honor
`prefers-reduced-motion: reduce` by disabling all non-essential animation.

## Iconography

One outline set (Lucide), 1.5px stroke, 20px in controls / 24px in tiles and
nav. No filled icons except the status dot. No emoji in UI chrome.

**Entity-type icons** — a cohesive Lucide-style set, one glyph per canonical
entity type (Person, Organization, Place, Event, Product, Animal, CreativeWork,
MedicalCondition, MedicalProcedure, Drug, Thing). Rendered in a round disc
tinted by the type's accent (see "Entity-type accents") on entity rows and the
entity hub header. The glyph carries the *type*; the row's dot still carries the
*domain*.

## Voice & microcopy

Terse, factual, lowercase-calm with em-dashes; say what the system is doing
and what it won't do: *"Ask anything about your notes — I only read; I won't
change anything."* Errors state the situation + the recovery: *"Server
unreachable — retrying…"*. Never blame the user; never exclamation marks.

## Accessibility

- Text contrast ≥ 4.5:1 against its surface in both themes (the muted accents
  are for chrome/tints; body text is always `--text`/`--text-2`).
- Visible focus rings on `:focus-visible`; full keyboard operability on
  desktop layouts.
- Status conveyed by dot color is always paired with text.
- Respect safe-area insets (`env(safe-area-inset-*)`) in top bar and bottom nav.

## UI development process

Binding workflow for every new screen or significant UI change:

1. **Mock-first, approval-gated.** UIs are built and reviewed against mock
   data before any backend wiring. The frontend ships a mock mode
   (`npm run dev:mock`) where the typed API client is backed by fixtures —
   realistic, varied data including empty, long, error, and offline states.
   Backend endpoints are implemented only after the owner approves the
   mocked UI.
2. **Options before commitment.** New surfaces are presented as **3–4
   distinct variants** (layout, interaction pattern, or visual treatment —
   not color-swaps of one idea). The owner picks; the *reasoning and chosen
   pattern* are added to this document in the same PR, so the next surface
   reuses the decision instead of re-litigating it.
   **No reuse exemption [decided]:** every NEW screen or surface gets an
   interactive mockup round before implementation, even when it composes
   entirely from established paradigms — "it's just a list" is not a
   waiver. Paradigm reuse shapes the variants; it does not skip the review.
   Small in-place changes to an existing surface (a chip state, a button on
   an existing card) remain exempt.
3. **Decisions accrete here.** If a review settles anything reusable — a
   list pattern, a modal flow, an empty-state style — it gets a subsection
   in this doc immediately. This document is the memory; "we decided this
   already" must be checkable by reading it.

## The omnibox home (approved Phase 1 review — reference mock: `docs/mocks/phase1-omnibox-approved.html`)

The home screen is a **bottom-docked omnibox** with a day-grouped transcript
stream above it (newest at the bottom). Capture is message-send: instant
local append with an amber "pending sync" chip until the outbox clears.

- **Modes**: one segmented row carries Entry / Research / Full Brain.
  **Tapping Entry while it is active morphs the other two slots into the
  entry sub-types (Medical / Financial); tapping it again morphs back.**
  The row is a full-width bordered rect with hairline dividers; the active
  segment takes its mode tint, colored icon, and bold label.
- **Fixed box height across all modes** (~300px). Medical/Financial show a
  destination row inside the box — mode icon, path (`notes/medical/`),
  destination select, `+ New` — and the text area absorbs the difference.
- **Footer**: mode-colored dot + mode microcopy left ("Saved to your wiki ·
  no AI." / "Read-only — nothing gets written." / "Files to notes/medical/ ·
  PDFs staged."); right icons are paperclip + send (Research swaps the
  paperclip for the bolt). Send button tint follows the mode.
- **Type sizes**: composer body/placeholder 17px (the 22px draft read too
  big), segments 15px/500, footer 14px, destination row 15px.
- Research / Full Brain sends hand off to the (Phase 4) conversation
  surface; in Phase 1 they explain themselves via toast.

## Navigation: the card launcher (no bottom nav)

There is **no bottom tab bar**. Navigation is a full-screen **card
launcher** (the v1 knowledge-hub tile grid: 3-column tiles under uppercase
section headers — KNOWLEDGE, AUTHORING, SYSTEM):

- Opened by tapping the **bolt icon** in the top bar, or by **swiping up on
  the omnibox**.
- Slides up over the home screen; dismissed by the **explicit ✕ button or
  tapping the handle row** (primary paths — gestures proved unreliable on
  real devices and are an enhancement only), swipe down, or Escape. It is a
  navigation surface, not a modal — no scrim-tap dismissal needed, it owns
  the whole screen.
- Every overlay surface must have a visible, tappable exit; a gesture is
  never the only way out (settled in Phase 1 polish).
- **Navigation is a tree, and swiping down climbs it** (settled in Phase 1
  polish): card screen → (swipe down at scroll-top) → launcher → (swipe
  down) → home. Swipe up on the omnibox descends into the launcher. The
  down-swipe on scrollable screens arms only at scroll-top so it never
  fights content scrolling; the top-bar chevron still jumps straight home.
- **Levels are stacked slide-up layers**: card screens animate exactly like
  the launcher — rising from the bottom over the still-open launcher,
  sinking back down to reveal it (150ms ease-out, disabled under reduced
  motion). Each card carries its own top bar (chevron + title); the bolt on
  a card climbs one level, like the down-swipe.
- Tiles for phases not yet built render disabled with their phase label.

### Full Brain lateral shortcuts (Sessions ← chat → Proposals)

In **Full Brain** mode (steel/agent) the conversation is the center of a
three-pane lateral model: **Sessions** to the left, **Proposals** to the right —
the mnemonic is temporal/actional (past sessions left, pending approvals right).
Both are first-class **card-launcher destinations** (tiles, under a SYSTEM/
ASSISTANT group) — that is their canonical, tappable home and the required visible
way in and out. The **Proposals** page is the unified review queue focused on the
agent's staged Proposal trees (see `docs/ASSISTANT.md`); **Sessions** lists past
and active agent sessions with their selected read scope.

As an **enhancement only** (never the sole path — the gesture rule above binds), a
**horizontal swipe on the omnibox / text-entry box** is a shortcut, following the
natural drawer convention — **the panel slides in from its own side to cover the
screen, in the direction your finger moves:**

- **Swipe right → Sessions** (the left panel shuttles in from the **left** edge to
  cover the screen).
- **Swipe left → Proposals** (the right panel shuttles in from the **right** edge).

Rules:

- **No edge chrome on the main screen** — there are no handles, tabs, or peek
  affordances flanking the composer. The conversation surface stays clean; the
  gesture is discovered, and the **card-launcher tiles** (under a SYSTEM/ASSISTANT
  group) are the canonical, always-visible tappable way to both pages.
- The gesture is anchored to the **composer**, not to transcript bubbles, so it
  never competes with message content; the recognizer favors the dominant axis, so
  it never fights the vertical nav-tree gestures (up → launcher, down → climb).
  Horizontal is available precisely because modes switch by *tap*, not swipe.
- Sessions and Proposals open as **standard full-screen cards** (own top bar + back
  chevron; bolt or down-swipe climbs home, satisfying the required visible tappable
  exit). The panel tracks the finger and snaps in past threshold; disabled under
  reduced motion.
- **Full-Brain-only:** Entry/Research composers do not carry these shortcuts (Entry
  keeps its transcript-item action rail).

Reference mocks: `docs/mocks/assistant-lateral-swipe.html` (the gesture, no edge
chrome), `docs/mocks/assistant-sessions-view.html` (the Sessions page + start-
session read-scope picker), `docs/mocks/assistant-proposals-view.html` (the
tree-structured Proposals page with whole/subtree/leaf approval and dependency
holds).

## Surface paradigms (which container for which job)

| Job | Paradigm |
|---|---|
| Primary tasks (capture, reading an article, chat) | Full screen with top-bar back chevron |
| App-wide navigation | Card launcher (bolt tap / swipe up on omnibox) |
| Contextual quick forms & actions (add list item, edit appointment, filters) | **Bottom sheet** — the workhorse modal on phone |
| Confirmation of a destructive/irreversible act | Center **confirm dialog**, destructive variant |
| Row-level detail that doesn't warrant navigation | Inline expansion within the list |
| Outcome feedback (saved, restarted, queued) | Toast |
| Connectivity / sync state | Status banner + dot — **never** a modal |

## Modal system (one implementation, reused everywhere)

- A single shared **`<Sheet>`** (bottom sheet) and a single shared
  **`<Dialog>`** (center confirm) component own all modal behavior: scrim
  (`--bg` at 60% alpha), focus trap, body-scroll lock, Escape/back-gesture
  dismiss, swipe-down dismiss for sheets, safe-area padding, 16px top radius.
  New modals compose these shells — building a bespoke modal is a design-doc
  violation.
- **One modal at a time, never nested.** If a flow seems to need a modal
  over a modal, the first one should have been a full screen.
- Sheets carry a 32×4px drag handle, a 18px/500 title, and at most one
  primary action; longer flows are full screens.
- Dialogs are for confirmation only: one sentence of consequence, two
  buttons max (destructive variant on the right), no scrolling content.

## Agent tool views (registered components, never bespoke markup)

Agent tools render rich UI — lab plots, tables, timelines, appointment cards,
confirm sheets — but **only through a closed registry of first-party
components**, never by emitting HTML, scripts, or markdown URLs (that would be the
exfiltration channel `docs/ASSISTANT.md` invariant I-9 forbids, and would let
model output drive the render). The contract:

- A tool result may carry a **`view`**: a schema-validated, **data-only** payload
  naming a registered component and filling its typed slots
  (`{ view:"lab_plot", series:[…], ref_fact_ids:[…] }`). The PWA looks the name up
  in a fixed component registry and renders the vetted React component; an
  unknown name renders nothing.
- A `surface` hint (`inline | sheet | dialog`) places it: inline in the chat
  transcript, or into the **shared `<Sheet>`/`<Dialog>`** shells. **This is not a
  bespoke modal** — the component is the *content*; the modal-system rules above
  still bind. Adding a component is a deliberate, versioned design+code change
  (extend this document in the same PR), exactly like adding a tool.
- View payloads are **data, not instruction** (I-1) and **render no external
  resources** (I-9); slots are escaped by the component. Data in a view came from
  an RLS-scoped tool call, so domain firewalls hold at the source; views carry
  `fact_id`/`entity_id` refs for citation hover-cards (pointers-not-copies).
- **Interactive views never mutate directly.** A button dispatches a tool call or
  stages a **Proposal** under the session's action policy — the agent proposes,
  the pipeline disposes.
- **One view names one component** (no nested trees/dashboards; multiple views in a
  turn render as sequential inline cards), and components express **`tone`/`flag`/
  `kind` enums, never colors or hex** — the model conveys meaning, the component owns
  the token mapping.

**The registry** (starter set; spec in `docs/archive/research/self-improving-agent/G-tool-
view-components.md`). Three composable primitives hold the count down —
`data_table`, `stat_block`, `citation_card` (the shared pointer-not-copy citation
surface every view reuses). **MVP:** those three + `lab_plot` + the interactives
`record_list`, `appointment_card`, `confirm_panel`. **Standard:** `entity_card`,
`timeline`, `wiki_preview`, `med_card`, `txn_table`. **Refused** (anti-bloat, tied
to invariants): no `form` (input flows through composer/sheets/review inbox), no
`markdown`/`html`/`image`/`iframe` (I-9), no external **map tile ever**, no free
`button`/`link`, no generic `chart` kitchen-sink (purpose-built plots only), no
dashboard/layout components.

**Proxy-tile carve-out for the location domain (L3).** The "no external map tile
ever" rule above was written when location had no on-box basemap; it is
**superseded for the location domain** by the registered Leaflet tool-views
`location_map` (#3) and `place_card` (#4). These are the *sanctioned* Leaflet
tool-views: they render tiles only from the **on-box `/api/tiles` proxy**
(`leafletMap.ts`), not an external host — so the exfiltration/I-9 concern that
motivated the ban (a model-authored URL reaching a third-party host) does not
apply. The invariants still bind: coordinates are **render-only** (lat/lon enter
only the Leaflet layers via the map glue, never model-facing text or a view
caption), a GPS gap is never bridged (the trail splits into separate polylines),
and derived `place_card` stats are owner-gated. The data still comes from an
RLS-scoped, full-owner-gated tool call. No other domain gets a tile view without
its own decision.

## Wiki Talk board (settled in a three-way GUI review — reference mock: `docs/mocks/wiki-talk-b-topics.html`)

The article's editorial board (Phase 6) — the wiki's second surface after the reader. Chosen
**B — threaded topics** over A (single chat thread) and C (claim-anchored annotations): discrete
collapsible topics with **open/resolved** badges (amber/green), signed + timestamped posts in
**three voices** — `You` (owner), `Editor` (the agent, violet signature), `Builder` (the batch
builder) — a **New topic** composer, a per-topic reply box, and an auto **Build log** topic
(`auto · N entries`) the builder posts a one-line decision summary to on every rebuild
("Created/Rebuilt article …; N facts across M domains", "Merged in X"). B won for the durable,
scannable archive it gives across many editorial threads over time; A/C are retained as the record
(`wiki-talk-{a,b,c}`). Owner-only; tokens-only; same shell as the reader (TopBar +
swipe-down-to-close). The wiki stays **machine-written** — Talk is the front-end over the sanctioned
levers (correction note, source exclusion, rebuild). **Wave T1** shipped the board + the Builder voice
+ owner topics/replies; **Wave T2** shipped the live **Editor** (agent) reply — an owner reply draws an
agent turn (`AgentLoop` + the wiki tools, a dedicated Editor system prompt) that explains sourcing and
enacts via the levers, posted as an `editor` post with an outcome chip. Reachable from the reader's
**Discussion** affordance (the quick-fix correction sheet stays beside it until T2 unifies them). DoD
fixtures: empty (Build-log only) / long-thread / pending-action / error / offline.

## Locations surface (the owner's place views — Phase 7)

The location domain's accent is the **`--location` teal (`#6FB6B1`)** (settled in L3);
amber (`--warn`) carries the stale/"last known" tone (matching the GPS-gap marker).
`LocationScreen` is a 3-tab segmented control (Map · Timeline · Devices) on `.seg-row`/
`.seg-on`. Two L7 affordances sit on it, both **names + times only — never a
coordinate** (this is why neither needs a basemap):

- **Inline digest panel (L7a — chosen Option C, reference mock `docs/mocks/location-l7/
  option-c.html`).** A **compute-on-read** place digest renders as a **collapsible
  inline panel ABOVE the Map** inside the Map tab — *not* a fourth tab (Options A/B's
  extra "Digest" segment was rejected). It is a **per-day place-track**: each local
  civil day is a horizontal bar of named place-segments (home teal, other places
  steel, a dashed amber "no signal" gap), with a headline summary (nights home, time
  at a place, longest trip), a compact legend, a first/last-seen line, and an
  owner-only footnote. It **defaults to the WEEKLY period**, with a nightly⇄weekly
  pill toggle (nightly expands a single day's hour-track) and a "computed just now ↻"
  recompute affordance that keeps it honestly compute-on-read (`GET
  /api/locations/digest?period=week|night`, owner + full-owner gated — the digest reads
  WEAK-RLS `app.events`/`place_geofence`, so the endpoint gate is the barrier, not RLS).
  The panel is a regular surface element (not a modal): it follows the inline-expansion
  paradigm, collapsed/expanded by its header.

## Implementation rules

1. Tokens live in one file (`frontend/src/styles/tokens.css`); components
   never hard-code colors, radii, or font sizes.
2. New components follow this document; if a needed pattern is missing, extend
   this document in the same PR that introduces the component.
3. Screenshot-test significant surfaces in both themes once Playwright lands.
4. The mock fixtures are maintained alongside the API client; a screen's
   mock states (default, empty, error, offline) are part of its definition
   of done.
