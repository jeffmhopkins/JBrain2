"""Block registry + the IDs-not-payloads guard. No Postgres, no DBOS — this is the
runnable-anywhere half of the workflow spike, so the engine's validation and
safety contracts have coverage even where Docker is absent."""

import uuid

import pytest
from pydantic import BaseModel

from jbrain.workflow.registry import BlockError, BlockRegistry, BlockSpec, block
from jbrain.workflow.safety import (
    MAX_REFERENCE_STR,
    PayloadLeakError,
    assert_reference_shaped,
    is_reference_shaped,
)


class _Params(BaseModel):
    since_days: int = 7


def _spec(name: str = "demo", **kw) -> BlockSpec:
    base = dict(
        name=name, version=1, params=_Params, kind="python",
        domains=("general",), description="d",
    )
    base.update(kw)
    return BlockSpec(**base)  # type: ignore[arg-type]


def test_register_bind_and_validate() -> None:
    reg = BlockRegistry()

    @block(reg, name="recent", version=2, params=_Params, domains=("general",))
    def recent(since_days: int) -> list[str]:
        return [f"id-{since_days}"]

    assert "recent" in reg and len(reg) == 1
    assert reg.handler("recent")(3) == ["id-3"]
    assert reg.spec("recent").version == 2

    parsed = reg.validate_params("recent", {"since_days": 30})
    assert parsed.since_days == 30  # type: ignore[attr-defined]
    # Defaults apply; the model is the schema of record.
    assert reg.validate_params("recent", {}).since_days == 7  # type: ignore[attr-defined]


def test_unknown_block_and_bad_params_are_block_errors() -> None:
    reg = BlockRegistry()
    reg.register(_spec("a"), lambda: None)
    with pytest.raises(BlockError):
        reg.handler("nope")
    with pytest.raises(BlockError):
        reg.validate_params("a", {"since_days": "not-an-int"})


def test_duplicate_registration_rejected() -> None:
    reg = BlockRegistry()
    reg.register(_spec("dup"), lambda: None)
    with pytest.raises(BlockError):
        reg.register(_spec("dup"), lambda: None)


def test_domain_scope_visibility_filter() -> None:
    reg = BlockRegistry()
    reg.register(_spec("gen", domains=("general",)), lambda: None)
    reg.register(_spec("med", domains=("health",)), lambda: None)
    assert reg.names() == ["gen", "med"]
    assert reg.names(domain_scope=("health",)) == ["med"]
    assert reg.names(domain_scope=("finance",)) == []


@pytest.mark.parametrize("bad", [
    dict(name="has space"),
    dict(name=""),
    dict(version=0),
    dict(domains=()),
])
def test_malformed_spec_rejected(bad: dict) -> None:
    with pytest.raises(BlockError):
        _spec(**bad)


def test_reference_shaped_accepts_ids_and_containers() -> None:
    assert is_reference_shaped(uuid.uuid4())
    assert is_reference_shaped(["entity-1", 2, None, True])
    assert is_reference_shaped({"entity_id": "e-1", "rank": 3})
    assert is_reference_shaped("e-" + "x" * (MAX_REFERENCE_STR - 2))


def test_reference_shaped_rejects_content() -> None:
    note_body = "x" * (MAX_REFERENCE_STR + 1)
    assert not is_reference_shaped(note_body)
    assert not is_reference_shaped("line one\nline two")  # multiline => content
    assert not is_reference_shaped({"summary": note_body})
    with pytest.raises(PayloadLeakError):
        assert_reference_shaped(["ok-id", note_body], where="step output")
