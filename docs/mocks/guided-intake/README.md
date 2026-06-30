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

## Owner management screen (round 2)

The owner-side **"Intake Links"** card-launcher destination — where live links are managed.
Self-contained, real tokens, 56px destination top bar, `tap-again-to-confirm` revoke, drill-in
to a link's submissions, and a mint sheet. Each is clickable (tap a row → detail; tap "New
intake link" → the stage sheet; copy/revoke fire toasts).

| Variant | File | Direction |
|---|---|---|
| **A — List-first** | `manage-a-list.html` | One clean list of link cards (Active / Closed); per-row status badge + counters + an "awaiting review" flag. Closest to the existing jcode-launcher / credential lists. |
| **B — Grouped by state** | `manage-b-grouped.html` | Sectioned **Needs review → Active → Closed**; the top section surfaces links with submissions waiting in the review inbox, each with a prominent "Review →" jump. Action-forward. |
| **C — Dashboard** | `manage-c-dashboard.html` | A summary strip (Active · Awaiting review · Opens today) + a review CTA banner, over rows with **runs/opens progress bars**. Most at-a-glance. |

Two faithful details baked into all three: **(1)** "+ New intake link" **stages a Proposal**
(creation routes through the agent → Proposal → approval), it does not mint directly; **(2)**
**Copy link is always available** with no one-time warning, reflecting the
re-copyable-encrypted-at-rest decision (the one documented divergence from show-once).

## Editable intake-link Proposal (round 3)

The full-screen **editable Proposal** the intake tool generates — the owner edits the agent
prompt + all link settings, then approves to mint. It lives in the **Proposals page** shell
(56px `panel-bar`, scrolling `panel-body`, footer action bar) but is a **first-class
departure**: every other Proposal kind is a read-only "judge the preview" surface, whereas
this one is editable owner-config (no firewall/truth implication). Footer action is
**Approve & mint** (not "Enact"). No modal — the whole thing is the surface. Each is
clickable (edit fields, toggles, segmented pickers, ± steppers; Reject is tap-again).

| Variant | File | Direction |
|---|---|---|
| **A — Form** | `proposal-a-form.html` | One scrolling form of stacked `settings-card`s (Agent prompt / Blurb / Who & where / Limits / Recipient). Most direct, settings-screen feel. |
| **B — Edit + Preview** | `proposal-b-preview.html` | The same form with an **Edit ⇄ Preview** tab — Preview renders the live recipient view (chosen surface B) from the current blurb/disclosure so you see the effect of edits before minting. |
| **C — Proposal-tree** | `proposal-c-tree.html` | Rendered as a real **Proposal tree** (root + collapsible editable nodes: Persona / Interview / Limits / Access, each with an op badge), most native to the Proposals page's existing tree rendering. |

All three reflect: creation = agent-generated Proposal; editable before approval; the
"nothing secret in the prompt" caveat; binding/runs/opens/TTL/disclosure as editable knobs.

## Decisions these mocks already encode (from the design exploration)

- Welcome captures the recipient's **name** (the untrusted `enterer_name`) + an explicit
  **consent** line.
- "Fix something" on the draft **sends the recipient back to the chat** (the agent always
  re-authors the summary — no inline text editing).
- The summary shows **per-claim leaves** the owner will later approve individually; the
  recipient only confirms accuracy here.
- Done state makes clear nothing lands until the **owner reviews** it.
