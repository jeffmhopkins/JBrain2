# Wiki reader — mock gate (Phase 6, decision #3)

Three interactive directions for the **read-only wiki article reader**, per the
PROCESS.md GUI gate. Pick one; the chosen mock becomes the binding spec and its
rationale is recorded in `DESIGN.md` when Wave B (UI) implementation starts.

All three render the **same sample article** — a cross-domain person ("Celine
Hopkins") with **domain-tagged sections** (General / Health / Finance), matching the
settled data model (one article, sections are the firewall/RLS unit; an out-of-scope
section is invisible to a scoped viewer). All three honor the binding design system:
self-contained HTML, phone-framed, dark-first with a working theme toggle, tokens-only
colors, outline SVG icons, amber read-only banner, and the "facts aren't edited here —
discuss to correct" rule (machine-written wiki, non-negotiable #7).

| File | Direction | Citations | "Discuss" | Best when |
|---|---|---|---|---|
| `wiki-reader-a-prose-rails.html` | **Prose scroll, domain rails.** One continuous read; sections stacked with a domain-colored left rail + header. | Inline dotted markers (¹²³) → tap opens a bottom citation card (source snippet + provenance). | One article-level action in the bottom bar. | Reading flows like an encyclopedia entry; sections feel like one coherent person. |
| `wiki-reader-b-domain-tabs.html` | **Domain tabs.** A General/Health/Finance tab bar shows one section at a time, with a per-section Sources list. | Inline markers + a numbered Sources list under each tab. | Per-**section** ("Discuss the Health section"). | The firewall is the mental model — each domain is its own view; a scoped guest literally sees fewer tabs. |
| `wiki-reader-c-evidence-cards.html` | **Evidence cards.** Every claim is a card with its **source snippet always visible** (provenance-forward); superseded/historical facts shown dimmed with a "history" badge. | Always-on, inline under each claim — no hidden hovercard. | Per-**claim**. | Trust/provenance is the point; you want to see *why* the wiki says something without tapping. |

## What each tests / trade-offs

- **A** is the most familiar and the least visually busy, but provenance is one tap
  away — good for reading, weaker for at-a-glance trust. Closest reuse of the
  `EntityScreen` full-screen paradigm + `FactCitation` hovercard.
- **B** makes the domain firewall legible and maps cleanly to scoped-viewer behavior
  (drop a tab), but breaks a person into separate views — cross-domain reading needs
  tab-hopping. Reuses the Search screen's chip/badge + sources idiom.
- **C** foregrounds the "every claim cites a note" invariant and naturally shows
  history (superseded facts) inline, but is the densest and least prose-like — more
  fact-sheet than article. Heaviest per-claim affordance (per-claim discuss).

## Decision

Owner picks one (or a hybrid, e.g. "A's prose with C's always-on citation for health").
The choice + reasoning land in `DESIGN.md`; the other two stay here as the record, per
the mock-first discipline. Mock fixtures for default / empty / long-article / error /
offline states are part of Wave B's definition of done.
