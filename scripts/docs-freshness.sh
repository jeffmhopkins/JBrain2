#!/usr/bin/env bash
# docs-freshness.sh — advisory freshness check for docs/, per docs/DOC_LIFECYCLE.md.
#
# Flags the rot patterns that this repo actually accumulated: volatile counters
# hardcoded in prose, shipped plans still labelled active, docs missing a
# freshness header, and directory indexes that under-list their own folder.
#
# Exit nonzero on any ERROR (CI-gateable). WARNs never fail the run.
# Checks patterns, never a specific migration number — the check must not rot.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
DOCS="$ROOT/docs"
errors=0
warns=0

err()  { printf 'ERROR %s\n' "$1"; errors=$((errors + 1)); }
warn() { printf 'WARN  %s\n' "$1"; warns=$((warns + 1)); }

# These two docs *are about* the rot patterns — they quote "no code lands",
# "migrations run through NNNN", etc. as the examples they teach, so the phrase
# checks (R1/R4) must not fire on them.
is_meta() { case "$1" in */DOC_LIFECYCLE.md|*/DOC_CLEANUP_PLAN.md) return 0;; *) return 1;; esac; }

# R1 — no volatile counters in prose outside archive/.
# Matches "migrations run through 0044", "migration head is 0044", etc.
while IFS=: read -r file line _; do
  [ -z "$file" ] && continue
  is_meta "$file" && continue
  err "$file:$line — hardcoded migration counter in prose (R1: state it under 'Last verified' or point at backend/migrations/versions/)"
done < <(grep -rniE 'migrations?[^.]{0,24}(run through|through|head[^.]{0,8}is|up to) +[0-9]{3,4}' \
           --include='*.md' "$DOCS" 2>/dev/null | grep -v '/archive/' || true)

# R4 — a "shipped" tell still living in an active doc (docs/ root or proposed/).
while IFS=: read -r file line _; do
  [ -z "$file" ] && continue
  case "$file" in */archive/*) continue;; esac
  is_meta "$file" && continue
  err "$file:$line — active doc claims unbuilt ('no code lands' / 'nothing is built yet') — flip status or archive (R4)"
done < <(grep -rniE 'no code lands|nothing (is|to) (built|build)|nothing is built yet' \
           --include='*.md' "$DOCS" 2>/dev/null || true)

# R2/R3 — every non-archived doc opens with a freshness header (Status line in first 6 lines).
while IFS= read -r f; do
  case "$f" in */archive/*|*/mocks/*) continue;; esac
  if ! head -n 6 "$f" | grep -q '\*\*Status:\*\*'; then
    warn "$f — no freshness header ('> **Status:** … · **Last verified:** …') in first 6 lines (R2/R3)"
  fi
done < <(find "$DOCS" -name '*.md' 2>/dev/null | sort)

# Homes — a directory README must list every *.md sibling (index not under-listing).
check_index() {
  local dir="$1" readme="$1/README.md"
  [ -f "$readme" ] || return 0
  local f base
  for f in "$dir"/*.md; do
    [ -e "$f" ] || continue
    base="$(basename "$f")"
    [ "$base" = "README.md" ] && continue
    grep -q "$base" "$readme" || warn "$readme — index omits $base (homes: an index must list its whole folder)"
  done
}
check_index "$DOCS/proposed"
check_index "$DOCS/archive"

# Freshness age — warn on Living docs verified > 90 days ago.
today=$(date +%s)
while IFS= read -r f; do
  case "$f" in */archive/*|*/mocks/*) continue;; esac
  d=$(grep -m1 -oE 'Last verified:\*\* *[0-9]{4}-[0-9]{2}-[0-9]{2}' "$f" 2>/dev/null | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' || true)
  [ -z "$d" ] && continue
  if ts=$(date -d "$d" +%s 2>/dev/null); then
    age=$(( (today - ts) / 86400 ))
    [ "$age" -gt 90 ] && warn "$f — Last verified $d is ${age}d old (>90); re-verify or bump"
  fi
done < <(find "$DOCS" -maxdepth 1 -name '*.md' 2>/dev/null | sort)

printf '\ndocs-freshness: %d error(s), %d warning(s)\n' "$errors" "$warns"
[ "$errors" -eq 0 ]
