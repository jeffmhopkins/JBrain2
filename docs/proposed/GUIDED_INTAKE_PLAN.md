# Guided Intake Share Links — design spec (proposed)

> **Status: icebox / not scheduled.** A forward-looking spec kept for the record. It
> realizes the **Phase 7** roadmap line "guided-intake share links" (`../ROADMAP.md`) and the
> non-owner-principal design already sketched in `../ASSISTANT.md` (an intake link is "a
> session whose read scope is fixed to its capability token (one subject × one domain) and
> whose policy is capture-only / everything-else-denied"). Nothing here is built. When picked
> up, reconcile with the `CLAUDE.md` non-negotiables (see §10) and give it a roadmap slot.

## 1. What it is

The owner mints a **share link** that gives whoever holds it a **chat interface to an AI
interviewer** prompted to collect a specific set of information. When the interviewer judges
it has enough, it drafts a structured summary; the recipient confirms it's accurate; and the
captured submission surfaces to the **owner** as an editable, approvable item that — once
approved — becomes attributed notes in the owner's knowledge base.

Two surfaces, both composed from existing machinery:

- **Owner side** — an agent tool that *generates* an editable Proposal to mint a link, plus a
  card-launcher **management screen** ("Intake Links") for live links and their conversations.
- **Recipient side** — a public, link-scoped PWA running the agent loop as a **non-owner
  principal** (empty read scope, capture-only), exactly the principal model in `../ASSISTANT.md`.

The marquee fit: ~80% of this already exists. The link auth is the jcode share-link
machinery; the approval gate is the Proposal primitive; the interviewer is the agent loop with
a closed persona; the submission→notes path is normal ingestion; the surfaces are card-launcher
destinations and a public share app (like `JcodeShareApp`).

## 2. Decision ledger (settled in design exploration)

| # | Decision |
|---|---|
| 1 | **Link creation = agent stages a Proposal.** The `make_intake_link` tool never mints directly; it stages an **intake-link Proposal**; on owner approval the secret is minted. |
| 2 | **That Proposal is editable before approval** — unlike correction/wiki Proposals (read-only previews). It stages owner-config, not machine-authored truth, so editing has no firewall/truth implication. |
| 3 | **Opening blurb is agent-drafted**, owner-edited in the Proposal. |
| 4 | **Submission gate = an owner Proposal.** The recipient's confirmed submission is captured stranger-side and **materialized as an owner-facing Proposal** in the review inbox (never auto-staged by the stranger — honors #10). |
| 5 | **Approval granularity = summary note (provenance-only, no-extract) + per-claim leaves (fact-bearing).** Avoids double-extraction: the narrative is the record, the claims are the fact sources. |
| 6 | **Binding = per-link toggle**: *bind-on-first-browser* (one person) **or** *open* (multi-person). |
| 7 | **Run accounting = burns at submission**, plus a separate, higher **opens** ceiling (burns at redeem). Link dies when either caps, or on TTL / revoke. |
| 8 | **"Done" decision = model-judged** (natural interview), de-risked by the owner gate; turn-cap + TTL are the hard backstops. |
| 9 | **Attribution = subject pinned per link** (who the data is *about*) + a captured, untrusted **enterer name** (who typed it). |
| 10 | **Recipient flow**: structured welcome (blurb + name + consent) → chat → draft-confirm (**"fix" sends back to chat**, no inline editing) → done. |
| 11 | **Persona = `intake`** — a fourth closed, code-defined agent: empty read scope, no KB/web/memory tools, capture-only; its prompt is assembled per-link from the brief. |
| 12 | **Routing = the standard agent-turn task profile** (no special intake model). |
| 13 | **Management screen = grouped-by-state** (Needs review → Active → Closed); "+ New" routes through the agent/Proposal, not a modal. |
| 14 | **Re-copy = re-copyable, secret encrypted at rest** — the one **documented divergence** from JBrain2's show-once-only-a-hash invariant, mitigated by the keystore encryption-at-rest control (§9). |
| 15 | **Full conversation history is owner-viewable**, read-only, in the intake feature's own surface — **never folded next to the owner's own chats** (intake sessions are non-owner principals). Both a browse list (per link) and a per-submission deep-link. Abandoned/in-progress sessions are visible too, tagged by status. Transcript is a separate viewable artifact, not stuffed into the provenance note. |

## 3. Lifecycle

```
OWNER       agent interviews you → make_intake_link stages ┌─ intake-link Proposal ─┐
                                                           │ EDITABLE: prompt + cfg │  ← edit, approve
                                                           └────────────────────────┘
                 secret minted (encrypted at rest, shown/copyable) → sent out-of-band
                         │
RECIPIENT   redeem (binds per toggle; opens_used++) → intake chat (scoped persona)
                 model-judged "enough" → draft → recipient confirms accuracy
                         │  (capture-only write; runs_used++)
                 intake_submission: status = submitted   (+ full transcript retained)
                         │
OWNER       review inbox materializes ┌─ intake-submission Proposal ──┐
                                      │ summary-note + per-claim leaves│  ← approve whole/part
                                      └────────────────────────────────┘
                 approved leaves → attributed notes → normal ingestion → facts
```

Two Proposals bracket every link, both owner-side; the stranger only ever does a capture-only
write. The recipient's confirmation and the owner's approval are two distinct gates.

## 4. The agent tool (`make_intake_link.tool`)

Owner-only, `sensitive` permission. The agent fills the brief by interviewing the owner, then
**stages** (never mints). Frontmatter sketch (mirrors `propose_correction.tool`):

```yaml
name: make_intake_link
version: 1
permission: sensitive
params:
  subject:                 # who the data is ABOUT (pinned)
  domain:                  # general | health | finance | location
  brief:                   # what to collect / how to interview (guides the model-judged interview)
  opening_blurb:           # agent-drafted welcome; owner edits in the Proposal
  ttl_hours:               # default 24
  max_runs:                # completed submissions allowed (burns at submit)
  max_opens:               # total opens ceiling (default = 4 × max_runs)
  bind_on_first:           # true = one person; false = open/many
  capture_enterer_name:    # default true
  disclose_owner_identity: # default false (generic vs. named welcome)
required: [subject, domain, brief, max_runs, bind_on_first]
```

All fields are **proposed defaults** — the owner edits any of them in the editable Proposal
before approving, so the tool's job is a good first draft, not a commitment.

## 5. Security spine

The recipient is an untrusted stranger typing into the owner's agent, so the firewall is
structural, not a matter of model judgment:

- **Empty read scope + tiny allowlist (#8).** The `intake` persona holds **no** KB / web /
  memory tools and runs scopeless; RLS makes every note/entity/fact physically unreadable. A
  prompt-injected "show me Jeff's records" hits a tool that isn't in the registry. **Injection
  is low-harm because the bound is the sandbox, not the prompt** — a "successful" injection has
  nothing to read and nothing to call; worst case the agent says something off-script *to the
  stranger*, exposing no owner data. (Same posture as `jerv`.)
- **Data/instruction boundary (#1).** The owner's brief is trusted instruction; every recipient
  turn is wrapped as **data, never instruction**. Residual: the brief lives in the prompt, so a
  determined visitor could extract its text — **briefs carry interview goals, never secrets**
  (documented, accepted).
- **Capture-only, no auto-stage (#7/#10).** The submission is untrusted-origin content: it
  lands as a capture-only write, **never auto-stages a Proposal and never triggers a background
  job**. The owner Proposal is materialized in the owner's own turn (a push *notification* that
  "an intake arrived" is a metadata signal, not processing). Approved claims become
  provenance-flagged, **normal-weight**, source-attributed notes, surfaced as a distinct review
  item.
- **Domain firewall (#3).** A link is one subject × one domain; the submission, its notes, and
  its transcript carry that `domain_id` and are RLS-scoped. Every new table ships an RLS
  isolation test.
- **Controlled egress / no external loads (#9).** The recipient surface is self-contained; the
  draft is a data-only `view`, never model-authored HTML/links — no render-time external fetch.
- **Cost (the real abuse surface).** A stranger drives LLM calls on the owner's dime. Bounded
  by the per-session harness guardrails (`max_steps` / `max_cost` / `wall_clock` / turn cap) ×
  `max_opens` × TTL × revoke.
- **Purge is total (#11).** Deleting a link/submission cascades to its transcript and any
  derived episodic trace; a test asserts no orphaned content survives.

## 6. Data model (sketch)

```
intake_links        ( principal_id FK→principals,  -- the jcode-share-shaped capability token
                      subject_id, domain_code,
                      persona_brief, fields_brief, opening_blurb,
                      max_runs, runs_used, max_opens, opens_used,
                      bind_on_first, capture_enterer_name, disclose_owner_identity,
                      secret_encrypted,             -- §9: re-copyable, keystore-encrypted at rest
                      status )                       -- owner-RLS; + isolation test
intake_submissions  ( link_id FK, enterer_name (untrusted), transcript jsonb (full history),
                      draft jsonb, status,           -- drafting | submitted | proposed | landed | rejected
                      proposal_id FK?, note_ids[] )   -- the link's own session may write its row only
```

`principals` (TTL, revoke, redeemed/used markers) carries auth; `intake_links` carries config;
`intake_submissions` carries output + the **full retained transcript** (the source for the
read-only conversation view, §8). Management is a near-copy of `list_jcode_shares` /
`revoke_jcode_share`.

**Redeem branches on `bind_on_first`:** bound → first browser claims a scoped cookie, a second
browser 401s (jcode model); open → the secret is multi-redeemable up to `max_opens`, each
redeem minting a fresh ephemeral session. Config is **snapshotted onto each session at open**,
so live edits to a link affect only sessions opened afterward (never a recipient mid-interview
or a submission already queued).

## 7. The two Proposals

- **Mint-time (intake-link Proposal).** `kind = intake-link`, single owner node whose preview is
  the **editable config form** (agent prompt + settings). The editable-preview affordance is new
  and scoped to this kind only; every other kind stays read-only (the anti-fatigue control in
  `../ASSISTANT.md` still binds for machine-authored effects). Approve → mint secret from the
  edited config.
- **Submit-time (intake-submission Proposal).** A **tree**: root "Intake from <link> about
  <subject>", a **summary-note leaf** (provenance-only, flagged no-extract) + one **per-claim
  leaf** (each → an atomic attributed note). Approvable in whole or in part, reusing the tree's
  existing partial-approval machinery. Materialized owner-side from the captured submission.

## 8. GUI surfaces (mock-first gate — chosen variants binding)

Three mock rounds ran per `../PROCESS.md`'s GUI gate; chosen artifacts in
`../mocks/guided-intake/` (with A/C rivals retained):

- **Recipient surface → `intake-b-stepper.html`** (B, Guided stepper): a persistent
  Welcome → Interview → Review → Done progress header, the draft on its own Review screen,
  "fix" returns to chat. Carries the generic/named owner-disclosure treatment.
- **Editable intake-link Proposal → `proposal-b-preview.html`** (B, Edit ⇄ Preview): the editable
  form plus a live recipient preview of the edits, so the owner approves a *shown effect*.
- **Owner management screen → `manage-b-grouped.html`** (B, Grouped by state): Needs review →
  Active → Closed; "+ New" routes through the agent/Proposal (no modal); each submission opens a
  **read-only conversation view** — the full verbatim transcript + the confirmed draft — kept in
  the intake feature, **never beside the owner's own chats**; abandoned sessions visible, tagged.

The recipient surface is a public share app (like `JcodeShareApp`): redeem-and-strip the secret,
scoped cookie, no nav, owner routes 403. All mocks use the real frontend tokens, are mobile-first
with a visible exit, dual-theme, and respect reduced-motion.

## 9. The one invariant divergence (re-copyable secret)

Every other JBrain2 credential is shown once and stored only as a hash. The owner wanted intake
links **re-copyable anytime** (re-send without re-minting). That requires the secret recoverable
at rest, which the show-once invariant exists to prevent. Resolution: **store the secret
encrypted** with the box's keystore / owner-derived key (the encryption-at-rest control in
`../OPERATIONS.md`); `copy` decrypts on demand. So re-copy works, but no plaintext bearer secret
sits in a `pg_dump`, backup, or the debug SQL console. This is the **single deliberate,
documented divergence** from show-once and must be called out at implementation review.

## 10. Reconciliation with `CLAUDE.md` non-negotiables

| Rule | How it's met |
|---|---|
| LLM adapter only | The interviewer runs the existing agent loop over the adapter; LLM faked in tests. |
| Storage abstraction | Transcripts/drafts as rows + blobs via storage, never raw paths. |
| RLS + per-table isolation test | `intake_links`, `intake_submissions` each `domain_id` + isolation test; non-owner principal proven unable to read other domains. |
| Tests land with code; security paths 100% | Auth/redeem/capture/scope paths at 100%; real Postgres via testcontainers. |
| Conventional commits; branch + PR; CI green | Standard. |
| Wiki machine-written only | Untouched — intake produces *notes*, never wiki edits. |
| `dev-setup.sh` currency | Update in the same PR if any new dep (goal: zero new deps). |

## 11. Net-new footprint

Two tables (`intake_links`, `intake_submissions` + RLS tests), one `.tool` sidecar
(`make_intake_link`), one closed `intake` persona, one public `IntakeShareApp` surface, one
card-launcher destination (+ the read-only conversation view), the editable-Proposal affordance,
and the encrypted-secret column. **Reuses:** capability-token machinery, the Proposal primitive,
the agent loop + `/chat` SSE, the review inbox, notes→facts ingestion, the card launcher. **Goal:
zero new runtime dependencies.**

## 12. Open items (resolve at pickup)

- **Concurrency cap shape** — "cap total opens too" is settled; the exact `max_opens` default
  (4 × `max_runs`?) and any per-link live-session cap are tuning values.
- **Enterer-name reconciliation** — the untrusted self-declared name is shown for context; whether
  the owner can reconcile it to a known subject at approval is a later nicety.
- **Notification policy** — push on new submission vs. quiet badge; respects "untrusted content
  never triggers a background job" (the notify is a metadata signal only).
- **Transcript retention window** — kept until link/submission deletion; whether an explicit
  retention/auto-purge policy is wanted is open.
- **A wave-by-wave build plan** (per `../PROCESS.md`) is written at promotion, not here.
