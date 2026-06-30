# Guided Intake — recipient-surface mocks

Interactive mock artifacts for the **recipient-facing** surface of the guided-intake
share-link feature (the public flow a stranger walks: **welcome → chat → draft-confirm
→ done**). Per `docs/PROCESS.md`'s GUI gate, three clickable variants are presented for
the owner to choose before implementation; the chosen one becomes the binding spec.

These are the **recipient surface only** — the owner-side management screen and the
editable intake-link Proposal are separate mock rounds.

> **Chosen: B — Guided stepper** (`intake-b-stepper.html`) is the binding recipient-surface
> spec. It now carries the `owner: generic / named` mock-bar toggle demonstrating both
> `disclose_owner_identity` treatments. A and C are retained for the record.

All three are self-contained single files (no external loads — invariant #9), use the
real frontend tokens from `frontend/src/styles.css`, are mobile-first with a visible
tappable exit on every screen, respect `prefers-reduced-motion`, and toggle light/dark
(the mock-only bar at top). Each is fully clickable end to end (the interview is a short
canned script; Send advances even with the field empty).

| Variant | File | Direction | Feel |
|---|---|---|---|
| **A — Conversational** | `intake-a-conversational.html` | Full-bleed messaging app; the draft-confirm renders **inline in the chat stream** as a card. | Lightest, most casual. Like texting an assistant. |
| **B — Guided stepper ✅ chosen** | `intake-b-stepper.html` | A persistent **Welcome → Interview → Review → Done** progress header; the draft is its **own Review screen**. Carries the generic/named owner-disclosure toggle. | Most reassuring / legible for non-technical relatives. "I know where I am." |
| **C — Portal card** | `intake-c-portal.html` | A calm contained **card on a tinted backdrop** with an **owner-identity band** (demonstrates `disclose_owner_identity = true`); chat and review live inside the card. | Most formal / "official hosted form." Trust-forward. |

## What to compare

- **Chrome vs. calm:** A is chrome-light and immediate; C is the most "designed/official";
  B sits between with explicit progress.
- **Where the draft-confirm lives:** inline in the stream (A) vs. a dedicated screen (B, C).
  Dedicated screens read as a clearer decision point; inline keeps momentum.
- **Owner disclosure:** C shows the **named** treatment (`disclose_owner_identity = true`);
  A and B show the **generic** treatment (default). The chosen variant should handle both,
  but they differ in how prominent that band is.

## Decisions these mocks already encode (from the design exploration)

- Welcome captures the recipient's **name** (the untrusted `enterer_name`) + an explicit
  **consent** line.
- "Fix something" on the draft **sends the recipient back to the chat** (the agent always
  re-authors the summary — no inline text editing).
- The summary shows **per-claim leaves** the owner will later approve individually; the
  recipient only confirms accuracy here.
- Done state makes clear nothing lands until the **owner reviews** it.
