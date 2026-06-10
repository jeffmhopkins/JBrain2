#!/bin/bash
set -euo pipefail

# Only bootstrap automatically in Claude Code on the web; local checkouts
# run scripts/dev-setup.sh by hand.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Async: the session starts while setup runs in the background. dev-setup is
# idempotent and exits fast when dependencies are already current.
echo '{"async": true, "asyncTimeout": 600000}'

"$CLAUDE_PROJECT_DIR/scripts/dev-setup.sh"
