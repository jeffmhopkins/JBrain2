# Session Panel UX — Domain / Read-Scope Selector

Researcher 1. Focus: the domain/read-scope selector in the Full Brain "Sessions"
panel, and the tension between the least-privilege security model and owner
ergonomics.

> Owner's complaint (verbatim intent): *"I feel like domains being up front and
> so obvious is too much. Is the default to being all domains unless I restrict
> it explicitly beyond that? It's still just a little bit clunky."*

The owner wants domains to **recede** — to stop gating every new session — without
us throwing away the containment story that the read scope exists to provide.

---

## 1. Diagnosis — what's clunky in the current picker

The current flow is a **mandatory, multi-select, up-front gate**. To get to a
conversation you must open a sheet, read a paragraph about firewalls, and make a
set-selection decision before a single word is typed.

Specific pain points (file:line):

- **The new-session button is a chore, not an invitation.**
  `SessionsPanel.tsx:64-66` — the only way to start is
  `＋ New session — choose sources`. The call to action *is* the domain task. You
  cannot "just start talking"; choosing sources is the price of entry.

- **The default is `general`-only, not all-domains.**
  `SessionsPanel.tsx:266` — `useState<Set<string>>(new Set(["general"]))`. So the
  answer to the owner's literal question ("is the default all domains unless I
  restrict it?") is **no — it's the opposite**. The default is the *narrowest*
  scope, and every other domain is an explicit widening act. This is the root of
  the "clunky" feeling: the owner experiences a restriction they didn't ask for
  and must repeatedly undo.

- **A four-row multi-select for a binary-feeling decision.**
  `SessionsPanel.tsx:296-309` — four full `.opt` cards (label + description) each
  needing an individual toggle. For an owner who "mostly wants everything," this
  is four taps to express *one* intent ("all of it"). There is no "everything"
  affordance and no "last time" affordance.

- **Security-jargon microcopy leads the sheet.**
  `SessionsPanel.tsx:285-288` — *"Choose which knowledge sources this session may
  read. Narrow by default — widen only when you need to. This sets the session's
  firewall."* This is the *implementation's* worldview ("firewall", "narrow by
  default") pushed onto the owner at the exact moment they want to think about
  their **question**, not their threat model. DESIGN.md voice is
  "lowercase-calm"; "this sets the session's firewall" is developer-facing.

- **The "reads nothing" dead-end.**
  `SessionsPanel.tsx:313-316` — the Start button disables at zero domains and
  reads *"reads nothing."* It is possible to toggle yourself into an invalid,
  un-startable state. A default-narrow model that can reach *empty* is a model
  that makes the owner babysit a set.

- **Scope on the card is shown but not editable.**
  `SessionsPanel.tsx:243-250` — pills render the scope, but there is no path to
  change scope after creation. A session's scope is frozen at the one moment the
  owner had the least context (before talking). The swipe rail offers only
  rename/delete (`SessionsPanel.tsx:181-214`) — to re-scope you must start over.
  This makes the up-front choice feel high-stakes, which makes it feel heavy.

- **The mock's Subject dial isn't implemented.** The mock
  (`assistant-sessions-view.html:134-139`) has a Subject row (Me / Dad / Mom);
  `types.ts:128` carries `subject_ids` and the panel never sets it. Worth a note:
  *adding* a second mandatory dial on top of domains would compound the clunk.
  Subject should follow whatever pattern we settle for domain (recede by default).

**Net:** the panel treats the security model's *resting state* (narrowest scope)
as the *owner's resting state*. For the principal who holds all scopes, that's a
mismatch — the friction lands on the one user the firewall isn't primarily
protecting against.

---

## 2. The default question — recommendation + honest trade-off

### Recommendation: **default to all-domains-for-the-owner**, shown as a single,
glanceable, one-tap-tightenable scope chip — not a gate.

The owner's read is correct and ASSISTANT.md explicitly licenses it:

- ASSISTANT.md:228 allows *"a deliberate minimal/**last-used** set"* as the
  default — last-used is sanctioned, and "minimal" is one option among others,
  not a mandate.
- ASSISTANT.md:244-247 names the owner as **the special case**: *"the owner
  session is just the general case where the read dial is selectable."* The owner
  *holds all scopes already*. Defaulting the owner's session to all-domains grants
  **nothing they don't already have** — it is not a privilege escalation, it's
  choosing a sensible resting point within rights they hold.

So the binding constraint is narrower than it first looks. The doctrine that
must survive is the one in ASSISTANT.md:228: **"widening scope is an explicit
owner act, never the resting state."** Read precisely, that protects *non-owner*
principals (intake links, scoped tokens — ASSISTANT.md:242, pinned dials) from
*silently* widening. It does **not** require that the *owner's* comfortable
default be the floor. We can honor "widening is explicit" by making the
**owner's default an explicit, remembered choice** rather than a silent
all-access grant — see the safeguards below.

### The honest security trade-off

All-domains-by-default **does** enlarge the blast radius of a prompt-injection in
the average owner session. The containment rationale (ASSISTANT.md:222-231) is:
an injected instruction in a note can't exfiltrate across a firewall the session
was never scoped to. A `general`-only session physically cannot read a health
fact, so injected content in it can't reach one. Default-narrow means the
*typical* session is *already* contained; default-wide means the typical session
is the wide one, and containment becomes an opt-in the owner must remember.

This is a real cost. I'm not hand-waving it. But three things keep it acceptable:

1. **RLS is the boundary, not the picker.** ASSISTANT.md:203-206 is explicit:
   visibility (which tools/scopes a session sees) is *convenience*; **RLS at the
   DB layer is the security boundary**, and the write path is independently
   gated. The picker's scope sets the GUC upper bound; widening the *default*
   doesn't weaken RLS itself, and **writes are still staged Proposals regardless
   of read scope** (ASSISTANT.md:236-240). The worst an injection in a wide owner
   session can do is *read* across domains and try to *propose* an egress —
   and egress is itself a staged Proposal whose exact payload the owner approves
   (ASSISTANT.md:267-269). The exfiltration chokepoint does not move.

2. **Containment stays one tap away and *visible*.** The recommendation is not
   "wide and hidden." It's "wide by default, with an **always-visible scope chip**
   the owner can tighten in one tap, on the session card and in the composer
   header." Glanceable scope (DESIGN.md principle 5, "honest status, always
   visible") is the trade for the up-front gate. The owner can drop a sensitive
   session to `health`-only before asking the sensitive question — which is the
   *actual* moment scope matters, and the moment they now have the most context.

3. **The dangerous principals are unaffected.** Non-owner sessions
   (ASSISTANT.md:242) have **pinned** dials — their scope is fixed by capability
   token, not chosen in this UI at all. Defaulting the *owner's selectable* dial
   to wide changes nothing for them. The firewall that matters most (the one
   facing intake links and scoped tokens) is untouched.

### Safeguards that keep "widening is explicit" true

- **Remember, don't silently re-grant.** Default = the owner's *last-used* scope,
  seeded to all-domains on first run. After the owner ever narrows, that becomes
  the remembered default. "All domains" is then a choice the owner made and can
  see, not an invisible standing grant — satisfying ASSISTANT.md:228 in spirit.
- **Sensitive domains can be sticky-narrow.** Optionally, treat `health` /
  `finance` / `location` as **auto-dropping**: a new session inherits them from
  last-use, but if the owner ever explicitly excludes one, re-including it is the
  explicit act. `general` is always in. (Phase-in; not required for v1.)
- **The chip names the wide state plainly.** "reads everything" is honest copy,
  not a checkmark buried in a list. Wide is *legible*, so it's never silent.

**If the owner rejects even default-wide:** the strong fallback is
**last-used, seeded narrow** (`general` on first run) — still removes the gate and
the four-tap chore, still answers "domains recede," and keeps the conservative
floor. But it does *not* answer the owner's literal ask ("all domains unless I
restrict"). Default-wide-for-owner does, and the doctrine permits it. I recommend
default-wide.

---

## 3. Concrete suggestions (prioritized, buildable)

Each respects DESIGN.md tokens, phone-first one-thumb, and the existing `.fb-shell`
class scope (styles.css:3096+). Reuses existing classes where noted.

### P0 — Fast-path session creation: "just start talking"

**Make the new-session row start a session immediately at the default scope; move
source-picking to an optional, secondary affordance.**

- Replace `＋ New session — choose sources` (`SessionsPanel.tsx:64-66`) with
  **`＋ New session`** that calls `onCreate({ domain_scopes: <default> })`
  directly and opens the chat. No sheet, no gate. (`general`/all per §2.)
- Add a smaller secondary control on the same row — a trailing **`choose
  sources`** ghost link / scope chip — that opens the existing sheet for the
  owner who *wants* to scope deliberately. Two intents, one row: the big target
  is "start," the small one is "scope first."
- Rationale: this is the iOS "Allow While Using App" default — powerful, frictionless,
  with the tighten path present but not mandatory. DESIGN.md principle 1
  (one-thumb, bottom-half primary action) and the voice rule both favor a verb
  ("start") over a chore ("choose sources"). 44px targets preserved.

### P1 — Scope becomes an ambient, editable chip, not a frozen gate

**Show scope as a single chip everywhere it's relevant, and make tapping it the
re-scope path — before *and* after the session starts.**

- **On the session card** (`SessionsPanel.tsx:243-250`): keep the domain pills,
  but when scope == all-domains render **one** `all domains` pill (the code
  already has this fallback at line 249 — make it the *common* case, styled
  steel-tint like `.pill.general`). Lots of pills = a narrowed, deliberate
  session; one "all" pill = the relaxed default. The card glanceably tells you
  which kind of session it is.
- **Make the card's scope tappable** to open a re-scope sheet (reuse
  `NewSessionSheet`'s domain grid as a shared `<ScopeSheet>`). Re-scoping an
  *active* session can only **narrow within the original upper bound** (you can't
  silently widen past what RLS was set to — widening starts a fresh session or is
  an explicit re-grant). This removes the "frozen at the worst moment" problem
  (Diagnosis) and lowers the stakes of the initial choice, which is what makes
  the initial choice feel heavy.
- **In the composer header**, surface the current session's scope as the same
  chip (DESIGN.md:512-521 — the session name already lives in the top bar; the
  scope chip rides beside it). Tap to tighten. This is DESIGN.md principle 5
  (honest status, always visible) applied to the firewall: you can always *see*
  what the session can read, and narrow it the instant a sensitive question
  comes up.
- Rationale: this is the iOS "Allow Once" insight — the moment the read actually
  matters is when you're about to do the sensitive thing, not at app launch.
  Ambient + editable scope moves the decision to where the context is.

### P2 — Collapse the picker to an "everything ↔ pick" progressive disclosure

When the owner *does* open the scope sheet, lead with the common case:

- Top of sheet: a single primary toggle / segmented control —
  **`everything` | `choose…`** — defaulting to `everything`. Picking `everything`
  is one tap and you're done. `choose…` reveals the existing four `.opt` rows
  (`SessionsPanel.tsx:296-309`) for deliberate scoping. Progressive disclosure
  (the Wikipedia/Nielsen pattern from research): the advanced, rarely-used
  multi-select is deferred behind the common case.
- Add an **`everything` / `clear`** affordance so "all of it" and "none of it"
  are each one tap, never four. Kills the "reads nothing" dead-end
  (`SessionsPanel.tsx:313-316`) — `everything` is always reachable in one tap.
- Rewrite the lead copy (`SessionsPanel.tsx:285-288`) to lowercase-calm,
  question-first voice: *"this session reads everything by default — tap a
  domain to narrow it."* Drop "firewall"/"narrow by default" from the owner-facing
  surface; keep the security framing in a small `writes-note`-style footnote
  (the existing `writes-note` at lines 318-320 is already the right register).

### P3 — Seed the default from last-used; remember narrowing

- Replace `new Set(["general"])` (`SessionsPanel.tsx:266`) with a value derived
  from the owner's last session's scope (fall back to all-domains on first run
  per §2, or `general` if the owner picks the conservative fallback). Persist
  device-local first (like theme/text-size, DESIGN.md:34-37), server-synced later
  (like image-analysis mode, DESIGN.md:232-239) so it follows across devices.
- Rationale: ASSISTANT.md:228 explicitly blesses "last-used." It makes the
  *typical* repeated workflow (the owner who always wants the same two domains)
  zero-friction, while a deliberate narrowing *sticks* — which is exactly the
  "widening is an explicit act" guarantee, now expressed as memory rather than as
  a forced re-choice every time.

### P4 — Subject dial follows the same recede-by-default rule

- When the Subject dial (mock `assistant-sessions-view.html:134-139`,
  `types.ts:128`) lands, default it to **Me** (or last-used) and present it as the
  same single chip, *not* a second mandatory row. Two stacked mandatory dials
  would re-introduce exactly the clunk we're removing. Subject is a *narrowing*
  refinement reachable from the same `choose…` disclosure, never an entry gate.

---

## 4. What good looks like

The owner taps **`＋ New session`** and is talking in one tap — the session reads
their whole brain by default, and a quiet steel **`reads everything`** chip sits
in the header so they can always *see* it and, the moment a question turns
sensitive, tap it to drop to `health`-only before they ask. Scope is **ambient
and reversible**, not a wall you climb before every conversation: the picker only
appears when the owner deliberately reaches for it, and even then it leads with
"everything" and reveals the four-domain detail only on request. Domains recede
to a glanceable, one-tap-tightenable status indicator — present and honest, never
a gate — while RLS, staged writes, and the egress chokepoint keep the containment
guarantees doing their real work at the database layer where they belong.

---

Sources (research): iOS "Allow Once / While Using App" + purpose-string pattern
([Apple Support — Location Services](https://support.apple.com/en-us/102515),
[Jamf — iOS app permissions](https://www.jamf.com/blog/ios-app-permissions/));
progressive disclosure
([Wikipedia](https://en.wikipedia.org/wiki/Progressive_disclosure)); permission
timing/strategy
([Dogtown Media](https://www.dogtownmedia.com/the-ask-when-and-how-to-request-mobile-app-permissions-camera-location-contacts/));
project-scoping session-management UX pain
([Claude Projects context scoping](https://memu.pro/blog/claude-ai-projects-memory)).
