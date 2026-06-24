# Email Archivist — build plan (a sandboxed Gmail persona)

A new Full Brain persona — **`archivist`** — whose job is to triage and organize a
20+ year Gmail history. It is built as a **sandbox** in the exact shape of `jerv`
(`docs/ASSISTANT.md` "Agent selection"): it reads **no** knowledge base, holds
**no** owner domain data, touches **no** owner table, and stages **no** notes. Its
only tools talk to Gmail; its only other dependency is the LLM adapter
(non-negotiable #1). Email is **never** ingested into the RAG corpus in this plan —
notes-from-email is a deliberately deferred second step.

This binds on top of `docs/DEVELOPMENT.md`, `docs/PROCESS.md`, and the `CLAUDE.md`
non-negotiables.

## What "no DB" buys us, and the one honest asterisk

Because the persona stores nothing on the box, this feature adds **no migration, no
table, and therefore no RLS isolation test** — the heaviest parts of a normal JBrain2
feature simply evaporate. The persona's "no DB access" is the same guarantee `jerv`
already makes: `reads_knowledge_base=False`, empty read scopes, no
note/entity/list/appointment/memory tool, no token table. (The interactive chat's
own run-log row is harness telemetry, not owner knowledge — the same as every jerv
turn. The nightly headless path below avoids even that.)

**The asterisk (recorded, not hidden):** `jerv` is allowed to egress *directly*
because it holds no owner data, so nothing sensitive can ride along into a request.
Gmail inverts that — the inbox **is** the owner's most sensitive data, and "borrowing
LLM access" means email content flows through the LLM adapter to whatever provider the
router points at. **In this design the LLM call is the real egress**, even though no
DB is touched and no Proposal is staged. That makes model routing an owner decision,
not an implementation detail (see the open decision below). Gmail *writes*, by
contrast, act only on the owner's **own** mailbox — not a leak, but an autonomous
mutation, which is why v1 writes are confined to reversible label/archive operations.

## Owner decisions

| Decision | Choice | Consequence |
|---|---|---|
| **Persona shape** | A 4th persona `archivist`, modeled on `jerv` | `reads_knowledge_base=False`, empty scopes, tools = Gmail-only allowlist |
| **Transport** | A thin `jbrain.gmail` client over **httpx** against the Gmail REST API — no Google SDK | Matches the `SearxngClient`/`WebFetcher` "thin client" pattern; no heavy new dependency |
| **Auth** | OAuth2 **refresh token + client id/secret in config** (env), like `mqtt_ingest_secret`; a one-time bootstrap script mints the refresh token | No token table, no DB; single-owner box, so a config secret is coherent |
| **OAuth scope** | `gmail.modify` only — read + create-label + label + archive. **No delete/trash scope requested** | Writes are non-destructive and reversible by construction; permanent delete is out of reach even if asked |
| **Organization model** | Gmail **labels, not folders** — the agent builds and applies a label taxonomy (Gmail's `Parent/Child` nesting). "Move" = apply a label + remove `INBOX` | Matches how Gmail actually organizes; a synced client shows the result as folders |
| **Tool primitives (v1)** | `gmail_search`, `gmail_read`, `gmail_list_labels`, `gmail_create_label`, `gmail_label` (apply/remove → move into a label), `gmail_archive` (remove `INBOX`) | A clean primitive set; the organizing *tasks* are built on top of these later. "Move" in Gmail = label + un-inbox; both reversible. Destructive ops deferred |
| **Permission class** | `web` (direct-exec, opt-in gate), allowlisted to `archivist` only | Runs directly (no Proposal), exactly like jerv's web tools; `curator` never gains them |
| **Persistence** | **None on the DB.** The nightly cursor lives in a single **storage-abstraction blob**; Gmail's own labels are the real state | Honors "no DB access"; storage is the sanctioned file-I/O path (non-negotiable #2) |
| **RAG ingestion** | **Out of scope.** Email never becomes a note in this plan | The notes/RLS/ingest surface stays untouched; a clean follow-on if desired |

### Open decision (owner sign-off before Wave 1)

- **LLM routing for email content.** The `archivist`'s turns send private email
  through the LLM adapter. Either (a) route this persona's task profile to a
  **local model** so content stays on-box, or (b) accept the configured cloud
  provider for email. This is the one genuine privacy fork and it is **not** locked
  here.

### Escalation-worthy decision (recorded)

The Gmail tools are placed in the **`web`** permission class (policy outcome
`direct`), allowlisted to `archivist` only — the same precedent as jerv's
`web_search`/`web_fetch` and the `current_location`/`generate_image` extensions
(`agent/contracts.py`: a tool "in this class purely for its gate"). This is a
**deliberate widening of the direct-egress exception (#9)** from "public reads with
no owner data in context" to "an owner-authorized single account." The widening is
bounded: one persona, one owner-configured account, write scope capped at reversible
label/archive (no delete). The alternative — routing every Gmail call through a
staged egress Proposal — is the correct model for *cross-domain knowledge* writes but
is the wrong grain for triaging tens of thousands of messages, and is **not** taken.
If the owner later wants per-batch human approval, the nightly path can emit a batch
egress Proposal instead of acting directly (noted in Wave E3).

## Wave split

- **Wave E1 — Gmail client + auth** (no GUI, no DB): the `jbrain.gmail` client over
  httpx (OAuth refresh-token → access-token mint, typed search/read/modify calls), the
  `gmail_*` config fields, a one-time **OAuth bootstrap script**, and a `FakeGmail`
  for tests. `dev-setup.sh` updated for the new env vars + bootstrap step (#8).
- **Wave E2 — tools + persona** (no GUI): the six `.tool` sidecars + handlers (thin
  over the client), the `archivist` persona + its allowlist in `agents.py`, the
  `archivist.prompt` system prompt, and the `web`-gate wiring in `main.py`. This is
  the **interactive agent session** — the user's "first step."

**Scope of this plan = E1 + E2: the persona and the tool primitives.** The
organizing *tasks* built on top of the persona — nightly/batch runs, how the label
taxonomy is decided, dry-run, cursor/checkpointing — are **designed separately,
later** (see "The task layer (deferred)" below). The foundation deliberately stops at
a clean, reusable set of Gmail primitives so any number of tasks can be built on it
without reopening the tool surface.

Per `PROCESS.md`: each wave runs its tasks in parallel worktrees off a `wave-N`
branch, gets an independent per-task review and a per-wave review, and lands as
exactly one PR per wave, CI green before merge.

---

## Wave E1 — Gmail client + auth

### `jbrain.gmail` client (the transport chokepoint)

A small module, `backend/src/jbrain/gmail/client.py`, that is the **only** place an
HTTP request to Google is made (the spirit of "no tool makes a raw HTTP request").
It is thin over `httpx.AsyncClient`, mirroring `jbrain.web.search.SearxngClient`:

- `__init__(client_id, client_secret, refresh_token, *, base_url, transport=None)` —
  `base_url` pinned to `https://gmail.googleapis.com/gmail/v1`, never model-supplied;
  `transport` injectable so tests need no network.
- `_access_token()` — exchange the refresh token at Google's OAuth token endpoint,
  caching the short-lived access token in memory with its expiry; refresh on miss.
  Never persisted.
- `search(query, *, max_results)` → message ids/threads (Gmail `users.messages.list`).
- `get(message_id)` → typed `GmailMessage` (subject, from, to, date, snippet, body
  text — HTML stripped to text in the client, not the handler).
- `list_labels()` → the account's labels (id + name, including nesting).
- `create_label(name)` → a new label (Gmail `users.labels.create`, a POST), using the
  `Parent/Child` name convention for hierarchy; returns its id. Idempotent at the
  handler layer (resolve-or-create), so re-running never duplicates a label.
- `modify(message_id, *, add_label_ids, remove_label_ids)` → the message write call
  (Gmail `users.messages.modify`, a POST with a JSON body). Archive = remove `INBOX`.

A `GmailError` mirrors `WebFetchError`/`WebSearchError` so handlers surface a clean
message instead of a stack trace. A `FakeGmail` (same interface, in-memory message
store) drives the handler and loop tests with scripted mailboxes.

### Config (`config.py`)

Four new env-backed fields, defaulting empty (fail-closed — empty `gmail_refresh_token`
disables the persona's tools, the same pattern as `comfyui_url`/`mqtt_ingest_secret`):

```
gmail_client_id: str = ""
gmail_client_secret: str = ""
gmail_refresh_token: str = ""
gmail_api_url: str = "https://gmail.googleapis.com/gmail/v1"
```

The `archivist`'s model routing is the open decision above; if (a), add a task-profile
entry pinning this persona's turns to a local model.

### OAuth bootstrap (no prior art in the repo)

A one-time, owner-run script (`scripts/gmail-oauth-bootstrap.py`) that runs the
installed-app authorization-code flow: prints the consent URL for the
`gmail.modify` scope, takes the pasted code, exchanges it for a refresh token, and
prints the three env values to paste into the box's config. It writes nothing to the
DB and is never part of the request path. Documented in `dev-setup.sh` and
`docs/OPERATIONS.md`.

### Tests

`FakeGmail`-driven unit tests for the client's token-refresh/cache logic and each
call's request shaping (search query, modify body); a transport stub asserts the
pinned base URL and that no field is model-supplied. **No RLS test — no table.**

---

## Wave E2 — tools + persona (the interactive session)

### The five `.tool` sidecars + handlers

Co-located `.tool` sidecars (`agent/tools/gmail_*.tool`), each `permission: web`,
each version CI-guarded, with handlers in a new `agent/gmailtools.py` thin over the
client (the `build_web_handlers` pattern):

| Tool | Params | Returns |
|---|---|---|
| `gmail_search` | `{query, limit?}` | matching messages (id, from, subject, date, snippet) |
| `gmail_read` | `{message_id}` | the full message as text (headers + body) |
| `gmail_list_labels` | `{}` | the account's labels (so the model reuses an existing one before creating) |
| `gmail_create_label` | `{name}` | creates a label (`Parent/Child` for nesting); idempotent — returns the existing one if the name is taken |
| `gmail_label` | `{message_id, add?, remove?}` | confirmation; `add`/`remove` are label **names** resolved to ids in the handler (this is "move into a label") |
| `gmail_archive` | `{message_id}` | confirmation (removes `INBOX`) |

Handlers receive the standard `ToolContext` but use **none** of its DB-backed
fields — they call only the client. Creation is its own primitive (`gmail_create_label`)
rather than a side effect of applying a label, so a task can build the taxonomy
deliberately; `gmail_label` resolves an `add` name against `list_labels()` and, if it
is missing, returns a message telling the model to `gmail_create_label` first
(predictable over implicit creation). The `archivist.prompt` instructs the
list → create → apply workflow and to reuse existing labels to avoid drift (a typo'd
near-duplicate). Errors return the `GmailError` message as the tool result.

### The persona (`agents.py` + `archivist.prompt`)

Following the jerv block exactly:

```python
GMAIL_TOOLS = frozenset(
    {
        "gmail_search",
        "gmail_read",
        "gmail_list_labels",
        "gmail_create_label",
        "gmail_label",
        "gmail_archive",
    }
)

AGENTS = {
    ...,
    "archivist": _profile(
        "archivist", "archivist.prompt", tools=GMAIL_TOOLS, reads_knowledge_base=False
    ),
}
```

`GMAIL_TOOLS` is added to the registry's web-gate set (the single source the gate
reads, beside `WEB_TOOLS`), so the tools are opt-in and `curator` can never reach
them. The `archivist.prompt` system prompt frames the triage job, the
labels-not-deletion discipline, and — like jerv's prompt — forbids the persona from
volunteering or acting on anything outside the mailbox.

### Wiring (`main.py`)

`build_gmail_handlers(GmailClient(settings...))` built only when `gmail_refresh_token`
is set (else the registry drops the sidecars, same graceful-degrade as ComfyUI), and
threaded into the registry build beside `web_handlers`.

### Tests

Loop tests drive `archivist` against `FakeGmail` (search → create-label → label →
archive); a persona test asserts the six-tool allowlist and that `archivist` is
rejected from any knowledge tool; a registry test asserts the gmail sidecars are
web-gated and absent from `curator`. Coverage stays at the 80% gate.

---

## The task layer (deferred — designed separately, later)

The persona + primitives above are the foundation; the organizing **tasks** are built
on top of them and are out of scope for this plan. They are sketched here only so the
foundation doesn't paint them into a corner — none of this is committed by E1–E2:

- **How the taxonomy is decided** — the agent infers categories from the mail vs. it
  is handed a fixed label scheme. A task-prompt decision, not a tool change.
- **Nightly / batch runs** — a standalone scheduled entrypoint chewing the 20-year
  backlog in slices, with a **cursor** (a JSON blob via the storage abstraction, since
  Gmail's labels are the real state and the loop is idempotent) and a per-run
  message/cost cap. Kept **independent of the Phase-5 workflow engine** (which is
  DB-backed) so the feature stays DB-free.
- **Dry-run** — a run that logs intended label/archive actions without calling the
  write, so the owner can vet the agent's judgment before it acts.
- **Batch-approve** — if the owner later wants human-in-the-loop at scale, a run can
  emit one batch egress Proposal of intended moves instead of acting directly (the
  architecture-native consent path).

Because every task reuses the same six primitives, the tool surface does not reopen
when a new task is designed.

---

## What this plan deliberately does **not** do

- **No RAG ingestion of email** (no `Note` rows, no ingest pipeline, no domain
  tagging) — the explicit second step, kept out.
- **No new table / migration / RLS test** — the persona is stateless on the box.
- **No destructive Gmail ops** — `gmail.modify` scope only; delete/trash unreachable.
- **No `curator` access** — the tools are web-gated to `archivist`.
