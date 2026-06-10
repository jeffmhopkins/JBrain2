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

Semantic aliases: `--ok: var(--green)`, `--warn: var(--amber)`,
`--danger: var(--rose)`, `--info: var(--steel)`. The location domain's color
is assigned when Phase 7 lands (candidate: a teal, distinct from the five
above).

**Mode/domain coding rule** (settled in the Phase 1 omnibox review):
green=entry/save, amber=research/read-only, steel=full-brain/agent,
rose=medical, violet=financial. A surface's active segment, status dot,
send button, and section markers all take its mode color — you can *see*
which mode and firewall you're inside.

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

**Calendar** — Day/Week/Month/List segments; month grid with hairline cell
borders, out-of-month days in `--text-3`, today = accent ring around the day
number; selected-day panel below with `+ Add` (accent link) and `Open day →`.

**Home stream** (settled in the Phase 2 home review): home is NOT an
infinite timeline — it shows the **last 2 days** of notes with an
"older notes live in Search" pill above. The stream area is
**mode-scoped**: Entry shows the note stream; Research / Full Brain show
that mode's **conversation cards** (title, last-message preview, time,
mode dot) — tapping one descends the tree into the conversation layer;
typing in those modes always starts a NEW conversation. Until Phase 4
ships conversations, those modes show an empty state ("conversations
arrive in Phase 4"). **Swiping a note bubble left** slides it to reveal an
action rail: **Edit** (loads the note into the omnibox; sending updates it
and re-triggers ingestion), **Delete** (inline tap-again confirm), **Move
domain** (small sheet). Tapping a bubble opens the note sheet.

**Capture location** (settled in the Phase 2 review): a Settings toggle,
**on by default** (browser permission prompt on first launch; denial just
means location-less notes). While on, the app keeps a warm geolocation fix
and attaches lat/lng/accuracy to a note at send **only if the fix is under
2 minutes old** — capture is never blocked or delayed waiting for GPS.
Note-location is owner-eyes metadata: Phase 7 scoped tokens never receive
location fields, regardless of the note's domain.

**Note view** (settled in the Phase 2 review): entry-stream bubbles clamp
at **3 lines**; tapping opens the **note view layer** (slide-up tree level,
swipe-down back) with a **Note / Analysis tab split**:

- *Note tab*: full markdown body, attachment cards with per-attachment
  extraction status from the dispatcher, and the Edit / Move domain /
  Delete actions (the swipe rail's longhand).
- *Analysis tab* (lights up by phase): generated title + 3-6 tags (P3 —
  pre-P3 the header shows only domain + date, **no title fallback**);
  salient facts with kind badges (measurement/state/event/preference),
  status chips (active / pending-review / **pinned**) and confidence;
  entity chips → entity pages; extraction provenance (model, prompt
  version, analyzed-when) with re-run and correct actions (P3); wiki
  backlinks → articles (P6).

Search results and stream taps open the same surface — this *is* the
former "note sheet", upgraded.

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
as a slide-up layer; swipe down returns to results. The omnibox Research
segment remains a Phase 4 surface; search lives behind the Search tile.

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

## Implementation rules

1. Tokens live in one file (`frontend/src/styles/tokens.css`); components
   never hard-code colors, radii, or font sizes.
2. New components follow this document; if a needed pattern is missing, extend
   this document in the same PR that introduces the component.
3. Screenshot-test significant surfaces in both themes once Playwright lands.
4. The mock fixtures are maintained alongside the API client; a screen's
   mock states (default, empty, error, offline) are part of its definition
   of done.
