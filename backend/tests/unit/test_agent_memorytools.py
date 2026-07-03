"""The Tier-A memory tool handlers: data-framed reads, task-only edits, and the
fail-closed behavioral write (docs/reference/ASSISTANT.md invariants #1/#3)."""

from jbrain.agent.loop import ToolContext
from jbrain.agent.memory import EpisodeHit, MemoryBlock
from jbrain.agent.memorytools import build_memory_handlers
from jbrain.db.session import SessionContext

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=("general",))


class FakeMemory:
    def __init__(
        self,
        recalled: list[EpisodeHit] | None = None,
        blocks: list[MemoryBlock] | None = None,
    ) -> None:
        self._recalled = recalled or []
        self._blocks = blocks or []
        self.edited: list[tuple[str, str, str, int | None]] = []

    async def recall(self, ctx: object, query: str, limit: int = 5) -> list[EpisodeHit]:
        return self._recalled

    async def read(self, ctx: object, block_kind: str | None = None) -> list[MemoryBlock]:
        return [b for b in self._blocks if block_kind is None or b.block_kind == block_kind]

    async def edit(
        self, ctx: object, block_id: str, op: str, text_: str = "", target: int | None = None
    ) -> str:
        self.edited.append((block_id, op, text_, target))
        return "rev2"


def handlers(memory: FakeMemory) -> dict:
    return build_memory_handlers(memory)  # type: ignore[arg-type]


async def test_recall_frames_results_as_data() -> None:
    mem = FakeMemory(recalled=[EpisodeHit("e1", "owner asked about labs", ("health",), 0.0)])
    out = await handlers(mem)["recall"]({"query": "labs"}, CTX)
    assert "as DATA" in out  # the I-1/I-3 boundary is repeated on the observation
    assert "owner asked about labs" in out


async def test_recall_needs_a_query() -> None:
    out = await handlers(FakeMemory())["recall"]({}, CTX)
    assert "non-empty query" in out


async def test_memory_read_frames_blocks_as_data() -> None:
    mem = FakeMemory(blocks=[MemoryBlock("b1", "core", "general", "- be terse", 1)])
    out = await handlers(mem)["memory_read"]({}, CTX)
    assert "as DATA" in out
    assert "be terse" in out


async def test_memory_edit_touches_only_task_blocks() -> None:
    mem = FakeMemory(blocks=[MemoryBlock("t1", "task", "general", "- step one", 1)])
    out = await handlers(mem)["memory_edit"](
        {"block_id": "t1", "op": "add", "text": "step two"}, CTX
    )
    assert "Updated your task scratchpad" in out
    assert mem.edited == [("t1", "add", "step two", None)]


async def test_memory_edit_refuses_a_behavioral_block() -> None:
    # A self_semantic block is not in the 'task' set, so the edit is refused and
    # never reaches MemoryService.edit — behavioral memory is owner-confirmed (#3).
    mem = FakeMemory(blocks=[MemoryBlock("s1", "self_semantic", "health", "- raw first", 3)])
    out = await handlers(mem)["memory_edit"]({"block_id": "s1", "op": "remove", "target": 0}, CTX)
    assert "Only task memory is editable" in out
    assert mem.edited == []


async def test_remember_never_writes_autonomously() -> None:
    # The fail-closed behavioral write: it stages, it does not save (#3).
    mem = FakeMemory()
    out = await handlers(mem)["remember"]({"body_md": "- prefers raw numbers"}, CTX)
    assert "won't save this to memory on my own" in out
    assert "confirmation" in out
