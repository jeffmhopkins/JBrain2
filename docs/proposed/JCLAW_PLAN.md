# jclaw — Multi-Channel Assistant Gateway (Design Sketch)

> **Status: proposed, not scheduled.** Forward-looking design dropped in for the
> record; nothing here is built and it is not on the current roadmap (the active
> frontier is Phase 6, the wiki). When/if picked up it must be reconciled with the
> root `CLAUDE.md` non-negotiables (LLM adapter, storage abstraction, RLS +
> isolation tests, Conventional Commits + PR + green CI).
>
> **Scope guard for this cut: no internal-DB access.** jclaw is specified here as
> **knowledge-base-blind** — it reaches neither the RAG store, the entity graph,
> the wiki, nor any owner data in Postgres. It is a *channel + sandboxed agent
> loop* only. The KB read path and any write-back are deferred to a later phase
> (see §7). This mirrors how `JCODE_PLAN.md` and the email archivist ship: an
> isolated sidecar that "reads no knowledge base."

jclaw is JBrain2's take on the OpenClaw pattern: **message your own assistant from
any chat app** (Telegram, Signal, Slack, WhatsApp, iMessage, …) instead of only
through the PWA's Full Brain surface. A self-hosted gateway maps an inbound
channel message to a JBrain2 agent turn running inside a per-session sandbox, and
maps the reply back out to the channel.

The point of *this* sketch is the sandbox: OpenClaw runs its agents in
Docker-isolated, network-policed containers; Claude Code on the web and Grok's
agent runs do the same with ephemeral cloud containers. JBrain2 already has that
substrate on-box (the jcode per-session sandbox, the web-gated sub-agent runner).
jclaw reuses it rather than inventing a new one.

---

## 1. Core idea

> **Recommended first cut: CLI-only.** A local terminal is the lowest-ingress
> channel — no Cloudflare Tunnel, no webhook, no provider polling. Running
> CLI-only also collapses the §5 impersonation surface entirely: there is no
> spoofable chat handle to resolve because the owner is already authenticated by
> shell access on the box (exactly like `jcode` / Claude Code's own CLI). The
> sandbox and KB-blind allowlist are unchanged — the CLI is just the outer shell.
> Messaging channels (§6) are a later, higher-ingress adapter over the same core.

A **channel gateway** process owns the connections to messaging providers and
does nothing intelligent itself. For each inbound message it:

1. Resolves the sender to the single owner (JBrain2 is single-tenant; anyone else
   is rejected — see §5).
2. Opens or resumes a **per-channel-thread session**, each backed by its own
   sandbox container.
3. Runs one JBrain2 agent turn *inside* that sandbox against the LLM adapter, with
   a **narrow, allowlisted toolset** (§4) — **not** the full `jerv` toolset.
4. Streams the reply back to the channel.

The gateway is the blast-radius boundary. The agent loop never has ambient access
to the host, the DB, or secrets — only what the sandbox and the tool allowlist
grant it.

```
 chat app  ──▶  channel plugin  ──▶  jclaw gateway  ──▶  per-thread sandbox
(Telegram…)     (adapter iface)      (owner auth,          ├─ LLM adapter (routing)
                                      session map)          ├─ web tools (policy-gated)
      ◀──────────────  reply  ◀───────────────────────────  └─ NO db, NO storage, NO KB
```

---

## 2. Why a sandbox (and which one)

| Approach | Isolation | Fit for jclaw |
|---|---|---|
| OpenClaw native | Docker container per session/agent, network + fs policy | The reference model we're copying. |
| Claude Code web / Grok | Ephemeral managed cloud container, egress policy | Same shape, vendor-hosted. |
| **jcode sidecar (ours)** | Per-session isolated git checkout + seccomp-profiled container, web-gated egress | **Reuse this.** Already on-box, already owner-scoped. |

jclaw does **not** need jcode's git checkout — it needs jcode's *isolation
envelope*: a container per session with (a) no host filesystem, (b) no Postgres
socket, (c) outbound network only through the same policy-gated egress the
web-sandboxed sub-agents already use. The scoping is *tighter* than jcode, not
looser: jcode edits files, jclaw touches nothing on disk.

`scope` mirrors OpenClaw's `session` vs `agent`: default **one sandbox per channel
thread**; a future multi-persona mode could go per-persona.

---

## 3. Non-negotiable alignment

Even KB-blind, jclaw touches the two universal firewalls:

- **LLM adapter (NN-1).** All model calls route through the adapter — same
  routing (local `gpt-oss-120b` vs cloud) the Full Brain uses. jclaw adds a
  channel; it does not add a provider SDK.
- **Storage abstraction (NN-2).** jclaw writes no notes and no files in this cut,
  so it holds *no* storage handle at all — the cleanest possible compliance.
- **RLS domain firewalls (NN-3).** By construction jclaw has **no DB session**,
  so health/finance/location isolation is enforced by *absence of the socket*, not
  by policy it could misconfigure. When the KB path is added (§7) it must arrive
  with an RLS-scoped session and isolation tests like any other table-touching code.

---

## 4. Tool allowlist (this cut)

Deliberately minimal — a conversational + web assistant, nothing that reads or
mutates owner state:

| Allowed | Rationale |
|---|---|
| plain conversation (LLM adapter) | the base capability |
| web search / fetch, **policy-gated** | same egress control as web sub-agents |
| scratch compute inside the sandbox | ephemeral, discarded on session end |

Explicitly **excluded in this cut** (deferred to §7): `search_notes`, entity-graph
reads, wiki reads, Proposals/notes writes, connectors, memory. If the owner asks
jclaw something about their notes, it answers "I can't see your knowledge base yet
from this channel" rather than reaching for a tool it doesn't have.

---

## 5. Auth & the channel trust problem

The hard security surface is **impersonation**: a chat channel identifies senders
by provider handle, which is spoofable/transferable and is *not* a JBrain2
session.

- Each channel binding stores a **pre-registered owner handle** (e.g. a specific
  Telegram user id) minted by the owner from the PWA — never discovered from an
  inbound message.
- First contact from an unknown handle is **rejected and logged**, never
  auto-enrolled.
- Optionally, a rotating **pairing code** the owner reads from the PWA confirms a
  new channel before it's trusted.
- Inbound message bodies are **untrusted external input** (prompt-injection
  surface). The narrow allowlist (§4) is the primary mitigation: with no KB/DB/
  storage tools, a hostile message can at worst burn web-tool budget in a throwaway
  sandbox.

---

## 6. Shape on the box

- One **gateway** service (Python, matches JBrain2), fronted by the existing named
  Cloudflare Tunnel + Caddy for any providers that need an inbound webhook; polling
  providers (e.g. Telegram long-poll) need no ingress.
- Channel adapters behind a single interface (`ChannelPlugin`: `recv → normalized
  message`, `send(reply)`), so providers are pluggable the OpenClaw way. **A local
  CLI is the first adapter** — stdin→message, stdout→reply — and needs neither the
  gateway service nor Caddy/Tunnel; it runs the sandbox loop directly.
- Sessions are **ephemeral and stateless on the box** — no `jclaw_*` table in this
  cut. Thread→session mapping lives in memory / a TTL cache; losing it just starts a
  fresh sandbox. (A persistent thread history table, if ever wanted, is owner-RLS
  and arrives in §7.)

---

## 7. Deferred (explicitly out of scope now)

1. **KB read path.** Give jclaw a *read-only, RLS-scoped* view of notes / entity
   graph / wiki. This is the real prize and the real risk: it re-introduces the
   domain firewalls jclaw currently sidesteps by having no socket. Requires an
   RLS session, isolation tests, and a decision on whether cloud LLM routing is
   allowed to see KB content per domain (cf. the email-archivist routing question).
2. **Write-back.** Turn a chat message into a Proposal (the existing review-inbox
   primitive) rather than a direct note — humans correct via notes, never direct
   edits (NN-7).
3. **Persistent per-thread memory** (owner-RLS table + isolation test).
4. **Multi-persona routing** (jerv / archivist / intake over channels).

Each deferred item is a phase gate, not a fast-follow — every one re-opens a
firewall this cut keeps shut.

---

## 8. Open decisions (owner sign-off before any build)

- Which channels first? (Telegram polling is the lowest-ingress, lowest-risk start.)
- Is a **cloud LLM** ever allowed on channel traffic, or local-only like jcode?
- Pairing-code confirmation: required, or is a pre-registered handle enough?
- Is the KB-blind cut worth shipping on its own, or only as step 0 of the §7 KB path?
