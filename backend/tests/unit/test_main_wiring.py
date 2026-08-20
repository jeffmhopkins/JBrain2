"""Wiring that only exists at app construction: a hook nobody binds is dead code that
passes every unit test of the thing it was supposed to connect."""

from typing import Any

from fastapi.testclient import TestClient

from jbrain.config import Settings
from jbrain.main import _restore_reload_casualties, create_app


def _local_settings(**kw: Any) -> Settings:
    kw.setdefault("secure_cookies", False)
    kw.setdefault("database_url", "postgresql+asyncpg://nobody@localhost:1/none")
    kw.setdefault("local_llm_enabled", True)
    kw.setdefault("local_models", ["qwen3-vl-30b", "gpt-oss-120b"])
    return Settings(**kw)


class _FakeCoordinator:
    def __init__(self) -> None:
        self.evicted: list[list[str]] = []
        self.restores = 0

    def note_evicted(self, names: Any) -> None:
        self.evicted.append(list(names))

    def schedule_restore(self) -> None:
        self.restores += 1


class _FakeState:
    """`app.state` is a bag of attributes; the helper reaches for `residency` with getattr,
    so the missing-attribute case has to be a genuinely missing attribute."""

    def __init__(self, residency: object | None) -> None:
        if residency is not None:
            self.residency = residency


class _FakeApp:
    def __init__(self, residency: object | None) -> None:
        self.state = _FakeState(residency)


def test_reload_casualties_are_noted_and_a_restore_is_scheduled() -> None:
    # Noting without scheduling would leave the bystander displaced until something else
    # happened to fire a restore — on a settings-screen load, that is nothing at all.
    coordinator = _FakeCoordinator()
    _restore_reload_casualties(_FakeApp(coordinator), ["gpt-oss-120b"])  # type: ignore[arg-type]
    assert coordinator.evicted == [["gpt-oss-120b"]]
    assert coordinator.restores == 1


def test_restoring_without_a_coordinator_is_a_no_op() -> None:
    # The hook is bound BEFORE the coordinator is constructed (it is built from the gateway
    # that calls this), so a load racing startup must not crash the load.
    _restore_reload_casualties(_FakeApp(None), ["gpt-oss-120b"])  # type: ignore[arg-type]


def test_the_gateway_actually_has_the_restore_hook_bound() -> None:
    """The guard that makes the three local_gateway restore tests mean something: they pass
    against a hook that production never binds."""
    app = create_app(_local_settings())
    with TestClient(app):
        gateway = app.state.local_gateway
        assert gateway._on_external_eviction is not None, (  # noqa: SLF001 — wiring is the subject
            "reload casualties would be recorded and then silently left dead"
        )
