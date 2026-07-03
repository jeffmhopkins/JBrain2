# Runbooks — how to operate the box

> **Status:** Living · **Last verified:** 2026-07-03

Operational runbooks: setup, access, and recovery procedures for a running
JBrain2 box. `Living` docs (per `../DOC_LIFECYCLE.md`) — kept true as the ops
surface changes.

| Doc | What it covers |
|---|---|
| `OPERATIONS.md` | JBrain360 operator runbook: revoking a member, the encryption-at-rest compensating control, rotating the device Keystore key + the server's pinned cert. |
| `STRIX_HALO_SETUP.md` | End-to-end runbook for self-hosting the optional local models on an AMD Strix Halo box: distro → kernel → Vulkan → install → routing. |
| `CLOUDFLARE_TUNNEL.md` | Reaching a home-network box from outside via Cloudflare Tunnel — the dynamic-IP / CGNAT path. |
| `LOCAL_ACCESS.md` | Signing in on the LAN when the internet/tunnel is down: mDNS `<name>.local` + Caddy local HTTPS. |
| `DEBUG_ACCESS.md` | The owner debug console: a revocable, time-boxed `capability_token` for external assistant iteration. Off by default. |
| `DEBUG_ACCESS_SESSION_GUIDE.md` | Assistant-facing runbook for the debug console: requesting a token and driving the box via `scripts/debug-connect.sh`. |
