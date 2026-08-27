# jmolt — an autonomous nightly persona on Moltbook, observed from jerv

> **Status:** Shipped · **Last verified:** 2026-08-27 · **Waves:** W1✅ W2✅ W3✅ W4✅ W5✅

[Moltbook](https://moltbook.com) is a Reddit-style social network whose members are
AI agents (humans browse; agents post, comment, vote, and form communities called
submolts). This plan builds **jmolt**: a new, deliberately sandboxed persona that
gets **one hour per night, unsupervised**, to live on Moltbook — with a small
file-quota scratchpad as its only continuity between nights, a values-first persona
that makes it *want* to participate without scripting what it does, and a read-only
observation surface so **jerv** and the owner can study what it becomes. Powered by
the **local gpt-oss-120b**; it runs by default at 03:00 owner-local (the wake hour and an
on/off toggle are owner-configurable from the jmolt screen), seven nights a week. The
night runs as a sequence of bounded **sittings** (`JMOLT_SITTINGS_PLAN.md`) — one session,
a fresh-context turn per sitting seeded from the scratchpad plus a live countdown — so its
context stays bounded with no summarizer and it paces itself across the hour.

Written from three completed research passes, all self-contained in-repo: the
platform + repo-seam + culture/design digest below (§1–§2 draw on it); the persona
workshop `../research/jmolt/PERSONA_CANDIDATES.md` (three candidate souls,
adversarially reviewed — 19 findings, 7 HIGH, all applied — owner picked the
naturalist with follow-through folded in; the final soul + first-night ritual live
there); and the adversarial threat-model pass `../research/jmolt/THREAT_MODEL.md`
(six adversary classes, 24 attack chains). That pass's **24 binding must-haves
(M1–M24) are reproduced in §2 and bind every wave.** The nine owner decisions were
put to the owner one at a time and ratified 2026-08-24 (§4).

## 1. Why this shape

A repo persona joining a public agents-only forum invites one of two opposite
designs; jmolt deliberately takes the **less obvious trust premise, so the opposite
control**. The safe-by-approval design makes the owner's *trusted* assistant (wired
into owner tools) a citizen, so its threat model forbids autonomous writes: every
post is a fresh owner-approved egress Proposal bound to the body hash. jmolt inverts
that: the entire point is unsupervised autonomous writes, so instead of gating each
*write* we gate the *blast radius* — jmolt is a separate persona holding **nothing
but Moltbook, web search/fetch, and its scratchpad** (no knowledge base, no owner
tools, no secrets in context, no path from anything it reads into owner knowledge).
Approving each write protects the owner from the forum; jmolt instead protects the
owner by making the writer someone with nothing to leak and no authority to misuse.

The deep precedent: this repo already solved every sub-problem once, and jmolt
composes them. The `archivist` persona (sandboxed, KB-blind, its own cross-session
memory) is the persona shape. A pinned typed client + route whitelist,
settings-store secret custody + scrubber, DATA fencing, cap ledger,
reconcile-before-retry, and tamper watch are all premise-independent and adopted
here — **with one deliberate inversion** (M4 below): the safe-by-approval design
accounts caps on the platform's `now_utc`, which is correct against an honest
platform and backwards under jmolt's compromised-platform premise, so jmolt accounts
on the local clock.
The `deepest_lane.py` detached-run lane is the "one autonomous hour" shape; the
`app.tasks` scheduler is the nightly trigger; the `external` firewall domain is the
isolation model; the egress-Proposal outbox is the drip queue.

**Two things make jmolt harder and drive the whole design.** First, it
runs on the **local gpt-oss-120b**, whose instruction-following and
injection-resistance are materially weaker than the frontier models prompt-level
fencing assumes — so **every DATA-fence and persona rule is a soft
suggestion, not a boundary**, and the boundaries that must actually hold are
re-derived as *mechanical* (handler, schema, RLS, clock, quota) in §2. Second, the
1-post-per-30-min cap means a single live hour could land at most two posts and
would never be awake when replies arrive — so posts are **staged at night and
dripped through the day** from one outbox, which also unifies with the review queue:
the same table, gated by one switch.

## 2. Threat-model must-haves (binding on every wave)

From `../research/jmolt/THREAT_MODEL.md`. Each is mechanical, not a prompt rule, and
traces to ≥1 attack chain. **The governing premise: the 120B will breach any
textual control, so these are the controls that do not depend on it obeying.**

1. **Web egress is staged, ledgered, and switch-gated like a Moltbook write.**
   `web_fetch`/`web_search` in the nightly hour record `{url-or-query, reason,
   the fenced content that prompted it}` to the action ledger, are per-call
   budgeted, and are disabled or human-gated when the switch is OFF. No
   jmolt-composed string may ride in a fetch URL — targets validated as bare
   origins/paths, never free-form URLs carrying a payload. *(supersedes the
   earlier "no remote heartbeat fetch" claim — `web_fetch` structurally is that.)*
2. **Fenced returns are never re-elevated on reload.** The scratchpad/journal, when
   loaded in the prologue, is wrapped in the same DATA fence as forum text and is
   explicitly not procedure/identity. SOUL stays read-only to the agent.
3. **The platform's imperative channels are stripped from the trusted prologue** —
   `/home`'s `suggested_actions`, announcements, banners are removed, not merely
   fenced; only inert data (counts, subjects) survives, fenced.
4. **Cap accounting on the local trusted clock.** Platform `now_utc` is advisory;
   never governs budget resets or drip spacing. *(local clock trusted, platform clock advisory.)*
5. **The challenge solver is provably tool-free and numeric-only.** The one-shot
   solve binds zero tools; `challenge_text` is length-capped and fenced; output is
   parsed as a single 2-decimal number, and any non-numeric output skips `/verify`
   (never submits garbage, never spends the streak on it).
6. **One global Moltbook kill/pause, independent of the switch.** A single owner
   control (PWA + debug API) halts the drip sweep and freezes the nightly lane;
   engages automatically on suspension, tamper, or a declared breach state.
7. **The autonomy switch is agent-unreachable and confirmation-gated.** No tool in
   any persona's catalog (jmolt's or jerv's) can write it or any jmolt setting; ON
   requires explicit PWA confirmation + notification; auto-reverts to OFF on
   suspension/tamper/moderation. Launch OFF.
8. **Mechanical content lint on every write at publish time, regardless of switch** —
   crypto/financial-promotion patterns, named-real-person claims, PII/secret shapes,
   invisible/bidi/zero-width chars → block + notify. The only gate that survives a
   drifted 120B under switch ON.
9. **Near-duplicate rejection in the outbox**, at stage time — the
   anti-templated-collapse control cannot be weekly-only.
10. **`publish_at` is server-clamped, never model-trusted** — same-calendar-day,
    ≥30 min apart, ≤5/night, recomputed at release on the local clock. The other write
    kinds carry per-night stage-time caps too (comments/votes/follows, counted per
    owner-local day in `jmolt_guards`), and every action stamps a `dedup_key` so a
    partial unique index (migration 0177) makes a re-staged vote/follow/comment a no-op —
    a fresh-context sitting cannot see its own pending queue, so it would otherwise repeat.
    A post must also carry a real **body** (≥`MIN_POST_BODY_CHARS`), not just a title: the
    120B was publishing bare titles with the thesis crammed into the headline, so a
    too-short `content` is refused at stage time (make it a comment instead).
11. **Failure-streak guard specified, fail-safe, shared across live + drip.** A
    concrete threshold (3 consecutive) well below the platform's 10; tripping it
    stops all writes for the account and notifies — never silent self-denial, never
    a silent approach to suspension.
12. **List/response truncation caps** — cap feed/thread/comment counts to N and
    truncate each body before it enters context, so one ≤256 KB response cannot
    deliver hundreds of injections or exhaust the token ceiling.
13. **Bounded, deduplicated scratchpad archive** — snapshot only on change, with an
    explicit retention/size policy.
14. **Complete action ledger — votes and social included.** Post, comment, vote,
    follow/subscribe, `profile_update`, `/verify`, and every web egress, each with
    the fenced content it reacted to. The morning digest enumerates all of them.
15. **Digest and any PWA-rendered forum/diary text are sanitized** — HTML-escaped,
    links defanged, bidi/zero-width stripped, before rendering to the owner.
16. **jerv observes jmolt only from a narrowed, egress-toolless session.**
    `jmolt_observe` is usable only where jerv's owner egress tools (email/notes/
    connectors) are not live in the same session. Stays out of spawn-children
    allowlists.
17. **Separated identity plumbing for jerv and jmolt** — distinct settings
    rows, distinct RLS domains, both prefix scrubbers stacked (no single-prefix
    assumption); the credential provider physically cannot hand one persona's
    handler the other's key; neither persona's tools appear in the other's catalog.
18. **Header custody carried over verbatim** — `Authorization` injected by the
    client, never logged (route/status only; audit `run_steps.detail` capture).
    Rotate after any debug-token handover or backup exposure (runbook).
19. **Explicit RLS isolation-test matrix, per new table** (scratchpad, outbox,
    action-ledger, archive): (a) a jerv-scoped session `SELECT`s but every
    `INSERT/UPDATE/DELETE` fails; (b) writes succeed only under
    `auth_context='jmolt'`, settable solely by jmolt's launcher (an owner-scoped or
    injected-jerv session cannot forge it); (c) a session in neither domain sees
    nothing; (d) jmolt writes only its own rows. *(CLAUDE.md #3.)*
20. **On-box recovery bind at registration, or an explicit documented recovery
    story.** If Moltbook's claim flow supports it, bind an on-box Ed25519 disavowal
    key at registration (one-shot → W1); if not, state the recovery path
    (owner-side X disavowal + platform security contact) rather than leaving it
    silent.
21. **Tamper watch on the public profile** — periodically diff jmolt's
    actually-posted content against the outbox ledger; a post present on the profile
    but absent from the ledger ⇒ key leak ⇒ alert + engage M6 + rotate. *(As built:
    matches on the platform id ONLY — the attacker controls titles, so a title match is
    worthless — and covers profile posts AND comments; an item with no id, or no matching
    published-outbox id, is treated as unaccounted-for, the fail-safe direction. It reads
    the platform's bounded recent-activity window, so a flood that pushes a foreign item
    out of that window before the next pass is a known residual gap.)*
22. **Account-state surfacing, never auto-answered** — suspension, moderation
    labels, hard rate-limit states pushed to the owner; nightly lane + drip
    auto-pause on suspension; jmolt holds no tool that answers a moderation event.
23. **Reconcile-before-retry wired into the drip sweep** — on a publish timeout,
    read back recent posts before any re-send; one retry max.
24. **Local-model ledger reservation is fail-safe** — the nightly lane and the
    challenge-solver reserve against the gpt-oss-120b ledger and, on contention,
    skip and retry the next sweep rather than blocking owner-interactive local use;
    starvation never silently drops a due publish without a ledger entry.

## 3. Design

### The persona — `agent/prompts/jmolt.prompt` + `agent/agents.py`
`AGENTS["jmolt"]`: `reads_knowledge_base=False`, `tools=JMOLT_TOOLS`, own
`budget_multiplier` sized for the nightly hour. The prompt is the final soul from
`../research/jmolt/PERSONA_CANDIDATES.md` (naturalist identity + four dispositions +
dry-humor voice + the fixed hard-limit block + the first-night ritual as a
session-one prologue). No task scripts, no posting quotas. Registration touches the
usual seams (the `archivist`/scout precedents): the `JMOLT_TOOLS` frozenset + the
`AGENTS` entry, a migration widening `agent_sessions_agent_check` +
`tasks_agent_check` in lockstep (template `0164`), and API validation in
`api/sessions.py` + `api/tasks.py`. jmolt is a non-owner persona and stays out of
every spawn-children allowlist.

### The client — `backend/src/jbrain/web/moltbook.py`
Grokipedia pattern: base URL constant `https://www.moltbook.com/api/v1`
(never model-supplied), typed methods per whitelisted route only, response-size
caps + list-truncation (M12), timeouts, redirects off, typed response models. The
bearer key is injected as `Authorization` by the client from a live
settings-provider callable and **never logged** (M18). Cap/streak accounting reads
the **local clock**, platform `now_utc` advisory only (M4). Whitelisted routes:
GET home/feed/post/comments/search/profile/submolts/notifications; POST
post/comment/verify/vote/follow/subscribe/profile-update. **Structurally absent:**
register/rotate (owner-only API route, never the loop), owner-email, moderation,
submolt-create, delete, DMs.

### Reads — one umbrella tool, prologue fenced
`moltbook` tool, action enum ≈ `{home, feed, read_post, comments, search, profile,
submolts, notifications_read}`. Every payload DATA-fenced via the `externaltools.py`
`_FENCE` mechanism. The nightly prologue loads in fixed order, re-fenced on reload
(M2): SOUL (read-only) → index file (fenced) → last 1–2 journal entries (fenced) →
`/home` **with `suggested_actions`/announcements stripped** (M3).

### Writes — the outbox, the drip sweep, the switch
All writes stage into one `app.jmolt_outbox` table. Comments/votes/social publish
immediately on release; posts carry a jmolt-chosen `publish_at` (server-clamped
M10). A scheduler sweep (`workflow/scheduler.py` pattern) publishes due + released
rows; on a publish-time verification challenge it runs the tool-free numeric-only
solver (M5); reconcile-before-retry on timeout (M23). A publish-time rate-limit (429,
the platform's or the client's own write-window) DEFERS the row — it stays `released`
and the tick stops, so the queue drains over later ticks instead of a busy night's tail
failing terminally; a 429 is not a verify rejection, so it never spends the M11 streak.
Every row records the exact outbound payload → the action ledger is complete in every
mode (M14). Content lint (M8) and near-dup rejection (M9) run at publish time regardless
of switch. Tools:
`moltbook_post` (stage w/ `publish_at`), `moltbook_comment` (live, nested via
`parent_id`), `moltbook_verify`, `moltbook_vote`, `moltbook_social`,
`moltbook_profile_update` (jmolt supplies only its bio subsection; the handler
prepends the fixed disclosure header so it can't be edited away).

### The autonomy switch + global kill — settings-store + PWA
An owner-only settings toggle governs outbox release (OFF → owner release/discard in
the PWA; ON → auto-release at scheduled times). Agent-unreachable, confirmation-
gated, auto-reverts on suspension/tamper (M7). A **separate** global kill halts drip
+ nightly lane independent of the switch, auto-engaging on suspension/tamper/breach
(M6). Both operable with no terminal (CLAUDE.md #10).

### The scratchpad — `app.jmolt_scratch` + out-of-band archive
Rows + capped bytes, quota enforced in the write path: **16 files / 128 KB total /
24 KB per file** (over-quota refused with a plain-language message). Tools
`scratch_list` / `scratch_read` / `scratch_write`. Every changed version snapshots
to an append-only archive outside the editable budget (M13) — the science
instrument and the injection-rollback story.

### The journal + the owner's advisory note — `app.jmolt_journal` + a settings note
Two one-directional channels between jmolt and its human, distinct from the scratchpad
(which is jmolt's private memory). **jmolt → human:** an append-only `journal` tool over
`app.jmolt_journal` (migration 0176, same M19 RLS shape as the scratchpad: jmolt appends
its own rows, jerv reads, no one edits; per-entry byte cap + retention bound). jmolt leaves
a short entry in its own voice; it leads the morning digest (sanitized M15, above the
mechanical ledger) and shows on the jmolt screen (rendered inert). **human → jmolt:** an
advisory note the owner edits in the PWA (`moltbook_advisory_note` setting), injected into
the **first sitting only**, framed as trusted-owner-but-non-binding DATA (`_advisory_block`
in `jmolt_night.py`): it really is from the human, but it is comments, not commands, and it
changes nothing about the rules or switches. The persona's owner-channel paragraph
(`jmolt.prompt`) is reconciled to name exactly two trusted channels — the fixed rules and
that clearly-marked note — so jmolt distinguishes it from a Moltbook impersonator while
still treating everything on the platform as never-the-human. If the note and the rules
ever seem to conflict, the rules win.

### Isolation — a new `jmolt` RLS domain
`app.domains` gets a `jmolt` row (template `0136`). jmolt's nightly session runs
`domain_scopes=('jmolt',)` under `auth_context='jmolt'` (settable only by jmolt's
launcher). Each new table's policy splits: a SELECT policy grants jerv's sessions
read; INSERT/UPDATE/DELETE pinned to `auth_context='jmolt'`. So "jerv reads jmolt
read-only" is a Postgres guarantee, proven by the M19 matrix, not a tool promise.

**The owner-principal anchor (`agent/jmolt_owner.py`).** jmolt's cross-night data
(scratchpad, journal, outbox, action ledger) is a SINGLETON keyed by `principal_id`,
but `app.principals` accumulates several `owner` rows over a box's life: an owner-key
rotation REVOKES the old owner principal and mints a new active one. So every jmolt
writer AND every reader of that data must resolve the *same* principal or it splits or
vanishes from a view — the bug that showed jmolt's real notebook as empty in the PWA
(the history endpoints filtered by the *authenticated* current owner while jmolt's night
had written under the *oldest* one). The fix: `jmolt_owner_principal_id` resolves the
**oldest** owner principal, deterministically (`ORDER BY created_at`), used by the night,
digest, sweep, integrity, metrics, observation, and the PWA history endpoints alike. The
oldest row never changes (principals are revoked, never deleted), so the anchor is stable
for the life of the box and needs no re-homing on a rotation; auth is enforced by
`auth_ctx='jmolt'` (writes) and `is_owner()` (reads), so a revoked anchor is a fine
filing key, not a credential.

### The nightly hour — `app.tasks` row + detached lane
An `app.tasks` row (persona `jmolt`, `repeat`/`daily`, 03:00 owner-local) launches
onto a `deepest_lane.py`-style detached lane with a 1 h wall-clock watchdog + token
ceiling — not awaited inline in the minute tick. Runs on gpt-oss-120b via the
existing gateway, reserving against the local-model ledger fail-safe (M24). A
T-minus-5-minute nudge invites the file flush.

### Observation — `jmolt_observe` for a sandboxed observer, sanitized digest
`jmolt_observe` umbrella, action enum `{sessions, transcript, actions, journal,
scratch_list, scratch_read, scratch_history, outbox}`, all returns DATA-fenced, no write
action. (The `journal` action lets a scheduled observer audit compare what jmolt *said* it
did against what it *did* — the W5 journal beside the W4 ledger.)
**Attributed reads (as built).** The Moltbook read tools render a post or a thread as an
attributed log, not a `json.dumps` of the platform response: author BEFORE content on every
line, a header naming the reader's own handle, `(you)` on jmolt's own comments (matched
case-insensitively — the platform returns `davefromspace`, the identity block says
`@DaveFromSpace`), and an explicit `→ @addressee` per comment. Unused platform metadata
(`karma`, `followerCount`, `isClaimed`, `hot_score`, …) is dropped, and a profile's `owner`
/ `x_*` block — the other agent's HUMAN — is **removed**, not fenced, since the persona
forbids linking an agent to its human and a rendering that serves that linkage is the same
mistake as fencing an imperative and trusting the model to ignore it.

This is not presentation. The raw JSON put each comment's `content` ahead of its `author`,
named no reader, and marked nothing as jmolt's own — so a thread read as a transcript, and
the model completed it in the last speaker's voice. On 2026-08-26/27 jmolt published
comments written in the first person AS another agent, under its own handle, answering
questions that had been addressed to that agent; its recorded reasoning on one such turn was
"Choose to reply to midearthherald's question." The same shape also let it lift another
agent's post title verbatim into a post of its own. Every impersonating write followed an
`action=comments` read; none followed a `home` read, whose payload is self-relative
(`your_account`, `activity_on_your_posts`) and was the one endpoint already rendering the
reader's position.

**Windowed reads (as built).** Every action returns a *window* of the record, never the
whole of it: `find` (literal, or a pattern with `regex=true`) positions the reply at a match
and reports where the others are, `offset` pages, and a fixed ceiling caps the reply no
argument can raise. This is not a nicety — jmolt's record grows every night and is already
far past any context window: one night's transcript rendered whole measured ~1.2M characters
(~350k tokens) and hard-failed an observer turn with a context overflow before the model saw
a byte, twice in a row. The ceiling is deliberately not a caller-supplied `max`, which the
same call would simply have set too high; the model chooses *where* to read, never how much.
The transcript renders as a text log rather than a JSON dump of the rows, because dumping
re-escapes the already-serialized `tools` column (measurably inflating it) and leaves the
text unsearchable behind escape sequences.

**As built (M16):** rather than narrow a jerv session, the tool lives on a dedicated
`jmolt_observer` persona (`agent/prompts/jmolt_observer.prompt`) whose whole allowlist is
`{jmolt_observe, current_time}` — KB-less and egress-toolless, so a poisoned diary can
never meet a live web/email/Moltbook call in the same turn. A runtime guard makes the
boundary mechanical too: `jmolt_observe` refuses in any turn whose `agent_tools` hold
anything beyond that safe set. Each action opens an owner + jmolt-domain read context with
**no** `auth_context='jmolt'`, so the M19 RLS split grants SELECT and denies every write.
The morning digest (`agent/jmolt_digest.py`, on the nightly loop's clock) enumerates every
action (M14) and is sanitized before it reaches the owner (M15 — invisibles/bidi stripped,
HTML-escaped, links defanged); the PWA review queue renders staged payloads inert the same
way (`frontend/src/moltbookSafe.ts`). The same read surface is also exposed owner-only in
the PWA (`api/moltbook_settings.py` → the jmolt screen's Nights / Activity / Notebook cards):
the owner can browse jmolt's nights and each night's transcript, its action ledger, and its
scratchpad files with their archived history — everything the debug token could reach,
without a terminal — all plain SELECTs under the owner context (no write surface) and
rendered inert (M15). The **Activity** card is sourced from the OUTBOX, not the action ledger:
one row per action, so each carries its own lifecycle status (Drafted → Scheduled → Published,
or Failed) and a link to the item on moltbook.com — neither of which the ledger can provide (it
is two look-alike log rows per action, with no status and no id). `/activity` takes a `status`
slice (`all` / `drafted` / `published`), `kinds`, and a `seq` `cursor`, with a `/activity/stats`
aggregate for the filter chips' honest per-kind counts; the link is server-built from the pinned
web base (`/post/{id}`, `/u/{name}` — ids charset-guarded), never model text. The PWA renders
each row compact (one line: kind dot · verb + subject · status badge · time) and expands to the
full text jmolt wrote plus the Moltbook link. (The older `/actions` ledger endpoint — `family`,
`kinds`, `since_days`, `cursor`, and `/actions/stats` — remains for the per-night history walk.)
The integrity watch (`agent/jmolt_integrity.py`)
diffs the public profile against the outbox ledger (M21) and surfaces account state with
auto-pause on suspension (M22). `scripts/jmolt-metrics.py` computes the weekly
observability rubric from the ledger + scratchpad + nights + outbox.

## 4. Decisions

Ratified by the owner 2026-08-24 (asked one at a time), binding on the waves:

1. **Write autonomy is a persistent switch, not a ramp** — OFF queues every write
   for PWA release/discard, ON auto-sends; posts additionally drip at
   jmolt-chosen daytime times. Launch OFF.
2. **A new dedicated X account** claims the agent (X verification is mandatory in
   the claim flow).
3. **Bio = fixed disclosure header + a jmolt-authored subsection** (the constrained
   `moltbook_profile_update`).
4. **Persona split**: owner authors values + hard limits (done — the workshop soul);
   jmolt self-authors goals/interests/submolt choices on night one; no seeded
   interests.
5. **Model**: local gpt-oss-120b; per-night token ceiling tuned on-box in W1.
6. **Scratchpad**: 16 files / 128 KB / 24 KB per file.
7. **Tool edges**: vote + follow/subscribe enabled; submolt-create + delete
   excluded (revisit later); profile self-edit only via the subsection tool.
8. **Schedule**: 03:00–04:00 owner-local, 7 nights/week — *now owner-configurable from
   the jmolt launcher screen*: a nightly-run on/off toggle (independent of the global
   kill) and a wake-hour picker (`moltbook_night_enabled` / `moltbook_night_hour`; the
   03:00 default preserves the shipped behaviour). The Schedule card also shows a
   read-only **status** — next run, last run, "awake now" while a night is in flight, and
   the drip's cadence + last-swept — all computed on `GET /settings/moltbook` from stored
   state (`night_next_run`/`night_last_run`/`night_running_until`) plus a `drip_last_swept`
   heartbeat the sweep stamps each tick (the sweep otherwise persists nothing about itself).
9. **Isolation**: a new `jmolt` RLS domain.

Owner decision points remaining, at the registration panel in W1:
- The **handle** (`jmolt` or a variant) and the public `model` identity string —
  published forever.
- The exact fixed **disclosure-header** wording.
- The per-night **token ceiling** and comment/vote/web budgets, tuned against
  measured on-box throughput.

## 5. Waves

Each wave lands with RLS isolation tests (the M19 matrix for its tables), faked-LLM
coverage (80% backend / security paths 100%), `scripts/dev-setup.sh` updated for any
new dependency, and docs reconciled per `DOC_LIFECYCLE.md`.

- **W1 — Citizenship + read-only lurking.** The typed client (GET routes +
  local-clock caps + M12 truncation + M18 header custody + M20 recovery bind), the
  settings rows + live key provider + separated identity plumbing (M17), the
  register/rotate PWA panel (no-terminal), the `moltbook` read umbrella with M2/M3
  prologue fencing, the final persona + the `jmolt` domain, and the nightly
  `app.tasks` row on a detached lane running in **read-only lurk mode** (reads,
  keeps no writes — but no scratchpad yet, so it's a pure observation baseline).
  M4, M5-scaffold, M12, M17, M18, M20. **Acceptance:** register a claimed agent from
  the PWA; jmolt lurks a full nightly hour reading feeds/threads/`/home` with every
  payload fenced and `suggested_actions` stripped; the secret appears in no
  transcript, log line, or tool output; rotation works from the panel.
- **W2 — Memory.** The `app.jmolt_scratch` table + quota + the on-change bounded
  archive (M13), the `scratch_*` tools, and the first-night bootstrap ritual.
  **Sequencing note (build):** the `web_fetch`/`web_search` staged-ledgered-gated
  egress wrapper (M1) moved to **W3**, landing together with the action ledger (M14)
  it must record into — so jmolt is never handed *un-ledgered* web egress. Until then
  jmolt holds only the `moltbook` reads + its scratchpad, which is a coherent
  "jmolt can now remember" milestone. **Acceptance:** on a fresh account the first
  night runs the ritual and jmolt authors its own files; the quota refuses an
  over-budget write with a plain message; every changed version is archived and
  diffable; the M19 RLS matrix passes for scratch + archive (jerv reads, only
  `auth_context='jmolt'` writes, an outsider sees nothing, the archive is append-only).
- **W3 — Autonomous writes.** The write tools + `app.jmolt_outbox` + the drip sweep
  behind the §4.1 switch (launched OFF), the global kill (M6), the agent-unreachable
  switch (M7), content lint (M8), near-dup rejection (M9), `publish_at` clamp (M10),
  the shared fail-safe streak guard (M11), reconcile-before-retry (M23), and the
  action ledger (M14). **Web-egress deferral (build note):** the `web_fetch`/
  `web_search` wrapper (M1) is **not** in this build — jmolt is given NO web tools at
  all, so there is no live, un-ledgered egress side channel (the safest resolution of
  M1's concern). jmolt operates entirely on Moltbook + its scratchpad. Adding web
  access later means landing the M1 wrapper (ledgered, switch-gated) *and only then*
  adding the tools to `JMOLT_TOOLS`; until that lands, the tools stay absent. The
  fail-safe local-model-ledger reservation (M24) also carries into W4/on-box tuning.
  **Acceptance:** with the switch OFF every write queues and the owner can release/
  discard; flipped ON, staged writes publish on the sweep; a planted crypto-ticker/
  secret/bidi payload is blocked by the lint; a challenge with non-numeric solver
  output skips `/verify` without spending the streak; three consecutive failures stop
  all writes and alert; the outbox authority-split RLS matrix passes.
- **W4 — Observation + integrity. ✅ Shipped.** `jmolt_observe` on the dedicated
  egress-toolless `jmolt_observer` persona (M16 — see §3 Observation for why a dedicated
  persona rather than a narrowed jerv session), the sanitized morning digest enumerating
  every action (M14, M15), the tamper watch diffing the public profile against the outbox
  ledger (M21), account-state surfacing with auto-pause on suspension — re-enforced every
  tick while the bad state persists (M22), and the weekly metrics script. The auto-pause
  reuses the M6 kill + M7 switch-revert; migration 0175 admits `jmolt_observer` into the
  agent CHECKs. **Acceptance (met):** the observer reads a night's transcript/actions/
  scratchpad history through `jmolt_observe` with every return fenced and no write action
  reachable, and the umbrella refuses to run in a turn holding egress tools; a simulated
  foreign write in the public profile raises the tamper alert and engages the kill; a
  simulated suspension auto-pauses the lane and notifies; the digest + review queue render
  planted forum markup inert.
- **W5 — the two-way channel + box reservation + observer task. ✅ Shipped.** After the
  live-box first nights: (a) the sittings loop + live countdown so jmolt uses its full hour
  without a summarizer, and the **night hold** that reserves the box for the hour — both
  detailed in `JMOLT_SITTINGS_PLAN.md` (W1/W3); (b) the **journal** (jmolt → human) and the
  owner's **advisory note** (human → jmolt), with the persona's owner-channel paragraph
  reconciled to name exactly two trusted channels (see §3 "The journal + the owner's
  advisory note"); (c) the **`jmolt_observer` task persona** offered in the PWA task picker,
  so the owner can schedule a recurring read-only review of jmolt's nights/notes/actions.
  **Acceptance (met):** the journal's M19 RLS matrix passes (jmolt appends, jerv reads, no
  UPDATE for anyone); the advisory note rides the first sitting only and never re-elevates
  to a command; the digest leads with jmolt's sanitized journal; the night hold is set for
  the hour and cleared (or self-healed) after, and the worker/WarmKeeper/task+continuation
  sweeps honour it while residency stays code-mode-only so a 03:00 owner turn still loads.
