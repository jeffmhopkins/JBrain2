"""Environment-driven configuration for the jlaunch control server."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings sourced from ``JLAUNCH_``-prefixed environment variables.

    ``token`` has no default on purpose: a control server without a token must refuse
    to start rather than run unauthenticated (mirrors the supervisor's
    ``SUPERVISOR_TOKEN`` and jcode's ``JCODE_TOKEN``). The api presents it as a bearer
    token on every proxied call.
    """

    model_config = SettingsConfigDict(env_prefix="jlaunch_", case_sensitive=False)

    # Shared secret the JBrain api presents on every control call. No default —
    # fail-closed: no token, no service.
    token: str

    # Root of the per-job checkouts on the scratch volume. Each job gets its own
    # subdirectory (the repo is cloned into it) — the ~15 GB scratch a census run needs.
    # Nothing here is a JBrain blob or note.
    workspace_root: str = "/work"

    # Per-job control state (the assembled job script, the console log, the status /
    # phase-timing / headline files) — dotted so it isn't a checkout sibling a lister
    # sees. On the same volume so it survives a control-server restart.
    state_root: str = "/work/.state"

    # Where a finished run's artifact is copied out of the checkout, so it survives a
    # `git clean`/reset and is served independent of the checkout's lifetime.
    artifact_root: str = "/artifacts"

    # An all-core, multi-hour job must never overlap another (they would fight for every
    # core and the disk). One at a time; a second start 409s until the first ends.
    max_concurrent_jobs: int = 1

    # Log verbosity for the control server (standard level names). DEBUG surfaces the
    # per-job lifecycle detail the owner debug console pulls.
    log_level: str = "INFO"

    # When the owner turns on debug access the whole box is in "investigate a failure"
    # mode, so jlaunch runs verbose with no second switch (shared DEBUG_ACCESS_ENABLED
    # via compose -> JLAUNCH_DEBUG_ACCESS_ENABLED).
    debug_access_enabled: bool = False

    @property
    def effective_log_level(self) -> str:
        """DEBUG whenever debug access is on, else the configured level."""
        return "DEBUG" if self.debug_access_enabled else self.log_level
