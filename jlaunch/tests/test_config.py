"""The control server must refuse to start without a token (fail-closed)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jlaunch_ctl.config import Settings


def test_missing_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JLAUNCH_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings()  # pyright: ignore[reportCallIssue]


def test_empty_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Compose's `${JLAUNCH_TOKEN:-}` turns an unset key into an empty variable.
    monkeypatch.setenv("JLAUNCH_TOKEN", "")
    with pytest.raises(ValidationError):
        Settings()  # pyright: ignore[reportCallIssue]


def test_minted_token_from_env_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    minted = "a" * 64  # install.sh and update-inner.sh mint a 64-hex token
    monkeypatch.setenv("JLAUNCH_TOKEN", minted)
    assert Settings().token == minted  # pyright: ignore[reportCallIssue]
