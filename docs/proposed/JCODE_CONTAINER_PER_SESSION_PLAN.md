# jcode container-per-session — independent containers, nested Docker, broker-orchestrated

**Status: RED-TEAMED — NOT VIABLE AS SCOPED. Three independent reviewers (security/
privilege, operability/lifecycle, requirement/feasibility) converged on the same verdict:
do not build full-nested-Docker-per-session as written. The architecture direction is
sound, but the locked requirement (full nested Docker) forces the maximum-privilege answer,
the security/firewall claims break under nested Docker, the lifecycle/state model is
incomplete, and the whole thing contradicts the owner's own decision to park the *cheaper*
netns isolation. See "Red-team verdict" at the bottom. Decision back to the owner before any
build.**

**Original framing (retained): Supersedes the parked per-session *namespace* plan
(`docs/JCODE_SESSION_ISOLATION_PLAN.md`) — the owner chose the heavier isolation boundary it
set aside. The red team flags this reversal as itself needing justification.**

## Goal

Replace today's "all sessions share one jcode container" model with **one container per
session**, so each session is a real isolation boundary that can **run its own services**
(its own dev server, DB, redis — its own `docker compose` stack) without colliding with
or reading any other session. This closes the cross-session filesystem-read residual,
removes the per-port pool, and makes per-session tool/version state (e.g. its own `grok`)
trivially independent.

## Owner decisions (locked — these scope the plan)

1. **Lifecycle = keep-running / archive-stops / delete-destroys.** A session's container
   keeps running when the browser disconnects (in-flight work continues, reattach later).
   **Archiving** a session **stops** its container (preserves the checkout + any built
   images/volumes, frees CPU/RAM). **Deleting** a session **destroys** the container and
   all its per-session state.
2. **Full nested Docker per session.** Each session can run `docker` / `docker compose`
   for arbitrary stacks. This is the dominant constraint (see §"The crux").
3. **Concurrency = 1–2 sessions.** Resource pressure is a non-issue; the cost of this plan
   is **privilege and machinery**, not CPU/RAM. Design for correctness/isolation, not scale.

## The shift, in one picture

```
                     ┌─────────────── host docker.sock (privileged) ───────────────┐
                     │                                                              │
  api ──/api/jcode/*──> jcode-broker  ──docker create/start/stop/rm──> per-session container(s)
  (proxy, Wave J2)      (the ONLY socket holder for jcode;             ├─ session shell (PTY)
                         extends/มirrors `supervisor`)                  ├─ nested dockerd (session's own)
                         │                                              │    └─ session's `docker compose` services
                         ├─ session→container registry                 ├─ /work checkout (per-session volume)
                         ├─ PTY router (exec into the container)        └─ reaches: model gateway + egress only
                         └─ preview proxy → container IP:port
```

**Key inversion vs today:** the jcode "control server" stops *hosting* the shells. It
becomes a **broker + router**: it owns the session→container map, brokers container
lifecycle through the Docker socket, ferries the PTY stream in/out of the session
container, and points the preview proxy at the container. The shells, the tools
(`claude`/`grok`), and the nested Docker all live **inside** the per-session container.

## Architecture — components

- **jcode-broker** (the privileged orchestrator). The *only* jcode-side holder of the
  host Docker socket. Mirrors the existing `supervisor` pattern (`docker-compose.yml:232`,
  the one service that already mounts `/var/run/docker.sock`) — likely **extends supervisor
  with a jcode-session API** rather than a second socket holder, so there's exactly one
  privileged broker on the box. Responsibilities: create/start/stop/remove session
  containers, create/destroy their per-session networks, apply resource caps, enforce the
  image + runtime + egress policy. **Untrusted agent shell never reaches this** — it runs
  one layer down, inside the session container.
- **Session container image.** Today's jcode image (claude + grok + node + git) **plus a
  nested Docker engine** so the session can run its own stacks. Built once; one container
  per session is created from it. Per-session tool versions (grok!) become per-container —
  the original thread's problem dissolves.
- **PTY router** (was `terminal.py`). Instead of `pty.fork` in-process, the broker opens an
  interactive exec **into** the session container (the shell runs there). Scrollback,
  reattach, single-driver takeover, and the keep-running-on-disconnect semantics are
  preserved at the router; only the shell's *location* moves.
- **Preview proxy** (`host_preview.py` / `preview_proxy.py`). Targets the **session
  container's IP:port** on its per-session network instead of a host port from the pool.
  The port pool (`5173–5199`) is retired — every session gets its own network, so every
  session can bind `5173`. (This is the win the parked netns plan chased, achieved a
  different way.)
- **Per-session network.** Each session container sits on its **own** Docker network whose
  only reachable peers are the model path (`claude-shim`, `local-llm`) and controlled
  egress (git/npm). **No** reach to `db`, `supervisor`, `worker`, `embed`, blobs, or other
  sessions — preserving the data-firewall posture from `docs/proposed/JCODE_PLAN.md`.
- **Per-session state.** Named volume(s) per session: the `/work` checkout, and the nested
  docker's storage (so built images/volumes survive an archive-stop). Destroyed on delete.

## The crux — nested Docker under an untrusted shell

Decision 2 (full nested Docker) is the make-or-break, because the session runs
**arbitrary agent-driven shell** *and* must run its own Docker. The naïve answer —
`privileged: true` DinD — gives that untrusted shell **effective host root**, collapsing
the entire sandbox. So the plan's central technical question is **how to give a session
nested Docker WITHOUT host privilege.** Candidate isolation boundaries, to be decided
(red-team + on-box spike):

| Option | Nested Docker? | Isolation vs untrusted shell | Host cost |
|---|---|---|---|
| **Privileged DinD** | yes, easy | **none** (≈ host root) — rejected | none |
| **Sysbox runtime** (`runtime: sysbox-runc`) | yes, unprivileged | strong (per-container userns, masked `/proc`) | install + maintain sysbox on the host |
| **Rootless DinD** (`docker:dind-rootless`) | partial | good, but storage-driver/cap caveats | none, but fragile |
| **microVM** (Kata/Firecracker) | yes | strongest (real VM) | heaviest; the docs call this overkill for a personal box |

**Working recommendation: Sysbox.** It's the purpose-built answer to "nested Docker in an
untrusted container" — unprivileged nested dockerd with a real isolation boundary — at the
cost of a **host-level runtime dependency** the box must install and maintain (and wire
into `scripts/jcode-setup.sh` + `scripts/dev-setup.sh`). Rootless DinD is the no-new-host-
dependency fallback if sysbox proves impractical on this kernel; microVM is the
break-glass if even sysbox's boundary is judged insufficient for arbitrary shell. **This is
the #1 thing for the red team to pressure-test.**

## Lifecycle mapping

| Session action | Container op | State kept? |
|---|---|---|
| create / open | `create` + `start` (first open) | n/a |
| browser disconnect | **nothing** — container keeps running | yes (live) |
| reattach | re-exec PTY into the running container | yes |
| idle reap | (open question — reap = stop? or leave running?) | tbd |
| **archive** | `stop` (preserve container + volumes) | yes (cold) |
| unarchive | `start` + reattach | yes |
| **delete** | `stop` + `rm` + remove volumes + network | **no** |

## Security posture (and what changes)

- **Non-negotiables unaffected for owner data.** jcode still reads no knowledge base, holds
  no owner data, has no DB/blob access; all model calls still go through the shim/adapter.
  This plan is about *process/service isolation*, not the data surface.
- **What gets stronger:** true per-session filesystem/process/network isolation — the
  cross-session-read residual is **closed**; per-session resource caps replace the aggregate
  cap; one session's services can't touch another's.
- **What gets riskier (the tradeoff to sign off):** nested Docker widens what a single
  session can do, and the broker is a new privileged surface. The isolation boundary choice
  (§crux) is what keeps "untrusted shell + nested Docker" from meaning host root. The broker
  must treat every session request as hostile input (no path/network/cap escapes).
- **Rejected, still rejected:** giving the *session* container the host Docker socket (DooD)
  — that's host root for untrusted shell, the exact thing the broker exists to avoid.

## Wave split (high-level)

- **Wave C0 — spike the isolation boundary (make-or-break).** On the box, prove a session
  container can run nested `docker compose` under sysbox (first choice) / rootless DinD
  (fallback) while reaching the model gateway + npm and **not** the host or `db`. If neither
  is clean on this kernel, the plan stops or downgrades to broker-managed sidecars.
- **Wave C1 — the broker.** Session-container CRUD through the (supervisor-held) socket:
  create/start/stop/rm, per-session network, resource caps, image/runtime/egress policy.
  No PTY yet. Adversarial review of the privileged surface; isolation test.
- **Wave C2 — PTY router cutover.** Shells run *inside* the session container (exec), with
  today's reattach / single-driver / keep-running semantics preserved. Retire in-process
  `pty.fork`.
- **Wave C3 — preview cutover.** Proxy targets container IP:port; retire the host port pool.
  Two concurrent sessions both on `5173`.
- **Wave C4 — lifecycle wiring.** archive→stop, unarchive→start, delete→rm+purge; reaper
  policy decided. Per-session volume lifecycle.
- **Wave C5 — UX + docs + setup.** `scripts/jcode-setup.sh` + `dev-setup.sh` install/verify
  the runtime; Ops/Settings reflect per-session containers; retire the netns plan doc.

Each wave: per-task + per-wave adversarial review (reviewer ≠ author), security paths at
100%, one PR, CI green (`docs/PROCESS.md`).

## Open questions for the red team

1. **Is sysbox the right boundary**, or is rootless-DinD / microVM the better fit for
   "arbitrary agent shell + nested Docker" on this kernel? What does each actually concede?
2. **Is "full nested Docker" even the right requirement**, or does it buy a large security
   bill for a need that **broker-managed sidecars** (the rejected middle option) would have
   met? Name the use cases that *truly* need nested Docker vs. declared sidecars.
3. **The broker as a new privileged surface** — what's the blast radius if it's tricked by a
   hostile session request (network attach to `internal`, cap grant, volume mount escape)?
4. **Egress + the model path** per per-session network — does each session still reach
   `claude-shim`/`local-llm` cleanly, and does strict egress (`JCODE_EGRESS_PROXY`) still
   compose?
5. **State semantics on archive** — nested docker images/volumes can be large; do we really
   preserve all of it on stop, and what's the disk-growth story across many
   archive/unarchive cycles?
6. **Reattach across a broker restart** — if the broker process restarts, can it re-adopt
   running session containers, or are sessions orphaned?
7. **Is extending `supervisor` the right home** for the broker, or does that over-couple the
   box's one privileged service to the untrusted-shell feature?
8. **Migration** — how do existing shared-container sessions cut over without data loss?

---

## Red-team verdict (3 independent reviewers)

All three converged: **not viable as scoped.** The direction (real per-session isolation)
is sound; the specific decision that breaks it is **locking "full nested Docker."**

### Blockers (must resolve before any build)

- **B1 — The requirement is the heaviest fix for a partly-already-solved need.** The
  motivation chain (grok-rebuild → per-session tool versions → own services → full nested
  Docker) is an escalation of non-sequiturs. Per-session *tool versions* = per-session
  image/volume under the existing single runtime (no nesting). "Own dev server" already
  works. The **only** use case that truly needs an in-session Docker daemon is "the session
  runs `docker compose up` for a containerized app it's developing" — and no concrete,
  current such project has been named. *Decision gate: name that project, or the requirement
  collapses to the cheaper variants below.*
- **B2 — It contradicts the owner's own recent decision.** `JCODE_SESSION_ISOLATION_PLAN.md`
  parked the *cheaper* netns isolation as "not worth the privilege/effort," and its rejected-
  approaches list names **container-per-session/Podman** explicitly. This plan takes on
  *strictly more* privilege (a host-socket broker + a host runtime like Sysbox, which itself
  re-enables the unprivileged-userns widening that doc flagged). The reversal must be
  justified to the owner, not assumed.
- **B3 — The broker is a host-root surface with no validation contract.** Whoever shapes a
  `docker create` to the socket *is* host root (`--privileged`, `-v /:/host`, `--network=host`,
  `runtime` override, socket-in-socket = DooD). "Treat input as hostile" is a slogan, not a
  design. Required contract: the broker's **only** input is an opaque session id; it builds
  the *entire* spec server-side from a pinned-by-digest template — no session-supplied image,
  mount, network, runtime, cap, device, or privileged flag, ever. This contract IS the
  security boundary and must be written + 100%-tested before C1.
- **B4 — State model + persistence are missing (operability).** Today `Status` is only
  `ready|stopped` — there is **no `archived` state**, no archive/unarchive methods/routes. And
  every map (session→container, preview slug) is **in-memory**, rebuilt empty on restart — so
  a broker restart *orphans the running containers the "keep-running" decision intends to
  preserve*. Need a real state enum + durable session↔container mapping (label containers with
  the sid) + boot-time reconciliation.

### Majors

- **M1 — Sysbox is an unverified, kernel-coupled host dependency.** Box is **kernel 6.18.5 /
  Ubuntu 24.04**; Sysbox support on a kernel this new is unproven, and it adds a standing
  maintenance tail (every kernel/Docker bump can break the feature). Honest C0-fail probability
  ~40–60% (the prior netns spike on this same box already failed on related capability walls).
- **M2 — Sysbox's boundary is shared-kernel.** For *arbitrary agent shell*, one kernel LPE or
  Sysbox emulation bug = host. That is materially **weaker** than today's single unprivileged
  container (default seccomp, no userns, no nested daemon, no socket nearby). If kernel-LPE→host
  is unacceptable, the answer is a **microVM (Kata/Firecracker), not Sysbox** — and the plan
  should say so up front, not as "break-glass."
- **M3 — "Stronger" is dishonest about the host.** Only *cross-session* isolation improves;
  the *host-compromise* surface is net **wider** (userns amplifier + emulated /proc//sys +
  agent-controlled nested dockerd + new host-root broker). The data-firewall non-negotiables now
  rest on **new, unproven** broker-validation + runtime-netns machinery, not the old simple
  "it's just not on that network" fact.
- **M4 — Per-session network does NOT preserve the firewall, and egress stops composing.**
  Inner containers / `docker build` don't inherit the outer `HTTP(S)_PROXY` env, so
  `JCODE_EGRESS_PROXY` is bypassed. Egress confinement must move to a **network-layer
  default-deny** (only model gateway + proxy reachable), with an isolation test that runs
  *inside a nested container* proving it can't reach `db`/host.
- **M5 — Don't extend `supervisor`.** Its gateway is a deliberately *fixed* surface (act only
  on pre-labeled compose containers; no `docker create`). Bolting a general session-broker on
  gives a single RCE control of the whole stack (start/stop, and the reset/import/update
  one-shots that already bind-mount the socket + `PROJECT_DIR` rw — DB-reset/data-loss reach).
  The broker must be a **separate, minimal, single-purpose** privileged service with its own
  token and no stack-control surface.
- **M6 — Host-pid kill machinery breaks across the container boundary.** `stop()/delete()` use
  `os.killpg` + `/proc/<pid>/cwd` scans on the broker host; once shells run *inside* the
  container those pids are in another PID namespace and the hard-kill backstop silently no-ops.
  Stop = `docker stop`, delete = `docker rm -f` + label-purge of volumes/network.
- **M7 — "Shell-EOF = pause" inverts.** A `docker exec` stream EOFs on user-exit, exec death,
  *and* container stop — collapsed today into one "pause." The router must inspect exec exit
  code + container state; only a clean shell-exit over a live container = pause.
- **M8 — Idle reap currently *deletes* (would nuke a running stack); no GC for archived nested-
  docker storage.** Per-session inner images/volumes preserved across archive grow unbounded
  with no GC tier. Need: idle→archive (not delete), a disk budget, and an archived-session GC.
- **M9 — Preview transport.** Proxy hardcodes loopback; container IP changes on every restart →
  address the dev server by **container name over docker DNS**, resolved per request, not cached.
  Stable preview slug must be persisted/derived, or URLs 404 after a restart.
- **M10 — Migration unsolved.** Existing checkouts live inside the shared container; a naive
  cutover loses uncommitted work. Need per-session copy-out or an explicit push-or-lose.

### Cheaper variants the red team says likely meet the *actual* need

1. **Per-session image + volume, one shared runtime (no nesting, no broker).** Gives
   independent grok/claude/node per session + cheap purge-on-delete — the literal originating
   complaint — at near-zero cost. Gives up: nested `docker compose`, strong process isolation.
2. **Broker-managed *declared* sidecars (the rejected middle).** Session declares "postgres +
   redis"; a constrained, allowlisted broker starts them as sibling containers on the session
   net. Covers the common multi-service dev case with **no nested dockerd, no userns widening**.
3. **Keep shared container + per-session tool dirs now; revisit the parked mount-ns wave later
   if cross-session-read ever actually matters.** Near-zero new cost, no host runtime dep.

### The one question to put back to the owner

**Name the specific, current project you want to run `docker compose up` for *inside* the
sandbox.** If there is one, the conversation is microVM-vs-Sysbox and the broker contract
(B3/M5). If there isn't, variant 1 or 3 gives you per-session tool versions and your own dev
server today — without a host runtime dependency, a privileged broker, or reversing the netns
decision.
