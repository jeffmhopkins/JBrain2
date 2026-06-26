"""The `model_already_loaded` action precondition: a scheduled sweep runs only when
the model its task resolves to is already resident, so it never forces a model swap
(docs/EMAIL_ARCHIVIST_PLAN.md). Driven against a real router (static routing, no DB
overrides) and an in-memory gateway — no network."""

from __future__ import annotations

from collections.abc import Iterable

from jbrain.llm.router import LlmRouter, resolve_tasks
from jbrain.workflow.preconditions import model_already_loaded


class FakeGateway:
    """The runtime-state slice of LocalGateway: report which served models are loaded."""

    def __init__(self, running: Iterable[str] = ()):
        self._running = set(running)

    async def running(self) -> set[str]:
        return set(self._running)

    async def unload(self, served_model: str) -> None:  # pragma: no cover - unused here
        self._running.discard(served_model)

    async def load(self, served_model: str) -> None:  # pragma: no cover - unused here
        self._running.add(served_model)


def _router(spec: str) -> LlmRouter:
    # Bake the triage route straight into the task table so effective_spec resolves it
    # without a live DB override loader (the unit-test equivalent of an operator's
    # per-task routing choice).
    return LlmRouter(clients={}, tasks=resolve_tasks({"triage.classify": spec}))


async def test_cloud_route_is_always_met() -> None:
    # A cloud model loads nothing on the box, so there is nothing to gate — met even
    # with an empty gateway.
    check = model_already_loaded(_router("xai:grok-4.3"), FakeGateway(), task="triage.classify")
    result = await check()
    assert result.met


async def test_local_route_met_only_when_the_model_is_resident() -> None:
    gateway = FakeGateway(running={"gpt-oss-120b"})
    check = model_already_loaded(_router("local:gpt-oss-120b"), gateway, task="triage.classify")
    assert (await check()).met


async def test_local_route_unmet_when_the_model_is_not_loaded() -> None:
    # A different model is resident (an in-use code/vision model); triage's model is
    # cold, so the gate is unmet and names the model in its reason.
    gateway = FakeGateway(running={"qwen3-vl-30b"})
    check = model_already_loaded(_router("local:gpt-oss-120b"), gateway, task="triage.classify")
    result = await check()
    assert not result.met
    assert "gpt-oss-120b" in result.reason


async def test_unreachable_gateway_reads_as_not_loaded() -> None:
    # running() returns an empty set on any error, so an unreachable gateway defers the
    # job rather than blindly forcing a load.
    router = _router("local:gpt-oss-120b")
    check = model_already_loaded(router, FakeGateway(), task="triage.classify")
    assert not (await check()).met
