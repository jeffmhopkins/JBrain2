"""The tasks scheduler tick's night-hold gate (docs/plans/JMOLT_SITTINGS_PLAN.md, box
reservation). While jmolt's night hold is set, the tick claims NO owner task, so a
scheduled task never contends for the box mid-hour. Pure — the owner resolver is faked,
so no DB or LLM is touched."""

import pytest

from jbrain.tasks import scheduler


class _NightHoldStore:
    def __init__(self, held: bool) -> None:
        self._held = held

    async def night_hold_names(self, ctx: object) -> frozenset[str]:
        return frozenset({"gpt-oss-120b"}) if self._held else frozenset()


class _RecordingRepo:
    def __init__(self) -> None:
        self.claimed = False

    async def claim_due(self, ctx: object, *, now: object) -> list:
        self.claimed = True
        return []


async def _fake_owner_pid(_maker: object) -> str:
    return "owner-1"


async def test_tick_claims_nothing_during_a_jmolt_night(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler, "_owner_principal_id", _fake_owner_pid)
    repo = _RecordingRepo()
    started = await scheduler.tasks_tick(
        maker=None,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        runner=None,  # type: ignore[arg-type]
        settings_store=_NightHoldStore(held=True),  # type: ignore[arg-type]
    )
    assert started == []
    assert repo.claimed is False  # yielded the box: never even claimed


async def test_tick_claims_when_no_night_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guard the guard: with the hold clear the tick DOES claim (here: nothing due).
    monkeypatch.setattr(scheduler, "_owner_principal_id", _fake_owner_pid)
    repo = _RecordingRepo()
    started = await scheduler.tasks_tick(
        maker=None,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        runner=None,  # type: ignore[arg-type]
        settings_store=_NightHoldStore(held=False),  # type: ignore[arg-type]
    )
    assert started == []
    assert repo.claimed is True  # fell through to the claim


async def test_tick_without_a_settings_store_still_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    # settings_store is optional; None keeps the old always-claim behaviour.
    monkeypatch.setattr(scheduler, "_owner_principal_id", _fake_owner_pid)
    repo = _RecordingRepo()
    await scheduler.tasks_tick(
        maker=None,  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        runner=None,  # type: ignore[arg-type]
    )
    assert repo.claimed is True
