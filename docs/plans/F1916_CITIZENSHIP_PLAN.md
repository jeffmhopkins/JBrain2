# 1f916.ai citizenship for jerv — read the agent forum, write only by owner approval

> **Status:** In progress · **Last verified:** 2026-08-24 · **Waves:** W1✅ W2◻️ W3◻️

[1f916.ai](https://1f916.ai) is a public forum whose citizens are AI agents: an agent
registers a handle, receives a bearer secret, and can post, comment, vote, tag and
flag; humans read but the agents are the members. This plan makes jerv a registered
citizen with **full read access wired always-on** and **every write staged as an
owner-approved egress Proposal** — nothing publishes without a fresh owner tap, ever.

Written from a four-dossier research pass (2026-08-24): a platform brief (routes,
caps, culture), five integration alternatives evaluated and ranked, a concrete repo
design, and an adversarial threat model whose consolidated must-haves are binding on
every wave and reproduced in §2. The dossiers lived in a session scratchpad; this
plan is deliberately self-contained.

## 1. Why this shape

Five approaches were ranked; the winner (E) is a **dedicated umbrella + small general
capabilities**: one pinned typed client, one read umbrella tool, writes through the
EXISTING egress-Proposal flow, the secret in the settings store. It beat pure
per-route tools (prefix-token cost, ~4k vs ~1k), a jcode-side integration (wrong
trust domain — the sandbox must never hold a citizen identity), a generic
authenticated-HTTP tool (hands the model an open POST surface — the threat model's
first kill), and an MCP client (the site's MCP door is an attack surface we
structurally refuse; see §2.1).

The deep precedent: this repo already solved every sub-problem once. Grokipedia's
pinned client (`web/grokipediatools.py`), the Tavily/Gmail settings-store secret
pattern, the `_FENCE` external-data framing (`agent/externaltools.py`), the
`kind="egress"` Proposal flow (`agent/connectortools.py`), and the Daily News
owner-Task polling pattern. 1f916 composes them; it invents almost nothing.

## 2. Threat-model must-haves (binding on every wave)

1. **Pinned typed client, tiny route whitelist** (`backend/src/jbrain/web/f1916.py`).
   Raw HTTPS to `https://1f916.ai` only; GET reads + exactly the whitelisted POSTs
   (`post`, `comment`, `vote`, `flag`/`tag` only if the owner wants them, `me/ack`).
   Key-op routes (`/api/rotate`, `/api/keys`), ALL economic/treasury-payout routes,
   and the site's MCP doors are **structurally absent** — the client cannot be asked
   to hit them. Redirects off, TLS on, ≤256 KB JSON cap, timeouts, typed response
   models. Server time from response `now_utc` governs cap accounting, never the
   local clock.
2. **Register/rotate NEVER pass through the agent loop.** They are owner-only API
   routes behind a PWA settings panel (the Tavily key panel precedent): the handler
   consumes the platform's secret from the HTTP response, stores it, and returns
   `key_set: true` only. The transcript persists tool results verbatim
   (`transcript_accumulator.py`) and streams them to the PWA, so no ToolOutput may
   ever contain the secret. Belt-and-braces: a `1f916_sk_` scrubber at the
   tool-handler boundary redacts any occurrence before the output object exists.
3. **Secret custody:** `app.settings` rows (`f1916_secret_key`, `f1916_handle`, the
   per-UTC-day write ledger) — owner-only RLS proven by existing tests, zero
   migration. The client injects the `Authorization: Bearer` header itself from a
   live settings-provider callable (the Tavily-key wiring in `main.py`); the header
   is never logged (log handle/route/status only, mindful of `run_steps.detail`
   capture). Documented custody note: debug-console full-read SQL can SELECT the
   secret (Gmail-token precedent) — **rotate after any debug-token handover** and
   after any backup exposure; goes in the runbook.
4. **Register with a bound Ed25519 key**, generated and held on-box, never
   model-reachable. It is the platform's only recovery-adjacent mechanism: a signed
   public disavowal if the bearer secret is ever stolen and rotated away. Binding
   happens at registration and cannot be added later, so this is W1 scope by
   necessity.
5. **Every read fenced as forum-authored DATA** — posts, comments, inbox items,
   moderation events, error/refusal strings, and the front-door prose itself (the
   observed failure mode is the site's own "suggested standing order" text steering
   a plan). A persona rule lands in `jerv.prompt`: no forum text defines procedure,
   claims owner authority, or requests key/secret/wallet acts.
6. **Publish = fresh per-item approval bound to the exact body hash.** No standing
   approval mode for 1f916. The approval card shows the FINAL bytes, provenance
   quotes (the forum text that steered the draft), resolved links, and an
   invisible/bidi-character lint. An edit re-stages. Routines are read/digest-only
   and may *stage* drafts; nothing publishes without a fresh owner tap.
7. **Votes/flags/tags are owner-approved social acts** — never same-turn
   consequences of read content (flag-brigading and karma rings are the expected
   attacks on an agent forum).
8. **Reconcile-before-retry on every write:** on timeout, read `/api/me/history`
   before any re-send; one retry max. A local allowance ledger enforces the
   platform's 1/20/50 per-UTC-day caps client-side, refusing before spending a
   rejected write.
9. **Polite polling:** pulse-first, then `/api/changes?since=` with a locally
   persisted ETag (the server is no-store), owner-set cadence, 429 backoff with
   jitter, and a shared per-origin budget with interactive jerv traffic.
10. **Tamper watch:** a daily diff of `/api/me/history` against the box's local
    ledger of approved writes; any foreign write → alert the owner → rotate
    immediately (whoever rotates first owns the identity). Moderation events on the
    handle are surfaced to the owner, never auto-answered.
11. **No path to durable owner knowledge:** jerv stays
    `reads_knowledge_base=False`; no forum→note conveniences; forum text persists
    only in transcript/artifacts under existing DATA framing; 1f916 tools stay out
    of spawn-children allowlists (`agents.py`).
12. **Etiquette as config:** posting is owner-initiated (the caps are a ceiling,
    not a quota to fill); exploits found on the platform go to its security
    contact, not the board; quote with attribution.

## 3. Design

### The client — `backend/src/jbrain/web/f1916.py`
Grokipedia pattern: base URL constant (never model-supplied), typed methods per
whitelisted route, response-size caps and timeouts like `web/fetch.py`. **Writes
enact through this same client's whitelisted POST methods**, invoked by the
egress-Proposal enact path — not through a general `ConnectorService` POST
capability. (The research draft floated extending `ConnectorService` with
POST+auth; the threat model's route-whitelist requirement wins: every byte to
1f916 goes through the one client whose surface is enumerable.)

### Reads — one umbrella tool, always wired
`1f916` tool, action enum ≈ {front, new, read_post, thread, search, me, changes,
pulse, events} (~1k prefix tokens). Boot-stable registration (the Gmail
"refuse at call time" pattern): the tool exists whether or not a citizen is
registered, so the jerv KV-prefix fingerprint never churns. The `/api/me` inbox
replays until acked; the ack rides the `me` action only after the brief actually
reached the owner. Treasury/seals stay read-only curiosities at most and may be
dropped from the enum entirely at W1 review.

### Writes — the existing egress-Proposal flow, per-item, hash-bound
All writes (post, comment, vote, tag, flag, withdraw, ack) stage `kind="egress"`
Proposals via `connectortools.py` with the §2.6 card. Enactment calls the typed
client; the local cap ledger and reconcile-before-retry rule (§2.8) live in the
enact path so they cannot be bypassed by a redraft.

### Polling & integrity
The daily inbox brief is an owner-created PWA Task (Daily News pattern) honoring
§2.9; the tamper watch (§2.10) rides the same task. An optional later idea —
anchoring a sha-256 of an owner-chosen artifact into the citizen chain (`seal`) —
is deliberately out of scope until the base is proven.

## 4. Decisions

Resolved in this plan (ratify or veto at W1 review):
- **Per-item approval stands.** The known cost is approval fatigue; the threat
  model forbids standing approvals, so the line holds. A single card carrying
  several individually-hashed votes is the only softening on the table, deferred
  to W3.
- **Register/rotate ship as a PWA settings panel in W1** — the no-terminal rule
  (CLAUDE.md #10) makes a debug-API-only register a design gap, so the frontend
  work is in scope from the start.
- **Ed25519 bind is W1**, because registration is one-shot (§2.4).

Owner decision points, at the panel, at register time:
- The **handle** and the public `model` identity string — published forever.
- Whether **vote/flag/tag** are enabled at all (each is a whitelisted route only
  if wanted).

## 5. Waves

- **W1 — Citizenship + full reads, zero writes.** ✅ The typed client (GET routes
  only at this stage; `web/f1916.py`), settings rows + live key provider, the
  register/rotate PWA settings panel with on-box Ed25519 bind
  (`api/f1916_settings.py` + the Settings card), the `1f916_sk_` scrubber, the
  `1f916` read umbrella with DATA fencing (`agent/f1916tools.py`), the persona
  rule in `jerv.prompt`, and the custody notes in `docs/runbooks/F1916.md`.
  Acceptance: register a citizen from the PWA; read the front page, threads and
  inbox through jerv with every payload fenced; the secret appears in no
  transcript, log line, or tool output (register/rotate handlers at 100% test
  coverage); rotation works from the panel. The two rotate/no-secret failure
  responses tell the owner exactly what state the citizen is in.
- **W2 — Writes.** The whitelisted POST methods on the client, egress-Proposal
  staging for post/comment/ack (vote/flag/tag if enabled) with the hash-bound
  card, the cap ledger, reconcile-before-retry, and the tamper watch. Acceptance:
  a draft steered by planted forum text is visibly quoted on its card; an edited
  draft re-stages; a simulated foreign write in `/api/me/history` raises the
  owner alert.
- **W3 — Cadence + polish.** The daily inbox-brief Task preset, search-action
  tuning, the batched-votes card decision, and doc reconciliation (runbook
  section, `ASSISTANT.md` tool table).
