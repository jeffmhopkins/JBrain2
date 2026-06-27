"""Environment-driven configuration for the jcode control server."""

from __future__ import annotations

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
    # sandbox's dev server. OFF by default — it exposes the running app to anyone with
    # the (unguessable) URL, so the owner opts in. Zero Cloudflare config needed
    # (TryCloudflare: no account/token/DNS); the URL dies with the session.
    preview_enabled: bool = False
    preview_default_port: int = 5173

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
