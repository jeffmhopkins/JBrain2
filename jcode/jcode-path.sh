# Prepend the session's private tool dirs to PATH so a session-local binary — e.g. one
# installed by `jcode-grok upgrade` into $JCODE_TOOLS_BIN — shadows the image's shared
# /usr/local/bin copy for THIS session only (docs/proposed/JCODE_SESSION_TOOLS_PLAN.md).
#
# This MUST run from /etc/profile.d: `bash -l` sources it AFTER /etc/profile, which on
# Debian RESETS root's PATH to a fixed default — so re-prepending here is the only thing
# that makes the per-session bin actually lead. A no-op for non-session shells (the var
# is unset), so it's safe image-wide. $JCODE_TOOLS_BIN / $HOME are set per session by the
# terminal (jcode_ctl.terminal.home_env).
if [ -n "${JCODE_TOOLS_BIN:-}" ]; then
  PATH="${JCODE_TOOLS_BIN}:${HOME:-}/.npm-global/bin:${PATH}"
  export PATH
fi
