#!/usr/bin/env bash
# es_1e12_launcher.sh — control harness for the Erdős–Straus 10^12 census.
#
# Wraps the one-shot ~6–10 h scientific run from
# github.com/jeffmhopkins/Erd-s-Straus-attack (scripts/run_1e12.sh) in a
# start / status / stop / publish lifecycle you can drive from a single
# terminal. It exists because that run is long, non-resumable, and cores-hungry:
# it must survive a dropped SSH session (tmux), report progress without
# interrupting it, be killable cleanly, and — on success — hand back one
# public-facing bundle plus a headline report.
#
# It never modifies or pushes to the upstream repo; the clone is read-only
# compute. The ~10 GB scratch npz is kept until you explicitly drop it
# (`cleanup-scratch`), so the dataset stays verifiable until integration.
#
# Usage:
#   scripts/es_1e12_launcher.sh preflight        # check the host is ready
#   scripts/es_1e12_launcher.sh start            # clone, venv, smoke test, launch
#   scripts/es_1e12_launcher.sh status           # phase, elapsed, progress
#   scripts/es_1e12_launcher.sh logs [console|generate|verify]
#   scripts/es_1e12_launcher.sh attach           # live tmux pane (Ctrl-b d to detach)
#   scripts/es_1e12_launcher.sh stop             # kill the run and its workers
#   scripts/es_1e12_launcher.sh publish          # verify, package, expose result
#   scripts/es_1e12_launcher.sh cleanup-scratch  # drop the ~10 GB scratch npz
#
# Environment overrides:
#   ES_WORKDIR     working root (default: $HOME/erdos-straus-1e12)
#   ES_PUBLIC_DIR  where publish/ drops the public bundle (default: $ES_WORKDIR/public)
#   ES_REPO_URL    upstream clone URL
#   ES_SESSION     tmux session name (default: es1e12)
#   ES_MIN_DISK_GB minimum free disk to start (default: 15)
#   ES_SKIP_TESTS  set to 1 to skip the pytest smoke check (not recommended)
#   WORKERS        worker processes passed to the run (default: all cores)
set -uo pipefail

ES_WORKDIR=${ES_WORKDIR:-"$HOME/erdos-straus-1e12"}
ES_REPO_URL=${ES_REPO_URL:-"https://github.com/jeffmhopkins/Erd-s-Straus-attack"}
ES_REPO_DIR="$ES_WORKDIR/Erd-s-Straus-attack"
ES_PUBLIC_DIR=${ES_PUBLIC_DIR:-"$ES_WORKDIR/public"}
ES_SESSION=${ES_SESSION:-es1e12}
ES_MIN_DISK_GB=${ES_MIN_DISK_GB:-15}
ES_SKIP_TESTS=${ES_SKIP_TESTS:-0}

CONSOLE="$ES_WORKDIR/console.log"        # every run line, UTC-timestamped
SPECS="$ES_WORKDIR/machine_specs.txt"    # captured once at start
SETUP_LOG="$ES_WORKDIR/setup.log"        # clone / venv / pip / pytest
BUNDLE_NAME=es_1e12_artifacts.tar.gz

# Upstream run_1e12.sh writes its logs and the bundle to the repo root.
GEN_LOG="$ES_REPO_DIR/run_1e12_generate.log"
VERIFY_LOG="$ES_REPO_DIR/run_1e12_verify.log"
SHA_FILE="$ES_REPO_DIR/run_1e12_sha256.txt"
BUNDLE="$ES_REPO_DIR/$BUNDLE_NAME"
META="$ES_REPO_DIR/data/hard_primes_1e12_minimalR.meta.json"
SCRATCH="$ES_REPO_DIR/data/hard_primes_1e12_scratch.npz"

step() { printf '\n=== %s ===\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

session_running() { tmux has-session -t "$ES_SESSION" 2>/dev/null; }

# The run appends "ES_LAUNCHER_EXIT=<n>" once the pipeline returns; its presence
# means the process finished (success or failure), its absence means still going.
run_exit_code() {
  [ -f "$CONSOLE" ] || { echo ""; return; }
  grep -oE 'ES_LAUNCHER_EXIT=[0-9]+' "$CONSOLE" | tail -1 | cut -d= -f2
}

# Epoch seconds of the first console line whose text matches $1 (a grep -E
# pattern). Console lines are "<ISO-UTC>\t<text>".
mark_epoch() {
  local ts
  ts=$(grep -E -m1 "$1" "$CONSOLE" 2>/dev/null | cut -f1) || true
  [ -n "$ts" ] && date -d "$ts" +%s 2>/dev/null || echo ""
}

human_dur() { # seconds -> "Hh Mm Ss"
  local s=$1
  printf '%dh %02dm %02ds' $((s / 3600)) $(((s % 3600) / 60)) $((s % 60))
}

require_tools() {
  local missing=()
  for t in git python3 tmux nproc tar sha256sum; do
    command -v "$t" >/dev/null 2>&1 || missing+=("$t")
  done
  [ ${#missing[@]} -eq 0 ] || die "missing required tools: ${missing[*]}"
}

capture_specs() {
  {
    echo "captured: $(date -u +%FT%TZ)"
    echo "host:     $(hostname 2>/dev/null || echo '?')"
    echo "os:       $( . /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || uname -sr)"
    echo "kernel:   $(uname -srm)"
    echo "cpu:      $(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^ *//')"
    echo "cores:    $(nproc) logical ($(getconf _NPROCESSORS_CONF 2>/dev/null || nproc) configured)"
    echo "memory:   $(grep -m1 MemTotal /proc/meminfo 2>/dev/null | awk '{printf "%.1f GiB", $2/1024/1024}')"
    echo "disk(wd): $(df -BG --output=size,avail "$ES_WORKDIR" 2>/dev/null | tail -1 | awk '{print $2" free / "$1" total"}')"
    echo "workers:  ${WORKERS:-$(nproc)}"
  } >"$SPECS"
}

cmd_preflight() {
  step "Preflight"
  require_tools
  info "tools: git, python3, tmux, tar, sha256sum present"
  info "python: $(python3 --version 2>&1)"
  mkdir -p "$ES_WORKDIR"
  local avail
  avail=$(df -BG --output=avail "$ES_WORKDIR" | tail -1 | tr -dc '0-9')
  info "free disk on ${ES_WORKDIR}: ${avail} GB (need >= ${ES_MIN_DISK_GB} GB)"
  [ "${avail:-0}" -ge "$ES_MIN_DISK_GB" ] || die "insufficient disk: ${avail} GB < ${ES_MIN_DISK_GB} GB"
  info "cores: $(nproc)  (run uses all cores; expect ~5-8 h generation on a 16-core box)"
  if session_running; then
    info "NOTE: tmux session '$ES_SESSION' already exists — a run is active."
  fi
  echo
  info "Preflight OK — 'start' to launch."
}

cmd_start() {
  require_tools
  session_running && die "run already active (tmux session '$ES_SESSION'). Use 'status' or 'stop'."
  if [ -f "$BUNDLE" ] && [ "$(run_exit_code)" = "0" ]; then
    die "a completed run already exists ($BUNDLE). Use 'publish', or remove $ES_WORKDIR to rerun."
  fi

  mkdir -p "$ES_WORKDIR"
  cmd_preflight >/dev/null || die "preflight failed"
  capture_specs
  : >"$CONSOLE"

  step "[setup] clone (read-only compute; never pushed upstream)"
  if [ -d "$ES_REPO_DIR/.git" ]; then
    info "reusing existing clone at $ES_REPO_DIR"
  else
    git clone --depth 1 "$ES_REPO_URL" "$ES_REPO_DIR" 2>&1 | tee "$SETUP_LOG" \
      || die "clone failed"
  fi

  step "[setup] venv + pip install -e \".[dev]\""
  ( cd "$ES_REPO_DIR" \
    && python3 -m venv .venv \
    && . .venv/bin/activate \
    && python -m pip install -q --upgrade pip \
    && pip install -e ".[dev]" ) 2>&1 | tee -a "$SETUP_LOG" \
    || die "environment setup failed (see $SETUP_LOG)"

  if [ "$ES_SKIP_TESTS" = "1" ]; then
    info "skipping pytest smoke check (ES_SKIP_TESTS=1)"
  else
    step "[setup] pytest smoke check"
    ( cd "$ES_REPO_DIR" && . .venv/bin/activate && python -m pytest -q ) 2>&1 | tee -a "$SETUP_LOG" \
      || die "smoke tests failed — not launching the long run (override with ES_SKIP_TESTS=1)"
  fi

  # Driver runs inside tmux. It timestamps every console line (so we can derive
  # per-phase wall clock without touching upstream) and records a sentinel exit
  # code the moment the pipeline returns.
  local driver="$ES_WORKDIR/_driver.sh"
  cat >"$driver" <<DRIVER
#!/usr/bin/env bash
set -uo pipefail
cd "$ES_REPO_DIR"
. .venv/bin/activate
{
  ${WORKERS:+WORKERS=$WORKERS }bash scripts/run_1e12.sh
  echo "ES_LAUNCHER_EXIT=\$?"
} 2>&1 | while IFS= read -r line; do
  printf '%s\t%s\n' "\$(date -u +%FT%TZ)" "\$line"
done | tee -a "$CONSOLE"
DRIVER
  chmod +x "$driver"

  step "[launch] starting run in tmux session '$ES_SESSION'"
  tmux new-session -d -s "$ES_SESSION" "bash '$driver'"
  sleep 1
  session_running || die "tmux session failed to start"
  info "launched. Generation is the long phase (~5-8 h on 16 cores)."
  info "watch:   scripts/es_1e12_launcher.sh status"
  info "live:    scripts/es_1e12_launcher.sh attach   (Ctrl-b then d to detach)"
  info "stop:    scripts/es_1e12_launcher.sh stop"
}

# Current phase label from the console markers run_1e12.sh prints.
current_phase() {
  [ -f "$CONSOLE" ] || { echo "not started"; return; }
  if grep -q 'VERIFICATION OK' "$CONSOLE" && grep -q '== done:' "$CONSOLE"; then
    echo "complete"
  elif grep -q '== \[3/3\] packaging' "$CONSOLE"; then
    echo "packaging"
  elif grep -q '== \[2/3\] verification' "$CONSOLE"; then
    echo "verification"
  elif grep -q '== \[1/3\] generation' "$CONSOLE"; then
    echo "generation"
  else
    echo "starting"
  fi
}

cmd_status() {
  step "Erdős–Straus 10^12 run status"
  if [ ! -f "$CONSOLE" ]; then
    info "no run found. 'start' to launch."
    return
  fi
  local phase code running
  phase=$(current_phase)
  code=$(run_exit_code)
  if session_running; then running="yes (tmux '$ES_SESSION')"; else running="no"; fi
  info "phase:    $phase"
  info "running:  $running"

  local t0 tv tp td now
  t0=$(mark_epoch '== \[1/3\] generation')
  tv=$(mark_epoch '== \[2/3\] verification')
  tp=$(mark_epoch '== \[3/3\] packaging')
  td=$(mark_epoch '== done:')
  now=$(date +%s)
  if [ -n "$t0" ]; then
    local end=${td:-$now}
    info "elapsed:  $(human_dur $((end - t0))) since generation start"
    [ -n "$tv" ] && info "  generation:   $(human_dur $((tv - t0)))"
    [ -n "$tv" ] && [ -n "$tp" ] && info "  verification: $(human_dur $((tp - tv)))"
    [ -n "$tp" ] && [ -n "$td" ] && info "  packaging:    $(human_dur $((td - tp)))"
  fi

  # During generation the scratch npz grows toward ~10.5 GB — a coarse progress proxy.
  if [ -f "$SCRATCH" ] && [ "$phase" = "generation" ]; then
    local sz pct
    sz=$(du -m "$SCRATCH" 2>/dev/null | cut -f1)
    pct=$(( sz * 100 / 10752 ))
    info "scratch:  ${sz} MB written (~${pct}% of the ~10.5 GB target — approximate)"
  fi

  if [ -n "$code" ]; then
    if [ "$code" = "0" ]; then
      info "result:   run finished cleanly. Run 'publish' to package the deliverable."
    else
      info "result:   run FAILED (exit $code). See 'logs console'."
    fi
  fi

  echo
  info "last console lines:"
  tail -n 8 "$CONSOLE" 2>/dev/null | sed 's/^/    /'
}

cmd_logs() {
  local which=${1:-console} file
  case "$which" in
    console)  file="$CONSOLE" ;;
    generate) file="$GEN_LOG" ;;
    verify)   file="$VERIFY_LOG" ;;
    setup)    file="$SETUP_LOG" ;;
    *) die "unknown log '$which' (console|generate|verify|setup)" ;;
  esac
  [ -f "$file" ] || die "no $which log yet at $file"
  if session_running; then tail -f "$file"; else tail -n 60 "$file"; fi
}

cmd_attach() {
  session_running || die "no active run to attach to."
  tmux attach -t "$ES_SESSION"
}

cmd_stop() {
  if ! session_running; then
    info "no active run (tmux session '$ES_SESSION' not found)."
    return
  fi
  step "Stopping run"
  # Kill the whole session so run_1e12.sh and every worker process die together.
  tmux kill-session -t "$ES_SESSION" 2>/dev/null || true
  pkill -f 'erdos_straus.bulk_generate' 2>/dev/null || true
  sleep 1
  session_running && die "session still present after kill — check manually" || true
  info "stopped. The run is not resumable; 'start' begins from scratch."
  info "Scratch/partial data kept under $ES_REPO_DIR/data (remove to reclaim disk)."
}

# Pull the headline block run_1e12.sh prints at the end of the console log.
extract_headline() {
  awk '/-- headline/{f=1; next} f' "$CONSOLE" 2>/dev/null | grep -vE 'ES_LAUNCHER_EXIT' | sed '/^$/d'
}

cmd_publish() {
  step "Publish result"
  [ -f "$VERIFY_LOG" ] || die "no verification log yet — run not finished."
  tail -n 3 "$VERIFY_LOG" | grep -q 'VERIFICATION OK' \
    || die "verification did NOT end with 'VERIFICATION OK' — refusing to publish. See 'logs verify'."
  [ -f "$BUNDLE" ] || die "bundle $BUNDLE not found — packaging did not complete."

  mkdir -p "$ES_PUBLIC_DIR"
  cp -f "$BUNDLE" "$ES_PUBLIC_DIR/"
  [ -f "$SHA_FILE" ] && cp -f "$SHA_FILE" "$ES_PUBLIC_DIR/"
  ( cd "$ES_PUBLIC_DIR" && sha256sum "$BUNDLE_NAME" >"$BUNDLE_NAME.sha256" )

  # Per-phase wall clock from the timestamped console.
  local t0 tv tp td gen ver pkg
  t0=$(mark_epoch '== \[1/3\] generation')
  tv=$(mark_epoch '== \[2/3\] verification')
  tp=$(mark_epoch '== \[3/3\] packaging')
  td=$(mark_epoch '== done:')
  [ -n "$t0" ] && [ -n "$tv" ] && gen=$(human_dur $((tv - t0))) || gen="n/a"
  [ -n "$tv" ] && [ -n "$tp" ] && ver=$(human_dur $((tp - tv))) || ver="n/a"
  [ -n "$tp" ] && [ -n "$td" ] && pkg=$(human_dur $((td - tp))) || pkg="n/a"

  local report="$ES_PUBLIC_DIR/es_1e12_report.md"
  {
    echo "# Erdős–Straus census to 10¹² — run report"
    echo
    echo "_Generated $(date -u +%FT%TZ) by es_1e12_launcher.sh._"
    echo
    echo "## Headline"
    echo '```'
    extract_headline
    echo '```'
    echo
    echo "## Wall-clock per phase"
    echo
    echo "| Phase | Duration |"
    echo "|---|---|"
    echo "| Generation | $gen |"
    echo "| Verification | $ver |"
    echo "| Packaging | $pkg |"
    [ -n "$t0" ] && [ -n "$td" ] && echo "| **Total (run)** | **$(human_dur $((td - t0)))** |"
    echo
    echo "## Verification"
    echo
    echo "- \`run_1e12_verify.log\` ends with **VERIFICATION OK**."
    echo
    echo "## Machine specs"
    echo '```'
    cat "$SPECS" 2>/dev/null
    echo '```'
    echo
    echo "## Deliverable"
    echo
    echo "- \`$BUNDLE_NAME\` — the single artifacts bundle (dataset + logs + checksums)."
    echo "- \`$BUNDLE_NAME.sha256\` — its checksum."
    echo "- \`run_1e12_sha256.txt\` — per-file checksums from the run."
  } >"$report"

  # A minimal landing page so the directory can be served as-is.
  cat >"$ES_PUBLIC_DIR/index.html" <<HTML
<!doctype html><meta charset="utf-8">
<title>Erdős–Straus census to 10^12</title>
<h1>Erdős–Straus census to 10<sup>12</sup></h1>
<ul>
<li><a href="$BUNDLE_NAME">$BUNDLE_NAME</a> (artifacts bundle)</li>
<li><a href="$BUNDLE_NAME.sha256">$BUNDLE_NAME.sha256</a></li>
<li><a href="es_1e12_report.md">es_1e12_report.md</a> (headline + timings + specs)</li>
</ul>
HTML

  info "public bundle ready at: $ES_PUBLIC_DIR"
  info "  - $BUNDLE_NAME ($(du -h "$BUNDLE" | cut -f1))"
  info "  - es_1e12_report.md, index.html, checksums"
  echo
  step "Headline"
  extract_headline | sed 's/^/  /'
  echo
  info "Scratch npz is kept ($SCRATCH). Run 'cleanup-scratch' only after integration is confirmed."
}

cmd_cleanup_scratch() {
  step "Cleanup scratch npz"
  [ -f "$SCRATCH" ] || { info "no scratch npz at $SCRATCH — nothing to do."; return; }
  info "removing $SCRATCH ($(du -h "$SCRATCH" | cut -f1))"
  rm -f "$SCRATCH"
  info "done. Published bundle and dataset outputs are unaffected."
}

cmd_help() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; }

main() {
  local cmd=${1:-help}
  shift || true
  case "$cmd" in
    preflight)       cmd_preflight "$@" ;;
    start)           cmd_start "$@" ;;
    status|progress) cmd_status "$@" ;;
    logs)            cmd_logs "$@" ;;
    attach)          cmd_attach "$@" ;;
    stop|kill)       cmd_stop "$@" ;;
    publish|result)  cmd_publish "$@" ;;
    cleanup-scratch) cmd_cleanup_scratch "$@" ;;
    help|-h|--help)  cmd_help ;;
    *) die "unknown command '$cmd' (try: help)" ;;
  esac
}

main "$@"
