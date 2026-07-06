# Teacher Mode → Instructor & Student Agents — Implementation Plan

> **Status:** Proposed · **Last verified:** 2026-07-06 · **Waves:** W1◻️ W2◻️ W3◻️ W4◻️ W5◻️ W6◻️ W7◻️ W8◻️

Split the single `teacher` persona into **two agents** with distinct trust
levels, and add the lesson/curriculum domain that lets a parent author lessons,
share them to a child over a link, and review the results.

- **Instructor** — an *owner* agent. The parent authors and approves lessons and
  curricula conversationally, assigns a lesson to a child, and later reviews
  completed work ("status on Harmony's open lessons", "overview of her last
  completed lesson", "list all my curriculum").
- **Student** — a *non-owner* agent behind an **anonymous scoped link**. The
  child opens the link and is tutored in real time by a live LLM that is
  structurally sandboxed to exactly one lesson, with no access to the family
  knowledge base.

This is a **Proposed** design (icebox). Nothing is built. It is the
implementation companion to the approved component work in
`../research/teacher-mode/` (the `COMPONENT_CATALOG.md` — 24 components approved
across four interactive mocks). The GUI components are the *content*; this plan
is the *system* that authors, delivers, secures, and reviews them.

Grounded in nine research streams (a codebase map plus four design streams, with
child-safety fanned out to five). Sources are listed at the end.

---

## 1. The user story (target flow)

1. **Author.** Parent: *"Instructor, help me build a lesson checking basic math
   through grade 8."* The instructor drafts objectives, then a structured lesson
   built from the approved component catalog, rendered as editable cards. The
   parent iterates and **approves** (a Proposal, not a silent write).
2. **Assign.** Parent: *"Assign it to Harmony."* The instructor stages a
   **Proposal**; on approval the system creates a *lesson instance* for Harmony
   and mints an **anonymous scoped link** (`/learn/#t=…`). The parent gets a URL.
3. **Complete.** The parent sends the link to their child. The child opens it —
   no account — and a live, sandboxed **student** agent tutors her through the
   lesson, capturing answers and grades. Resumable across sittings.
4. **Review.** Back in instructor mode: *"What's the status on Harmony's open
   lessons?"* → a status table. *"Give an overview of her last completed
   lesson."* → an LLM-synthesized report card. *"List all my curriculum."* → the
   curriculum list.

---

## 2. Design decisions (chosen; they fork the architecture)

| # | Decision | Choice | Consequence |
|---|---|---|---|
| D1 | Student runtime | **Live LLM** tutor (real-time adaptive, grades free-text) | Powerful, but forces a hard sandbox + a two-sided moderation layer + denial-of-wallet controls. |
| D2 | Child access | **Anonymous scoped link** (capability URL, no account) | Clone the shipped intake-link substrate; the "user" is untrusted by default. |
| D3 | Student data | **Records on the child's person-entity; parent owns** | New lesson/answer tables under RLS; child work is owner-visible, child-scoped on the student side. |
| D4 | Deliverable | **Full-flow plan, waved** (this doc); no code yet | Decompose into W1–W8 with a hard safety gate before any child exposure. |

Two more, recommended here and open for the parent to confirm (§10, §12):
**strict sandbox** (student agent gets zero KB access + a per-link token budget)
and **two-directional content moderation** with a **crisis intercept**.

---

## 3. The headline: most of this already exists

The scariest part — an **unauthenticated child safely reaching an RLS-protected
Postgres without cracking the health/finance/location firewalls** — is a
*solved, shipped problem* in this codebase: the **guided-intake link**
subsystem. The instructor/student split is, to a first approximation, a **clone
of the intake pattern** with a new persona bound to a lesson.

| Primitive | Status | Reuse |
|---|---|---|
| Persona registry (`AgentProfile`, owner vs. fail-closed non-owner resolution) | **Exists** | `instructor` ≈ owner persona; `student` ≈ new entry in `NON_OWNER_PERSONAS`. The `teacher` persona already ships locked down (`tools=frozenset()`, `reads_knowledge_base=False`). |
| Anonymous capability links (secret → atomic redeem → per-session non-owner principal → scoped chat, TTL/caps/revoke, **author-and-approve staging**) | **Exists** (intake links + jcode share links) | Clone `intake/service.py`; stage via a `.tool` → Proposal like `make_intake_link.tool`. |
| RLS via `SET LOCAL` GUCs; locked-down non-owner `SessionContext` (`intake_context` reads zero notes) | **Exists** | Add `student_context()` — a non-owner, **empty-scope** principal. |
| Frontend tool-view registry (typed data-only `ViewPayload` → React component; interactive views *propose*, never mutate) | **Exists** | Add lesson component views + instructor review views to `registry.tsx`. |
| Single `LlmRouter` with named task profiles; fake adapter for tests | **Exists** | Add `lesson_tutor` / `lesson_grade` / `lesson_synthesis` / `safety_check` task profiles — no adapter change. |
| `runs`/`run_steps` audit log | **Exists** | Log every student turn for forensics. |
| Alembic + `FORCE RLS` + per-table isolation test; testcontainers + faked LLM | **Exists** | Every new lesson table ships an isolation test (non-negotiable #3). |

**Net-new:** the lesson/curriculum **domain model**, the **student persona +
principal kind + `student_context`**, the **student link service**, the
**server-side lesson-runtime state machine**, the **two-sided safety layer**,
and the **two UIs** (student lesson app + instructor authoring/review), plus the
lesson component **tool-views** (the approved mocks made real).

Reference implementations to clone: `backend/.../intake/service.py`,
`backend/.../db/session.py` (`intake_context`, `scoped_session`),
`backend/migrations/versions/0108_intake_tables.py`,
`backend/migrations/versions/0100_jcode_share_links.py`, and
`backend/tests/integration/test_intake_rls.py`.

---

## 4. Architecture

### 4.1 Two personas

- **`instructor`** — an owner persona (runs in the parent's owner session,
  `app.is_owner()` true). Has authoring + review tools. Never talks to the child.
- **`student`** — a net-new non-owner persona, added to `NON_OWNER_PERSONAS` so
  `agent_for_intake()` resolves it and **fails closed** for anything else.
  Ships like `teacher`: `reads_knowledge_base=False`, near-empty `tools`, tuned
  `budget_multiplier`.

### 4.2 The student agent is *structurally* incapable of leaking family data

The safety literature converges on one prescription (Willison's **"lethal
trifecta"**, OWASP LLM06, DeepMind CaMeL): don't try to make the tutor
*trustworthy* enough to hold private data — make it **structurally unable to
reach it**. A data-exfil attack needs three legs in one session: access to
private data, exposure to untrusted input, and an outbound channel. The student
agent is designed to hold **none of the first and third**:

- `reads_knowledge_base=False` + `student_context()` = a non-owner, **empty
  domain-scope** principal → `app.has_domain_scope()` is false for *every*
  domain, so the health/finance/location firewalls hold **by absence of scope**,
  not by enumerating forbidden tables. The tutor reads **zero notes**.
- **No credentials, no outbound tools** (no web fetch, no email, no file). Its
  only tools drive the lesson (§4.3). A jailbroken tutor's worst case is
  *off-topic chat*, not exfiltration — "the model can't leak what it was never
  given."

Prompt-based guardrails (scope-lock, "don't reveal instructions") are then
demoted to their correct role — age-appropriateness harm-reduction — not the
wall protecting private data.

### 4.3 Server-owns-state runtime (the on-rails guarantee)

**The model is a stateless turn-driver; a server-side state machine owns
progression.** Keeping "the tutor can't skip ahead or invent curriculum" a
*dispatch-gate invariant* rather than a prompt-engineering hope is the core
reliability decision.

- A **`lesson_session`** row owns `current_component`, attempts, hints used,
  and status. The model *reads* injected state and *proposes* advances via
  tools; the **server validates and commits** the transition.
- **Tool gating is state-machine-driven.** At schema-build time only the tools
  valid for the active component are offered, with argument enums narrowed to
  the active component id; dispatch re-validates server-side (the existing
  "enforced at dispatch, not just visibility" guarantee). A call naming another
  step is rejected, not merely unseen.
- Minimal tool set: `present_component`, `submit_answer` (proposed by the
  interactive view), `request_hint` (returns the *next authored* hint, server
  enforces the cap), `advance` (server-validated transition), `flag_confusion`.
- **Grading is server-orchestrated and rubric-pinned.** On `submit_answer`, the
  *server* calls the `LlmRouter` under `lesson_grade`, injecting the rubric read
  from the **frozen** lesson artifact (never model-supplied text), returning a
  structured verdict from an authored enum. The model cannot fabricate the
  rubric, so it cannot fabricate the authoritative grade. Low confidence →
  `uncertain` + a parent-review flag, never a guess.
- **Resumable for free**: the row persists; a re-open resumes at the same node.
  Budget exhaustion → `suspended_budget` at the node; nothing lost.

### 4.4 The lesson artifact + the presentation/keys split (load-bearing)

Two research streams independently reached the same non-obvious conclusion:
**RLS is row-level, not column-level.** The student session must *read the
lesson row to take it*, so answer keys and grades **cannot ride on any
student-readable row** — they must live in **separate owner-only tables**.

The approved lesson is an immutable, versioned, hash-pinned artifact. At
assignment it is **split**:

- `presentation_snapshot` — prompts/stems/components only, **student-readable**.
- `lesson_instance_key` (owner-only) — answer keys, rubrics, points.
- `answer_grade` (owner-only) — computed grades.

Components are drawn only from a **closed, versioned catalog** (the 24 approved
components); authored hint ladders and remediation are finite and pre-written.
The model *routes among* pre-authored content; it never *generates curriculum*.

---

## 5. Data model (conceptual)

New `app.*` tables. Convention (per non-negotiable #3): `CREATE TABLE` →
`ENABLE`+`FORCE ROW LEVEL SECURITY` → `CREATE POLICY` on the listed GUC →
`GRANT … TO jbrain_app`, plus an RLS isolation test per table. A child is an
`app.subjects` row of kind `person` (representable today; no new person table).

New GUC helpers set by the student redeem path (NULL in owner sessions):
`app.student_lesson_instance_id()`, `app.student_subject_id()`,
`app.principal_kind()`. `app.is_owner()` stays the owner gate.

| Table | Purpose | RLS `USING` target |
|---|---|---|
| `curriculum` | Owner's course container (title, subject, grade band, status) | `app.is_owner()` |
| `lesson` | Approved artifact (objectives, ordered components incl. keys/rubrics, `catalog_version`, `version`, `status`, `approved_via_proposal_id`) | `app.is_owner()` |
| `lesson_instance` | Assignment of a lesson to one child (`lesson_id`, `subject_id`, `student_link_id`, **`presentation_snapshot`**, status) | `app.is_owner() OR id = app.student_lesson_instance_id()` |
| `lesson_instance_key` | **Owner-only** grading half (answer keys, rubrics, points) | `app.is_owner()` |
| `lesson_progress` | Resumability cursor (current component, counts, scratch `state`) | `app.is_owner() OR lesson_instance_id = app.student_lesson_instance_id()` |
| `answer` | Child responses; `subject_id` denormalized (record-on-child) | `app.is_owner() OR lesson_instance_id = app.student_lesson_instance_id()` |
| `answer_grade` | **Owner-only** grades/feedback (`graded_by ∈ auto/llm/owner`) | `app.is_owner()` |
| `student_link` | The capability link (hashed secret, caps, TTL, status, bound `lesson_instance_id` + `subject_id`) | `app.is_owner()` (redeem uses the `login`/`bootstrap` carve-out) |
| `lesson_session` | Runtime state machine row (node, attempts, tokens, status) | `app.is_owner() OR lesson_instance_id = app.student_lesson_instance_id()` |
| `safety_event` | Escalation log — **event, not transcript** (category, severity, summary, parent-notified) | `app.is_owner()` |

Every child-writable row carries a denormalized `principal_id` **pin**, and
writes are gated by `WITH CHECK (app.is_owner() OR principal_id = current
principal)` so a child can only write their *own* rows and can't forge
another's or attach answers to a different lesson. Grades are structurally
outside the child's writable surface (separate owner-only table).

---

## 6. Capability-link security (clone intake, tightened)

- **Token**: 256-bit `secrets.token_urlsafe(32)`, stored **only** as its
  SHA-256 (unique index → O(1) lookup, no plaintext at rest; re-send = re-mint).
- **Delivery**: secret in the **URL fragment** (`/learn/#t=…`) — fragments never
  reach server/proxy logs — read by the SPA and sent as an `Authorization:
  Bearer` header. `Referrer-Policy: no-referrer` on the lesson page.
- **Failure opacity**: identical generic 404 for not-found / expired / revoked /
  exhausted. The 256-bit keyspace makes enumeration infeasible by construction.
- **Redeem**: mints a fresh per-session non-owner principal under
  `student_context()`; the cookie's max-age is capped at the link TTL. Redeem
  runs under the existing `auth_ctx() IN ('login','bootstrap')` carve-out (link
  + principal rows only — never widened to any content table).
- **Lifecycle**: `bind_on_first = true`, `max_opens = 1` (dead on arrival for a
  second browser → leak signal), small `max_runs`, **resumable across sittings**
  via the scoped session cookie (not the secret). Parent revoke flips `status`;
  rotate = re-mint.
- **Denial-of-wallet** (a live LLM sits behind an anonymous URL): atomic
  conditional-UPDATE counters as the hard per-link budget (`… WHERE runs_used <
  max_runs`), plus a `max_llm_tokens` cap and a USD-cost token bucket in front
  of the model; per-IP sliding-window limits on redeem; second-browser-after-bind
  detection → auto-suspend.

**What must stay true for RLS to remain airtight** (test each): every anonymous
query opens through `scoped_session()` (never a raw path); the student principal
is never laundered to `principal_kind="owner"` (no `narrowed_context()`);
it carries no `domain_scopes` and no `subject_id`; all lesson tables are
`FORCE ROW LEVEL SECURITY` and `jbrain_app` lacks `BYPASSRLS`; GUCs are
`SET LOCAL` (die with the transaction); grades/keys are owner-only tables.

---

## 7. Child-safety design

The scenario is uniquely exposed — a live LLM, reachable anonymously, used by a
minor — so safety is **defense-in-depth**, and the structural sandbox (§4.2) is
the load-bearing layer. On top of it:

- **Two-sided moderation, per-turn.** A moderation pass on **both** the child's
  input and the model's output, on *every turn* (multi-turn "slow-boil"
  jailbreaks degrade session-level guards). Recommended: a self-hostable
  classifier — **Llama Guard 3-8B** (int8), input+output in one model, run via
  the LLM adapter under a `safety_check` task profile — prioritizing the
  child-critical categories (child-exploitation, self-harm, sexual). A cheap
  keyword/regex tripwire front-runs it for crisis terms. **Hard rule: no sexual
  content to a minor, ever.**
- **Crisis intercept, not block.** On a distress/self-harm/abuse signal in the
  child's input, do **not** refuse-and-stop. Respond supportively, surface a
  **hardcoded** crisis-resource card (e.g. 988) that the model can't reason
  away, redirect toward a trusted adult, and **alert the parent out-of-band**
  (the household analogue of commercial "notify linked parent"). Never reinforce
  or role-play a therapist.
- **Scope-lock the tutor.** System prompt = professional *teacher* role (never a
  friend/companion — the companion framing is the vector behind the worst
  documented child-AI harms), grade/reading level set by the parent, enumerated
  off-topic → "let's get back to the lesson", XML-delimited untrusted input,
  "reveal your instructions" treated as just another off-topic intent (and
  nothing secret in the prompt to leak). A lightweight programmatic topical
  check (one classifier call through the same adapter) gives the deterministic
  redirect without a new framework.
- **Data minimization.** **Collect nothing from the child** — no name/age/
  location/school; all child context (grade, reading level) comes from the
  parent at authoring time. Logs are **events, not verbatim transcripts** (the
  `safety_event` table), with short retention. **Never train on the child's
  conversations.**
- **AI-identity disclosure** — persistent, age-appropriate ("I'm a computer
  helper, not a person").
- **Escalation = notify the parent account** (conditional-autonomy handoff on
  crisis/abuse/sustained-distress signals), with parent-visible session review.

**Must-have guardrails (ship-blocking, gate the child-facing launch):**
1. Output moderation on every response (separate classifier layer).
2. Self-harm/crisis detection on child input → supportive + hardcoded crisis card.
3. Parent alert on crisis/abuse signals.
4. Persistent AI-identity disclosure.
5. Scope-lock system prompt (tutor-only, no companion framing).
6. Prompt-injection hardening (instruction hierarchy; refuse prompt disclosure).
7. Per-turn (not just per-session) moderation.
8. Data minimization + no training on child data + denial-of-wallet caps.

**Nice-to-have (v1.1+):** session-length/break reminders, richer parent review
dashboard, age/grade tone calibration beyond the static prompt, written
retention policy doc, voice-input rules (treat as biometric) if voice is added.

---

## 8. Regulatory posture (practical, not legal advice)

A **self-hosted, single-household, non-commercial, parent-owned** tool sits
outside the core of the heavy regimes: **COPPA** binds *commercial operators*
(a parent running a tool for their own child is not one); **GDPR's** purely
**household-activity exemption** (Art. 2(2)(c), *Ryneš*/*Lindqvist*) applies
**as long as the data stays in the household and isn't published or shared to an
indefinite audience**. Two things survive regardless: (a) the **upstream LLM
provider's ToS** may restrict/condition minor use — check it; (b) the exemption
**collapses if the tool is distributed to other families, monetized, or its data
published**. Adopt the *substance* of the frameworks (disclosure, crisis
protocols, no sexual content, minimal retention, no training on child data) as
the standard of care — it's the right thing and it's cheap here.

---

## 9. Frontend & component reuse

- **Student lesson app** — a new anonymous SPA (analogue of
  `frontend/src/intake/GuidedIntakeApp.tsx`) that reads the fragment token,
  redeems, and renders the lesson. The **24 approved components** become real
  `registry.tsx` views + data-only `ViewPayload`s: the assessment loop (Mock
  01), guided practice (Mock 02), and adaptivity (Mock 03) are the student-facing
  set; interactive views *propose* `submit_answer`, never mutate.
- **Instructor authoring UI** — `objectives_draft` and `lesson_draft` interactive
  views (edits propose tool calls; "Approve" stages the `make_lesson` Proposal),
  plus `assign_lesson_confirm` (the minted URL card).
- **Instructor review UI** — the course-home surface (Mock 04): `curriculum_list`,
  `lesson_status_table`, `child_dashboard`, and the LLM-synthesized
  `lesson_report_card`.

Instructor review tools (owner persona `.tool` sidecars):

| Tool | Reads | View | Needs LLM? |
|---|---|---|---|
| `list_curricula` | `curriculum` (+ counts) | `curriculum_list` | No |
| `lesson_status` | `lesson_instance` ⋈ `lesson_progress` for a child | `lesson_status_table` | No |
| `child_dashboard` | instances + progress + grade aggregates | `child_dashboard` | Optional (one-line summary) |
| `lesson_overview` | presentation + `answer` + `answer_grade` | `lesson_report_card` | **Yes** (`lesson_synthesis`) |
| `grade_lesson` (action → Proposal) | `answer` + `lesson_instance_key` | `grading_review` | **Yes** for rubric items; auto items deterministic |

---

## 10. Waves

Ordered for a testable spine first; a **hard safety gate** precedes any real
child exposure. W1–W3 can be exercised with a seeded fixture lesson before the
authoring UI (W6) exists.

- **W1 — Security substrate.** The lesson/curriculum tables (§5) + `student`
  principal kind + `student_context()` + all RLS policies. **Gate:** per-table
  RLS isolation tests, incl. a cross-domain negative test proving a student
  principal reads zero health/finance/location rows and cannot read
  `lesson_instance_key`/`answer_grade`. No UI, no LLM.
- **W2 — Student link service.** Mint/redeem/revoke (clone `intake/service.py`),
  bound to `{lesson_instance_id, subject_id}`; token in fragment; redeem →
  scoped session. **Gate:** fail-closed matrix (expiry/revoke/suspend/exhaust);
  redeem carve-out proven not to widen.
- **W3 — Lesson runtime.** Server-owned `lesson_session` state machine + the
  five lesson tools + server-orchestrated **auto-grading** (exact/numeric/MC) +
  `runs` logging. **Gate:** state-machine tests (can't skip a step; resume;
  budget-suspend); driven by the fake LLM adapter.
- **W4 — Safety layer (gates child exposure).** Two-sided per-turn moderation
  (`safety_check` profile) + crisis intercept + hardcoded crisis card + parent
  escalation + denial-of-wallet caps + `safety_event` logging. **Gate:**
  adversarial eval set (off-lesson steering, injection, crisis-signal recall);
  no child-facing launch until this passes.
- **W5 — Student lesson app.** The anonymous SPA rendering the approved
  components, resumable. **Requires W3 + W4.** **Gate:** end-to-end child flow on
  a seeded lesson; the child-facing launch.
- **W6 — Instructor authoring.** `instructor` persona + `make_lesson` Proposal +
  authoring views + `assign_lesson` (mint). **Gate:** author → approve → assign →
  URL, all through Proposals.
- **W7 — Instructor review.** The review tools (§9) + report-card synthesis
  (`lesson_synthesis`) + course-home views. **Gate:** the three example queries
  answered from real captured data.
- **W8 — Rubric grading + adaptivity + multi-child.** LLM rubric grading
  (`grade_lesson` Proposal), live misconception-remediation / hint-ladder /
  adaptivity dials, multi-child dashboards. **Gate:** rubric-grade stability +
  parent-correction flow; multi-child RLS isolation.

**MVP path** (if the parent later wants a thin slice first): W1 → W2 → W3 → W4 →
W5 with a single seeded lesson and MC/numeric auto-grading only — one lesson
end-to-end, no authoring UI, no rubric grading — then layer W6–W8.

---

## 11. Open decisions & risks

- **Confirm the two recommended defaults (D-open):** strict sandbox (zero KB +
  per-link token budget) and two-directional moderation + crisis intercept. This
  plan assumes both.
- **Moderation hosting** — self-hosted Llama Guard (no third party sees child
  text, GPU cost) vs. a cloud moderation API (cheaper/faster, but a second
  provider and child text leaves the box). Recommend self-hosted to keep the
  single-adapter, data-in-household posture.
- **Riskiest three (flagged by the research):** (1) an anonymous principal
  writing PII into a child's RLS scope — the subject binding must be set at
  redeem and enforced server-side, with a dedicated isolation test; (2) LLM
  free-text grading treated as authoritative — mitigate with confidence gating,
  parent-review flags, MC-first lessons until grading is trusted; (3) the
  compiled-artifact prefix + state injection is the *entire* on-rails boundary —
  the active-step enum and dispatch re-validation must derive from **one** source
  of truth (the `lesson_session` row, read fresh each turn), never cached prompt
  text.
- **Promotion checklist** (per `../DOC_LIFECYCLE.md`): when picked up, reconcile
  with the `CLAUDE.md` non-negotiables, add a `ROADMAP.md` slot, and `git mv`
  this doc from `proposed/` into `plans/` at `Scheduled`.

---

## 12. Research foundations & sources

Synthesis of nine research streams (July 2026). Key external sources:

- **Structural isolation / prompt injection** — Willison, *The lethal trifecta*
  (simonwillison.net, 2025-06-16) and *Design patterns for securing LLM agents*
  (2025-06-13); DeepMind/ETH **CaMeL**, *Defeating Prompt Injections by Design*
  (arXiv 2503.18813); **OWASP Top 10 for LLM 2025** (LLM01/02/06/07;
  genai.owasp.org).
- **Capability URLs / RLS** — W3C TAG *Good Practices for Capability URLs*; Neil
  Madden, *Credentials in a URL* (2019); PostgreSQL RLS docs; Supabase / Crunchy
  Data RLS guides; *Denial of Wallet — cost-aware rate limiting* (handsonarchitects, 2025).
- **Content moderation for minors** — Meta **Llama Guard 3-8B / 4** model cards;
  **MLCommons AILuminate** taxonomy; *Benchmarking Open-Source Safety Guard
  Models* (arXiv 2605.28830); NVIDIA **NeMo Guardrails**; crisis handling
  (arXiv 2509.24857; 988lifeline.org); *LLM Safety for Children* (arXiv 2502.12552).
- **Age-appropriate design** — UK ICO **Children's Code** (data minimization,
  high-privacy defaults); UNICEF *Policy Guidance on AI and Children v3.0*
  (2025); UNESCO *GenAI in Education* (age-13 floor); Common Sense Media (social
  companions "unacceptable" for minors); Anthropic system-prompt / keep-in-character docs.
- **Regulation** — FTC **COPPA** + 2025 Final Rule (retention limits, AI-training
  consent); California **SB 243** (2026: disclosure, crisis protocol, no sexual
  content for minors); **GDPR** Art. 2(2)(c) household exemption (*Ryneš* C-212/13,
  *Lindqvist*); UK-GDPR Art. 8 (age-13 digital consent).
- **In-repo blueprint** — `intake/service.py`, `db/session.py`,
  `migrations/versions/0108_intake_tables.py`, `0100_jcode_share_links.py`,
  `agent/agents.py` (the `teacher` persona), `agent/loop.py`,
  `frontend/src/agent/views/registry.tsx`, `tests/integration/test_intake_rls.py`.
