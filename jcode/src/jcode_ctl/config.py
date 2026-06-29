"""Environment-driven configuration for the jcode control server."""

from __future__ import annotations

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings sourced from ``JCODE_``-prefixed environment variables.

    ``jcode_token`` has no default on purpose: a control server without a token
    must refuse to start rather than run unauthenticated (mirrors the
    supervisor's ``SUPERVISOR_TOKEN``). The api presents it as a bearer token on
    every proxied call.
    """

    model_config = SettingsConfigDict(env_prefix="jcode_", case_sensitive=False)

    # Shared secret the JBrain api presents on every control call. No default —
    # fail-closed: no token, no service.
    token: str

    # Root of the per-session git checkouts, on the sandbox volume. Each session
    # gets its own subdirectory; nothing here is a JBrain blob or note.
    workspace_root: str = "/work"

    # Root of per-session $HOME dirs, on the SAME volume as the checkouts so they
    # persist across a pause and are purged with the session on delete. Each session
    # gets its own $HOME — own ~/.grok, ~/.claude, history, npm prefix, a private bin
    # on the front of PATH — so per-session tool versions never collide (see
    # JCODE_SESSION_TOOLS_PLAN). Dotted so it's not a checkout sibling a lister sees.
    home_root: str = "/work/.home"

    # The Anthropic-compatible base URL the terminal's ``claude`` CLI is pointed at —
    # the on-box gateway's /v1/messages (or a thin shim in front of it). Informational
    # here; the CLI reads ANTHROPIC_BASE_URL from the process environment (set by the
    # Dockerfile/compose).
    model_base_url: str = "http://local-llm:8080"

    # The served model name the gateway resolves (llama-swap id). Local-only. Pins the
    # terminal's ``claude`` CLI to the on-box coder.
    model: str = "qwen3-coder-next"

    # Outbound hosts the sandbox may reach for git/package work (no LLM egress —
    # the model is on-box). The declared intent; full enforcement is the opt-in
    # egress-proxy seam in compose (Wave J5), left opt-in pending on-box verification.
    egress_allowlist: list[str] = ["github.com", "registry.npmjs.org"]

    # Ceiling on concurrent live sandboxes. CPU/mem/PID governance ships as compose
    # resource caps and idle-TTL GC (Wave J5). Zero or negative disables the cap.
    max_sessions: int = 8

    # Per-session web preview (Wave J4): an ephemeral Cloudflare quick-tunnel to the
    # sandbox's dev server. ON whenever code mode is — this control server only runs
    # under the opt-in `jcode` profile, so "jcode enabled" already means "preview
    # available", with no second env var to set. The flag here only GATES whether a
    # preview may be opened; nothing is exposed until the owner deliberately opens one
    # on a specific session. Zero Cloudflare config needed (TryCloudflare: no
    # account/token/DNS); the URL dies with the session. Set JCODE_PREVIEW_ENABLED=false
    # to withhold the capability entirely.
    preview_enabled: bool = True
    preview_default_port: int = 5173

    # The zone host previews hang under, reached at
    # https://<slug>-preview.<preview_base_host>. Flattened to one label so the zone's
    # *.<host> Universal SSL covers it (no Advanced Certificate Manager). Empty (the
    # default) fail-closes host mode. Set with JCODE_PREVIEW_BASE_HOST.
    preview_base_host: str = ""
    # Host mode's per-session dev-port pool [low, high] — its size is the max
    # concurrent previews. Each session reserves one for its life; the shell binds it.
    preview_port_low: int = 5173
    preview_port_high: int = 5199

    @model_validator(mode="after")
    def _check_preview_port_pool(self) -> Settings:
        # An inverted range silently yields an empty pool (every allocation
        # "exhausted"), so reject the typo at startup with a clear message instead.
        if self.preview_port_low > self.preview_port_high:
            raise ValueError("preview_port_low must be <= preview_port_high")
        return self

    # Session GC (Wave J5): reap a session (its checkout + any tunnel) after this many
    # seconds with no activity — abandoned sandboxes don't pile up. An open terminal
    # keeps a session fresh; a deliberately-paused (stopped) session is never reaped.
    # Default 24h; 0 disables. A committed/pushed branch survives a reap; only the local
    # checkout is dropped.
    session_ttl_seconds: int = 86_400
    # How often the reaper sweeps for idle sessions.
    reap_interval_seconds: int = 600

    # Log verbosity for the control server (standard level names). INFO logs the session
    # + terminal lifecycle, errors, and lifecycle events — flip to DEBUG
    # (JCODE_LOG_LEVEL=DEBUG) when chasing a failure, then pull the logs via the owner
    # debug console (/debug/jcode/logs for the whole system, or /debug/logs/jcode for
    # just this service).
    log_level: str = "INFO"

    # When the owner turns on debug access (docs/DEBUG_ACCESS.md) the whole box is in
    # "investigate a failure" mode, so jcode runs verbose with no second switch: this
    # forces the effective level to DEBUG, surfacing the per-request / tunnel-line
    # detail the owner debug console pulls. From the shared DEBUG_ACCESS_ENABLED flag
    # via compose (JCODE_DEBUG_ACCESS_ENABLED), picked up on the same `jbrain up` that
    # enables debug access (a recreate, not a restart).
    debug_access_enabled: bool = False

    @property
    def effective_log_level(self) -> str:
        """DEBUG whenever debug access is on (the owner is debugging the box), else the
        configured level. Keeps the gating in one place for the entrypoint to read."""
        return "DEBUG" if self.debug_access_enabled else self.log_level
