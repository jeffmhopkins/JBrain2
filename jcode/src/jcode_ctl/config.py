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
    # the model is on-box). Enforcement of this allowlist is a Wave J5 hardening
    # item (an egress proxy / firewall); J1 carries the declared intent.
    egress_allowlist: list[str] = ["github.com", "registry.npmjs.org"]

    # Ceiling on concurrent live sandboxes (full governance — CPU/mem/disk, TTL —
    # is Wave J5). Zero or negative disables the cap.
    max_sessions: int = 8
