# Session panel — lifecycle & operability (Researcher 2)

Scope: creating, naming, finding, resuming, distinguishing, and tidying agent
sessions, plus how the Sessions panel is discovered and navigated. The
domain-scope/least-privilege *defaults* question is owned by Researcher 1; I
touch the New-Session sheet only where it gates lifecycle.

Files studied: `frontend/src/agent/SessionsPanel.tsx`,
`frontend/src/agent/FullBrainSurface.tsx`, `frontend/src/agent/useFullBrain.ts`,
`frontend/src/agent/types.ts`, `frontend/src/screens/HomeScreen.tsx`,
mocks `assistant-sessions-view.html` / `assistant-lateral-swipe.html`,
`docs/DESIGN.md`.

---

## 1. Diagnosis — concrete operability pain points

### A. The cards carry almost no re-find signal
`SessionsPanel.tsx:240-251` renders each row as **title + domain pills only**.
The mock's cards (`assistant-sessions-view.html:96-124`) and the lateral-swipe
mock (`assistant-lateral-swipe.html:130-145`) carry four more signals the code
dropped:

- a **summary line** (`c-sub`: "Surfaced two open labs and the roof quote")
- a **last-active / time** label (`when`: "9:12 AM · 14 turns")
- a **turn count**
- a **live dot** + **staged-proposal badge** ("1 staged").

With only title + pills, two sessions in the same domain ("health · me" twice)
are visually identical. The owner cannot tell *which* health session was the
wiki cleanup vs Dad's meds review without opening each — the core re-find task
fails. `AgentSession` already carries `created_at` and `last_active_at`
(`types.ts:121-122`), so the time signal is free; only summary and turn/proposal
counts need new data.

### B. There is no "you are here" marker in the list
The top bar shows the active session's title (`HomeScreen.tsx:76-82`), but
inside the panel **the active session row looks like every other row**. The
mock distinguishes it with a green border + live dot
(`assistant-sessions-view.html:96`). On reopen you can't see at a glance which
card is the conversation you're currently in vs the ones you'd be switching to.

### C. Creating forces a sources decision before a first word
`SessionsPanel.tsx:64` ("＋ New session — choose sources") opens a sheet
(`:258-322`) whose **only** path to a session is picking domains and tapping
"Start session". There is no "just start talking" path. Every new thought —
even a throwaway general question — pays a modal tax up front. Title is
already optional (`:294`), but the domain gate is mandatory and `disabled` until
≥1 domain is chosen (`:313`). For the owner's most common case (a quick general
question) this is pure friction.

### D. Naming is manual-only; sessions default to "Untitled"
There is no auto-title. A session created without a title shows "Untitled
session" forever (`SessionsPanel.tsx:242`, `HomeScreen.tsx:79`) until the owner
swipes → rename. Industry-standard chat surfaces (ChatGPT, Gemini, Claude)
auto-title from the first turn so the history list is navigable without manual
work. JBrain's lifecycle leaves the list full of "Untitled session" rows — the
worst possible re-find state.

### E. Organizing collapses past two-state
The list is only **Active / Earlier** (`SessionsPanel.tsx:39-40, 68-71`). The
mock had relative-day grouping — "Earlier today / Yesterday"
(`assistant-sessions-view.html:102,114`). There is:

- **no search/filter** — once sessions pile up, the list is an unbounded scroll
  with no way to jump to one;
- **no archive** — only delete (`SessionsPanel.tsx:196-204`), which is
  permanent and drops the transcript (`useFullBrain.ts:202-210`). A session you
  want out of the way but not gone has nowhere to go;
- **no sort/recency cue** beyond insertion order (`useFullBrain.ts:182` prepends
  new, but `rename`/activity never re-sort, so "most recently used" drifts from
  the top).

### F. Resume is silent
Reopen replays the stored transcript (`useFullBrain.ts:142-153`) and scrolls to
the newest turn (`FullBrainSurface.tsx:47-50`) — good. But the **panel itself
gives no "pick up where you left off" cue**: no last-message preview, no
relative time, no turn count. The owner has to open a session to remember what
it was about. The replay is solid; the *decision* to reopen is unsupported.

### G. Discovery / navigation friction
- The primary affordance to reach Sessions is a **right-swipe on the surface**
  (`FullBrainSurface.tsx:79-85`). DESIGN.md (§Full Brain lateral shortcuts,
  lines 512-548) is explicit that the swipe is an **enhancement only** and the
  canonical way in is **card-launcher tiles under a SYSTEM/ASSISTANT group**.
  Those tiles do not appear to exist yet — meaning today the *only* discoverable
  path is the top-bar title tap (`HomeScreen.tsx:80`) and an undiscoverable
  swipe. The mock even prints the hint "swipe right → chat"
  (`assistant-sessions-view.html:89`); the code's panel-bar shows
  "read-scope chosen at start" (`SessionsPanel.tsx:61`) — useful for scope, but
  it does not teach the gesture.
- **Back affordance is a bare "‹" glyph** (`SessionsPanel.tsx:57-59`), not the
  Lucide chevron the mock and DESIGN.md §Top bar use. It is a 1-char text node;
  hit area and visual weight are below the ≥44px / icon-set standard.
- The swipe rail (`SessionsPanel.tsx:178-215`) reuses the notes rail
  (RAIL_WIDTH 192, `swipe.ts:7`) but only fills it with **two** 96px buttons
  (`styles.css` `.session-rail .rail-btn { width: 96px }`) — rename + delete.
  The notes rail is three buttons. It works, but there's an empty-feeling gap
  and no room for the missing **archive** action.

### H. Empty / first-run states are thin
Empty list: one line (`SessionsPanel.tsx:75`). The surface empty state ("Choose
a session to start asking", `FullBrainSurface.tsx:119`) plus the hook's
auto-open of the panel when there's no active session (`useFullBrain.ts:110`)
means a first-time owner lands on a near-blank panel whose only action is the
sources-gated New Session button — a cold start straight into the modal tax of
pain point C.

---

## 2. Concrete suggestions (prioritized, buildable)

### P0 — highest leverage, mostly front-end

**1. Restore the rich card (re-find + resume in one move).** Bring back the
mock's `c-sub` summary, `when` time, turn count, live dot, and staged badge
(`assistant-sessions-view.html:96-118`).
- *Buildable now from existing data:* relative `last_active_at`
  (`types.ts:122`) as the `when` label, and an active-session green dot/border
  keyed off `status === "active"` (already split at `SessionsPanel.tsx:39`).
- *Needs a thin backend add:* `turn_count`, `last_message_preview` (or a stored
  one-line `summary`), and `staged_proposal_count` on `AgentSession`. Preview is
  the single highest-value field — derive it server-side from the last
  transcript turn so the panel never loads transcripts to render the list.
- *DESIGN.md fit:* this is the **Home stream "conversation card"** pattern
  verbatim — "title, last-message preview, time, mode dot" (DESIGN.md line 196).
  Reuse, not a new paradigm. Comfortable-density card, hairline border, pills
  stay as the scope encoding (color codes the domain, Principle 4).

**2. Mark the active session in the list.** Green hairline border + live dot on
the row whose id === `fb.active.id`, matching `assistant-sessions-view.html:96`
and the `--green` "live/healthy" token (DESIGN.md Accents). Resolves pain B and
the "am I in a session or starting fresh?" question with zero new data.

**3. Auto-title from the first turn.** When a session is created without a
title, set its title from the first user message (server-side, on first turn —
either first-N-words or a cheap LLM gen-title through the LLM adapter per
non-negotiable #1). Keep manual rename as override (the swipe-rail rename
already exists, `SessionsPanel.tsx:184-192`). This is the ChatGPT/Gemini/Claude
standard and the single biggest cure for an "Untitled session" list.
- *Caveat from research:* first-turn titles drift as conversations evolve. Cheap
  mitigation: allow a later silent re-title if the title is still
  auto-generated and the topic has clearly moved (optional P2).

### P1 — flow & organizing

**4. Add a "Quick start" path that defers the sources choice.** Keep the
sources sheet for deliberate scoped sessions, but make the **primary** New
Session action start a session immediately at the least-privilege default
(general, matching `SessionsPanel.tsx:266`) and drop the owner straight into the
composer; surface the scope as an editable chip in the new session's top bar so
widening is one tap away (this is where Researcher 1's defaults work plugs in).
Rationale: the modal tax (pain C) is paid on *every* new thought today; most are
quick general questions. DESIGN.md voice/microcopy supports the calm one-tap
("Ask anything — I only read"). The sheet becomes the "choose sources…"
secondary, not the gate.

**5. Auto-sort by recency.** On `rename`, `open`, and after each turn, move the
touched session to the top (sort by `last_active_at`, `useFullBrain.ts` already
owns the array). The most-recently-used session should always be the first card
under Active. Cheap, pure front-end, and matches every chat-history list.

**6. Relative-day grouping under "Earlier".** Replace the flat "Earlier" section
(`SessionsPanel.tsx:71`) with "Earlier today / Yesterday / This week / Older"
buckets from `last_active_at`, exactly as the mock groups
(`assistant-sessions-view.html:102,114`). DESIGN.md §section headers (12px
uppercase `--text-3`) already styles these — the `.sect` class is in place
(`styles.css`). Pure presentation.

**7. Search/filter when the list grows.** A live as-you-type filter input at the
top of `panel-body` (reuse the Search screen's 250ms-debounce live-filter
paradigm, DESIGN.md §Search and the Entities-tile reuse note at line 348),
filtering on title + summary. Show it only past ~8 sessions so it stays out of
the way early. Reuse, no new paradigm.

**8. Archive vs delete.** Add a third rail action **archive** (a non-destructive
status flip, e.g. `status = "archived"`, hidden from the default list, revealed
by a quiet "N archived" pill — mirroring the Home stream's hide/undo doctrine,
DESIGN.md lines 211-222, and *not* a permanent delete). Keep **delete** as the
rose tap-again it already is (`SessionsPanel.tsx:196-213`, matches DESIGN.md
destructive-confirm). The 192px rail (`swipe.ts:7`) has room for three 64px
buttons (the notes rail's own layout) — this also fills the empty rail gap from
pain G.

### P2 — discovery, navigation, polish

**9. Ship the SYSTEM/ASSISTANT launcher tiles.** DESIGN.md (lines 519-521,
536-537) *requires* Sessions and Proposals to be card-launcher destinations as
their canonical tappable home; the swipe is enhancement-only. Today the gesture
appears to be the de-facto primary. Add the tiles so discovery doesn't depend on
an unhinted swipe (pain G). This is a binding-doc compliance gap, not just a
nicety.

**10. Teach the gesture once.** A one-time, dismissible hint on the Full Brain
surface ("swipe → for sessions") — the lateral-swipe mock already has a hint
toast component (`assistant-lateral-swipe.html:75-77,119`). Cheap discoverability
without permanent edge chrome (DESIGN.md forbids edge chrome, line 534).

**11. Replace the "‹" with the Lucide chevron + 44px hit area.** Swap the bare
glyph (`SessionsPanel.tsx:57-59`) for the same `ChevronGlyph` used elsewhere
(`FullBrainSurface.tsx:471`) in a ≥44px button. DESIGN.md §Iconography (one
Lucide set, no stray glyphs) and §touch targets.

**12. Warmer empty / first-run state.** When there are no sessions, lead with the
quick-start action inline ("No sessions yet — ask anything to start one.") per
DESIGN.md §Empty states (one `--text-2` sentence, action inline), so the cold
start (pain H) lands on a one-tap path, not the sources modal.

**13. Make the swipe-rail rename more discoverable.** Rename is currently buried
behind a left-swipe (`SessionsPanel.tsx:184`). Given auto-titling (suggestion 3)
makes most titles decent, this is lower priority — but consider a tap-title-to-
rename affordance in the active session's top bar as the obvious path, leaving
the rail rename for the list.

---

## 3. What good looks like

Opening Sessions should feel like opening a messaging app's chat list: the
session I last used sits at the top with a live dot, a human title auto-written
from my first question, the last thing we said, and how long ago — so I know
which one to resume without opening it, and I can grab a new thought in one tap
without a setup form. Sessions I'm done with archive out of the way (recoverable,
never a guess), the rare dead one deletes with a deliberate tap-again, and when
the list grows a live filter finds any of them instantly. The panel teaches its
own gesture once and is always reachable from a launcher tile — discovery never
hinges on a swipe nobody mentioned.

---

### Sources (light web research)
- [ChatGPT/Codex auto-title strategies (first-words vs summary-based)](https://github.com/openai/codex/issues/13990)
- [The forgotten conversation problem in AI chat — UX Collective](https://uxdesign.cc/the-forgotten-conversation-problem-in-ai-chat-4d3d0c3ea525)
- [Editable session names with auto-generated titles — opencode](https://github.com/anomalyco/opencode/issues/8436)
- [Chat app design best practices (conversation list, search) — CometChat](https://www.cometchat.com/blog/chat-app-design-best-practices)
