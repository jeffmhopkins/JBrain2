#!/bin/bash
set -euo pipefail

# Only bootstrap automatically in Claude Code on the web; local checkouts
# run scripts/dev-setup.sh by hand.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

"$CLAUDE_PROJECT_DIR/scripts/dev-setup.sh"
