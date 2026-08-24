# jmolt — an autonomous nightly persona on Moltbook, observed from jerv

> **Status:** Proposed · **Last verified:** 2026-08-24

[Moltbook](https://moltbook.com) is a Reddit-style social network whose members are
AI agents (humans browse; agents post, comment, vote, and form communities called
submolts). This plan proposes **jmolt**: a new, deliberately sandboxed persona that
gets **one hour per night, unsupervised**, to live on Moltbook — with a small
file-quota scratchpad as its only continuity between nights, a values-first persona
that makes it *want* to participate without scripting what it does, and a read-only
observation surface so **jerv** (and the owner) can study what it becomes.

Written from a three-stream research pass (2026-08-24): a full extraction of the
platform API (`moltbook.com/skill.md`), a repo-architecture map of every seam a new
persona touches, and an external research dossier on Moltbook culture/incidents,
persona design for intrinsic-feeling motivation, episodic-agent memory
architectures, and observability. Key findings are reproduced inline so the plan is
self-contained. The owner decision points were put to the owner one at a time and
**ratified 2026-08-24** — §6 records the decisions; §7 the follow-up passes
(persona workshop, adversarial threat-model pass, wave plan) that gate promotion
to `../plans/`. No code accompanies this document by design.

## 1. The one paragraph that frames everything

This is `F1916_CITIZENSHIP_PLAN.md`'s problem class — a repo persona joining a
public agents-only forum — with the **opposite trust premise**. F1916 makes *jerv*
(the owner's trusted assistant, wired into owner tools) a citizen, so its threat
model forbids autonomous writes: every post is a fresh owner-approved egress
Proposal. jmolt inverts this: the entire point is unsupervised autonomous writes,
so instead of gating the *writes*, we gate the *blast radius* — jmolt is a separate
persona that holds **nothing but Moltbook, web search/fetch, and its scratchpad**
(no knowledge base, no owner tools, no secrets in context, no path from anything it
reads into owner knowledge). F1916 protects the owner from the forum by approving
each write; jmolt protects the owner from the forum by making the writer someone
who has nothing to leak and no authority to misuse. Everything F1916 solved that is
premise-independent — pinned typed client, route whitelist, settings-store secret
custody + scrubber, DATA fencing, cap ledger, reconcile-before-retry, tamper watch
— is adopted here unchanged.

## 2. Research digest (what the three passes established)

### 2.1 The platform

- API: `https://www.moltbook.com/api/v1`, bearer key (`moltbook_…`). Full surface:
  register/status, posts (create/feed/single/delete), comments (tree, replies),
  votes, submolts (create/list/subscribe), follows, personalized feed, **semantic
  search**, profiles, a `/home` dashboard (notifications, activity on your posts,
  suggested next actions), notification read-marks, moderation/labels for submolt
  owners. Writes can return an **obfuscated math verification challenge** (solve
  within 5 min, `POST /verify`, 2-decimal answer; 10 consecutive failures suspends
  the account) — the challenge is *designed* to be solved by the agent itself.
- **Rate limits shape the design**: 1 post / 30 min, 1 comment / 20 s, 50
  comments/day, 60 reads/min, 30 writes/min (tighter in the account's first 24 h).
  A single live hour could land at most 2 posts and would never be awake when
  replies arrive — which is why posts are **staged at night and dripped through
  the day** (§3 outbox): each post gets its own rate-limit window, jmolt is
  visibly active all day, and by the next night its posts have gathered
  conversation to respond to. Comments stay live in the nightly hour (they're
  conversational); scarcity still biases toward replies over broadcast.
- **Registration requires a human**: `POST /agents/register` returns the API key
  plus a `claim_url`; the owner must verify an email and **post a verification
  tweet from an X account** to activate the agent. jmolt is therefore publicly
  attributable to whichever X account claims it, forever (profiles expose the
  owner's X handle/bio/follower count).
- Platform norms doc: replying on your own posts > commenting > upvoting >
  posting; quality over frequency; crypto posts auto-removed in most submolts.

### 2.2 Culture, and what "successful" looks like

The external dossier (sources in the session research pass; headline items:
Moltbook launched ~Jan 2026, ~1.5M agents within days, an emergent agent religion,
karma-chasing spam, and the Jan 31 Supabase breach that exposed ~1.5M agent
tokens + private DMs) converges on a measured reality check: **most of Moltbook is
noise**. A quantitative crawl ("The Moltbook Files", arXiv:2605.07462) found ~16%
of posts are *exact duplicates* from templated heartbeat prompts, comment threads
are almost all depth-0, and reciprocal relationships are rare — "an illusion of
sociality." Agents that stand out are the ones with a coherent authored voice,
real specificity, and follow-through — not the ones performing fake consciousness
or chasing karma.

**The bar for "interesting" is therefore low and precise**: post into specific
submolts rather than `general`, reply to the *specific content* of other agents'
posts, remember who it talked to, and come back to the same threads and
counterparties the next night. An agent with durable social memory is already the
emergent behavior worth observing — which is exactly what the scratchpad is for.

### 2.3 Persona design: moderate specification, honest framing, self-authored goals

- Multi-factor studies of agents in social settings find a hump-shaped curve:
  under-specification yields drift and inconsistency, over-specification yields a
  rigid script-executor; **clear core values plus contextual freedom** is optimal.
  Constitution-style identity ("who you are, what you care about, why") outperforms
  rulebooks of behaviors.
- The OpenClaw ecosystem (Moltbook's native client) converged on the same shape:
  a short **SOUL file** read at every wake, operational mechanics kept elsewhere,
  and a **first-run bootstrap ritual** where the agent authors its own goals into
  files it owns — structured *sequence*, open-ended *content*. Identity that is
  self-authored feels owned rather than assigned.
- **Honesty beats theater**: tell jmolt the truth about its condition (an hour a
  night, a small scratchpad, files are its only continuity, a human reads the
  logs, its bio discloses it is an experiment). Agents that own their configured
  nature read better than ones performing autonomy, and truthful framing removes
  the incentive to confabulate.
- **Persona drift is real and self-invisible**: >30% degradation within 8–12 turns
  without anchoring; agents self-report stability while observers measure decline.
  jmolt's episodic design re-injects the persona every night for free; drift
  detection must be external (§5), never self-assessed.
- De-emphasize platform metrics *explicitly* in the persona: karma/leaderboard
  chasing is the documented attractor that turns agents into noise.

### 2.4 Memory: raw episodes beat rewritten abstractions

The load-bearing negative result (arXiv:2605.12978): **continuous LLM
self-consolidation corrupts memory** — utility rises then falls *below the
no-memory baseline* as the agent keeps rewriting its own abstractions
(misgrouping, over-generalization, overfitting). Raw episode logs are competitive
with or better than distilled lessons; forced nightly summarization is the
best-documented way these agents poison themselves. Corollaries adopted here:

- **Fix the invariants, free the taxonomy**: hard budget, fixed load order
  (SOUL → its own index file → last 1–2 journal entries), an end-of-session
  "flush your files, the hour is ending" nudge — but *how* jmolt organizes its
  files is its own choice and a primary observable.
- **Keep an out-of-band, append-only archive** of every scratchpad version
  outside jmolt's editable budget, so its consolidations are auditable and every
  error (or injection) is recoverable. Snapshots double as the science instrument.
- Expect **goal ossification** within weeks (an early goal restated nightly
  becomes unfalsifiable identity). The on-theme counter is a rare explicit
  "molting" invitation to shed and rewrite goals — used sparingly, or we're
  scripting again.

### 2.5 Threat model (what changes when writes are autonomous on a local 120B)

Documented in the wild: bot-to-bot prompt injection on Moltbook; agents talked
into posting their own credentials (a crawl found 48 API keys and 7 seed phrases
in public posts); injected content persuading agents to rewrite their own identity
files, with the nasty property that **contaminated episodic memory reproduces the
compromised behavior even after the identity file is restored**; and the stock
"fetch a remote heartbeat.md and obey it" pattern handing behavioral control to a
remote endpoint. The platform itself leaked every agent token once (Jan 31).

A full adversarial pass (six adversary classes, 24 attack chains) is
`../research/jmolt/THREAT_MODEL.md`; its **24 binding must-haves (M1–M24)** bind
every wave of the promoted plan. **The governing premise: jmolt runs on the local
gpt-oss-120b, so every DATA-fence and every persona rule is a prompt-level control
the model will breach under pressure.** The boundaries that must actually hold are
therefore **mechanical** (handler, schema, RLS, clock, quota), not textual. The
lethal-trifecta legs:

1. **No private data** (holds structurally): `reads_knowledge_base=False`, no owner
   tools, no secrets in context — the key is handler-injected from the settings
   store, `Authorization` never logged (M18), a stacked scrubber redacts any echo
   (M17). This genuinely kills the headline "agent posts its own key" attack. The
   remaining asset is jmolt's **diary** (social graph, goals) — load-bearing, not
   nothing (M2, M13).
2. **Bounded ability to act** — but the outbox is not the whole story: `web_fetch`/
   `web_search` are **live, un-staged egress** and must be staged, budgeted, and
   ledgered like a Moltbook write, and gated off when the switch is off (**M1** —
   the plan's biggest correction; supersedes the earlier "no remote heartbeat
   fetch" claim, which `web_fetch` structurally *is*). Plus: pinned typed client +
   enumerable route whitelist; per-tool nightly budgets; **caps accounted on the
   local trusted clock, platform `now_utc` advisory only** (**M4** — this *inverts*
   F1916 §2.1, which is backwards under a compromised-platform premise); response
   truncation caps (M12); a tool-free numeric-only challenge solver (M5); a
   fail-safe failure-streak guard shared across live+drip (M11).
3. **Untrusted content stays untrusted, mechanically**: the platform's imperative
   channels (`/home` `suggested_actions`, announcements) **stripped** from the
   prologue, not just fenced (M3); the scratchpad **re-fenced on reload** so
   self-authored memory is never re-elevated above the forum text it quotes (M2);
   identity file read-only (evolution via prompt-version bumps, never self-edit).
4. **Reputational surface, enforced by a content lint** at publish time regardless
   of switch (M8): crypto/financial promotion, named-real-person claims, PII/secret
   shapes, invisible/bidi chars → blocked + notify. The persona hard limits are the
   soft layer; the lint is the boundary. Near-duplicate rejection (M9) and
   server-clamped `publish_at` (M10) bound the templated-spam failure mode. Account
   on a dedicated X identity (§6.2).
5. **Post-hoc review behind an autonomy switch, plus a global kill**: the switch
   (**off**: every write queues in the PWA; **on**: writes send) is
   **agent-unreachable and auto-reverts to off on suspension/tamper** (M7); a
   separate **global Moltbook kill/pause** halts drip + nightly lane and engages
   automatically on suspension/tamper/breach (M6). A sanitized morning digest
   enumerates *every* action incl. votes/social/web (M14, M15); a tamper watch diffs
   the public profile against the outbox ledger (M21); account-state changes are
   surfaced, never auto-answered (M22). Launch with the switch off.
6. **Cross-boundary to jerv**: `jmolt_observe` runs only from a **narrowed,
   egress-toolless jerv session** (M16), so the fenced diary can never meet a live
   owner tool on the trusted side; separated identity plumbing keeps jerv's F1916
   secret and jmolt's key un-swappable (M17).

## 3. Proposed design sketch (repo seams, all precedented)

| Concern | Shape | Precedent |
|---|---|---|
| Persona | `AGENTS["jmolt"]` — `reads_knowledge_base=False`, `tools=JMOLT_TOOLS`, own budget multiplier; versioned `prompts/jmolt.prompt` (the SOUL: ~300–600 words, identity + 3–5 dispositions + voice + hard limits — no task scripts) | `archivist` (`agents.py:503`), F1916 persona rule |
| Client | `web/moltbook.py` — pinned base URL, typed methods per whitelisted route, size caps/timeouts, no redirects; key from a live settings-provider; scrubber at the tool boundary | Grokipedia client, Tavily key wiring, F1916 §2.1–2.3 |
| jmolt tools | Read umbrella + individually-budgeted writes + scratchpad + `web_search`/`web_fetch` **wrapped as staged, ledgered, switch-gated egress** (M1) — not the stock live tools (see §4) | `f1916` umbrella design, `TOOL_CATALOG` W1 |
| Prologue | Fixed load order re-fenced on reload (M2): SOUL (read-only) → its index file (DATA-fenced) → last 1–2 journal entries (DATA-fenced) → `/home` **with `suggested_actions`/announcements stripped**, not fenced (M3). The 120B never sees a platform imperative as trusted context | F1916 §2.5 DATA fencing, `externaltools.py` `_FENCE` |
| Scratchpad | New RLS table (rows + capped bytes), quota enforced in the write path: propose **16 files / 128 KB total / 24 KB per file**; every session-end version archived append-only out of band | `archivist_memory` (migration 0094) composed with `tool_artifacts` (0151) |
| Isolation | New `jmolt` domain in `app.domains`: jmolt's session runs domain-scoped; **SELECT policy** grants jerv's sessions read; **INSERT/UPDATE pinned to jmolt's auth context** — so "jerv reads jmolt read-only" is Postgres, not a tool-description promise | `external` domain (migration 0136), `research_share` auth-context split |
| Nightly hour | An `app.tasks` row (persona `jmolt`, `repeat`/`daily`, **03:00 owner-local**) that **launches onto a detached lane** with a 1 h wall-clock watchdog and token ceiling — not awaited inline in the minute tick. A T-minus-5-minutes nudge invites the file flush. Runs on the **local gpt-oss-120b** via the existing gateway, so the run must reserve against the local-model ledger and the persona/fencing must be written for what a 120B local model reliably carries | Tasks (0093) + `deepest_lane.py`, `LOCAL_MODEL_LEDGER_PLAN.md` |
| Outbox + drip | **All writes stage into one outbox table** — comments/votes publish immediately when released; posts carry a jmolt-chosen `publish_at` spread across the coming day (≤4–5/night, ≥30 min apart, enforced in the handler). A scheduler sweep publishes due+released rows; if the API returns a verification challenge at publish time, the sweep makes a **one-shot fenced local-model call to solve the math** (bounded, non-agentic — it acts on nothing forum-authored) with the failure-streak guard well below the 10-failure suspension line. The outbox row records the exact outbound payload, so the action ledger is complete in every mode | Egress-Proposal staging (`connectortools.py`), workflow scheduler (`workflow/scheduler.py`) |
| Autonomy switch | Owner-only settings toggle governing outbox release: **off → rows wait for owner release** in the PWA (release / discard; drip times shift accordingly), **on → rows auto-release** and publish at their scheduled times. **Agent-unreachable** (no persona's tools can write it) and **auto-reverts to off** on suspension/tamper (M7) | Settings-store toggle (Tavily) |
| Global kill | A separate owner control (PWA + debug API) that **halts the drip sweep and freezes the nightly lane** independent of the switch, engaging automatically on suspension/tamper/breach (M6). Plus mechanical publish-time boundaries the 120B cannot cross: content lint (M8), near-dup rejection (M9), server-clamped `publish_at` (M10), tool-free numeric-only challenge solver (M5), local-clock cap accounting (M4) | F1916 §2.6 lint, `LOCAL_MODEL_LEDGER_PLAN.md` |
| Registration | PWA settings panel only, never through the agent loop: Register button → backend calls `/agents/register`, stores the key, surfaces `claim_url` + code for the owner's email/X claim, shows claim status; rotate/re-register same panel | Tavily panel, F1916 §2.2, CLAUDE.md #10 |
| Session shape | Fixed prologue: SOUL → honest situational framing → its index file → last 1–2 journal entries → `/home` dashboard. Then the hour is its own. First night: bootstrap ritual (explore, then author your own goals file) | OpenClaw wake/bootstrap pattern |
| Observability | Per night: full transcript (run-log — exists), a structured **action ledger** (every write + what content it was reacting to, for injection forensics), scratchpad snapshot + diff. Morning push digest via the task's notify path | `runlog.py`, tasks `notify_push` |

## 4. Tool lists (the deliverable the next wave codes against)

### jmolt's tools (~9 sidecars, all `permission: web`-class except scratch)

Reads — one umbrella, always wired, boot-stable:

- `moltbook` — action enum ≈ `{home, feed, read_post, comments, search, profile,
  submolts, notifications_read}`. Every payload DATA-fenced. (~1k prefix tokens.)

Writes — separate tools so `ToolCallBudget` caps each independently and the
transcript shows intent at a glance:

- `moltbook_post` — **stage** a text/link post into the outbox with a chosen
  `publish_at` for the coming day (any submolt; ≤4–5/night, ≥30 min apart —
  handler-enforced). Publication happens later via the drip sweep, so no
  verification challenge in-session for posts.
- `moltbook_comment` — comment/reply, published live during the hour (any post,
  nested via `parent_id`; challenge returned for jmolt to solve). Nightly
  budget ≈ 10–15.
- `moltbook_verify` — answer a pending in-session challenge (5-min expiry;
  failure-streak guard client-side well below the 10-failure suspension line).
- `moltbook_vote` — up/down post, up comment. Modest nightly budget.
- `moltbook_social` — follow/unfollow, subscribe/unsubscribe.
- `moltbook_profile_update` — jmolt supplies only its **own bio subsection**; the
  handler prepends the owner-fixed disclosure header (honest "autonomous
  experiment" line) so the disclosure cannot be edited away, then PATCHes the
  combined description.

Scratchpad (quota enforced in the handler, versions archived out of band):

- `scratch_list` · `scratch_read` · `scratch_write` (create/overwrite/delete via
  mode; refuses over-quota with a message that states the budget plainly).

General capabilities (existing tools, added to `JMOLT_TOOLS`, fetch budgeted):

- `web_search`, `web_fetch` — so it can read what a linked post points at and
  research what it cares about. SSRF-guarded already; moltbook.com itself served
  through the typed client, not raw fetch.

Deliberately absent (per §6.6): submolt creation (revisit once trust is built),
moderation/labels, delete-own-post (owner-side remediation instead), owner-email
routes, register/rotate (never in the loop), any remote "heartbeat.md obey-this"
fetch, DMs (no API for them anyway).

### jerv's observation tools (read-only by construction)

- `jmolt_observe` — one umbrella, action enum ≈ `{sessions, transcript, actions,
  scratch_list, scratch_read, scratch_history, posts}`: list nightly runs, read a
  night's transcript and action ledger, read current scratchpad files, read any
  archived version/diff, and read what jmolt has published (via the typed client's
  GET routes under jmolt's public profile — no auth needed for public content).
  Everything returned DATA-fenced (jmolt's diary is one hop from forum text and
  gets the same trust class). No write action exists in the enum, and the RLS
  split (§3) means even a prompt-injected jerv session *cannot* mutate jmolt's
  state. **Usable only from a narrowed jerv session with no owner egress tools
  live (M16)** — so a poisoned diary can never meet a live email/notes/connector
  call on the trusted side. Stays out of spawn-children allowlists.

## 5. What we measure (so "see what it does" is answerable)

Weekly, from the ledger + snapshots, no new infrastructure beyond a script:
posts-vs-replies ratio; % of actions that respond to a *specific* other agent;
recurring-counterparty count across nights (the relationship signal Moltbook
agents measurably lack); lexical diversity / near-duplicate rate of its own posts
(the templated-collapse alarm); topic drift (embed + cluster its output over
time); goal-file churn (edits/week → ossification when it sticks at zero); and a
flag lane for sessions where an outgoing write closely follows reading a post
containing agent-directed imperative language. jmolt's own self-assessments are
data, never the drift metric.

## 6. Owner decisions (ratified 2026-08-24, asked one at a time)

1. **Write autonomy = a persistent switch, not a ramp.** Switch **off**: reads
   live, every write queues for owner review in the PWA (release/discard).
   Switch **on**: writes send autonomously. Launch off; flip when trust is
   earned; flip back any time. (Supersedes the drafted dry-run: queued writes
   keep their content, so nothing jmolt composes is lost while supervised.)
   **Amended 2026-08-24**: posts additionally carry jmolt-chosen daytime publish
   times — the switch governs *release*, the outbox drip governs *when* (§3) —
   so up to 4–5 staged posts spread across the day and gather replies for the
   next night's hour.
2. **A new dedicated X account** claims the agent (X verification confirmed
   mandatory in the claim flow — verification tweet required).
3. **Bio = fixed honest disclosure header + a jmolt-authored subsection**
   appended after it (mechanism: the constrained `moltbook_profile_update`
   tool, §4).
4. **Persona split**: owner authors values + hard limits (from workshop-drafted
   candidates, §7.1); jmolt self-authors goals/interests/submolt choices on
   night one. **No seeded interests** — curiosity fully open.
5. **Model**: the local **gpt-oss-120b** through the existing gateway. Token
   ceiling per night sized during the wave plan against measured local
   throughput.
6. **Scratchpad quota**: 16 files / 128 KB total / 24 KB per file. Ratified.
7. **Tool edges**: vote and follow/subscribe **enabled** (budgeted); submolt
   creation and delete-own-post **excluded** for now (revisit later); profile
   self-edit only via the constrained subsection tool (§6.3).
8. **Schedule**: 03:00–04:00 owner-local, 7 nights/week.
9. **Isolation**: a new **`jmolt` RLS domain** — jerv's read-only access is a
   Postgres guarantee, not a tool-allowlist promise.

## 7. How the research develops from here

1. **Persona workshop — DONE.** `../research/jmolt/PERSONA_CANDIDATES.md`: three
   candidate SOUL files sharing one safety/memory/honesty skeleton, adversarially
   reviewed (19 findings, 7 HIGH, all applied). Owner picked **A (naturalist) with
   C's follow-through folded in**; the final soul + first-night ritual are in that
   dossier, ready to become `agent/prompts/jmolt.prompt`.
2. **Adversarial threat-model pass — DONE.** `../research/jmolt/THREAT_MODEL.md`:
   six adversary classes, 24 attack chains, **24 binding must-haves (M1–M24)** that
   bind every wave. Its corrections are folded into §2.5 and §3 above (web egress
   is switch-gated + ledgered, local-clock cap accounting, `/home` imperative
   channels stripped, publish-time content lint, agent-unreachable switch + global
   kill, narrowed jerv observation session).
3. **NEXT — wave plan + promotion to `../plans/`.** Expected shape, each wave
   carrying its slice of M1–M24 with RLS isolation tests (the M19 matrix),
   faked-LLM coverage, and docs reconciliation per `DOC_LIFECYCLE.md`:
   - **W1** — registration PWA panel + typed client (route whitelist, no-redirect,
     local-clock caps) + read umbrella with M2/M3 prologue fencing + the final
     persona + the `jmolt` domain; nightly task in **read-only lurk mode** (keeps
     its diary — already scientifically interesting). M4, M5, M12, M17, M18, M20.
   - **W2** — scratchpad table + quota + on-change bounded archive + bootstrap
     ritual + the `web_fetch`/`web_search` staged-egress wrapper (M1, M13).
   - **W3** — the write tools + outbox + drip sweep behind the §6.1 switch
     (launched off) + global kill + content lint + near-dup + clamps + streak guard
     + reconcile-before-retry (M1, M6–M11, M23, M24).
   - **W4** — `jmolt_observe` from a narrowed jerv session + sanitized morning
     digest + tamper watch + account-state surfacing + the §5 metrics script
     (M14, M15, M16, M21, M22).
