#!/bin/sh
# Keep host-mode jcode web preview turnkey across updates: when the operator has
# enabled it (JCODE_PREVIEW_MODE=host) but the base host is missing, backfill it from
# JBRAIN_DOMAIN — so an update never silently loses the preview hostname. Mirrors how
# the other jcode keys self-heal (deploy/jbrain, deploy/update-inner.sh).
#
# Invariants (why this isn't just an inline grep/printf):
#   - NEVER auto-enables host mode — acts only when MODE=host is already set, so a
#     tunnel-mode / stock box is never flipped.
#   - NEVER stacks duplicate or empty keys — skips an empty or non-hostname domain
#     (the same A-Za-z0-9.- charset proxy-preview-conf.sh enforces), and replaces any
#     stale empty key in place rather than appending beside it.
#   - A no-op (exit 0) on every other box, so update paths can call it unconditionally.
#
# Split out from the update paths so it can be exercised against a throwaway .env.
set -eu

env_file="${1:?usage: jcode-preview-backfill.sh <env-file>}"
[ -f "$env_file" ] || exit 0

# Only act on a box the operator put into host mode...
grep -q '^JCODE_PREVIEW_MODE=host' "$env_file" || exit 0
# ...and only when the base host is missing or empty (a non-empty one already stands).
if grep -q '^JCODE_PREVIEW_BASE_HOST=..*' "$env_file"; then
	exit 0
fi

dom="$(grep '^JBRAIN_DOMAIN=' "$env_file" | cut -d= -f2-)"
# Skip an empty or malformed domain rather than write a bad/empty key that would
# re-stack on every future update and could reach the edge.
case "$dom" in
	"" | *[!A-Za-z0-9.-]*) exit 0 ;;
esac

# Drop any stale empty JCODE_PREVIEW_BASE_HOST line first, so we replace rather than
# stack, then append the value.
if grep -q '^JCODE_PREVIEW_BASE_HOST=' "$env_file"; then
	tmp="$env_file.tmp.$$"
	grep -v '^JCODE_PREVIEW_BASE_HOST=' "$env_file" > "$tmp"
	mv "$tmp" "$env_file"
fi
printf 'JCODE_PREVIEW_BASE_HOST=%s\n' "$dom" >> "$env_file"
