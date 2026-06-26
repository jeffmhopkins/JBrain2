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


def test_per_session_ceilings_default_on() -> None:
    # A stock box is bounded out of the box: the concurrency + disk ceilings ship
    # enabled (non-zero), so the aggregate compose caps aren't the only thing standing
    # between a runaway session and the whole box.
    s = Settings(token="x")
    assert s.max_concurrent_turns > 0
    assert s.session_disk_limit_mb > 0
