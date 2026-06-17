"""`skill_sweep` action seam logic (no DB): the kill-switch gate refuses before any demotion, and a
clean gate runs the eviction with the cap read live from settings. The eviction ORDER itself is SQL
and lives in the integration test (it needs Postgres)."""

from typing import Any

import pytest

from jbrain.agent.skillsweep import SkillSweepAction
from jbrain.queue import PermanentJobError


class _FakeSettings:
    """Duck-types the SqlSettingsStore methods the gate + action read."""

    def __init__(self, *, kill: bool = False, cap: int = 3) -> None:
        self._kill = kill
        self._cap = cap

    async def self_improvement_kill_switch(self, ctx: Any) -> bool:
        return self._kill

    async def self_improvement_daily_budget(self, ctx: Any) -> int:
        return 200_000

    async def self_improvement_spent_today(self, ctx: Any, *, day: str) -> int:
        return 0

    async def skill_active_cap(self, ctx: Any) -> int:
        return self._cap


class _FakeSkills:
    def __init__(self) -> None:
        self.cap: int | None = None

    async def demote_over_cap(self, ctx: Any, cap: int) -> list[tuple[str, str]]:
        self.cap = cap
        return [("s1", "general")]


def _action(settings: _FakeSettings, skills: _FakeSkills) -> SkillSweepAction:
    return SkillSweepAction(
        None,  # type: ignore[arg-type]  # maker unused (the repo is injected)
        settings=settings,  # type: ignore[arg-type]
        skills=skills,  # type: ignore[arg-type]
    )


async def test_sweep_demotes_with_the_configured_cap() -> None:
    skills = _FakeSkills()
    await _action(_FakeSettings(cap=7), skills).run({})
    assert skills.cap == 7  # cap is read live from settings, not hardcoded


async def test_sweep_refused_when_kill_switch_on() -> None:
    skills = _FakeSkills()
    with pytest.raises(PermanentJobError):
        await _action(_FakeSettings(kill=True), skills).run({})
    assert skills.cap is None  # nothing demoted behind the gate
