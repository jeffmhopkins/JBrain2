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

**Chosen:** pending owner review.
