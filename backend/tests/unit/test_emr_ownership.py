"""The importer/tool boundary for an EMR note (W4/D9).

`ingest/emr/ownership.emr_owned` decides which notes the deterministic EMR importer
owns the graph writes for, and `agents.narrow_for_emr` is what that costs the note
conversation: every graph-write verb, on BOTH passes. These tests pin the predicate
against the shipped trigger filter (migration 0122) and pin the subtraction against the
two shipped tool sets, so a fifth write verb cannot be added without landing here.
"""

from __future__ import annotations

import pytest

from jbrain.agent.agents import (
    NOTE_GRAPH_WRITE_TOOLS,
    NOTE_INGEST_ON_REPLY_TOOLS,
    NOTE_INGEST_UNATTENDED_TOOLS,
    agent_for,
    agent_for_owner_reply,
    narrow_for_emr,
)
from jbrain.ingest.emr.ownership import (
    EMR_DESTINATION,
    EMR_DOMAIN,
    PDF_MEDIA_TYPE,
    ZIP_MEDIA_TYPES,
    emr_owned,
)


@pytest.mark.parametrize("media", [PDF_MEDIA_TYPE, *ZIP_MEDIA_TYPES])
def test_a_health_records_note_with_emr_media_is_importer_owned(media: str) -> None:
    assert emr_owned(EMR_DOMAIN, EMR_DESTINATION, [media])


def test_the_predicate_is_the_0122_trigger_filter_and_nothing_looser() -> None:
    # Each of the three markers is load-bearing: the domain (the automatic firewall the
    # EMR triggers pin), the destination the owner picked, and an EMR-shaped attachment.
    assert not emr_owned("general", EMR_DESTINATION, [PDF_MEDIA_TYPE])
    assert not emr_owned(EMR_DOMAIN, "Inbox", [PDF_MEDIA_TYPE])
    assert not emr_owned(EMR_DOMAIN, EMR_DESTINATION, ["image/jpeg"])
    assert not emr_owned(EMR_DOMAIN, EMR_DESTINATION, [])
    assert not emr_owned(None, None, [PDF_MEDIA_TYPE])


def test_a_photo_on_a_records_note_alongside_a_pdf_is_still_importer_owned() -> None:
    # Stage 2 fires on `has_pdf_attachment`, not on "only PDFs" — a scan dropped beside
    # the archive must not take the note back off the deterministic path.
    assert emr_owned(EMR_DOMAIN, EMR_DESTINATION, ["image/jpeg", PDF_MEDIA_TYPE])


def test_narrowing_removes_every_write_verb_from_the_unattended_pass() -> None:
    narrowed = narrow_for_emr(agent_for("note_ingest"))
    assert narrowed.tools is not None
    assert not (narrowed.tools & NOTE_GRAPH_WRITE_TOOLS)
    # And nothing else: `ask_owner` and the reads are exactly what an EMR conversation
    # is for — being told what the parse did, and asking when it cannot proceed.
    assert narrowed.tools == NOTE_INGEST_UNATTENDED_TOOLS - NOTE_GRAPH_WRITE_TOOLS
    assert "ask_owner" in narrowed.tools


def test_narrowing_removes_every_write_verb_from_the_reply_turn_too() -> None:
    # This is the one place W4 breaks D8's "the full surface unlocks when you reply":
    # `correct_fact` at an empty address commits active + PINNED, and a pinned lab head
    # makes every later import of that reading `held`.
    narrowed = narrow_for_emr(agent_for_owner_reply("note_ingest"))
    assert narrowed.tools is not None
    assert not (narrowed.tools & NOTE_GRAPH_WRITE_TOOLS)
    assert narrowed.tools == NOTE_INGEST_ON_REPLY_TOOLS - NOTE_GRAPH_WRITE_TOOLS
    assert {"ask_owner", "search", "read_note", "prefs_write"} <= narrowed.tools


def test_the_write_verb_set_is_the_two_shipped_sets_write_halves() -> None:
    # A fifth write verb added to either shipped set without joining
    # NOTE_GRAPH_WRITE_TOOLS would be a verb the EMR narrowing silently keeps.
    assert NOTE_GRAPH_WRITE_TOOLS <= NOTE_INGEST_ON_REPLY_TOOLS
    assert {"resolve_entity", "assert_fact"} == (
        NOTE_GRAPH_WRITE_TOOLS & NOTE_INGEST_UNATTENDED_TOOLS
    )


@pytest.mark.parametrize("agent", ["curator", "jerv", "archivist", "intake"])
def test_narrowing_is_a_no_op_for_every_other_persona(agent: str) -> None:
    # So a caller may apply it unconditionally, the way `agent_for_owner_reply` is.
    profile = agent_for(agent)
    assert narrow_for_emr(profile) is profile


# --- the reply-turn lookup fails CLOSED ---------------------------------------


async def _reply_profile(maker: object, notes: object) -> object:
    from jbrain.analysis.clarify import reply_profile_for_session
    from jbrain.db.session import SessionContext

    return await reply_profile_for_session(
        maker,  # type: ignore[arg-type]
        notes,  # type: ignore[arg-type]
        SessionContext(principal_id="owner", principal_kind="owner"),
        session_id="sess-1",
        agent="note_ingest",
        profile=agent_for_owner_reply("note_ingest"),
    )


async def test_an_unreadable_conversation_narrows_the_reply_turn_rather_than_widening_it() -> None:
    """Fail CLOSED, which is the direction W4's merge settled on.

    The third-party lookup on this same turn (`thirdparty.conversation_is_third_party`)
    reads the same conversation row and the same note and already fails closed, so a blip
    narrows the turn either way — to the third-party set, which still holds
    `resolve_entity` and `assert_fact`. Failing open here would have left those bound on
    exactly the notes where a model write is unsupersedable: `correct_fact` at an empty
    address commits active + PINNED, and a pinned lab head holds every later draw."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/none")
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        narrowed = await _reply_profile(maker, object())
    finally:
        await engine.dispose()
    assert narrowed.tools == NOTE_INGEST_ON_REPLY_TOOLS - NOTE_GRAPH_WRITE_TOOLS  # type: ignore[attr-defined]


async def test_a_missing_conversation_row_or_note_narrows_the_reply_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two non-exception ways the lookup comes back empty. A soft-deleted note reads
    as `None` through `get_note` (`notes/repo.py`), so it lands here too."""
    import jbrain.analysis.clarify as clarify_mod

    class _Rows:
        def __init__(self, conversation: object) -> None:
            self._conversation = conversation

        async def get(self, _s: object, _session_id: str) -> object:
            return self._conversation

    class _Notes:
        async def get_note(self, _ctx: object, _note_id: str) -> object:
            return None

    class _Maker:
        def __call__(self, *_a: object, **_k: object) -> object:
            raise AssertionError("unreachable: the repo is stubbed")

    class _NoSession:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_a: object) -> bool:
            return False

    monkeypatch.setattr(clarify_mod, "scoped_session", lambda *_a, **_k: _NoSession())
    narrow = NOTE_INGEST_ON_REPLY_TOOLS - NOTE_GRAPH_WRITE_TOOLS

    monkeypatch.setattr(clarify_mod, "NoteConversationRepo", lambda: _Rows(None))
    assert (await _reply_profile(_Maker(), _Notes())).tools == narrow  # type: ignore[attr-defined]

    class _Conversation:
        note_id = "1e3fa71a-49ad-4754-b4d9-333fe4a45645"

    monkeypatch.setattr(clarify_mod, "NoteConversationRepo", lambda: _Rows(_Conversation()))
    assert (await _reply_profile(_Maker(), _Notes())).tools == narrow  # type: ignore[attr-defined]
