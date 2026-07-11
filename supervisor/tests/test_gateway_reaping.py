"""ComposeDockerGateway reaps a hung one-shot instead of wedging forever.

The mutual-exclusion guard blocks a new update/provision while one is Running. A
one-shot that HANGS (a provision stuck on a silently stalled hf download) would
otherwise never leave Running and block every future update forever — the wedge
that stranded a model install. Past _ONESHOT_MAX_RUNTIME_S a still-running one-shot
is treated as dead and reaped so a fresh one can start. These drive the real
gateway against a fake docker client (no daemon).
"""

from __future__ import annotations

import time
from typing import Any, cast

import pytest

from supervisor.gateway import (
    _ONESHOT_MAX_RUNTIME_S,
    ONESHOT_LABEL,
    ComposeDockerGateway,
    UpdateInProgressError,
)


class _FakeContainer:
    def __init__(self, name: str, labels: dict[str, str], running: bool) -> None:
        self.name = name
        self.labels = labels
        # Created is only used by _latest's tie-break; a fixed value is fine here.
        self.attrs = {"State": {"Running": running}, "Created": "2026-07-11T00:00:00Z"}
        self.removed = False

    def remove(self, force: bool = False) -> None:
        self.removed = True


class _FakeContainers:
    def __init__(self, items: list[_FakeContainer]) -> None:
        self._items = items
        self.run_kwargs: list[dict[str, Any]] = []

    def list(
        self, all: bool = False, filters: dict[str, str] | None = None
    ) -> list[_FakeContainer]:
        key, sep, value = (filters or {}).get("label", "").partition("=")
        return [
            c
            for c in self._items
            if not c.removed and key in c.labels and (not sep or c.labels[key] == value)
        ]

    def run(self, image: str, **kwargs: Any) -> _FakeContainer:
        self.run_kwargs.append(kwargs)
        c = _FakeContainer(kwargs["name"], kwargs.get("labels", {}), running=True)
        self._items.append(c)
        return c


class _FakeClient:
    def __init__(self, items: list[_FakeContainer]) -> None:
        self.containers = _FakeContainers(items)


def _provision(age_s: int, *, name: str | None = None) -> _FakeContainer:
    """A Running provision one-shot started `age_s` seconds ago (epoch in the name)."""
    epoch = int(time.time()) - age_s
    return _FakeContainer(
        name=name or f"jbrain-provision-{epoch}",
        labels={ONESHOT_LABEL: "provision"},
        running=True,
    )


def _gateway(items: list[_FakeContainer]) -> tuple[ComposeDockerGateway, _FakeClient]:
    client = _FakeClient(items)
    # The fake stands in for a docker.DockerClient; only the methods the reaping path
    # touches are implemented, so cast past the constructor's concrete type.
    gw = ComposeDockerGateway(
        cast(Any, client), project="jbrain", project_dir="/opt/jbrain2"
    )
    return gw, client


def test_recent_running_oneshot_still_blocks_a_new_one() -> None:
    stuck = _provision(age_s=60)  # a minute in — legitimately in progress
    gw, client = _gateway([stuck])
    with pytest.raises(UpdateInProgressError):
        gw.start_provision()
    assert not stuck.removed
    assert client.containers.run_kwargs == []


def test_hung_oneshot_past_max_runtime_is_reaped_and_unblocks() -> None:
    stuck = _provision(age_s=_ONESHOT_MAX_RUNTIME_S + 3600)  # hours past the ceiling
    gw, client = _gateway([stuck])

    name = gw.start_provision()  # must NOT raise

    assert stuck.removed, "the hung one-shot must be force-removed to free the slot"
    assert name.startswith("jbrain-provision-")
    # A fresh provision container was actually started after reaping.
    assert len(client.containers.run_kwargs) == 1
    assert client.containers.run_kwargs[0]["name"].startswith("jbrain-provision-")


def test_unparseable_name_is_never_reaped() -> None:
    # A name without an epoch suffix reads as age 0, so it can only ever be treated as
    # in-progress (block), never spuriously reaped — the safe, over-cautious default.
    stuck = _provision(age_s=0, name="jbrain-provision")
    gw, client = _gateway([stuck])
    with pytest.raises(UpdateInProgressError):
        gw.start_provision()
    assert not stuck.removed
    assert client.containers.run_kwargs == []
