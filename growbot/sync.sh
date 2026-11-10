#!/bin/sh
# Re-build growbot/site/ and growbot/firmware/ from the upstream commit pinned in
# patches/upstream.json. To move to a new upstream, change the pin, run this, and review the
# diff in its own PR: a patch that no longer applies fails here, naming itself.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["repo"])' "$here/patches/upstream.json")
commit=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["commit"])' "$here/patches/upstream.json")
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git init -q "$work/upstream"
git -C "$work/upstream" fetch -q --depth 1 "$repo" "$commit"
git -C "$work/upstream" checkout -q FETCH_HEAD
python3 -I "$here/patches/apply.py" --upstream "$work/upstream" --vendor-cache "${GROWBOT_VENDOR_CACHE:-$work/vendor}" --out "$here"
