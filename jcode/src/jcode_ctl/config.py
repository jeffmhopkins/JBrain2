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

    # The Anthropic-compatible base URL the Claude Agent SDK is pointed at — the
    # on-box gateway's /v1/messages (or a thin shim in front of it). Informational
    # here; the real adapter reads ANTHROPIC_BASE_URL from the process environment
    # (set by the Dockerfile/compose) so the SDK and its CLI agree. The on-box
    # smoke test in JCODE_PLAN.md decides native-endpoint vs shim.
    model_base_url: str = "http://local-llm:8080"

    # The served model name the gateway resolves (llama-swap id). Local-only.
    model: str = "qwen3-coder-next"

    # Outbound hosts the sandbox may reach for git/package work (no LLM egress —
    # the model is on-box). The declared intent; full enforcement is the opt-in
    # egress-proxy seam in compose (Wave J5), left opt-in pending on-box verification.
    egress_allowlist: list[str] = ["github.com", "registry.npmjs.org"]

    # Ceiling on concurrent live sandboxes. CPU/mem/PID governance ships as compose
    # resource caps and idle-TTL GC (Wave J5). Zero or negative disables the cap.
    max_sessions: int = 8

    # Ceiling on concurrent in-flight turns across ALL sessions. Each turn drives a
    # model + tool loop — the real CPU/mem load — so this bounds load independently of
    # the live-session cap above: 8 idle sandboxes are cheap, 8 simultaneous turns are
    # not. Over the cap a new turn is refused (the client sees a clean "at turn
    # capacity" error) until one finishes. The aggregate compose CPU/mem caps stay the
    # hard ceiling; this keeps a burst of turns from thrashing them. Zero/neg disables.
    max_concurrent_turns: int = 4

    # Per-session checkout disk ceiling (MB), checked at each turn start: a session that
    # exceeds it refuses new turns (reset or delete to recover) so a runaway
    # build or log can't fill the shared sandbox volume and take every session down with
    # it. This is du-style/after-the-fact — it stops the NEXT turn, not the write in
    # flight (real-time would need a filesystem quota, out of the aggregate-caps lane).
    # Zero disables.
    session_disk_limit_mb: int = 2048

    # Per-session web preview (Wave J4): an ephemeral Cloudflare quick-tunnel to the
    # sandbox's dev server. OFF by default — it exposes the running app to anyone with
    # the (unguessable) URL, so the owner opts in. Zero Cloudflare config needed
    # (TryCloudflare: no account/token/DNS); the URL dies with the session.
    preview_enabled: bool = False
    preview_default_port: int = 5173

    # Session GC (Wave J5): reap a session (its checkout + any tunnel) after this many
    # seconds with no turn — abandoned sandboxes don't pile up. A *running* turn keeps
    # a session fresh, so an active session is never reaped. Default 24h; 0 disables.
    # A committed/pushed branch survives a reap; only the local checkout is dropped.
    session_ttl_seconds: int = 86_400
    # How often the reaper sweeps for idle sessions.
    reap_interval_seconds: int = 600

    # Log verbosity for the control server (standard level names). INFO logs the turn
    # lifecycle, tool calls, errors, and lifecycle events; DEBUG adds every SDK message
    # — flip to DEBUG (JCODE_LOG_LEVEL=DEBUG) when chasing an on-box turn failure, then
    # pull the logs via the owner debug console (/debug/jcode/logs for the whole system,
    # or /debug/logs/jcode for just this service).
    log_level: str = "INFO"
