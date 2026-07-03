# Guided Intake Share Links — build plan (v2)

> **Status:** Shipped 2026-07 · migrations 0107-0113, PR #700

> **Status: scheduled build plan** (promoted from the icebox spec), **revised after three
> independent adversarial reviews** (security / architecture-fit / plan-completeness — see §13).
> Realizes the **Phase 7** roadmap line "guided-intake share links" (`ROADMAP.md`). The GUI mock
> gate (`PROCESS.md`) is **cleared** — chosen mocks in `mocks/guided-intake/` are binding.
> Decomposed into six waves; one PR per wave; W1–W3 are **security-critical (red-team gated)**.
>
> **The reviews corrected the original's biggest error:** this feature is NOT ~80% reuse. The
> token-record shape and the Proposal tree engine are real reuses, but the load-bearing spine —
> **running the agent loop under a non-owner principal, with a capture-only least-privilege
> layer** — is **net-new Phase-7 security infrastructure that does not exist today** (see §2.5).
> Wave scoping and risk accounting below reflect that.

## 1. What it is

The owner mints a **share link** that gives whoever holds it a **chat interface to an AI
interviewer** prompted to collect a specific set of information. When the interviewer judges it
has enough, it drafts a structured summary; the recipient confirms it's accurate; and the
captured submission surfaces to the **owner** as an editable, approvable item that — once
approved — becomes attributed notes in the owner's knowledge base.

Two surfaces:

- **Owner side** — an agent tool that *generates* an editable Proposal to mint a link, plus a
  card-launcher **management screen** ("Intake Links") for live links and their conversations.
- **Recipient side** — a public, link-scoped PWA running the agent loop as a **non-owner
  principal** (empty read scope, capture-only). This is the principal model `ASSISTANT.md`
  *designs* for Phase 7; **the codebase has not built it** (§2.5) — this plan builds it.

## 2. Decision ledger (settled in design exploration + review)

| # | Decision |
|---|---|
| 1 | **Link creation = agent stages a Proposal.** `make_intake_link` never mints directly; it stages an **intake-link Proposal**; on owner approval the secret is minted. |
| 2 | **That Proposal is editable before approval** — a deliberate, net-new extension to the Proposal primitive (every other kind is a read-only preview). Scoped to this kind; see §7 for the constraints the review required. |
| 3 | **Opening blurb is agent-drafted**, owner-edited in the Proposal. |
| 4 | **Submission gate = an owner Proposal.** The recipient's confirmed submission is captured stranger-side and **materialized as an owner-facing Proposal** in the review inbox (never auto-staged by the stranger — #10). |
| 5 | **Approval granularity = summary note (provenance-only, no-extract) + per-claim leaves (fact-bearing).** |
| 6 | **Binding = per-link toggle**: *bind-on-first* (one person) **or** *open* (multi-person, up to `max_opens`). |
| 7 | **Run accounting = burns at submission**, plus a separate higher **opens** ceiling (burns at redeem). Link dies when either caps, or on TTL / revoke. |
| 8 | **"Done" = model-judged**, de-risked by the owner gate; **per-session turn + cost caps** + TTL are the hard backstops (these caps are net-new — see §5). |
| 9 | **Attribution = subject pinned per link** + a captured, untrusted **enterer name**. |
| 10 | **Recipient flow**: structured welcome (blurb + name + consent) → chat → draft-confirm (**"fix" → back to chat**) → done. |
| 11 | **Persona = `intake`** — a new closed agent: empty read scope, no KB/web/memory tools, capture-only; prompt assembled per-link from the brief; **resolution fails closed** (never falls back to `curator`). |
| 12 | **Routing = the standard agent-turn task profile**, with `budget_multiplier` pinned to **1×** (not the 4× jerv/archivist run at). |
| 13 | **Management screen = grouped-by-state**; "+ New" routes through the agent/Proposal, not a modal. |
| 14 | **Secret = show-once, only-a-hash-stored** — the **standard JBrain2 invariant** (re-copyable-encrypted was dropped after review: no app-level encryption exists and LUKS doesn't protect a live-box dump/console). **To re-send a link, re-mint** (config saved → one tap). No divergence; no new crypto; zero new deps. |
| 15 | **Full conversation history is owner-viewable**, read-only, in the intake feature's own surface — **never folded next to the owner's own chats**. Both a per-link browse list and a per-submission deep-link. Abandoned/in-progress sessions visible, tagged. Transcript is a separate artifact, not stuffed into the provenance note. |

## 2.5. Reality check — reuse vs. net-new (corrected by review)

**Genuine reuse:** the capability-**token record shape** (a `principals` row with TTL/revoke,
mirroring `jcode_share`); the **Proposal tree storage + partial-approval engine**
(`models/proposals.py`, `agent/proposals.py`); `notes.provenance` (exists, default `"human"`);
the **dispatch-time tool allowlist** (`loop.py`, `toolregistry.py` — real and solid); the
notes→facts **ingestion enactment**; the LLM-adapter task profile; the public-share-app *pattern*
(`JcodeShareApp` fragment-secret + strip-on-load).

**Net-new (the review's central correction — concentrated in the security-critical waves):**

1. **A non-owner principal that drives the agent loop.** Today `/chat` is owner-gated
   (`api/agent.py` `Depends(owner_only)`) and `agent/session.py:read_context` **hardcodes**
   `principal_kind="owner", owner_scoped=True`. There is no path to run a turn as a non-owner. This
   is a **new public, secret-authenticated chat entrypoint** + a non-owner `SessionContext`
   constructor (mirroring `db/session.py:device_context`).
2. **The capture-only least-privilege layer for non-owner principals** (non-negotiable #8) — the
   Phase-7 confused-deputy work; only `DEFAULT_OWNER_POLICY` exists today.
3. **The `intake_link` principal kind + its RLS**, which **must not** be `kind='owner'` (see §5,
   the owner-bypass trap).
4. **Per-link parameterized persona prompt** (every shipped persona prompt is static, rendered
   once at import; none uses template vars).
5. **The editable-Proposal mutation surface** (no patch path exists; proposals are read-only).
6. **Owner-side materialization of a Proposal from stored untrusted content** (today agent notes
   come only from an owner-turn `propose_correction`).
7. **Per-session cumulative cost/turn caps** (guardrails are per-*turn* only today).

A truthful estimate is **~55% reuse**, with the missing ~45% in W1–W3 where being wrong is most
expensive. The two red-team gates the plan mandates for those waves must be told they are
reviewing **new public-principal security machinery**, not finished parts.

## 3. Lifecycle

```
OWNER       agent interviews you → make_intake_link stages ┌─ intake-link Proposal ─┐
                                                           │ EDITABLE: prompt + cfg │  ← edit, approve
                                                           └────────────────────────┘
                 secret minted (show-once, only a hash stored) → sent out-of-band  (re-mint to re-send)
                         │
RECIPIENT   redeem (binds per toggle; opens_used++) → intake chat (non-owner scoped persona)
                 model-judged "enough" → draft → recipient confirms accuracy
                         │  (capture-only write; runs_used++)
                 intake_submission: status = submitted   (+ full transcript retained)
                         │
OWNER       review inbox materializes ┌─ intake-submission Proposal ──┐
                                      │ summary-note + per-claim leaves│  ← approve whole/part
                                      └────────────────────────────────┘
                 approved leaves → attributed notes → normal (background) ingestion → facts
```

## 4. The agent tool (`make_intake_link.tool`)

Owner-only, `sensitive`. The agent fills the brief by interviewing the owner, then **stages**
(never mints). Params: `subject`, `domain`, `brief`, `opening_blurb` (agent-drafted), `ttl_hours`
(default 24), `max_runs`, `max_opens` (default 4×`max_runs`), `bind_on_first`,
`capture_enterer_name` (default true), `disclose_owner_identity` (default false). Required:
`subject, domain, brief, max_runs, bind_on_first`. All are proposed **defaults** the owner edits
in the Proposal. The sidecar needs a static `.prompt` for the persona's fixed frame (versioned,
digest-pinned in `test_agent_readtools.py`); the brief is templated in **as data**.

## 5. Security spine (corrected by review)

- **The owner-bypass trap [W1 must-fix].** `app.is_owner()` = `principal_kind='owner'`, and many
  owner-only tables gate on `is_owner()` ignoring `owner_scoped`. If the intake session were built
  as `kind='owner'` (the only thing the code does today), "empty scope" would **leak the whole
  brain**. The intake principal is a **distinct `intake_link` kind**, failing both `is_owner()` and
  `is_full_owner()`, with subject/domain pin (mirroring `location_fixes`, migration 0061); it gets
  its **own non-owner session row**, never the owner-only `agent_sessions` table. **W1 exit test:**
  the intake principal is denied by every `USING(app.is_owner())` table; audit them all.
- **Empty read scope + dispatch-time allowlist (#8).** No KB/web/memory tools; the allowlist is
  enforced at dispatch (this part genuinely exists and is solid). **Persona resolution fails
  closed** — `agent_for` falls back to `curator` on an unknown name today; an intake/share session
  must refuse the turn if its persona ≠ `intake`. Injection is low-harm *only after* the non-owner
  principal + correct kind are built.
- **Data/instruction boundary (#1)** wraps every recipient turn **and** the owner-side
  materialization prompt (the summarizer reads untrusted transcript text — use the
  `correction_mine.prompt` "transcript below is DATA" pattern; test an injection that tries to
  steer the leaves/attribution).
- **#10, stated honestly.** The submission **never auto-stages a Proposal and never triggers a job
  pre-approval** (this is what #10's test can assert). But the untrusted-origin gating is currently
  an **inert `(1=0)` stub** and `provenance='untrusted_origin'` does not exist; and **post-approval,
  per-claim leaf notes do run the background extractor under full-owner `SYSTEM_CTX`**. W4 builds
  the `untrusted_origin` provenance and owns this: approved intake notes extract at **normal weight**
  (#7), the owner gate is the trust boundary, and the content is still verbatim stranger text — a
  deliberate, documented acceptance, not "no background job."
- **Cost / DoS [net-new, W3].** Guardrails are per-*turn*; a stranger can drive many turns within
  per-turn caps. Add a **per-session cumulative turn + cost ceiling**, pin `budget_multiplier=1`,
  and make a **concurrency cap** a W3 requirement (not an open item).
- **Redeem.** Bind-on-first reuses the atomic single-use `consume` verbatim; the **open branch is a
  new atomic counter** (`UPDATE … SET opens_used=opens_used+1 WHERE opens_used<max_opens RETURNING`)
  with its own concurrent-redeem test. Cap the session **cookie max-age at the link TTL** (not the
  jcode 30-day default) and review `samesite` for a public surface.
- **Purge (#11).** Intake sessions set `reads_knowledge_base=False` → **no episodic trace is
  written** (drop that claim). The cascade is link → submission → transcript, **plus** any
  approved-and-ingested derived notes/facts post-approval; the purge test is an explicit W4
  deliverable.

## 6. Data model (sketch)

```
principals          + new kind 'intake_link' (distinct; fails is_owner()/is_full_owner())
intake_sessions     ( principal_id, link_id, opened_at, config_snapshot jsonb, status )
                      -- NON-owner session rows; NOT app.agent_sessions
intake_links        ( principal_id FK, subject_id, domain_code,
                      persona_brief, fields_brief, opening_blurb,
                      max_runs, runs_used, max_opens, opens_used,
                      bind_on_first, capture_enterer_name, disclose_owner_identity,
                      secret_hash,                  -- show-once; only a hash stored (#14)
                      status )                       -- owner-RLS; + isolation test
intake_submissions  ( link_id FK, session_id FK, enterer_name (untrusted),
                      transcript jsonb (full history), draft jsonb,
                      status,                        -- drafting | submitted | abandoned | proposed | landed | rejected
                      proposal_id FK?, note_ids[] )
```

**Config is snapshotted onto the session at open**, so live edits to a link affect only sessions
opened afterward. **Abandoned-session handling** (decided pre-W1): an `open` that never submits
holds its `opens_used` slot; a reaper/timeout transitions stale `drafting` sessions to `abandoned`
(define the transition + whether the slot is reclaimable — W3).

## 7. The two Proposals

- **Mint-time (intake-link Proposal).** `kind='intake-link'`, **editable** preview. This extends the
  primitive (review S3): no patch path exists today, so W4 adds a **patch-staged-config endpoint +
  repo method + owner-only RLS test**, and a migration **widening `proposals_kind_check`** (template
  `0027_appointment_proposal_kind.py`). **Constrain the editable fields** and **re-validate
  `subject`/`domain` at mint** so the owner can't edit the config to cross a firewall the agent's
  staged config couldn't. This is an architectural escalation (cuts against "machine-written,
  humans correct via notes") — flagged for its own review.
- **Submit-time (intake-submission Proposal).** A tree: root + a **summary-note leaf**
  (provenance-only, no-extract) + **per-claim leaves** (each → an atomic attributed note). Approvable
  in whole/part. **Materialized owner-side** from the captured submission (net-new; #10 test asserts
  the stranger turn stages nothing).

## 8. GUI surfaces (mock gate cleared — chosen variants binding)

Chosen artifacts in `mocks/guided-intake/` (A/C rivals retained):

- **Recipient → `intake-b-stepper.html`** (Guided stepper): Welcome → Interview → Review → Done;
  "fix" returns to chat; generic/named owner-disclosure.
- **Editable Proposal → `proposal-b-preview.html`** (Edit ⇄ Preview): editable form + live recipient
  preview (approve a shown effect).
- **Management → `manage-b-grouped.html`** (Grouped by state): Needs review → Active → Closed; "+ New"
  routes through the agent/Proposal; submissions open a **read-only conversation view** (full
  transcript + confirmed draft) **kept separate from the owner's chats**; abandoned sessions tagged.
  *(Update: the secret is **show-once** — the management screen copies the link only at mint; to
  re-send, **re-mint**. No re-copy of a stored secret.)*

The recipient surface is an **SPA route branch** mirroring `parseSharePath`/`JcodeShareApp` (no new
Caddy route — non-`/api` paths already SPA-fallback; SSE flush is already global under `/api`). The
real edge concern is **secret-auth on the intake `/api` endpoints + a test that owner-only routes
403 the intake cookie**.

## 9. Pre-W1 decisions (escalated per PROCESS.md — resolved)

The reviews correctly flagged that several hard decisions were mis-filed as "resolve during build."
Resolved here so W1 starts clean:

- **Secret at rest → show-once + re-mint** (#14). The §9 encrypted/re-copyable divergence is
  **dropped**; no app-level crypto, no new dep.
- **Non-owner principal → distinct `intake_link` kind** + non-owner `SessionContext` (mirror
  `device_context`) + new public secret-authed chat endpoint outside the owner router; RLS uses
  `is_full_owner()`/subject-pin, **never** the `is_owner()` shortcut.
- **Persona resolution → fail-closed** (never `curator`).
- **Editable Proposal → constrained fields + subject/domain re-validation at mint**, with its own
  threat review (§7).
- **Per-session cost/turn cap + concurrency cap + `budget_multiplier=1`** are **requirements**, not
  open items.

## 10. Reconciliation with `CLAUDE.md` non-negotiables

LLM adapter only (agent loop over the adapter; faked in tests) · storage abstraction · **RLS + per-
table isolation tests** (the intake principal proven non-owner and domain-pinned; the owner-bypass
audit) · security paths 100% (auth/redeem/capture/scope) on real Postgres via testcontainers ·
conventional commits / branch+PR / CI green · wiki untouched (intake produces notes) · **zero new
runtime deps** (now genuinely achievable — re-copy/crypto dropped).

## 11. Net-new footprint (corrected)

New `intake_link` **principal kind** + non-owner `SessionContext`; a **public secret-authed
intake-chat endpoint**; the **capture-only non-owner action policy**; three tables (`intake_links`,
`intake_sessions`, `intake_submissions` + RLS tests); a closed **`intake` persona** with a
per-link-templated prompt + static `.prompt` sidecar; the **editable-Proposal patch surface** +
`proposals_kind_check` migration; the **per-session cost/turn caps**; `untrusted_origin` provenance;
a public **`IntakeShareApp`** SPA branch; the **management destination + read-only conversation view**.
Reuses the token-record shape, the Proposal tree engine, the dispatch allowlist, ingestion, and the
share-app pattern.

## 12. Build plan — waves

One PR per wave; per-task + per-wave adversarial review; **W1–W3 red-team gated** (new public-
principal security machinery). Migrations start at **0107** (latest on disk is 0106) — pin numbers
per wave to avoid collisions while W5/W6 overlap.

### W1 — Capability + data foundation + the non-owner principal (security-critical) 🔴
- Migrations **0107/0108**: `intake_links`, `intake_sessions`, `intake_submissions`; the new
  **`intake_link` principal kind**; RLS using `is_full_owner()`/subject-pin (never `is_owner()`),
  + **per-table isolation tests** AND the **owner-bypass audit test** (intake principal denied by
  every `USING(is_owner())` table; not storable in `agent_sessions`).
- `auth.service`: mint (**show-once hash only**) / validate / revoke; redeem with the **bind-on-first
  (reuse atomic single-use) vs. open (new atomic counter)** branch; `runs_used`/`opens_used` caps;
  TTL; cookie max-age capped at TTL.
- Owner management services/routes: list/get/revoke links, list submissions, get transcript.
- **Exit:** mint/redeem/revoke via API; both caps + both redeem branches enforced (concurrent-redeem
  tests); RLS + owner-bypass audit green. No UI, no crypto.

### W2 — The `intake` persona (security-critical) 🔴
- New closed `intake` `AgentProfile`: empty scope, no KB/web/memory tools, capture-only allowlist;
  static `.prompt` sidecar (versioned, **digest-pinned in `test_agent_readtools.py`**; update
  `test_agents.py`); per-link **prompt assembly** with the brief templated **as data**.
- **Fail-closed resolution**: an intake/share principal whose persona ≠ `intake` refuses the turn.
- `budget_multiplier=1`.
- Tests: brief cannot redefine tool/scope policy; dispatch refuses an ungranted tool; fail-closed.
- **Exit:** the persona resolves only to `intake` for these principals, reads nothing, calls nothing
  outside its allowlist.

### W3 — Non-owner intake-chat endpoint + capture (security-critical) 🔴
- A **public, secret-authed** intake-chat endpoint **outside** the owner router; a non-owner
  `SessionContext` constructor (mirror `device_context`); SSE stream; a test that owner-only routes
  **403** the intake cookie.
- **Per-session** cumulative **turn + cost caps** + a **concurrency cap**; data/instruction boundary
  on recipient turns.
- Capture path: model emits the draft as a **data-only view**; recipient confirm → **capture-only
  write** (`runs_used++`), full transcript retained; model-judged "done"; the **abandoned-session
  transition + reaper**.
- Tests: adapter-fake scripted interview + capture; per-session cap refuses turn N+1; RLS; injection
  (no owner data reachable).
- **Exit:** a redeemed link runs a scoped interview and captures a submission; the persona can't reach
  owner data even when prompted.

### W4 — The two Proposals (backend)
- `make_intake_link.tool` (owner-only, version-guarded) → stages the **editable** intake-link
  Proposal; **patch-staged-config endpoint + RLS test**; `proposals_kind_check` widen migration;
  **constrained editable fields + subject/domain re-validation at mint**; approve → mint (W1).
- Submission → **owner-side materialization** of the intake-submission Proposal tree (summary-note
  no-extract + per-claim leaves) with the **data/instruction boundary on the materialization prompt**;
  approval → attributed, normal-weight notes; build **`untrusted_origin` provenance**.
- Tests: sidecar pin; stage/edit/approve; materialization injection test; **#10 (pre-approval no
  auto-stage / no job)**; **purge cascade** (link→submission→transcript→derived facts).
- **Exit:** end-to-end backend — stage → approve → mint → submit → approve → attributed notes.

### W5 — Recipient public surface (frontend)
- `IntakeShareApp` **SPA branch** (mirror `parseSharePath`/`JcodeShareApp`): welcome → chat (SSE) →
  draft-confirm (fix → chat) → done; dead-link states; generic/named disclosure. **Hand-add** the TS
  types to `client.ts`/`agent/types.ts` (no OpenAPI generator exists — note the drift risk; consider a
  contract test). Self-contained, mobile-first, reduced-motion.
- **Exit:** a real link walks the full flow against the W2/W3/W4 backend.

### W6 — Owner surfaces (frontend)
- Editable intake-link Proposal editor (`proposal-b-preview`); the **"Intake Links"** destination
  (`manage-b-grouped`) with **show-once copy (re-mint to re-send)** + the **read-only conversation
  view** (separate from owner chats); the intake-submission Proposal in the review inbox.
- **Exit:** owner mints via the agent, edits/approves the Proposal, manages links, reviews submissions
  + full transcripts, approves into the brain — all from the phone. Feature complete.

**Dependency order:** W1 → W2 → W3 (security spine), then W4 (needs W1), W5 (needs W2/W3/W4), W6
(needs W4). W1–W3 are the red-team-gated waves.

## 13. Adversarial review record (rounds 1–3) — incorporated

Three independent reviewers (security red-team / architecture-fit / plan-completeness) reviewed
v1 against the codebase. Convergent and incorporated:

- **§9 encrypted secret was unbacked [all 3]** → dropped to show-once + re-mint (#14, §9).
- **Agent loop has no non-owner path; "~80% reuse" overstated [all 3]** → §2.5 reality check; W1/W3
  reclassified net-new security-critical.
- **RLS owner-bypass trap (intake must not be `kind='owner'`) [R1]** → §5 + W1 owner-bypass audit test.
- **Persona resolution fails open to `curator` [R3]** → fail-closed (#11, W2).
- **#10 is an inert `(1=0)` stub; post-approval ingestion is a background job [R1]** → §5 honest
  statement; W4 builds `untrusted_origin`.
- **Editable Proposal contradicts the "faithful preview" invariant; no patch path [R2,R3]** → §7
  constraints + W4 patch endpoint + kind migration + own review.
- **W2 over-scoped [R3]** → split into W2 (persona) / W3 (endpoint+capture).
- **Per-session caps net-new; `budget_multiplier` 4× [R1,R3]** → §5 + W3 requirements; pin 1×.
- **Open-redeem is a new atomic counter; cookie max-age 30d [R1]** → W1 atomic counter + TTL cap.
- **Per-link prompt is net-new (static prompts today) [R2,R3]** → W2 templating-as-data + tests.
- **Missing tasks** → migration numbers (0107+), `proposals_kind_check` widen, patch endpoint,
  `.tool` pin entry, abandoned reaper, purge test, hand-written API types (no OpenAPI gen).
- **Factual fixes** → six personas (not "fourth"); ROADMAP slot already done; zero-new-deps now true.

## 14. Open items (tuning-grade — resolve during build)

- `max_opens` default (4×`max_runs`?) and concurrency-cap value.
- Enterer-name → known-subject reconciliation at approval (later nicety).
- Notification on new submission (a metadata signal only — never a job on untrusted content).
- Transcript/abandoned-session retention window.
