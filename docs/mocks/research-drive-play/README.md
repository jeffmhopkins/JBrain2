# Research card — drive-friendly play button (GUI gate)

The `deep_research_report` tool-view (`frontend/src/agent/views/registry.tsx`, `.tv-dr-*`)
reads the report aloud from a 26px icon beside copy/download. The daily-news brief is meant
to be played while driving, so that target is too small to hit at a glance.

Open `compare.html`: three variants in phone frames mirroring the News chat, each keeping
the rest of the card unchanged and making the play controls tappable in the mock.

| Variant | Treatment | Build cost |
|---|---|---|
| **A — Play bar** | Full-width 56px amber bar under the question; doubles as the progress bar while speaking. | Play/pause only — no new plumbing. |
| **B — Hero disc** | 72px amber disc leading the card, title beside it, progress + time beneath. | Play/pause only — no new plumbing. |
| **C — Drive mode player** | 52px "Play brief" button that opens an in-card player: 88px pause, 60px previous/next-section, current section heading, progress. | Needs the TTS split per report section for skip/back. |

**Round 1:** the owner liked C, but its back/skip circles were hard to tell apart at a
glance. `compare-c.html` tries three ways to make *skip story* clearly different from pause:

| Variant | Treatment |
|---|---|
| **C1 — Labelled story buttons** | Big round pause on its own row; two full-width rectangles under it that name where they go ("Previous · …" / "Restart · …", "Next · …"). |
| **C2 — Chapter bar** | Progress split into one segment per story (tap a segment to jump); skip buttons are big steel-blue squares labelled "Prev story" / "Next story", as tall as pause. |
| **C3 — Story list** | The player is the list of stories: the current story expands into an amber row with its own pause button, finished ones get a ✓, and a full-width "Next story" button sits at the bottom. |

**Chosen: C2 — Chapter bar** (owner, 2026-10-12). Built as `frontend/src/agent/ReportPlayer.tsx`
(`.rp-*` styles), on both the deep-research card and the Research Library report; the spec is
folded into `docs/reference/DESIGN.md` §"`deep_research_report` tool-view". Both HTML files stay
as the record of the review.
