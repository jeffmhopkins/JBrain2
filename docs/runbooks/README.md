# Runbooks — how to operate the box

> **Status:** Living · **Last verified:** 2026-09-16

Operational runbooks: setup, access, and recovery procedures for a running
JBrain2 box. `Living` docs (per `../DOC_LIFECYCLE.md`) — kept true as the ops
surface changes.

| Doc | What it covers |
|---|---|
| `OPERATIONS.md` | JBrain360 operator runbook: revoking a member, the encryption-at-rest compensating control, rotating the device Keystore key + the server's pinned cert. |
| `STRIX_HALO_SETUP.md` | End-to-end runbook for self-hosting the optional local models on an AMD Strix Halo box: distro → kernel → Vulkan → install → routing. |
| `CLOUDFLARE_TUNNEL.md` | Reaching a home-network box from outside via Cloudflare Tunnel — the dynamic-IP / CGNAT path. |
| `LOCAL_ACCESS.md` | Signing in on the LAN when the internet/tunnel is down: mDNS `<name>.local` + Caddy local HTTPS. |
| `CORPUS_RESET.md` | Wiping the notes, the graph, the wiki and the fact projections while keeping what notes did not derive — the scope, the backup, and why a reset is a migration. |
| `ENDPOINT_RECOVERY.md` | Recovering a room-endpoint panel, in the order to try it: automatic OTA rollback, the factory app, and the ROM download mode that makes the chip unbrickable by software (BOOT + reset). Also what is *not* recoverable — an NVS erase loses a unit's provisioning and needs the cable. |
| `DEBUG_ACCESS.md` | The owner debug console: a revocable, time-boxed `capability_token` for external assistant iteration. Off by default. |
| `DEBUG_ACCESS_SESSION_GUIDE.md` | Assistant-facing runbook for the debug console: requesting a token and driving the box via `scripts/debug-connect.sh`. |
| `EXTERNAL_VIDEO_WATCH.md` | Auto-ingesting a YouTube channel's new videos into the search corpus via a recurring Jerv Task (`check_channel` → `analyze_stream` → `search_external_video`). |
