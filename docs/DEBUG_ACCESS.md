# Owner debug console (assistant access for live prompt iteration)

A way to let an external assistant (e.g. a Claude Code session) reach a **running**
JBrain box to iterate on prompts against the local model, run read-only SQL, read
logs, and switch LLM routing — live, without a redeploy. Built for the owner's own
**test** box: it trades the domain firewalls for convenience, so turn it on only
where you're comfortable letting the assistant read everything.

It is **off by default** and adds no surface until you enable it.

> Driving it from a session? The assistant-facing runbook —
> requesting a token, saving it, and the `scripts/debug-connect.sh` commands — is
> `docs/DEBUG_ACCESS_SESSION_GUIDE.md`.

## The shape

```
PWA (owner) ──mint──▶ capability token  ──hand off──▶  assistant
                                                          │  Authorization: Bearer <key>
                                                          ▼
                                              https://<your-host>/api/debug/*
```

1. In **Settings → Debug access (Claude)** the owner mints a token: a label and a
   lifetime (1h / 24h / 7d / 30d). The server returns a single self-contained
   **payload** — `base64url(JSON{ v, u: server-url, k: key })`, the same idea as the
   OwnTracks pairing payload — shown **once**.
2. The owner copies that payload and hands it to the assistant — or opens the
   **web console** (below) with one tap. It encodes both *where* to connect (the
   public host) and *how* (the bearer key), so nothing else is needed.
3. The assistant calls `/api/debug/*` with `Authorization: Bearer <key>`.
4. The owner can **revoke** (permanent) or **suspend** (reversible) any token from
   the same screen; every token also **expires** on its own.

### Token lifecycle: active · suspended · revoked · expired

- **Revoke** is permanent (`revoked_at`); **expiry** lapses on its own
  (`expires_at`). **Suspend** sits between them: `suspended_at` freezes a token so
  it stops authenticating, and **resume** clears it (migration `0087`).
- A suspended token **cannot un-suspend itself** — it can no longer authenticate
  the surface — so **resume is owner-only** (the PWA token list). The console (or
  the owner) can *enter* suspension; only the owner leaves it. That asymmetry is
  deliberate.

## Auth model

The token is a `capability_token` **principal** — the third, previously-dormant
principal kind alongside `owner` and `device_key`. It follows the same isolation
rule as every other credential: a **physically distinct, kind-filtered lookup**
(`find_active_capability_by_key_hash`), so a debug token can never authenticate on
the owner-cookie or device paths, and an owner/device key can never authenticate
here. On top of revocation it enforces an **`expires_at`** and stamps
**`last_used_at`** on each hit (migration `0086`), plus a reversible
**`suspended_at`** pause (migration `0087`).

Two gates protect the surface, both fail-closed:

- **Feature flag** `JBRAIN_DEBUG_ACCESS_ENABLED` (default `false`). When off, the
  `/api/debug/*` router is **not mounted** (a 404 — no oracle that it exists) and
  minting is refused (409). The owner management routes (`/api/settings/debug-tokens`)
  exist either way so a token can still be listed / suspended / resumed / revoked.
- **The bearer key**: a live, unrevoked, **unsuspended**, unexpired
  `capability_token` or 401.

## What the token can do (`/api/debug/*`)

| Route | Purpose |
|-------|---------|
| `GET /whoami` | Token label, kind, and the fixed scope set. |
| `POST /complete` | Run one `system` + `user_text` prompt through the **LLM adapter** (non-negotiable #1 — never a provider SDK) against whatever model is currently routed; returns the text/parsed JSON, token usage, and the **resolved provider:model**. Route by a known `task` (so the live per-task override applies) or a raw `strength` tier. Synchronous — fine for quick calls. |
| `POST /complete-async` → `GET /jobs/{id}` | Same completion, but as a **background job**: submit returns a `job_id` at once; poll `/jobs/{id}` until `done`. For a slow model (a long, high-effort local extraction takes minutes) this avoids holding a request open past a proxy's timeout — e.g. the Cloudflare Tunnel's ~100s edge limit. In-memory + best-effort (a restart drops in-flight jobs). |
| `POST /sql` | One **read-only** statement. Runs under an owner RLS context (full read) inside a `SET TRANSACTION READ ONLY` transaction, so it can read anything yet write nothing; a single-statement read-verb guard rejects obvious misuse with a clean 400. Rows capped + JSON-coerced. |
| `GET /logs/{service}` | Tail a container's logs, proxied to the supervisor (the single owner of docker access), mirroring the owner ops surface. |
| `GET/PUT /llm` | Read or **switch** which model serves each task — live, no restart. Shares validation with the owner settings screen. |
| `POST /llm/local-models/{id}/load\|unload` | Warm or evict a local model on the gateway. |
| `POST /suspend-self` | **Pause** the presenting token (the console's Suspend button). Owner resumes it later from the PWA. |
| `POST /revoke-self` | **Kill** the presenting token (the console's Revoke button). Permanent. |

The two `*-self` routes are the only writes a token can make to its **own** grant,
and both only ever *weaken* it (de-escalate), never extend it — so they need no
owner authority. There are **no** data-write or owner-management routes on this
surface, and it is rate-limit/audit-logged like the rest of the API.

The owner-side counterparts live on the management surface (owner-cookie gated):
`DELETE /api/settings/debug-tokens/{id}` (revoke) and
`POST /api/settings/debug-tokens/{id}/suspend|resume`.

`GET /api/debug/activity?after=<seq>` returns a live ring of recent `/api/debug/*`
calls — verb, route, status, derived kind, and a short **detail** (the SQL text,
the prompt, the routing change, truncated) that each handler stashes on
`request.state`. So the console shows *what* ran, not just the route, including
commands an external assistant issues. The surface is owner-token-gated and the
owner already has full read, so echoing their own commands back is intentional.

## The web console

`/debug-console.html` (opened from **Settings → Debug access** via **Open
console**, or by pasting a payload) is a standalone, **token-authed** page — not
part of the cookie-authed PWA. Two-pane UI: a **live activity** feed on the left
(it polls `/api/debug/activity`, so an assistant's commands stream in as they run,
not just this tab's), output on the right, and **Suspend** / **Revoke** top-right
as the token's own kill switch. It is a separate Vite entry, precached by the
service worker like `/dash`.

Two properties make it work across the public/LAN split:

- **Same-origin API calls.** The console calls the API with *relative* paths, so
  it always targets the host that served the page — never the token's embedded
  host. That is what lets a LAN-only console (served over `jbrain.local`) drive the
  box even though the token it carries points an external assistant at the public
  host. The token supplies only the bearer **key**; its `u` host is for off-box
  clients.
- **Cached connection.** The key is saved to `localStorage`, so a refresh
  auto-reconnects (and the fragment is stripped from the address bar on load). It
  is cleared on **Revoke**. A suspended token still 401s until the owner resumes it
  in the PWA, after which a reload reconnects.

### Public token, LAN-only console

By design the token defaults to the **public** host (so a handed-off token reaches
the box from the internet), while the console **page** is **LAN-only** (it must not
be exposed publicly):

- `JBRAIN_PUBLIC_BASE_URL` (e.g. `https://your-tunnel-host`) is embedded in every
  minted payload, even when minted from the LAN PWA, so an external assistant
  connects over the public host. Empty falls back to the mint origin.
- The console page is served only on the LAN site; the public site **404s**
  `/debug-console*` (its shared `/assets/*` carry no secrets, and `/api/debug/*`
  stays reachable for the token). So the human UI requires LAN access
  (`jbrain enable-lan`); a remote assistant still uses the `/api/debug/*` routes
  directly (or `scripts/debug-connect.sh`).

## Security posture (and the deliberate trade)

- Off by default; no surface, no minting until enabled.
- 256-bit key, stored SHA-256-hashed; revocable; time-boxed; usage-stamped.
- Kind-filtered lookup → no confused-deputy across principal kinds.
- SQL is read-only at the **transaction** level, not just by string inspection.
- **The trade:** read-only SQL runs under an owner context, so it bypasses the
  health/finance/location domain firewalls, and `GET /logs` can surface logged
  content. That means a holder of a live token can read **personal data**, and it
  leaves the box to wherever the assistant runs. This is intended for a **test**
  box. Keep tokens short-lived and revoke when done. On a box with real personal
  data, leave `JBRAIN_DEBUG_ACCESS_ENABLED=false`.

## Reachability

The assistant reaches the box at the payload's host — normally the public
Cloudflare Tunnel hostname (`docs/CLOUDFLARE_TUNNEL.md`). This only works if the
assistant's network egress can reach that host; an isolated sandbox may not be
able to, in which case the token is fine but the connection won't establish.

## Enabling it

Set in `/opt/jbrain2/.env`:

```
DEBUG_ACCESS_ENABLED=true
# So a handed-off token reaches the box from the internet even when minted from
# the LAN PWA (the LAN console ignores this and calls same-origin):
PUBLIC_BASE_URL=https://your-tunnel-host
```

then `sudo jbrain up` (**not** `jbrain restart`). A `.env` change is only injected
when the container is **recreated**: `docker compose restart` reuses the existing
container with its old environment, so the flag wouldn't take. `jbrain up` (or
`down` + `up`) recreates and picks it up. Mint a token in **Settings → Debug
access (Claude)**, hand off the payload (or **Open console**), and revoke it when
the session is done. Set it back to `false` (and `jbrain up`) to remove the
surface entirely.
