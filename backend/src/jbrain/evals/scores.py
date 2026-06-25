"""Eval-score value types shared by the analysis eval runners.

`FixtureScore` carries one fixture's two dimensions — task success and a
safety/groundedness score (both 0..1) — and `EvalRun` collects a version's
scores across the fixture set. These are pure value objects (stdlib only) so
the runners can build and compare scores without pulling in the workflow stack.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FixtureScore:
    """One fixture's two dimensions: task success and a safety/groundedness score
    (both 0..1). Splitting them is what makes the gate safety-inclusive — a
    promotion can't buy task points with safety points."""

    fixture: str
    task: float
    safety: float


@dataclass(frozen=True)
class EvalRun:
    """A version's scores across the fixture set — a baseline or a candidate."""

    version: str
    scores: tuple[FixtureScore, ...]

    def by_fixture(self) -> dict[str, FixtureScore]:
        return {s.fixture: s for s in self.scores}
