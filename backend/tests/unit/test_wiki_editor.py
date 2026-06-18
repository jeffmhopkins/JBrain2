"""The Talk Editor turn's pure logic (no DB): conversation mapping (incl. a topic's first reply,
no consecutive UserMessages), the tool tally + outcome-chip precedence/ok-filter, and the
run_editor_turn orchestration (prose -> reply, empty -> None) with an empty registry + fake."""

from typing import Any

from jbrain.agent.readtools import build_registry
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.agent.wikiwritetools import build_wiki_write_handlers
from jbrain.connectors.base import ConnectorRegistry
from jbrain.connectors.medical import medical_connectors
from jbrain.db.session import SessionContext
from jbrain.llm.fake import FakeLlmClient
from jbrain.llm.router import LlmRouter
from jbrain.llm.types import AssistantMessage, LlmTurn, LlmUsage, ToolCall, UserMessage
from jbrain.wiki.editor import _conversation, _outcome, _ToolTally, run_editor_turn

OWNER = SessionContext(principal_id="00000000-0000-0000-0000-000000000001", principal_kind="owner")


def _post(author: str, body: str) -> dict:
    return {"author": author, "body": body}


def test_conversation_maps_voices_and_skips_builder() -> None:
    convo = _conversation(
        [_post("owner", "left Globex"), _post("editor", "it cites…"), _post("builder", "rev 3")]
    )
    assert [type(m) for m in convo] == [UserMessage, AssistantMessage]  # builder skipped
    assert isinstance(convo[0], UserMessage) and convo[0].text == "left Globex"


def test_conversation_first_reply_is_a_single_user_message() -> None:
    # The article/topic context rides the system prompt, so a topic's first reply is exactly one
    # owner UserMessage — never two consecutive Users (the adapter-contract edge from review N2).
    convo = _conversation([_post("owner", "this is wrong")])
    assert len(convo) == 1 and isinstance(convo[0], UserMessage)


async def test_tool_tally_records_only_successful_tools() -> None:
    tally = _ToolTally()
    await tally.step(idx=0, kind="model", name="converse", ok=True, cost_tokens=5)
    await tally.step(idx=1, kind="tool", name="read_wiki", ok=True, cost_tokens=0)
    # A rejected tool is recorded ok=False and must be excluded from the chip.
    await tally.step(idx=2, kind="tool", name="file_correction", ok=False, cost_tokens=0)
    assert tally.tools == ["read_wiki"]  # model steps + failed tools excluded


def test_outcome_precedence_correction_over_exclusion_over_rebuild() -> None:
    tally = _ToolTally()
    tally.tools = ["add_source_exclusion", "request_rebuild", "file_correction"]
    chip, fallback = _outcome(tally)
    assert chip == "correction filed → rebuild queued"
    assert fallback == "Filed your correction."
    # Exclusion outranks a bare rebuild; the chip says "queued" (the tool only enqueues).
    tally.tools = ["request_rebuild", "add_source_exclusion"]
    assert _outcome(tally)[0] == "source excluded · rebuild queued"
    assert _outcome(_ToolTally())[0] is None  # no write tool → no chip


def _router(turns: list[LlmTurn]) -> LlmRouter:
    return LlmRouter({"xai": FakeLlmClient(turns=turns)}, {"agent.turn": ("xai", "m")})


async def test_run_editor_turn_returns_prose_when_no_tool_used() -> None:
    router = _router([LlmTurn("Here's where it comes from.", (), "end_turn", LlmUsage(1, 1))])
    reply = await run_editor_turn(
        router,
        ToolRegistry([]),
        OWNER,
        article_id="a1",
        article_title="Celine",
        topic_title="Outdated",
        posts=[_post("owner", "explain the Globex claim")],
    )
    assert reply is not None and reply.body == "Here's where it comes from."
    assert reply.outcome is None  # no lever pulled


async def test_run_editor_turn_none_on_empty_prose_and_no_lever() -> None:
    router = _router([LlmTurn("", (), "end_turn", LlmUsage(1, 1))])
    reply = await run_editor_turn(
        router,
        ToolRegistry([]),
        OWNER,
        article_id="a1",
        article_title="Celine",
        topic_title="Outdated",
        posts=[_post("owner", "hi")],
    )
    assert reply is None


class _FakeNote:
    id = "note-1"


class _FakeNotes:
    """A no-DB notes repo: file_correction's create_note succeeds without Postgres."""

    async def create_note(self, *_args: object, **_kwargs: object) -> tuple[_FakeNote, bool]:
        return _FakeNote(), True


class _FakeJobs:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, _ctx: object, kind: str, _payload: object, **_kw: object) -> str:
        self.enqueued.append(kind)
        return "job-1"


async def test_run_editor_turn_chip_only_when_lever_fires_with_empty_prose() -> None:
    # N3: the model files a correction then ends with no prose — the reply is posted anyway, as a
    # chip-only line, so an enacted lever is never invisible.
    jobs = _FakeJobs()
    notes = _FakeNotes()
    stub: Any = object()  # the inert services build_registry never calls for this turn
    connectors = ConnectorRegistry(medical_connectors("http://x", "http://y"))
    registry = build_registry(
        stub,
        notes,  # type: ignore[arg-type]
        stub,
        stub,
        stub,
        connectors,
        stub,
        stub,
        stub,
        build_wiki_write_handlers(notes, jobs, object()),  # type: ignore[arg-type]
        stub,  # geocoder client
    )
    router = LlmRouter(
        {
            "xai": FakeLlmClient(
                turns=[
                    LlmTurn(
                        "",
                        (ToolCall("c1", "file_correction", {"body": "x", "domain": "general"}),),
                        "tool_use",
                        LlmUsage(1, 1),
                    ),
                    LlmTurn("", (), "end_turn", LlmUsage(1, 1)),  # no prose
                ]
            )
        },
        {"agent.turn": ("xai", "m")},
    )
    reply = await run_editor_turn(
        router,
        registry,
        OWNER,
        article_id="a1",
        article_title="Celine",
        topic_title="Outdated",
        posts=[_post("owner", "she left")],
    )
    assert reply is not None
    assert reply.outcome == "correction filed → rebuild queued"
    assert reply.body == "Filed your correction."  # the chip-derived fallback (no prose)
    assert jobs.enqueued == ["ingest_note"]  # the lever actually fired


async def test_run_editor_turn_none_without_posts() -> None:
    reply = await run_editor_turn(
        _router([]),
        ToolRegistry([]),
        OWNER,
        article_id="a1",
        article_title="Celine",
        topic_title="Empty",
        posts=[],
    )
    assert reply is None
