# Erdős–Straus 10¹² census launcher

> **Status:** Living · **Last verified:** 2026-08-03

Operator runbook for `scripts/es_1e12_launcher.sh`, the control harness that runs
the Erdős–Straus hard-prime census to 10¹² on a local server and hands back a
single public-facing artifact. The computation itself lives upstream at
[`jeffmhopkins/Erd-s-Straus-attack`](https://github.com/jeffmhopkins/Erd-s-Straus-attack)
(`scripts/run_1e12.sh`); the launcher wraps it so a long, non-resumable run can be
started, watched, killed, and published from one terminal.

This supports a math paper — it is standalone compute, not part of the JBrain2
knowledge-system runtime. The launcher clones the upstream repo read-only and
**never pushes to it**.

## What the run is

A one-shot census extending the verified count from 10¹¹ to 10¹². It generates
the compact minimal-R dataset for every hard prime below 10¹², verifies it by
sampled reconstruction plus a complete minimality-checked tail, and packages the
result. The question it answers: does the R = 107 residual-certificate record
(held since p = 8,803,369) break below 10¹²?

- **Time:** ~5–8 h generation + ~1–2 h verification on a 16-core machine.
- **Disk:** ~15 GB free (scratch npz ~10.5 GB, outputs ~1.3 GB).
- **CPU:** uses all cores.
- **Not resumable:** a kill means starting over — run it in tmux (the launcher does).

## Prerequisites

`git`, `python3` (≥ 3.9) with `pip`, `tmux`, and standard coreutils. Run
`preflight` to confirm, including the ≥ 15 GB free-disk check.

## Lifecycle

```bash
# 1. Confirm the host is ready (tools, disk, cores).
scripts/es_1e12_launcher.sh preflight

# 2. Clone, build the venv, run the pytest smoke check, launch in tmux.
scripts/es_1e12_launcher.sh start

# 3. Check progress any time (safe to run repeatedly; never interrupts the run).
scripts/es_1e12_launcher.sh status

# 4. Follow logs live, or attach to the tmux pane.
scripts/es_1e12_launcher.sh logs generate      # console | generate | verify | setup
scripts/es_1e12_launcher.sh attach             # Ctrl-b then d to detach

# 5. Kill it cleanly if needed (session + all workers).
scripts/es_1e12_launcher.sh stop

# 6. When finished, verify + package the public deliverable and print the headline.
scripts/es_1e12_launcher.sh publish

# 7. Only after the dataset is integrated into the paper, reclaim ~10 GB.
scripts/es_1e12_launcher.sh cleanup-scratch
```

`status` reports the current phase (generation / verification / packaging /
complete), whether the tmux session is alive, per-phase wall clock derived from
the timestamped console log, an approximate generation-progress proxy from the
growing scratch npz, and the last console lines.

## The deliverable

`publish` refuses unless `run_1e12_verify.log` ends with **VERIFICATION OK** and
the bundle exists. It then writes a public directory (default
`$ES_WORKDIR/public`) containing:

- `es_1e12_artifacts.tar.gz` — the single artifacts bundle (dataset, run logs,
  checksums), plus its `.sha256`.
- `es_1e12_report.md` — headline block (hard-prime count, max minimal R and
  whether the R = 107 record stands or breaks, R ≥ 87 counts), wall-clock per
  phase, and captured machine specs.
- `index.html` — a minimal landing page so the directory can be served as-is
  (e.g. behind the Cloudflare tunnel, see `CLOUDFLARE_TUNNEL.md`).

The ~10 GB scratch npz is deliberately **kept** after publish so the dataset
stays verifiable; drop it with `cleanup-scratch` only once integration is
confirmed.

## Configuration

Override via environment variables (see the script header for the full list):
`ES_WORKDIR` (working root, default `$HOME/erdos-straus-1e12`), `ES_PUBLIC_DIR`,
`ES_MIN_DISK_GB` (default 15), `ES_SKIP_TESTS` (skip the smoke check — not
recommended), and `WORKERS` (worker count, default all cores).
