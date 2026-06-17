# Wiki Talk board — mock gate (Phase 6, §4)

Three interactive directions for the **editorial discussion board** — the persistent
"Talk page" where the owner converses with the agent about an article (PROCESS.md GUI
gate; the wiki's *second* new surface after the reader). Pick one; the chosen mock
becomes the binding spec.

All three honor the model: the wiki stays **machine-written** — Talk is a conversational
front-end over the sanctioned levers (correction note, source exclusion, rebuild,
split/merge proposal). **Two voices** appear: the batch **builder** posting
decision summaries, and the interactive **agent** (the Phase-4 chat agent with
wiki-editorial tools) that explains sourcing and enacts outcomes. Owner-only; the agent's
reads are firewalled. Same design system as the reader mocks (phone-framed, dark-first +
theme toggle, tokens-only, outline icons).

| File | Direction | Shape | Best when |
|---|---|---|---|
| `wiki-talk-a-chat-thread.html` | **Chat thread.** One conversation with the editor; builder posts as bot cards, owner/agent as bubbles, green/amber **action chips** inline when a turn becomes a correction/exclusion/rebuild. Composer at the bottom. | Linear messaging (reuses the Phase-4 chat paradigm). | Quick back-and-forth feels like texting the editor; lowest friction. |
| `wiki-talk-b-topics.html` | **Threaded topics** (true Wikipedia Talk). Discrete collapsible topics with status badges (open/resolved), signed + timestamped replies, a "New topic" button, and an auto **Build log** topic. | Structured, archival. | Many separate editorial threads over time; you want a durable, scannable record. |
| `wiki-talk-c-anchored.html` | **Anchored annotations.** The (condensed) article itself; discussion is attached to a specific **claim** — tap a line to open its thread in a sheet; flagged claims show a marker. | Doc-comments style. | The conversation is usually "about *this* claim" — provenance + discussion co-located. |

## Trade-offs

- **A** is the most natural for a single ongoing dialogue and reuses the existing chat
  surface almost wholesale, but long editorial histories become a hard-to-scan scroll.
- **B** mirrors real Wikipedia Talk best (topics + build log archive) and scales to many
  threads, but it's heavier and a step removed from the article text.
- **C** ties discussion tightest to *what* is being discussed (the claim) and doubles as a
  provenance view, but a free-floating "general" discussion fits it less cleanly and it
  partly overlaps the reader surface.

## Decision

**Chosen: B — threaded topics** (`wiki-talk-b-topics.html`): true Wikipedia Talk —
collapsible topics with open/resolved badges, signed + timestamped replies, a "New topic"
action, and an auto **Build-log** topic for the builder's per-build decision posts. A/C
retained as the record. The choice + rationale land in `DESIGN.md` when the Talk UI is
built. DoD includes fixtures for empty (no discussion yet) / long-thread / pending-action /
error / offline states.
