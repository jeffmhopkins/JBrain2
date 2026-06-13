# Sessions Panel — IA, Mental Models & Comparable Patterns

Researcher 3 of 3 (the "zoom out and compare to the wider world" lens).
Focus: information architecture, mental models, interaction patterns.

Owner's complaint (verbatim intent): *"Make the session panel more friendly /
more intuitively usable. Domains being up front and so obvious is too much."*

---

## 1. Mental-model diagnosis — "capability, not identity"

`docs/ASSISTANT.md` (~215–248) frames a session as a **capability**: two
least-privilege dials — a **read-scope** chosen at start (sets the RLS GUC) and
**writes always staged as Proposals**. This is the crux: a session is *not* a
named thing you build and return to (that's the "identity" model — ChatGPT
Custom GPTs, Claude Projects). It's *the bounded context you happen to be
talking through right now*. That's genuinely novel, and the current IA both
helps and fights it.

**Where the current IA helps the model:**
- The **two-panel symmetry is conceptually right**. Sessions (left) = the
  *capability* you're operating under; Proposals (right) = the *consequences*
  staged for approval. Inputs-vs-outputs / power-vs-accountability is a sound
  spine, and the lateral-swipe mock makes the chat the centre of gravity.
- "Reads only… any change is **staged as a Proposal**" (sheet footer + `writes-note`)
  is the single clearest sentence in the whole flow. It teaches the writes half
  of the capability honestly.
- Least-privilege default (`new Set(["general"])`) and "widening is an explicit
  act" are encoded in code, not just prose. Good.

**Where the current IA *fights* the model (the owner's actual complaint):**
- **The gate-before-start exposes plumbing.** `NewSessionSheet` forces a
  domain-picker (and the mock adds a subject-picker) *before the first message*.
  That makes the **firewall** — an implementation truth — the **first thing the
  owner thinks about**, every single time. The capability model says scope is an
  *upper bound*, a safety rail; the UI presents it as a *mandatory configuration
  step*. Rails should be felt, not configured.
- **"Session" reads as identity, not capability.** The word, plus persistent
  named/renameable rows with "Active / Earlier" history, makes each one feel
  like a *project you return to* — exactly the identity model the doc rejects.
  The rename/delete swipe-rail reinforces "these are durable objects I curate."
- **Jargon leaks the architecture.** "read-scope chosen at start", "this sets
  the session's firewall", "reads X · Y · Z", domain code pills (`general`,
  `health`, `finance`, `location`) — these are *RLS GUC concepts in a trench
  coat*. `DOMAIN_LABEL` in `modes.ts` already proves the owner-facing names are
  "Medical/Financial," yet the panel shows raw `health`/`finance`. The pills are
  the single loudest "too much" element the owner is reacting to.
- **Domain ≠ subject conflation.** The live code only has domains; the mock adds
  subjects ("Me / Dad / Mom"). Two orthogonal axes presented as one flat wall of
  pills is the densest, least-legible part of the design.

**One-line diagnosis:** the panel teaches *what a session can read* but not *why
you'd ever care* — it surfaces the firewall as a setup chore instead of letting
scope be ambient and adjustable. That's the gap between "capability" (a dial you
nudge when needed) and what's built (an identity you must configure to create).

---

## 2. Comparable patterns (real products)

Six patterns, each with a one-line "how it'd apply to JBrain2."

1. **iOS/Android per-app permissions — "Allow Once / While Using" (ephemeral,
   contextual grant).** The OS *never* makes you pre-configure a permission
   matrix; it asks **in context, at the moment of need**, with a least-privilege
   default and an easy widen. NN/g's guidance: request permission *just-in-time
   with rationale*, never up-front as a gate. ([NN/g](https://www.nngroup.com/articles/permission-requests/))
   → *JBrain2: don't gate session creation on a domain picker. Start in the
   least-privilege default and ask to widen only when the agent actually needs a
   locked domain ("This needs your Medical notes — allow for this chat?").*

2. **Perplexity Spaces — a 3-way scope toggle ("Web / Space files / Web+Space").**
   Scope is **one compact, legible control with named options**, not a checkbox
   matrix; the rest of the Space (instructions, files) is configured once and
   fades. ([Perplexity Spaces](https://alternativeto.net/news/2024/12/introducing-perplexity-spaces-customize-your-web-sources-))
   → *JBrain2: collapse the domain grid into a single scope control with a few
   named presets ("Everyday / Everything / Medical only…") rather than four raw
   domain checkboxes the owner must reason about à la carte.*

3. **Firefox Multi-Account Containers — colour/icon as the *ambient* identity.**
   You don't read a config panel to know which container you're in; a **colour
   cue on the chrome** tells you at a glance, and isolation is invisible until it
   matters. ([Mozilla MAC](https://github.com/mozilla/multi-account-containers/))
   → *JBrain2 already has domain colours (DESIGN.md: rose/violet/steel). Make
   scope an **ambient chip in the top bar** (the lateral-swipe mock already shows
   "Full Brain · all domains"), not a wall of pills on every row. You should
   *see* your firewall, not *read* it.*

4. **Notion AI / Agent — implicit scope from context, adjustable via `@`.**
   "By default, Agent takes the context of the page you're on"; narrowing is an
   *optional* `@`-mention, not a precondition. Scope defaults from where you
   already are. ([Notion Agent](https://www.notion.com/help/notion-agent),
   [Enterprise Search](https://www.notion.com/help/enterprise-search))
   → *JBrain2: derive the starting scope from the omnibox mode the owner came
   from (modes.ts already maps Medical→health, Financial→finance). If they enter
   Full Brain from a Medical context, default the read-scope there — no picker.*

5. **Progressive disclosure with sensible defaults.** Research shows defaults +
   deferred advanced options yield 30–50% faster task start while keeping
   features discoverable; advanced settings live behind a toggle, *not* in the
   spotlight. ([NN/g via IxDF](https://ixdf.org/literature/topics/progressive-disclosure),
   [UXPin](https://www.uxpin.com/studio/blog/what-is-progressive-disclosure/))
   → *JBrain2: the New-Session sheet should be **one button + an "Adjust sources"
   disclosure**. 90% of sessions start on the default and never open it; the
   firewall controls exist for the day they're needed.*

6. **ChatGPT/Claude Projects — durable identity is the *opposite* end of the
   spectrum.** Projects are heavyweight, named, persistent workspaces you
   *return to* and curate. They're great for recurring identity-shaped work and
   *wrong* for "a bounded conversation I have once." ([ChatGPT Projects vs Custom GPTs](https://www.adventuresincre.com/chatgpt-projects-vs-custom-gpts/),
   [Claude Projects](https://alternativeto.net/news/2024/6/claude-launches-projects-feature-with-structured-sets-of-knowledge-for-pro-and-team-users))
   → *JBrain2: this is the trap to avoid. The current named/renameable/curated
   rows pull toward the Projects identity model — exactly what ASSISTANT.md says
   a session is **not**. Lighten the rows so they read as conversation history,
   not project files.*

---

## 3. IA recommendations (prioritized, buildable within DESIGN.md)

**P0 — Move scope out of the gate; make it ambient + adjustable.**
This is the direct answer to "domains up front is too much."
- New Session = **one tap → straight into chat** on the default scope (last-used
  or `general`). Replace the mandatory picker with a quiet **"Sources: Everyday
  ▸"** affordance under the composer/top bar that opens the picker *on demand*.
- Surface the live scope as a **single ambient chip in the top bar** (the
  lateral-swipe mock's `scopechip` already does this), coloured by domain per
  DESIGN.md §"Mode/domain coding rule." One chip, not a pill row per session.
- When the agent needs a domain outside current scope, **ask in-line, in
  context** (pattern #1) instead of relying on the owner to have pre-granted it.

**P1 — Reduce pill noise; speak owner language.**
- On session rows, **drop the per-row domain-code pill wall** by default. Show at
  most one summarizing chip ("Everyday", "Medical", "All sources") using
  `DOMAIN_LABEL`/`DOMAIN_TITLE` names — never raw `health`/`finance` codes.
- Replace the New-Session domain *grid* with a small set of **named presets** +
  an "Advanced / choose individual sources" disclosure (patterns #2, #5). The
  four-checkbox matrix becomes the advanced layer, not the front door.
- Kill the jargon strings: "read-scope chosen at start" → "Sources"; "this sets
  the session's firewall" → drop entirely (the writes-note already conveys
  safety); "reads X · Y" → "Using your Everyday notes."

**P2 — Naming / metaphor.**
- "**Sessions**" leans identity. Consider "**Chats**" or "**Conversations**" for
  the *list/history* (matches the lightweight, ephemeral mental model and what
  ChatGPT/Claude trained everyone to expect), while keeping "session = capability"
  as the *internal/architecture* term. The owner shouldn't meet the word
  "session" at all; they're just having chats that happen to be scoped.
- If a single noun is wanted that carries the scope idea, "**Spaces**" (Perplexity)
  is the closest familiar metaphor — but it re-imports the heavyweight/identity
  baggage, so prefer "Chats" + an ambient scope chip.

**P2 — Make the Sessions↔Proposals relationship explicit.**
- The two panels are a coherent **capability (left) / consequences (right)** pair,
  but nothing on screen says so. Mirror their bars: Sessions sub = "what this
  chat can read," Proposals sub = "what it wants to change." Consider a **count
  badge** ("Proposals · 2") on the right edge/chip so output pressure is visible
  from the chat, closing the loop the mock hints at ("1 Proposal → swipe left").
- Keep the symmetric swipe (right→capability, left→consequences); it's a strong,
  learnable spatial model. Ensure both edges show a **persistent peeking
  affordance** (the mock relies on a hidden gesture; DESIGN.md §"Honest status"
  argues against hiding navigation behind an undiscoverable swipe).

**Where scope belongs in the flow (synthesis):** **ambient-and-adjustable**, with
an **implicit default from context**, and **just-in-time widening** — *not*
gate-before-start. That's the unanimous lesson from iOS permissions, Notion,
Perplexity, and progressive-disclosure research.

---

## 4. What good looks like

The owner starts a chat the way they'd start any chat — one tap, no setup — and
just *sees*, in a quiet coloured chip, what this chat can touch ("Everyday").
When the conversation reaches for something locked (Dad's meds, finances), the
agent asks once, in context, and the chip updates — scope is a rail you feel, not
a form you fill. The left panel is "what this chat can see," the right is "what it
wants to change," and the chat between them never makes the owner think the word
"firewall."
