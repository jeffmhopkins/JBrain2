# jcode — build plan (a sandboxed local coding agent, fronted by the PWA)

A **code mode** for JBrain2: tapping a `jcode` launcher tile opens a sandboxed
coding session — **Claude Code's agent engine driving an on-box coder model** — over
the JBrain phone interface, with the session running on the owner's own server. It is
built as an **opt-in on-box sidecar service** in the exact spirit of the ComfyUI
image-gen service (`docs/IMAGE_GEN_PLAN.md`) and the local-LLM gateway
(`docs/STRIX_HALO_SETUP.md`): a stock deploy never starts it, JBrain **fronts** it
rather than **embodies** it, and it reads **no** knowledge base, holds **no** owner
domain data, and touches **no** owner note/fact/entity.

It is **local-only**: the coder model is the on-box **Qwen3-Coder-Next 80B-A3B**, so no
code ever leaves the box and there is no cloud-LLM egress or third-party-terms surface
at all (see "Why local-only" below). This binds on top of `docs/DEVELOPMENT.md`,
`docs/PROCESS.md`, and the `CLAUDE.md` non-negotiables.

> **Status: proposed (icebox).** Per `docs/proposed/README.md`, nothing here is built
> and no phase is committed. Promotion requires a roadmap slot in `docs/ROADMAP.md` and
> the reconciliation below being accepted. The two interactive mocks already built
> (`docs/mocks/jcode-launcher.html`, `docs/mocks/jcode-session.html`) are the design
> reference, **not** a substitute for the per-surface GUI gate (Wave J3).

## The central reconciliation: jcode is a *fronted sidecar*, not the JBrain agent

`docs/ASSISTANT.md` is explicit that the personal agent refuses exactly what Claude
Code is: *"no agent framework runtime … no code execution in the agent … no
model-provider or terminal-backend zoo."* jcode does not violate that, because **it is
not the agent.** It is **not** a fifth persona in `backend/src/jbrain/agent/agents.py`,
it does **not** run in the `/chat` `AgentLoop`, and it never gets a knowledge tool. It
is a separate container the JBrain api **proxies a session to** — the same relationship
the api has with ComfyUI (`http://comfyui:8188`) and the local-LLM gateway. The agent
loop's invariants are untouched because the agent loop is not involved.

**The honest asterisks (recorded, not hidden) —** a reviewer will raise these, so they
are settled up front:

- **CLAUDE.md #1 (all LLM via the adapter).** jcode does **not** call the JBrain LLM
  adapter; Claude Code's engine is itself the LLM client, talking to the on-box gateway.
  This is *not* a breach of #1, because **the JBrain api makes no LLM call for jcode** —
  it proxies a session to a sidecar that is its own LLM client, exactly as it does not
  "go through the adapter" to reach ComfyUI or llama-swap. The adapter governs JBrain's
  *own* model calls; jcode has none. Recorded as a deliberate boundary, not an exception.
- **CLAUDE.md #2 (all file I/O via the storage abstraction).** The sandbox works in its
  **own** container filesystem (a per-session git checkout on a jcode-owned volume) — scratch
  workspace, never a JBrain blob or note. The storage abstraction governs *owner content*;
  the sandbox holds none, so it is correctly outside that chokepoint.
- **CLAUDE.md #3 (RLS).** jcode adds exactly **one** owner-only table — `jcode_sessions`
  (the launcher's session index) — with `app.is_owner()` RLS mirroring `generated_images`
  / `archivist_memory`, and the mandatory RLS isolation test. The sandbox itself touches
  no domain-scoped data, so there is no domain-firewall surface to leak across.
- **ASSISTANT.md egress (#9).** The sandbox's egress is an **allowlist** (git remotes +
  package registries the owner permits) — and crucially **there is no LLM egress**,
  because the model is on-box. The optional preview tunnel (Wave J4) is the one
  deliberate outbound surface and is owner-only, per-session, and ephemeral.

**The bright line:** jcode may run arbitrary code **in its sandbox**, against an
**isolated checkout the owner pointed it at** — it may never read the knowledge base,
the DB (beyond its own owner-only session index), the Docker socket, or any other
service. It is `reads_knowledge_base=False` taken to the level of process isolation.

## Why local-only (and why that *simplifies* the feature)

The cloud path was considered and **dropped**. Anthropic's Claude Code legal/compliance
docs are explicit that subscription (Pro/Max) OAuth *"is intended exclusively for …
ordinary use of Claude Code and other native Anthropic applications"* and that *"Anthropic
does not permit third-party developers to … route requests through Free, Pro, or Max plan
credentials."* The Agent SDK setup docs repeat it: custom wrappers *"should use API key
authentication."* So a wrapped cloud path would have to be API-key-metered, not Max-plan —
and the owner chose to **skip cloud entirely**. The consequence is strictly positive:

- **No third-party-terms surface, no metering, no API key.** Nothing leaves the box.
- **A stronger privacy posture** consistent with JBrain's local-first ethos (on-box
  geocoding, on-box vision/image models): *your code never leaves your hardware.*
- **Less to build** — one model, no cloud/local routing, no fallback, no key management.

The deliberate trade, recorded plainly: **jcode's quality is whatever Qwen3-Coder-Next
gives** (>70% SWE-Bench Verified with the SWE-Agent scaffold — strong, but not cloud
Claude), and open models drive Claude Code's tool loop with rougher edges (community
reports: malformed tool calls, edit/line mismatches). This *is* the "try the capabilities
of local models" experiment, with eyes open.

## Owner decisions (settled)

| Decision | Choice | Consequence |
|---|---|---|
| **Shape** | A fronted **sidecar service** (`jcode`), not a persona/agent | Not in `agents.py`, not in the `/chat` loop; proxied like ComfyUI. Agent invariants untouched |
| **Runtime** | **Claude Code via the Claude Agent SDK**, headless (the SDK is "the infrastructure layer of Claude Code, exposed as a library") | Supported wrapping pattern; gives streaming, resumable `session_id`, the full tool set, hooks |
| **Model** | **Local-only, single** model: **Qwen3-Coder-Next 80B-A3B** on the Strix Halo box | No cloud, no pair, no swap; 80B capacity at ~3B-active speed, co-resident-friendly on 128 GB |
| **Model bridge** | Claude Code `ANTHROPIC_BASE_URL` → the on-box gateway's **Anthropic-compatible `/v1/messages`** (llama.cpp / llama-swap); a thin LiteLLM/router shim is the fallback if the pinned build lacks it | llama.cpp support is merged (PR #16095, release b7186); the native endpoint may remove the shim — **verify on box** (open decision below) |
| **Workspace** | **Isolated per-session git checkout** (clone a repo URL, or a scratch workspace) on a jcode-owned volume | No host source access by default; operating on JBrain's own source is a separate later escalation, deliberately not in scope |
| **Isolation** | Own container + volume; **no Docker socket, no DB, no blob store, no knowledge base**; egress = git/package allowlist only | The sandbox is the security boundary; the api never mounts the socket and neither does jcode |
| **Auth** | **Owner-only.** jcode routes are owner-gated; non-owner principals (intake links, device keys) never reach it | Falls out of the existing principal model — no new auth primitive |
| **Persistence** | One **owner-only** `jcode_sessions` metadata table (the launcher index); transcript + workspace live in the jcode container (the SDK's session store + the checkout volume) | One small RLS surface (owner-only + isolation test); no domain data; no notes/RAG |
| **Preview** | Optional **per-session Cloudflare tunnel** to the sandbox's dev server, owner-only, dies with the session, never indexed | Lets the phone hit a running dev app; the one deliberate outbound surface (Wave J4) |
| **Lifecycle** | Opt-in compose `profiles: [jcode]`; start/stop/restart/logs via the **existing supervisor** (`/start`, `/stop`, `/restart`); `jbrain update` re-syncs the image | Reuses the Ops surface verbatim; stock deploy never starts it |

## Open decisions (escalation-worthy, per `PROCESS.md`)

These are the deferred choices to surface to the owner at the relevant wave, not to guess:

1. **Model bridge — native vs shim.** Whether the pinned gfx1151 llama.cpp/llama-swap
   build exposes a working Anthropic `/v1/messages` (so jcode points straight at it), or
   a thin LiteLLM/claude-code-router shim container is needed. Decided by an **on-box
   smoke test** in Wave J1, not by assumption. Also verify the Gated-DeltaNet hybrid
   attention behaves on the Vulkan/ROCm backend.
2. **Preview-tunnel exposure model.** Opening an externally-reachable tunnel to
   sandbox-run code is real surface. Confirm: owner-auth in front, bound to the one dev
   port, dies-with-session, never indexed — and whether it rides the existing
   `cloudflared` (`docs/CLOUDFLARE_TUNNEL.md`) or a separate ephemeral tunnel. (Wave J4,
   red-team gated.)
3. **Resource governance.** Per-session CPU/memory/disk caps, max concurrent sessions,
   and session TTL/GC — an unbounded coding sandbox on the box needs ceilings. (Wave J5.)
4. **Operating on JBrain's own source.** Explicitly **out of scope** here (isolated
   checkouts only); a dev-box-only escalation to be designed separately if wanted.

## Architecture — the pieces, and what they reuse

```
PWA: jcode tile → JcodeLauncherScreen → JcodeSessionScreen
        │                 │                    │  (SSE: text_delta / tool_call / tool_result / done)
        ▼                 ▼                    ▼
  api  /api/jcode/*  (owner-gated proxy + jcode_sessions index)
        │
        ▼  internal network (no published port)
  jcode container: control server → Claude Agent SDK (headless)
        │                                   │  per-session git checkout (jcode volume)
        │                                   ▼
        └── ANTHROPIC_BASE_URL ─────▶ on-box gateway /v1/messages → Qwen3-Coder-Next 80B-A3B
                                            (restricted egress: git remotes + package registries only)
```

| Need | Reuses | Net-new |
|---|---|---|
| Opt-in on-box service, internal-only, owner lifecycle | Compose `profiles:` gating + `networks: [internal]` (comfyui/local-llm/whisper); supervisor `/start`·`/stop`·`/restart`; `jbrain update` re-sync | The `jcode` service + `scripts/jcode-setup.sh` |
| Graceful-degrade config | `comfyui_url`/`whisper_url` `*_url`/`*_enabled` fail-closed pattern (`config.py`) | `jcode_url` / `jcode_enabled` / `jcode_model` + egress allowlist |
| Streaming a turn to the phone (detached, reconnect, cancel) | The `/chat` SSE machinery (`AgentLoop.run_stream`; `docs/ASSISTANT.md` "Streaming to the phone") | A jcode stream bridge (Claude Code stream-json → the same `ChatEvent` frames) |
| Session UI | The Chats-picker paradigm (`docs/DESIGN.md`), `Stream.tsx`, tool-use accordions, the card launcher (`Launcher.tsx`) | The launcher + session screens (Wave J3, GUI-gated) |
| Owner-only durable state | `generated_images` / `archivist_memory` owner-only RLS (`app.is_owner()`) + isolation test | The `jcode_sessions` index table |
| Local model serving | The llama-swap gateway (`docs/STRIX_HALO_SETUP.md`) | `ANTHROPIC_BASE_URL` wiring (+ shim only if needed) |

**Net-new is small:** one container + setup script, one owner-only table, a handful of
owner-gated proxy routes, two screens, and an optional preview-tunnel path. The rest
composes existing machinery — the measure of fit JBrain holds every feature to.

## Security posture (the sandbox is the boundary)

jcode runs an agent that executes arbitrary shell, so the container *is* the threat model.
Binding properties (red-team gated at the security-touching waves):

- **No Docker socket** (root-equivalent; the api never mounts it and neither does jcode).
- **No DB, no blob store, no knowledge base** — the sandbox cannot reach Postgres, the
  storage volume, or any JBrain service; `jcode_sessions` is written by the **api**, not
  the sandbox.
- **Egress allowlist, no LLM egress.** Outbound is limited to owner-permitted git remotes
  + package registries; the model is on-box so completions never leave. Default-deny.
- **Owner-only.** Every `/api/jcode/*` route is owner-gated; non-owner principals can't
  reach it. Purge: deleting a session removes its checkout and its index row.
- **Resource ceilings** (Wave J5): per-session CPU/mem/disk caps, max concurrency, TTL.

## Wave split

Per `PROCESS.md`: each wave runs its tasks in parallel worktrees off a `wave-N` branch,
gets an independent per-task review and a per-wave review (security/red-team for any
sandbox/egress/RLS-touching wave), and lands as exactly one PR, CI green before merge.
Any GUI surface goes through the **three-interactive-mock gate** before implementation.

- **Wave J1 — the on-box jcode service + model bridge** (no JBrain GUI, no JBrain DB).
  The `jcode` compose service (`profiles: [jcode]`, `networks: [internal]` + a restricted
  egress network, sandbox volume, **no socket**): a small **control server** wrapping the
  **Claude Agent SDK headless** that can create/resume/stream/cancel a coding session in a
  per-session git checkout. `ANTHROPIC_BASE_URL` wired to the on-box gateway's
  `/v1/messages` (Qwen3-Coder-Next), with the **on-box smoke test** that decides native vs
  shim (open decision 1). `jcode_url`/`jcode_enabled`/`jcode_model` + egress-allowlist
  config (fail-closed empty → feature off, graceful degrade). `scripts/jcode-setup.sh`
  (sibling of `comfyui-setup.sh`/`local-llm-setup.sh`); `dev-setup.sh` updated for the new
  service + env (#8). Tests: a transport/`FakeJcode` stub for the control protocol; the
  pinned base URL asserted; **no JBrain DB, no RLS test yet**.

- **Wave J2 — api control plane + session index** (no GUI). Owner-gated `/api/jcode/*`
  proxying the control server: `create_session` (repo + branch → clone), `list_sessions`,
  `send_turn` (**SSE**, reusing the `/chat` detached-stream + `?after=N` reconnect + cancel
  contract; Claude Code's stream-json mapped to the existing `ChatEvent` frames),
  `reset_sandbox`, `delete_session`. The owner-only **`jcode_sessions`** table (id, repo,
  branch, status, created/last_active, run state) with `app.is_owner()` RLS + the
  **mandatory RLS isolation test**. A `JcodeClient` provider (live url/creds over env
  fallback, rebuild-on-change, graceful degrade when `jcode_url` empty — the
  ComfyUI/Gmail-provider pattern). Tests: fake-driven route tests + the isolation test;
  coverage at the 80% gate.

- **Wave J3 — session launcher + live session** (GUI; **three-mock gate first**). Present
  three interactive mock variants **per surface** — the **launcher** (new-session setup +
  resume list) and the **live session** (coding stream + sandbox card); the owner picks,
  the chosen mocks land in `docs/mocks/` as the binding spec. (The existing
  `jcode-launcher.html` / `jcode-session.html` are variant 1.) Then implement: a `jcode`
  **launcher tile** (`Launcher.tsx`), `JcodeLauncherScreen` (new session = repo/branch +
  the pinned on-box model; resume list off `jcode_sessions` in the Chats-picker paradigm),
  and `JcodeSessionScreen` (the live stream via the chat SSE client + `Stream.tsx`,
  tool-use accordions, the sandbox card). Mock-first per `DESIGN.md`; component tests in
  mock mode.

- **Wave J4 — preview tunnel** (security-touching; **red-team gated**; open decision 2).
  The opt-in per-session Cloudflare tunnel to the sandbox's dev server: open/close routes
  (owner-only), bound to the one dev port, **dies with the session**, never indexed. The
  session screen's preview control (a small in-place addition, but *opening external
  surface* is escalation-worthy — confirm the exposure model with the owner). Tests: tunnel
  lifecycle + dies-with-session + owner-gating.

- **Wave J5 — lifecycle, Ops & hardening** (security-touching; **red-team gated**; open
  decision 3). jcode in the Ops AI/Infra service group (start/stop/restart/logs via the
  existing supervisor); a **Settings → jcode** panel (enable, model/health status, egress
  allowlist view); `jbrain update` image re-sync. The **sandbox hardening pass**:
  per-session CPU/mem/disk caps, max concurrency, session TTL/GC, purge-on-delete (checkout
  + index row), and an assertion sweep that the container holds no socket/DB/blob/notes
  access. Tests: lifecycle + the hardening assertions.

**Scope of this plan = J1–J5: the sidecar, its control plane, the GUI, the preview path,
and operability/hardening.** Operating jcode on JBrain's own source, and any non-isolated
workspace, are explicitly deferred (open decision 4).

## What this plan deliberately does **not** do

- **No cloud LLM path** — no Anthropic API key, no Max-plan OAuth, no metering; local-only.
- **No knowledge-base access** — the sandbox reads no note/fact/entity/list/appointment,
  and gets no JBrain agent tool; it is dataless like `jerv`/`archivist`.
- **No JBrain LLM-adapter call for jcode** — the api proxies a session; the sidecar is its
  own LLM client (recorded asterisk above).
- **No Docker socket, no host source, no second datastore** — one owner-only index table,
  isolated checkouts only.
- **No `curator`/agent surface change** — jcode is not a persona; `agents.py` is untouched.
- **No RAG ingestion of code or transcripts** — sessions are coding scratch, never notes.

## On-box bring-up (open decision 1 — the last mile)

The full path is shipped except the on-box validation of the **model bridge** and the
**SDK→event mapping**. `ClaudeCodeAgent.run_turn` now drives `claude_agent_sdk.query()`
(bypass-permissions in the isolated checkout, `resume` for multi-turn) and maps the
message stream → `TurnEvent`s; it runs only on the box (the SDK is image-only). Bring it
up in this order on the Strix Halo box, once a coder GGUF is provisioned and the stack is
up:

**1. Probe the gateway's API surface** (from a peer on the jcode network):

```bash
docker compose exec api curl -s http://local-llm:8080/v1/models                 # served name?
docker compose exec api curl -s http://local-llm:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"qwen3-coder-next","messages":[{"role":"user","content":"hi in 3 words"}]}'
docker compose exec api curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST http://local-llm:8080/v1/messages \
  -H 'content-type: application/json' \
  -d '{"model":"qwen3-coder-next","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}'
```

- `/v1/models` confirms the served-model name matches `JCODE_MODEL` and that the dropdown
  resolves it; the chat call confirms the model answers **and emits tool calls** (the agent
  is useless without them).
- `/v1/messages` is the fork: **`404` ⇒ shim needed** (the expected case — llama.cpp serves
  OpenAI, not Anthropic). `200` ⇒ the gateway is Anthropic-native and jcode points straight
  at it.

**2. If a shim is needed** (likely): run an Anthropic↔OpenAI translator as an opt-in service
on the `jcode` network (claude-code-router or LiteLLM, exposing `/v1/messages` over the
gateway's OpenAI API), and set `JCODE_ANTHROPIC_BASE_URL` in `.env` to the shim's URL. The
compose `ANTHROPIC_BASE_URL` reads that var (default = the gateway), so switching is a
`.env` change + `jbrain up jcode` — no code change. (The shim service itself is added once
the curls confirm the gateway's exact request/response shape, to avoid shipping unverified
plumbing.)

**3. Smoke-test a real turn** end-to-end: open a jcode session in the PWA, send a one-line
prompt ("create hello.txt with 'hi'"), and confirm the stream shows text + a tool_use
(Write) + done, and the file lands in the session checkout. Then finalize the
`_to_events` block-shape mapping in `jcode/src/jcode_ctl/agent.py` against the real SDK
output (the mapping is defensive but unverified) and harden cancellation if the cooperative
(between-messages) interrupt isn't tight enough.

## Promotion checklist (out of the icebox)

1. Owner accepts the reconciliation (the asterisks) and the local-only trade.
2. A roadmap slot in `docs/ROADMAP.md` (a Phase-6-follow-on-style standalone, like the
   image-gen / archivist plans — it depends only on the local-LLM gateway, not the wiki).
3. Open decisions 1–4 resolved at their waves (or explicitly deferred again).
4. The Wave J3 GUI gate run for real (three mocks per surface), not waived by the existing two.
