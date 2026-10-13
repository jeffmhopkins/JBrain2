"""The desk panel's voice post, apart from the database: the upload's integrity check, and the
pickup's tag, which must never cost the pickup itself."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from fastapi import HTTPException

from jbrain.api import panel_messages
from jbrain.auth.service import PrincipalInfo

AUDIO = b"\x10\x20" * 800
DESK = PrincipalInfo(id="d", kind="device_key", label="panel desk", subject_id="s")


def test_a_trailing_digest_is_checked_and_stripped() -> None:
    body = AUDIO + hashlib.sha256(AUDIO).digest()
    assert panel_messages._verified(body, "trailer", "q") == AUDIO
    for bad in (AUDIO + b"\0" * 32, b"x" * 31, AUDIO[:-2] + hashlib.sha256(AUDIO).digest()):
        with pytest.raises(HTTPException) as e:
            panel_messages._verified(bad, "trailer", "q")
        assert e.value.status_code == 422


def test_a_header_digest_is_checked_as_the_twins_send_checks_it() -> None:
    assert panel_messages._verified(AUDIO, hashlib.sha256(AUDIO).hexdigest(), "q") == AUDIO
    with pytest.raises(HTTPException):
        panel_messages._verified(AUDIO, "0" * 64, "q")


def test_an_unverified_upload_is_taken_whole() -> None:
    body = AUDIO + b"\1" * 32
    assert panel_messages._verified(body, "", "q") == body


async def test_the_pickup_tag_is_empty_when_the_gate_or_the_owner_says_no(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refused(maker: Any, principal: Any) -> bool:
        return False

    async def allowed(maker: Any, principal: Any) -> bool:
        return True

    async def no_owner(maker: Any) -> None:
        return None

    monkeypatch.setattr(panel_messages, "agent_allowed", refused)
    assert await panel_messages.pickup_tag(object(), DESK) == ""  # type: ignore[arg-type]
    monkeypatch.setattr(panel_messages, "agent_allowed", allowed)
    monkeypatch.setattr(panel_messages, "owner_principal_id", no_owner)
    assert await panel_messages.pickup_tag(object(), DESK) == ""  # type: ignore[arg-type]


async def test_the_pickup_tag_never_breaks_the_pickup(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(maker: Any, principal: Any) -> bool:
        raise RuntimeError("database gone")

    monkeypatch.setattr(panel_messages, "agent_allowed", broken)
    assert await panel_messages.pickup_tag(object(), DESK) == ""  # type: ignore[arg-type]
