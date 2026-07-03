#!/usr/bin/env bash
# docs-freshness.sh — advisory freshness check for docs/, per docs/DOC_LIFECYCLE.md.
#
# Flags the rot patterns this repo actually accumulated: volatile counters
# hardcoded in prose (R1), shipped plans still labelled active (R4), docs
# missing a freshness header (R2/R3), and directory indexes that mis-list their
# folder (homes). Checks patterns, never a specific migration number — the check
# must not itself rot. Examples inside ``` fences and `inline code` are ignored,
# so a doc may quote a rot pattern to teach it without tripping the gate.
#
# Exit nonzero on any ERROR (CI-gateable). WARNs never fail the run.
# Note: the >90-day age check uses `date -d` (GNU/Linux); it no-ops elsewhere.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
DOCS="$ROOT/docs"
errors=0
warns=0

err()  { printf 'ERROR %s\n' "$1"; errors=$((errors + 1)); }
warn() { printf 'WARN  %s\n' "$1"; warns=$((warns + 1)); }

# DOC_LIFECYCLE.md and DOC_CLEANUP_PLAN.md are *about* the rot patterns and quote
# them in prose (outside code spans) as the examples they teach; exempt them from
# the prose phrase check (R1).
is_meta() { case "$1" in */DOC_LIFECYCLE.md|*/DOC_CLEANUP_PLAN.md) return 0;; *) return 1;; esac; }

# Emit "<origline><TAB><text>" for prose lines only: skip fenced code blocks and
# blank out `inline code` spans, so examples don't read as claims.
prose() {
  awk '
    /^[[:space:]]*```/ { infence = !infence; next }
    { if (infence) next; line = $0; gsub(/`[^`]*`/, "", line); printf "%d\t%s\n", NR, line }
  ' "$1"
}

# R1 — no volatile migration-head counter in prose outside archive/.
# Matches "migrations run through 0044", "head at 0114", "**0044**", `0114`, etc.
# Requires a 0NNN shape (migration ids are zero-padded) so years never match.
R1='(migration|schema)[^.]{0,30}(run through|running through|through|head|latest|up to|as of|at)[^.0-9]{0,12}0[0-9]{3}'
while IFS= read -r f; do
  case "$f" in */archive/*) continue;; esac
  is_meta "$f" && continue
  while IFS=$'\t' read -r ln _; do
    [ -z "$ln" ] && continue
    err "$f:$ln — hardcoded migration counter in prose (R1: state it under 'Last verified' or point at backend/migrations/versions/)"
  done < <(prose "$f" | grep -iE "$R1" || true)
done < <(find "$DOCS" -name '*.md' 2>/dev/null | sort)

# R4 — a plan doc whose header Waves are all done (✅) but whose Status is not
# Shipped/archived should be archived. Activates once a doc carries the header.
EMPTY_BOX='◻|◻️|⬜|▫️|▢|◯|❌|🔲|\[ \]'
while IFS= read -r f; do
  case "$f" in */archive/*) continue;; esac
  header="$(head -n 8 "$f" || true)"
  waves="$(printf '%s' "$header" | grep -m1 -oE 'Waves:\*\*[^|]*' || true)"
  [ -z "$waves" ] && continue
  printf '%s' "$waves" | grep -q '✅' || continue
  printf '%s' "$waves" | grep -qE "$EMPTY_BOX" && continue   # some wave unfinished
  status="$(printf '%s' "$header" | grep -m1 -oiE 'Status:\*\* *[A-Za-z]+' || true)"
  case "$status" in *[Ss]hipped*) ;; *) err "$f — all header Waves are ✅ but Status is not Shipped — archive it (R4)";; esac
done < <(find "$DOCS" -name '*.md' 2>/dev/null | sort)

# R2/R3 — every non-archived doc opens with a freshness header (a Status line in
# the first 6 lines). Absence is a warning.
while IFS= read -r f; do
  case "$f" in */archive/*|*/mocks/*) continue;; esac
  head -n 6 "$f" | grep -q '\*\*Status:\*\*' \
    || warn "$f — no freshness header ('> **Status:** … · **Last verified:** …') in first 6 lines (R2/R3)"
done < <(find "$DOCS" -name '*.md' 2>/dev/null | sort)

# Homes — a directory README must name every *.md sibling in the folder.
# (Under-listing only: a README also cross-references non-sibling docs in prose,
# so "names a file not present" can't be told from a legitimate mention.)
check_index() {
  local dir="$1" readme="$1/README.md" f base
  [ -f "$readme" ] || return 0
  for f in "$dir"/*.md; do
    [ -e "$f" ] || continue
    base="$(basename "$f")"
    [ "$base" = "README.md" ] && continue
    grep -Fqw "$base" "$readme" || warn "$readme — index omits $base (homes: an index must name every file in its folder)"
  done
}
check_index "$DOCS/proposed"
check_index "$DOCS/archive"

# Freshness age — warn only on ACTIVE plan docs (Scheduled/In progress) verified
# > 90 days ago. Stable Living runbooks are exempt (GNU date only).
today="$(date +%s)"
while IFS= read -r f; do
  status="$(head -n 8 "$f" | grep -m1 -oiE 'Status:\*\* *[A-Za-z ]+' || true)"
  case "$status" in *[Pp]rogress*|*[Ss]cheduled*) ;; *) continue;; esac
  d="$(grep -m1 -oE 'Last verified:\*\* *[0-9]{4}-[0-9]{2}-[0-9]{2}' "$f" 2>/dev/null | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' || true)"
  [ -z "$d" ] && continue
  if ts="$(date -d "$d" +%s 2>/dev/null)"; then
    age=$(( (today - ts) / 86400 ))
    [ "$age" -gt 90 ] && warn "$f — Last verified $d is ${age}d old (>90); re-verify or bump"
  fi
done < <(find "$DOCS" -maxdepth 1 -name '*.md' 2>/dev/null | sort)

printf '\ndocs-freshness: %d error(s), %d warning(s)\n' "$errors" "$warns"
[ "$errors" -eq 0 ]
