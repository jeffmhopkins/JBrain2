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
