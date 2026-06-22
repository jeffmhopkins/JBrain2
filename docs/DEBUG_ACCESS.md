# Owner debug console (assistant access for live prompt iteration)

A way to let an external assistant (e.g. a Claude Code session) reach a **running**
JBrain box to iterate on prompts against the local model, run read-only SQL, read
logs, and switch LLM routing — live, without a redeploy. Built for the owner's own
**test** box: it trades the domain firewalls for convenience, so turn it on only
where you're comfortable letting the assistant read everything.

It is **off by default** and adds no surface until you enable it.

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
2. The owner copies that payload and hands it to the assistant. It encodes both
   *where* to connect (the public host) and *how* (the bearer key), so the assistant
   needs nothing else.
3. The assistant calls `/api/debug/*` with `Authorization: Bearer <key>`.
4. The owner can **revoke** any token from the same screen; every token also
   **expires** on its own.

## Auth model

The token is a `capability_token` **principal** — the third, previously-dormant
principal kind alongside `owner` and `device_key`. It follows the same isolation
rule as every other credential: a **physically distinct, kind-filtered lookup**
(`find_active_capability_by_key_hash`), so a debug token can never authenticate on
the owner-cookie or device paths, and an owner/device key can never authenticate
here. On top of revocation it enforces an **`expires_at`** and stamps
**`last_used_at`** on each hit (migration `0083`).

Two gates protect the surface, both fail-closed:

- **Feature flag** `JBRAIN_DEBUG_ACCESS_ENABLED` (default `false`). When off, the
  `/api/debug/*` router is **not mounted** (a 404 — no oracle that it exists) and
  minting is refused (409). The owner management routes (`/api/settings/debug-tokens`)
  exist either way so a token can still be listed/revoked.
- **The bearer key**: a live, unrevoked, unexpired `capability_token` or 401.

## What the token can do (`/api/debug/*`)

| Route | Purpose |
|-------|---------|
| `GET /whoami` | Token label, kind, and the fixed scope set. |
| `POST /complete` | Run one `system` + `user_text` prompt through the **LLM adapter** (non-negotiable #1 — never a provider SDK) against whatever model is currently routed; returns the text/parsed JSON, token usage, and the **resolved provider:model**. Route by a known `task` (so the live per-task override applies) or a raw `strength` tier. |
| `POST /sql` | One **read-only** statement. Runs under an owner RLS context (full read) inside a `SET TRANSACTION READ ONLY` transaction, so it can read anything yet write nothing; a single-statement read-verb guard rejects obvious misuse with a clean 400. Rows capped + JSON-coerced. |
| `GET /logs/{service}` | Tail a container's logs, proxied to the supervisor (the single owner of docker access), mirroring the owner ops surface. |
| `GET/PUT /llm` | Read or **switch** which model serves each task — live, no restart. Shares validation with the owner settings screen. |
| `POST /llm/local-models/{id}/load\|unload` | Warm or evict a local model on the gateway. |

There are **no** data-write or owner-management routes on this surface, and it is
rate-limit/audit-logged like the rest of the API.

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
```

then `sudo jbrain restart`. Mint a token in **Settings → Debug access (Claude)**,
hand off the payload, and revoke it when the session is done. Set it back to
`false` (and restart) to remove the surface entirely.
