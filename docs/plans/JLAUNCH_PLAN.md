# JLAUNCH — a self-serve launcher for long scientific computations

> **Status:** In progress · **Last verified:** 2026-08-03

A **job launcher** for JBrain2: tapping a `Math` launcher tile opens a self-contained
screen where the owner starts a long-running (~6–10 h, all-core, ~15 GB) one-shot
computation, watches a **live terminal**, can **stop/kill** it, and when it finishes
**generates a public results sharelink** — a no-login page (headline block + machine
specs) with the artifact to download. No manual terminal commands, ever. Built as an
**opt-in on-box sidecar service** in the exact spirit of jcode/ComfyUI.

The first registered job is the **Erdős–Straus census to 10¹²**
(`github.com/jeffmhopkins/Erd-s-Straus-attack`, `scripts/RUN_1E12.md`): venv →
`pip install -e ".[dev]"` → test-suite smoke → `bash scripts/run_1e12.sh`, producing
`es_1e12_artifacts.tar.gz` and a `run_1e12_verify.log` ending `VERIFICATION OK`, plus a
headline block (hard-prime count, max minimal R + the R=107 record verdict, R≥87 counts).
The runner **clones read-only and never pushes**; a finished run's checkout + ~10 GB
scratch are **kept until the owner deletes the run** ("keep the scratch until integration
is confirmed").

## Architecture

```
PWA "Math" tile → JlaunchScreen (owner)              Public results page
   · spec list → job list → job detail                /results/{token}
   · live xterm + start/stop/kill + sharelink         (shell-less app, no login)
        │ WS + REST (owner cookie)                          │ token in path
        ▼                                                   ▼
 api ── api/jlaunch.py           owner REST proxy + run mirror + share mint
     ├─ api/jlaunch_terminal.py  owner WS proxy (Origin-gated) → job PTY
     └─ api/jlaunch_share.py     PUBLIC results + artifact download (no owner dep)
        │ httpx bearer / stream artifact → blob store
        ▼
 jlaunch container (profile [jlaunch], port 9101, isolated `jlaunch` network)
   · jlaunch_ctl.app  bearer REST + terminal WS + artifact fetch
   · jobs.py  JobManager (clone → phased pipeline → succeeded/failed/killed), reaper-EXEMPT
   · runner.py  JobSpec → one bash script fed to a PTY (live + teed to a durable log)
   · terminal.py  vendored byte-for-byte from jcode's PTY core (drift-guarded)
   · volumes: jlaunch_work (checkout + scratch), jlaunch_artifacts
```

At **mint-share** time the api streams the tarball out of the control server straight
into the content-addressed blob store (`BlobStore.put_stream`, no whole-file buffering)
and snapshots the headline + machine specs onto the run row — so the artifact stays inside
the trust boundary and the launcher stays network-isolated (no shared blob volume).

## Components

- **Control server** `jlaunch/` (`jlaunch_ctl`): `config`, vendored `terminal`,
  `workspace`-free clone-as-phase-0, `specs` (the `erdos_straus_1e12` registry entry),
  `runner`, `jobs` (`JobManager`, PTY created at start, outcome from a status file + the
  verify/artifact gates, no idle reaper), `app` (bearer REST + attach-only terminal WS).
- **Backend** `backend/src/jbrain/`: `jlaunch/client.py` (`JlaunchClient` + `FakeJlaunchClient`),
  `storage.py` `put_stream`, `models/jlaunch.py` (owner-only `jlaunch_runs` mirror + repo),
  `external/jlaunch_shares.py` (mint/list/revoke/resolve/fetch over `jlaunch_share_links`),
  `api/jlaunch{,_terminal,_share}.py`, `db/session.py` `jlaunch_share_context`, and the two
  migrations under `backend/migrations/versions/` (`jlaunch_runs` owner RLS, then
  `jlaunch_share_links` + the `jlaunch_runs_share` keystone — a text-compared, fail-closed
  pin). Config: `jlaunch_url`/`jlaunch_enabled`/`jlaunch_token` (empty url fail-closes).
- **Deploy** `deploy/`: the `jlaunch` compose service (profile-gated, isolated network,
  `jlaunch_work`/`jlaunch_artifacts` volumes, **uncapped** cpu/mem — a run is meant to take
  the box), api env passthrough + network join, `scripts/jlaunch-setup.sh`,
  `jbrain enable-jlaunch`, the turnkey fold-in in `update-inner.sh`/`jbrain update`, and
  `sync_python jlaunch` in `dev-setup.sh`.
- **Frontend** `frontend/src/`: the `Math` launcher tile (config-gated like the Image tile,
  probing `/jlaunch/specs`), `screens/JlaunchScreen.tsx` (the tabbed Overview·Terminal·Result
  job screen — variant C — with start/stop/kill, a live xterm reusing jcode's `attachTerminal`,
  and the sharelink mint), the public `screens/JlaunchShareApp.tsx` (mounted by `main.tsx`
  `pickRoot` at `/results/{token}`), `jlaunch/{types,terminal,share}.ts`, the api client
  methods, and the `jl-*` styles.

## Status by wave

- **W1 — control server** ✅ (`jlaunch/`, 23 unit tests incl. real-PTY lifecycle + the
  `terminal.py` drift guard).
- **W2 — backend + RLS** ✅ (client/fake, storage, model, shares, three routers, two
  migrations; 18 unit + 5 RLS-integration tests).
- **W3 — deploy** ✅ (compose service/network/volumes, setup script + `jbrain` command,
  turnkey update fold-in, `dev-setup`).
- **W0 — GUI gate** ✅ three interactive mocks in `docs/mocks/jlaunch/`; owner chose
  **variant C** (tabbed), which the shipped `JlaunchScreen.tsx` implements.
- **W4 — frontend** ✅ `Math` tile + config gate, tabbed job screen with live xterm +
  start/stop/kill + sharelink mint, public results app, api client, `jl-*` styles; 9 vitest
  tests (share/terminal helpers, public app render). Full suite green (1299 tests).

## Verification

Enable with `bash scripts/jlaunch-setup.sh` (or `jbrain enable-jlaunch`); the `Math` tile
appears. Register a fast smoke-variant spec (census bound overridden to ~1e6) to exercise
start → live terminal → stop/kill → success → mint sharelink → open `/results/{token}` in a
fresh browser (no cookie) → headline + specs render, download the tarball, confirm the sha
matches; revoke → 404. Then run the real `erdos_straus_1e12` job end to end (~6–10 h),
confirm `VERIFICATION OK`, mint the public link, and leave the checkout/scratch in place
until integration is confirmed.
