# Gmail MCP Migration — contingency plan

> **Status:** Proposed · **Last verified:** 2026-10-12 · **Waves:** W0◻️ W1◻️ W2◻️ W3◻️ W4◻️

**This is a plan to keep on the shelf, not one to build.** Gmail stays on the REST API
(`gmail.googleapis.com/gmail/v1`) by the owner's decision of 2026-10-12. This doc
exists so that if Google ever *requires* AI agents to reach Gmail through its
Workspace MCP servers, the switch is a known, sized job rather than a scramble.

## Why this exists

In October 2026 Google announced Workspace MCP servers (Gmail, Drive, Calendar, …)
and an `is_agentic` flag on OAuth clients:

| Date | What changes |
|---|---|
| 2026-10-12 | OAuth clients can be marked "agentic" in the Cloud console. |
| 2026-11-11 | Workspace MCP servers reject clients **not** marked agentic. |
| 2026-11-12 | The flag becomes permanent; changing it means a **new** OAuth client ID. |

The notice exempts apps that use only the REST APIs, and that is how JBrain2 calls
Gmail. So nothing breaks, and the existing client (project
`project-b9904618-1eeb-4275-9d5`) was deliberately **left unmarked**. One result: after
2026-11-12 that client can never call MCP. Any switch starts with a new client, which
is fine, because the PWA can already swap credentials (see W3).

## Triggers — when to pick this up

Promote this doc to `Scheduled` only on one of:

1. **Google restricts REST for agents.** For example, agentic clients lose REST access,
   or REST calls start failing with an "AI agent must use MCP" style error. Watch
   `GmailError` text in the archivist's tool results and the `triage_inbox` run log.
2. **Google deprecates the Gmail REST API**, or the `gmail.modify` scope, for
   third-party apps.
3. **The owner chooses to.** For example, to add Calendar or Drive through the same
   servers. Then this plan covers the shared plumbing, and Gmail can stay on REST.

None of these is a reason to switch early. REST gives tighter control (below).

## Design principle: swap the transport, not the tools

Everything Gmail-related already goes through one interface, the `GmailApi` Protocol
in `backend/src/jbrain/gmail/client.py`:

`search` · `get` · `list_labels` · `create_label` · `modify` · `count` ·
`search_all` · `sender_sample` · `batch_modify`

Two callers sit on it: the 10 archivist tools (`agent/gmailtools.py` plus the
`agent/tools/gmail_*.tool` specs) and the `triage_inbox` worker job
(`gmail/triage.py`). Both get their client from `GmailClientProvider.client()`
(`gmail/provider.py`).

So the migration is **one new class, `McpGmailClient`, that satisfies `GmailApi` by
calling Google's MCP tools**, plus a provider switch. The model never sees Google's
MCP tool list. It keeps seeing the same 10 hand-built tools, with the same caps,
truncation, idempotent label creation and "N+" partial-result wording. That keeps
what made the REST design safe:

- **No send, no delete, ever.** `McpGmailClient` holds a fixed **allowlist** of the
  Google tool names it maps to. Any other tool, including whatever send, draft, delete
  or trash tools Google ships, is unreachable by construction, not by prompt.
- **Least-privilege OAuth scope.** Request the narrowest scope Google's Gmail MCP
  server accepts. If it requires a broader scope than `gmail.modify`, record that in
  W0 and get the owner's sign-off before proceeding.
- **Fixed endpoint.** The MCP URL is config, like `gmail_api_url`, never model-supplied.
- **MCP output is untrusted data.** It is the same email content as before. It flows
  through the same body rendering (`gmail/body.py`) and tool-result framing, and
  nothing in an MCP response is treated as an instruction (the stance
  `web/mcp_client.py` already takes).

**Not chosen: handing Google's MCP tools straight to the agent.** It is less code, but
it gives up the allowlist, the bulk caps, the triage job's typed client and the
token-cheap tool schemas, and it puts send/delete one prompt injection away. Rejected.

## Waves

### W0 — Spike: what does Google's Gmail MCP server actually expose? ◻️

No production code. Against a **new agentic OAuth client** (created in the Cloud
console for this; the existing REST client stays untouched):

- List the tools (`tools/list`) and their input/output schemas; save them as test
  fixtures.
- Map each `GmailApi` method to a Google tool, or mark it **no equivalent**. Likely
  gaps: `count` (REST uses `resultSizeEstimate` plus paging), `batch_modify` (≤1000 ids
  per call), and metadata-only header fetch for `sender_sample`.
- Record the required OAuth scope(s), auth method (bearer access token?), rate limits,
  session behaviour, and whether "agent access controls" restrict anything on a
  personal Google account.
- **Exit:** a filled-in mapping table appended to this doc, with a decision for each
  gap: emulate it client-side (e.g. page through results to count), degrade with an
  honest tool error, or keep REST for that method.

### W1 — Make the MCP client usable against a remote, authenticated server ◻️

`web/mcp_client.py` is written for the on-box browser sidecar. To reuse it:

- **Auth header:** add an optional async header provider, so each request carries
  `Authorization: Bearer <access token>` minted from the refresh token. Reuse the
  token-minting and caching already in `GmailClient` rather than duplicating it.
- **Proxy:** `_http()` hard-codes `trust_env=False` because the sidecar is local.
  Google is on the open internet, so make this a constructor argument.
- **Errors:** messages say "the browser server …". Name the server instead, so a
  Gmail failure doesn't read as a browser failure.
- **401 handling:** a 401 refreshes the token once and retries, matching
  `GmailClient`'s existing 401 path.
- **Tests:** MockTransport (no network), the auth header, the 401 retry, and the
  existing browser tests staying green.

### W2 — `McpGmailClient` behind `GmailApi` ◻️

- New `backend/src/jbrain/gmail/mcp_client.py`: implements every `GmailApi` method
  through the W0 mapping. It reshapes responses into `GmailMessage` / `GmailLabel`
  (including the plain/html split `render_body` needs), applies the same caps
  (`_COUNT_CAP`, `_BULK_CAP`, sample size), and raises `GmailError` with `status` set,
  so the 401 and 409 branches in the handlers keep working.
- **One contract suite run against both implementations.** Parametrize the existing
  client tests over `GmailClient` (REST MockTransport) and `McpGmailClient` (MCP
  MockTransport replaying W0 fixtures). A behaviour difference is a failing test,
  not a surprise in production.
- **Allowlist test:** calling any Google tool outside the mapping raises before any
  network call.
- Coverage: the allowlist and auth paths are security paths, so 100% (DEVELOPMENT.md).

### W3 — Owner-operable switch in the PWA ◻️

Non-negotiable #10: the owner must be able to do the whole switch from the PWA.

- New owner-only setting **Gmail transport: REST / MCP** in the settings store (default
  REST), next to the existing Gmail credential fields. `GmailClientProvider.client()`
  builds the matching class and rebuilds when the transport changes, like it does for
  a credential change today. Env fallback `JBRAIN_GMAIL_TRANSPORT` for parity, plus a
  pinned `gmail_mcp_url` in `config.py`.
- Credential swap needs **no new work.** Settings already edits client ID/secret
  (`PUT /api/settings/gmail`) and runs the OAuth Connect flow
  (`/settings/gmail/connect` → `/callback`), so the new client is a paste-and-click.
  `_SCOPE` in `api/gmail_settings.py` becomes transport-aware if W0 found MCP needs a
  different scope.
- **Test connection** (`POST /api/settings/gmail/test`) exercises whichever transport
  is selected and reports which one answered.
- Settings shows the active transport, so "which path is live?" is answerable without
  a terminal.

### W4 — Cutover, soak, and (only if forced) retire REST ◻️

1. Owner creates the new agentic OAuth client (Cloud console; the click-path is in
   `docs/archive/EMAIL_ARCHIVIST_PLAN.md`'s OAuth appendix, plus the agentic toggle)
   and registers the PWA callback URL as a redirect URI.
2. In Settings: paste the new client ID/secret, **Connect Gmail**, set transport to
   **MCP**, **Test connection**.
3. Soak for about a week: archivist sessions plus at least one scheduled `triage_inbox`
   run, watching the run log for `GmailError`s. Rollback is the same toggle back to
   REST, as long as the old credentials still work.
4. **Only if the trigger was REST being shut off:** delete `GmailClient`'s REST
   transport, the transport toggle, `gmail_api_url`, and the REST-only contract-test
   leg, in a follow-up PR. Otherwise keep both. REST is the fallback.

Docs travel with each wave: `docs/reference/ASSISTANT.md` (archivist tools),
`docs/runbooks/` for the owner click-path, and this doc promoted out of `proposed/`
when scheduled.

## What stays out of scope

- Calendar, Drive and Docs tools. If trigger 3 fires, those are their own plan. W1's
  client work is the shared part they would reuse.
- Marking the **current** REST client agentic. Google says to keep agent and
  non-agent traffic on separate clients, and the REST path doesn't need the flag.
- Any change to the 10 tool specs the model sees.

## Open questions (answered by W0)

- Does Gmail MCP accept the `gmail.modify` scope, or does it require something broader?
- Is there a bulk-modify tool, or does `batch_modify` become N single calls (rate limits)?
- Do "agent access controls" apply to a personal (non-Workspace) Google account at all?
- Does the MCP server return message bodies in a shape `render_body` can use, or
  pre-rendered text?
