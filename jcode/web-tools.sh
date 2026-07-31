# Announce the web-search / web-fetch helpers in a session that opted into web search, so
# the owner — and the coding agent's own shell output — sees the capability at a glance.
# Silent when the session did not opt in; the helpers themselves explain the gate if called
# (docs/plans/JCODE_GROK_INTERNET_PLAN.md). Sourced by the interactive /bin/bash -l via
# /etc/profile.d, like grok-config.sh.
if [ "${JCODE_INTERNET_SEARCH:-}" = "1" ]; then
  echo "web search on: 'web-search <query>' and 'web-fetch <url>' query this box's SearXNG."
fi
