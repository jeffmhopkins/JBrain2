# jcode grok Internet Access — Design Spec

> **Status:** In progress · **Last verified:** 2026-07-31 · **Waves:** S0✅ S1✅ S2✅ S3✅ S4✅ S5✅ E1◻️ (S1–S4 = SearXNG search for grok, shipped; S5 = the AGENTS.md/CLAUDE.md discovery hook so grok/claude actually reach for the shell helpers — the on-box banner alone didn't. CI covers the bridge/helpers/plumbing with fakes + a localhost stub; final on-box sign-off that the CLIs read the workspace-root memory file is pending. E1 = raw-egress toggle, deferred on the shared-container caveat in §6 — the UI toggle ships disabled)

> Reconciled with the root `CLAUDE.md` non-negotiables: the search bridge runs
> the same on-box SearXNG discipline jerv already uses (invariant #9 — no owner
> data rides a query; only model-supplied query text leaves, via the owner's own
> searxng). No new DB table (the per-session flags travel with the session like
> `model`/`planner`), so no RLS test is required unless a persisted per-owner
> default is added (§4). All new outbound stays behind the api bridge — the
> sandbox itself gains no raw internet from the search path.

Give the in-sandbox **grok Build** CLI the ability to **search the
web**, backed by the box's existing self-hosted **SearXNG** — and, separately and
explicitly, an opt-in to let a session's own shell reach the **raw internet**.

---

## 1. Why — the failure this fixes

The owner ran grok (Qwen3-Coder-Next, on-box) in a jcode session and asked it to
search the internet. It correctly refused: it has no web-search tool. That's not a
model limitation — it's that nothing in the sandbox exposes one.

The box **already runs SearXNG** (`deploy/docker-compose.yml` `searxng`, plus a
`reader` for fetches) to back jerv's `web_search`/`web_fetch`
(`backend/src/jbrain/agent/webtools.py`). grok simply has no path to it.

## 2. The constraint that shapes everything — the network

The jcode sandbox sits on its **own `jcode` network** (`docker-compose.yml:544`),
reachable peers by design: **only `local-llm` and `api`** (`:425-429`). SearXNG
lives on the **`internal`** network (`:625`) and is **unreachable from the
sandbox**. Widening the sandbox onto `internal` is rejected — it's the whole point
of the isolation (no `db`, no notes, no blob store).

**The `api` is the one process on both `jcode` and `internal`** (`:164`), and every
grok completion already flows through the api's residency proxy
(`backend/src/jbrain/api/jcode_llm.py`). So **the api is the bridge**: the sandbox
calls the api, the api calls SearXNG.

## 3. How grok gets the tool — shell helpers on `PATH`

grok is driven purely by `~/.grok/config.toml` (models + subagents), rendered by
`jcode/grok-config.sh`. There is **no MCP or custom-tool surface** in the jcode
code; grok's non-model extension points here are its built-in tools and **shell
helpers on `PATH`** (`jcode-grok`, installed in `jcode/Dockerfile`). A
shell-driven coding agent uses its bash tool for exactly this.

**Decision (owner, 2026-07-31): shell helpers on `PATH`.** Ship `web-search` and
`web-fetch` scripts (mirroring `jcode-grok`) that curl the api bridge. This works
regardless of what the pinned grok binary's tool system supports.

## 4. The two checkboxes — decision (owner, 2026-07-31: two separate)

Two independent per-session capabilities, **plumbed exactly like `model`/`planner`
are today** — fixed at create so a mid-session settings change never re-points a
live session:

`CreateSessionBody` (`api/jcode.py:193`) → `JcodeApi.create_session(...)`
(`jcode/client.py:80`) → `SessionManager.create(...)` → `Session` fields
(`jcode/sessions.py:43`) → `terminal.py model_env()` → env into the login shell.

1. **SearXNG search** (`internet_search`) — exposes `web-search`/`web-fetch` to
   grok. Sandbox egress stays locked; only query text / target URLs leave,
   through the owner's own searxng. **This is the safe default and covers the
   reported failure.**
2. **Raw egress** (`internet_egress`) — lets the session's own shell reach the
   open internet (pip, `git clone`, arbitrary `curl`). Materially riskier (it's an
   arbitrary-code sandbox) and architecturally heavier — see §6.

**Gating note (honest):** the api bridge is authed by the shared `GROK_API_KEY`
that every session already holds, so the `internet_search` checkbox controls
**tool exposure** (whether the helper is on `PATH` / advertised), not a hard api
boundary. That is acceptable precisely because the search path carries **no owner
data** — identical to jerv's sandboxed web tools. The bridge is still globally
gated on SearXNG being configured, and optionally passes the `sid` for
defense-in-depth logging.

## 5. Waves — SearXNG search (S1–S4)

- **S1 — api search bridge.** Add `POST /api/jcode/llm/v1/web_search` and
  `/web_fetch` beside the jcode proxy (`api/jcode_llm.py`), authed by the same
  bearer, reusing the already-constructed `SearxngClient(settings.searxng_url)`
  (`main.py:372`) and `WebFetcher`. Return the same shaped hits/text jerv's
  handlers build. Tests: fake transport, 200/empty/unavailable, auth reject.
- **S2 — sandbox helpers + env.** `web-search`/`web-fetch` scripts curling the
  bridge (base URL from the same `GROK_MODELS_BASE_URL` root); `COPY` + `chmod`
  in `jcode/Dockerfile`. Test: helper renders and points at the bridge; no-op when
  the flag is off.
- **S5 — discovery (resolved on-box).** A login banner (`web-tools.sh`) alone did
  NOT make grok use the helpers: on-box it inspected only its MCP tool registry,
  found nothing, and told the owner it had no web access. Fix: a `jcode-agents.sh`
  login hook writes an `AGENTS.md` + `CLAUDE.md` to the workspace root (the parent
  of every checkout, so it never dirties the owner's repo) — grok and claude both
  read those memory files, so they now discover `web-search` / `web-fetch` / `ocr`
  as shell commands. Static content (the helpers self-gate). Verify on the next
  deploy that the CLIs traverse *above* the git root to find it; the fallback is to
  write it inside the checkout with a `.git/info/exclude` entry.
- **S3 — flag plumbing.** `internet_search: bool` through `CreateSessionBody` →
  `JcodeApi.create_session` → `SessionManager.create` → `Session.internet_search`
  → `model_env()` → `JCODE_INTERNET_SEARCH`. Tests updated at each seam.
- **S4 — frontend.** A "Web search (SearXNG)" checkbox in the create-session UI;
  send it in the create body. Component test.

## 6. Wave E1 — raw egress, and the shared-container caveat

Egress is a property of the **shared `jcode` container** (one HTTP(S)_PROXY /
one network for all sessions — `docker-compose.yml:532-540`). So a *per-session*
raw-internet toggle **cannot** be cleanly enforced by flipping a container-level
env: all sessions share the container's outbound. Honest options:

- **Container-per-session** (`docs/archive/JCODE_CONTAINER_PER_SESSION_PLAN.md`):
  the clean home for per-session egress — each session gets its own container, so
  its egress (direct NAT vs the allowlisting forward proxy) is its own. E1 likely
  **depends on** that model.
- **Per-session authorizing forward proxy:** the allowlist proxy authorizes by
  session identity and opens the wider allowlist only for opted-in sids. More
  infra, needs on-box verification (the compose comments already flag cloudflared-
  over-proxy as unverified).

**E1 is therefore specified but not built here.** The `internet_egress` flag is
plumbed through the same seams as `internet_search` (so the UI + session shape are
ready), but the actual egress relaxation lands with, or after, container-per-session
and needs on-box sign-off. The UI checkbox should surface this (e.g. disabled with
a "needs per-session containers" note) until then.

## 7. Docs to reconcile on merge

`docs/reference/ASSISTANT.md` ("Agent selection" — SearXNG now also backs the
jcode CLIs), the `jcode` + `searxng` compose comments, and this plan's status
block as each wave lands (archive on Shipped, per `docs/DOC_LIFECYCLE.md`).
