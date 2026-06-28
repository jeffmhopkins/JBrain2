# jcode session isolation — per-session network namespace (and the path to a real sandbox)

**Status: Wave 0 (this plan). Open decisions await owner sign-off before Wave 1 —
the gating one is a security tradeoff (below), so nothing is built yet.**

Sessions today share one jcode container and one network namespace. This plan gives
each session its **own** network namespace — its own `lo` — so every session can bind
the same port independently (each gets its own `5173`). That fixes the concurrent-Vite
ergonomics (`docs/JCODE_PREVIEW_HOST_PLAN.md` left this as a residual), and it's the
first step of the larger goal: making the sandbox an actual sandbox, closing the
**cross-session filesystem-read residual** documented in `proposed/JCODE_PLAN.md` (§
"Cross-session reads are an accepted residual").

## The reframe: isolate the namespace, not the port

The current model hands each session a distinct port from a pool (`5173–5199`,
`host_preview.py`) because they share one loopback — there's only one `5173` to go
around. Servers that honour `$PORT` (Next/CRA/Astro, `terminal.py:preview_env`) already
bind their own port and work concurrently; the gap is dev servers that **ignore `$PORT`
and hardcode `5173`** (Vite), which collide across concurrent sessions.

Give each session its own **network namespace** and the collision is gone at the root:
every session has a private `127.0.0.1`, so they can all use `5173`. The host-preview
proxy reaches the right one by **entering that session's namespace** to dial it. The
port pool becomes unnecessary; the per-session capability is the namespace, not the
number.

## Spike findings (verified on the box, kernel 6.18.35)

Probed inside the live jcode container (`docker exec -i … unshare -Urn …` + a
`python:3.12-slim` throwaway). Concrete, not assumed:

1. **Unprivileged user+net namespace creation is blocked TODAY** — `unshare -Urn`
   returns `EPERM` in the jcode container as it runs (default, unprivileged).
2. **The *only* gate is Docker's default seccomp profile.** With
   `--security-opt seccomp=unconfined` it succeeds immediately. The host has **no**
   userns sysctl/AppArmor gate to flip (`kernel.apparmor_restrict_unprivileged_userns`
   and `kernel.unprivileged_userns_clone` don't exist on this kernel). So the fix is
   **per-container**, not host-wide, and needs **no capability grant**.
3. **Two namespaces can each bind `5173` concurrently** (proven directly), where the
   un-namespaced control collides with `EADDRINUSE` — i.e. the model delivers exactly
   the duplicate-port property we want.
4. **The in-process path is stdlib.** jcode runs Python 3.12, which has `os.unshare`
   **and** `os.setns` — so the control server can create/enter namespaces without
   shelling out or a C extension.

**Implication:** feasible here, and the cost is contained — a tailored seccomp profile,
no host changes, no `CAP_SYS_ADMIN`, no container runtime.

## Open decisions (escalation-worthy, per `PROCESS.md`)

1. **GATING — accept the seccomp relaxation?** To create namespaces, the jcode service
   needs a seccomp profile = Docker's default **+ allow** `unshare`/`clone`/`clone3`/
   `setns` (and `mount`/`pivot_root`/`umount2` if we also do a mount ns). This is a
   **narrow, scoped** allowlist (not `seccomp=unconfined`, not a capability). The real
   residual cost: enabling unprivileged user namespaces **widens the kernel attack
   surface** reachable from inside jcode (userns is a known privilege-escalation
   amplifier). On a single-owner box running the owner's own agents this is modest, but
   it is the decision to weigh. **No code lands until this is signed off.**
2. **Scope — network-only, or a fuller sandbox?** Network ns alone solves the port
   ergonomics. Adding **mount + PID** namespaces (and a per-session root view) also
   closes the cross-session filesystem-read residual (`proposed/JCODE_PLAN.md:164`) —
   the bigger isolation prize, but a bigger build. Phased so network-only can ship first.
3. **Wrapper — `bubblewrap` vs in-process `os.unshare`.** bwrap (Flatpak's engine) is a
   battle-tested wrapper that handles `lo` bring-up, mount, and pid in one exec; the
   in-process route uses 3.12 stdlib and keeps everything in the control server. Both
   are viable now; pick for auditability vs. dependency surface.
4. **Outbound — how a namespaced session still reaches the gateway.** A fresh netns has
   only a down `lo`: **no outbound**, which would cut `claude`/`grok` off from the
   on-box model gateway and `npm` from the registry. Options: **pasta/passt** (Podman's
   default, rootless), **slirp4netns**, or a **veth pair to a host bridge**. This needs
   its own spike (Wave P1) and is the make-or-break for the whole approach.
5. **Tailored profile vs blanket `seccomp=unconfined`.** Recommend the tailored profile
   (least privilege; every other default filter intact). `unconfined` is the fallback
   only if the tailored allowlist proves impractical.

## Architecture — the pieces, and what they reuse

```
session shell  ──(own netns: lo + 5173)──┐
                                          │  os.setns into the session's net ns
control server (preview proxy) ───────────┘──> 127.0.0.1:5173  (HTTP + HMR WS)
        │
        └── outbound for the session (pasta/veth) ──> on-box model gateway, npm
```

- **Allocator** (`host_preview.py`) gains a per-session **netns handle** alongside (or
  replacing) the port. Create on session start (the existing `ensure`), release on
  delete/reap (`release`) — the lifecycle hooks already exist.
- **Shell spawn** (`terminal.py`, the `pty.fork`+`execvpe`) runs the child **inside the
  session's namespaces** (bwrap exec, or `os.unshare` in the child before `execvpe`).
- **Preview proxy** (`preview_proxy.py`, Wave P3 of the host-preview plan) `os.setns`-es
  into the target session's netns to dial `127.0.0.1:<port>` — reusing the dual-stack
  loopback connect already there. The api↔jcode hop is unchanged.
- **Outbound backend** (pasta/veth) gives the session netns connectivity to the gateway.

## Security posture

- **Non-negotiables unaffected.** This is process/network isolation inside the existing
  sandbox; it touches no data surface. All LLM calls still go through the adapter, all
  I/O through storage, all queries on an RLS session. jcode still has **no Docker
  socket, no DB, no blob store, no knowledge base** (`proposed/JCODE_PLAN.md`).
- **Net effect: more isolation between sessions, a narrow widening jcode→kernel.** The
  seccomp allowlist is scoped to namespace syscalls; documented as a deliberate,
  owner-box, single-tenant tradeoff. With the mount-ns option (decision 2) the
  cross-session filesystem read — today's documented residual — is **closed**, a net
  security *gain* beyond the ergonomic one.
- **Rejected approaches** (don't reinvent / don't over-privilege): **container-per-
  session / rootless Podman** — nested-rootless needs *more* privilege (fuse-overlay,
  subuid, often `SYS_ADMIN`) plus image/lifecycle machinery we don't need; **host Docker
  socket (DooD)** — grants jcode effective host root, antithetical to sandboxing;
  **microVM/gVisor** — overkill for a personal box. bwrap/netns is the least-privilege
  fit for this box (spike-confirmed).

## Wave split

- **Wave P0 — seccomp profile + enablement flag (no behaviour change).** Ship the
  tailored profile under `deploy/`, wire `security_opt` on the jcode service behind an
  **off-by-default** flag, and confirm on-box that `unshare -Urn` now succeeds inside
  jcode. Nothing uses it yet. *Red-team: the profile is a true superset of the default
  + only the named syscalls.*
- **Wave P1 — outbound spike + decision (the make-or-break).** Prove a session in its
  own netns still reaches the model gateway and `npm` via pasta (first choice) or a
  veth bridge. If neither is clean, the plan stops here and we keep the per-port model.
- **Wave P2 — per-session netns lifecycle.** Allocator creates/releases a namespace per
  session; the shell spawn runs the child inside it (bwrap or in-process `unshare`).
  Sessions get a private `lo`. *Security-touching → adversarial review, isolation test.*
- **Wave P3 — proxy enters the namespace.** `os.setns` in `preview_proxy.py` (HTTP +
  HMR WS); retire the port pool (every session on a fixed `5173`) or keep `$PORT` now
  that it's duplicatable. End-to-end: two concurrent Vite sessions, both on `5173`.
- **Wave P4 — (optional, gated by decision 2) mount + PID namespaces.** Per-session
  filesystem + process view; **closes the cross-session read residual**. Its own
  isolation test.
- **Wave P5 — UX + docs.** Drop the `$PORT` hint from the Preview empty-state (every
  session is `5173` again); update `JCODE_PREVIEW_HOST_PLAN.md` and the runbook.

Each wave: per-task + per-wave adversarial review (reviewer ≠ author), security paths at
100%, one PR, CI green before merge (`PROCESS.md`).

## What this plan deliberately does **not** do

- **No container-per-session, no Podman, no microVM** — bwrap/netns is the least-
  privilege fit; the heavier runtimes cost more privilege and machinery for no gain here.
- **No host Docker socket**, ever.
- **No host reconfiguration** — the spike confirmed the gate is per-container seccomp,
  not a host sysctl/AppArmor knob.
- **No multi-tenant / per-session CPU-mem split** — aggregate cap stays as is
  (`proposed/JCODE_PLAN.md`); this is network/filesystem/process isolation only.
- **No change to the model bridge, RLS, or any data surface.**

## On-box bring-up (the last mile)

Owner-gated, after the waves land and behind the P0 flag:

1. The seccomp profile + `security_opt` reach the jcode service on `jbrain up` (a
   recreate; `.env`/compose change isn't picked up by `restart`).
2. Start a dev server on `5173` in two sessions concurrently; confirm both serve their
   own app at their own `<slug>-preview.<host>` with plain `npm run dev` (no `--host`,
   no `--port`).
3. With the mount-ns option: confirm a session's shell **cannot** read another session's
   checkout (the isolation test, on-box).
