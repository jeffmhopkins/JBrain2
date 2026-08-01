# Tell the in-sandbox coding agents (grok / claude) about the shell tools this sandbox
# provides. Both CLIs read CLAUDE.md / AGENTS.md as instruction context, but NEITHER treats
# the shell's login banner as available tools — so without this grok inspects only its MCP
# tool registry, finds nothing, and tells the owner it can't search the web or read an image
# (observed on-box). We write the memory file to the workspace ROOT
# (${JCODE_WORKSPACE_ROOT}), the parent of every session's git checkout, so both CLIs pick it
# up as a parent-directory memory file WITHOUT ever dirtying the owner's repo (it never shows
# in the checkout's `git status`). Static content — the helpers self-gate (web-search prints
# "not enabled" when the session didn't opt in), so one shared file serves every session.
# Sourced by the interactive /bin/bash -l via /etc/profile.d, like grok-config.sh.
_root="${JCODE_WORKSPACE_ROOT:-/work}"
if [ -d "$_root" ] && [ -w "$_root" ]; then
  cat > "$_root/AGENTS.md" <<'MD'
# Sandbox tools

This coding sandbox gives you a few extra capabilities as **shell commands** you run in
your terminal. They are NOT MCP tools and will NOT appear in any tool/MCP registry — do not
go looking for them there. Just run the command:

- `ocr <file>` — read the EXACT text out of a local image or PDF using the box's on-box OCR
  (deterministic, verbatim; a PDF is read page by page). Always available. Prefer this over
  telling the owner you can't read an image.
- `web-search <query>` — search the web via this box's own SearXNG; prints ranked results
  with URLs.
- `web-fetch <url>` — fetch a URL and print its readable text.

`web-search` / `web-fetch` work when this session has web search enabled; if it isn't, they
print a clear "not enabled" message (nothing to do — just tell the owner it's off). Reach for
these instead of telling the owner you have no web access — in this sandbox, you do.
MD
  # grok reads the AGENTS.md family AND CLAUDE.md; claude reads CLAUDE.md. Same content in
  # both names so whichever convention a CLI honors, it finds the guidance.
  cp -f "$_root/AGENTS.md" "$_root/CLAUDE.md" 2>/dev/null || true
fi
