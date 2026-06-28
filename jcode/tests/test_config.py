"""The control server must refuse to start without a token (fail-closed)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jcode_ctl.config import Settings


def test_token_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JCODE_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings()  # pyright: ignore[reportCallIssue]


def test_defaults_are_local_and_fail_closed() -> None:
    s = Settings(token="x")
    assert s.model_base_url.startswith("http://local-llm")
    assert "github.com" in s.egress_allowlist
    # No cloud host in the default egress allowlist — local-only, no LLM egress.
    assert not any("anthropic" in h for h in s.egress_allowlist)


def test_session_cap_defaults_on() -> None:
    # A stock box is bounded out of the box: the live-session cap ships enabled
    # (non-zero), alongside idle-TTL GC and the aggregate compose caps.
    s = Settings(token="x")
    assert s.max_sessions > 0


def test_preview_defaults_on() -> None:
    # This control server only runs under the opt-in `jcode` profile, so code mode
    # being on already means preview is available — no second env var. The flag only
    # gates the capability; a preview is still opened deliberately, per session.
    s = Settings(token="x")
    assert s.preview_enabled is True


def test_effective_log_level_defaults_to_configured_level() -> None:
    # Debug access off → the effective level is whatever JCODE_LOG_LEVEL set (INFO).
    s = Settings(token="x")
    assert s.debug_access_enabled is False
    assert s.effective_log_level == "INFO"
    assert Settings(token="x", log_level="WARNING").effective_log_level == "WARNING"


def test_inverted_preview_port_pool_is_rejected() -> None:
    # A low>high pool would silently make every host-preview allocation "exhausted";
    # the validator turns that config typo into a clear startup error.
    with pytest.raises(ValidationError):
        Settings(token="x", preview_port_low=5200, preview_port_high=5173)


def test_debug_access_forces_debug_level() -> None:
    # With debug access on, jcode runs verbose regardless of the configured level — the
    # owner is investigating the box, so DEBUG wins (overriding even a quieter setting).
    s = Settings(token="x", debug_access_enabled=True, log_level="WARNING")
    assert s.effective_log_level == "DEBUG"
