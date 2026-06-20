#!/usr/bin/env bash
# Idle-CPU benchmark: sample per-container CPU while the stack rests, so a tuning
# change can be attributed to a specific service instead of guessed (docs/PERF_IDLE.md).
#
# Run it with NO client connected (close the PWA/app) for a true zero-request
# baseline, take a reading, apply one lever, and read again. `docker stats` reports
# CPU as a percentage of ONE core, so 100% == one fully-busy core; totals can exceed
# 100% on multi-core hosts.
#
#   deploy/idle-cpu-bench.sh            # 12 samples, 5s apart (~1 min)
#   deploy/idle-cpu-bench.sh 30 2       # 30 samples, 2s apart
set -euo pipefail

SAMPLES="${1:-12}"
INTERVAL="${2:-5}"

command -v docker >/dev/null || { echo "docker not found" >&2; exit 1; }

echo "Sampling $SAMPLES times every ${INTERVAL}s (close the app for a true idle reading)..."

# name -> running CPU% sum, and a sample count, accumulated across passes.
declare -A sum
n=0
for _ in $(seq 1 "$SAMPLES"); do
  # --no-stream takes one instantaneous reading of every running container.
  while IFS=$'\t' read -r name cpu; do
    sum["$name"]=$(awk -v a="${sum[$name]:-0}" -v b="${cpu%\%}" 'BEGIN{print a+b}')
  done < <(docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}')
  n=$((n + 1))
  sleep "$INTERVAL"
done

echo
printf '%-28s %10s\n' "CONTAINER" "MEAN_CPU%"
printf '%-28s %10s\n' "---------" "---------"
total=0
# Mean per container, highest first; accumulate the total in this (non-subshell) loop.
while IFS= read -r line; do
  printf '%-28s %10s\n' "${line% *}" "${line##* }"
done < <(
  for name in "${!sum[@]}"; do
    awk -v nm="$name" -v s="${sum[$name]}" -v n="$n" 'BEGIN{printf "%s %.2f\n", nm, s/n}'
  done | sort -k2 -rn
)
for name in "${!sum[@]}"; do
  total=$(awk -v a="$total" -v s="${sum[$name]}" -v n="$n" 'BEGIN{printf "%.2f", a + s/n}')
done
printf '%-28s %10s\n' "TOTAL" "$total"
