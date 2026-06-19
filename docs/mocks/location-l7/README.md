# Location L7 — GUI gate mocks (digest + presence chip)

Three interactive directions for the **two small GUI surfaces** Wave L7 adds,
mocked **together** in each option per the `docs/PROCESS.md` GUI gate. Pick one
direction; the chosen mock becomes the binding spec for L7a + L7b and its
rationale is recorded in `DESIGN.md` in the implementing PR.

> **CHOSEN (owner decision, GUI gate):** **Option C — week timeline track +
> corner presence toast**, with the digest **defaulting to the weekly period**
> (the nightly⇄weekly toggle stays). L7a renders the digest as an inline
> collapsible per-day place-track panel above the Map tab; L7b shows presence as
> a self-dismissing corner toast on app/chat open. `option-c.html` is the binding
> spec. The teal `--location` accent + amber stale tone carry over; record the
> inline-digest-panel + presence-toast patterns in `DESIGN.md` in the build PR.

- **L7a — Nightly/weekly digest** (#29): a **compute-on-read** rollup of recent
  place activity, rendered as a **pull artifact** in the Locations surface
  (`GET /api/locations/digest`, owner-only). Place **names + times only — no
  coordinates**. Not a notification feed: a refreshable panel.
- **L7b — App-open presence chip** (#31): a compact chip shown when the app/chat
  opens, reading the **owner's own** latest place. **Freshness-honest** — a stale
  fix reads *"last known"*, never *"here now"*.

Each file is **self-contained and opens standalone** (double-click → browser).
No build, no network: Lucide icons are inlined SVG; there is no map tile / CDN
dependency (the digest and chip carry no coordinates, so no basemap is needed).
All three are **tokens-only**, dark-first with a working theme toggle, and use
the **`--location` teal accent (`#6FB6B1`)** the owner chose for the location
domain in L3.

## How to drive each mock

| Mock | Presence-tone toggle (fresh ⇄ stale) | Nightly/weekly toggle | Notes |
|---|---|---|---|
| `option-a.html` | top-right **↻** in the top bar | "This week / Last night" pills above the brief | banner ✕ dismisses; ↻ also reopens it |
| `option-b.html` | top-right **clock** icon | "This week / Last night" pills above the grid | use the two view tabs to swap App-open ⇄ Digest |
| `option-c.html` | top-right **bell** (replays toast, alternating tone) | "This week / Last night" pills inside the panel | tap the panel header to collapse/expand |

## Comparison

| | **A — Narrative brief** | **B — Stat-grid dashboard** | **C — Week timeline track** |
|---|---|---|---|
| **Digest layout** | Written **paragraph** brief + small supporting stat strip + first/last-seen list | 2-column grid of **glanceable stat cards** + first/last-seen table | Per-day **horizontal place-track** (Home/Office/out bars) + headline + legend |
| **Digest home** | Its own **Digest tab** (Map · Timeline · Devices · Digest) | Its own **Digest tab** | **Inline collapsible panel atop the Map tab** (no extra tab) |
| **Chip placement** | Full-width **top banner** under the top bar | Inline **pill** prepended to the home transcript | **Corner toast** above the nav (auto-dismiss) |
| **Chip dismissal** | ✕ button (returns next app-open) | auto-clears on scroll/interaction; tap → Locations | auto-dismiss ~4s; single "open" action |
| **Freshness / tone** | Banner switches teal "currently at" ⇄ amber "last known" (loudest) | Pill switches teal ⇄ amber (lightest) | Toast switches teal ⇄ amber, amber tints the whole toast |
| **Nightly ⇄ weekly** | pill toggle swaps the paragraph + strip | pill toggle swaps the card grid | pill toggle collapses the week into a single day's hour-track |
| **Key tradeoff** | Reads instantly as a sentence, but least scannable for a single number | Most scannable numbers, but no narrative / story | Only one that shows the **shape** of the week, but densest to read |

## Trade-offs in prose

- **A (Narrative brief + top banner)** answers "how was my week" in plain
  sentences and makes presence **impossible to miss** — a full-width banner, with
  the amber "last known" variant unmistakable. Cost: a paragraph is the worst for
  "just give me the number" (mitigated by the stat strip), and the banner eats
  top space on every app-open.
- **B (Stat grid + inline pill)** is the most **scannable** — every metric is its
  own card you glance and leave — and the presence pill is the **lightest** touch,
  sitting in the natural home-transcript reading flow rather than claiming chrome.
  Cost: a grid carries no story, and an inline pill is easier to overlook (fine —
  presence is a low-stakes glance, not an alert).
- **C (Week timeline + corner toast)** is the only one that shows the **shape** of
  the week — you *see* which nights were home and how far Saturday's trip ran — and
  the corner toast is **self-clearing**, leaving no chrome behind (it matches the
  existing toast paradigm). Cost: the track is the densest to read and leans on a
  legend; the toast auto-dismisses, so a missed glance is gone (re-openable from
  the Map's live caption).

## Invariants honored by all three (so the chosen mock is buildable)

- **Names + times only — no coordinates** anywhere in the digest or the chip.
  (This is why the mocks need no basemap/tiles at all.)
- **Freshness-honest** — a stale fix renders as *"last known: <place>, Nh ago"*,
  never *"here now"*. Each mock's tone toggle demonstrates both states.
- **Owner-only** — every digest carries an "owner-only · computed on read"
  footnote; the chip renders the owner's own latest place only. (Backend: the
  `GET /api/locations/digest` full-owner guard; the chip line is the data-framed,
  owner-gated `UserMessage` prepend, absent for a narrowed session.)
- **Compute-on-read** — each digest is a refreshable panel with a recompute /
  "computed just now" affordance, **not** a stored artifact or push feed.

## DESIGN.md interpretation made

- **Where the digest lives.** DESIGN.md has no "Locations surface" subsection
  yet; `LocationScreen.tsx` ships a 3-tab segmented control (Map · Timeline ·
  Devices) styled with `.seg-row`/`.seg-on`. Options A & B add a **fourth
  segment ("Digest")**, reusing that exact control; Option C instead places the
  digest as an **inline collapsible panel atop the Map tab** (no new tab). The
  owner's pick settles which, and DESIGN.md gains a "Locations surface" note in
  the implementing PR.
- **Presence chip is a new chrome element.** DESIGN.md's surface-paradigm table
  doesn't list an app-open presence affordance. A is a **status-banner-like**
  strip (closest existing paradigm; DESIGN.md reserves banners for connectivity,
  so the implementing PR should note this as a *distinct, dismissible* presence
  banner, not a connectivity banner); B reuses the **transcript pill** idiom
  ("older notes live in Search"); C reuses the **toast** paradigm verbatim
  (bottom-anchored above the nav, auto-dismiss, single action). C is therefore
  the most paradigm-faithful for the chip.
- **Teal accent.** All three use the L3-settled `--location` teal (`#6FB6B1`) and
  its 13% tint for the location domain's active segment, headline, and chip;
  amber (`--warn`) carries the stale/"last known" tone, matching the
  warning/pending semantics already used for the location trail's GPS-gap marker.
