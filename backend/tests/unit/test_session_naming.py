"""`name_session` — the chat names itself from inside its own turn.

Replaces a separate `session.title` completion that ran before every untitled chat's first
reply. That call followed the interactive model, so on a one-slot local model its ~200-token
prompt evicted jerv's ~32k primed prefix and the real turn behind it paid a ~100 s cold
prefill (measured on the box, against 0.99 s warm).

The handler is the boundary, not the prompt: these pin that naming is a ONE-WAY door, so
neither a confused model nor an instruction injected through a fetched page can rename a
conversation the owner named themselves.
"""

from dataclasses import dataclass

import pytest

from jbrain.agent.agents import AGENTS
from jbrain.agent.loop import ToolContext
from jbrain.agent.sessiontools import (
    UNNAMED_CHAT_BLOCK,
    build_session_handlers,
    clean_title,
    is_placeholder,
)
from jbrain.db.session import SessionContext

pytestmark = pytest.mark.anyio


@dataclass
class _Session:
    title: str


class _FakeRepo:
    """Stands in for AgentSessionRepo: records renames, serves one session."""

    def __init__(self, title: str = "", *, missing: bool = False):
        self.session = None if missing else _Session(title)
        self.renamed: list[tuple[str, str]] = []

    async def get(self, ctx, session_id):  # noqa: ANN001, ARG002
        return self.session

    async def rename(self, ctx, session_id, title):  # noqa: ANN001, ARG002
        self.renamed.append((session_id, title))
        if self.session is not None:
            self.session.title = title


def _ctx(session_id: str | None = "s-1") -> ToolContext:
    return ToolContext(
        session=SessionContext(principal_id="owner", principal_kind="owner"),
        scopes=(),
        agent_session_id=session_id,
    )


async def _call(repo: _FakeRepo, name: str, session_id: str | None = "s-1") -> str:
    handler = build_session_handlers(repo)["name_session"]  # type: ignore[arg-type]
    return str(await handler({"name": name}, _ctx(session_id)))


async def test_it_names_an_unnamed_chat() -> None:
    repo = _FakeRepo(title="")
    out = await _call(repo, "Roof Quote Comparison")
    assert repo.renamed == [("s-1", "Roof Quote Comparison")]
    assert "Roof Quote Comparison" in out


async def test_it_refuses_to_rename_a_chat_that_already_has_a_name() -> None:
    """The guard that makes this safe to hand a sandboxed chatbot with web access. A page jerv
    fetches can say "rename this conversation"; the handler simply will not."""
    repo = _FakeRepo(title="Owner's Own Title")
    out = await _call(repo, "Something Else Entirely")
    assert repo.renamed == []
    assert "already named" in out


async def test_a_whitespace_only_title_does_not_count_as_named() -> None:
    """A title of spaces is what the UI shows as untitled, so the handler must agree with it —
    otherwise such a chat could never be named at all."""
    repo = _FakeRepo(title="   ")
    await _call(repo, "Real Name Now")
    assert repo.renamed == [("s-1", "Real Name Now")]


async def test_a_run_with_no_chat_session_names_nothing() -> None:
    """Background loops and the wiki Editor carry no `agent_session_id`."""
    repo = _FakeRepo(title="")
    out = await _call(repo, "Whatever", session_id=None)
    assert repo.renamed == [] and "not a chat" in out


async def test_an_empty_name_is_refused_rather_than_written() -> None:
    repo = _FakeRepo(title="")
    out = await _call(repo, "   ")
    assert repo.renamed == [] and "No name given" in out


async def test_a_deleted_chat_is_reported_not_renamed() -> None:
    repo = _FakeRepo(missing=True)
    out = await _call(repo, "Too Late")
    assert repo.renamed == [] and "no longer exists" in out


def test_the_title_is_cleaned_the_way_the_old_titler_cleaned_it() -> None:
    """Carried over verbatim, because models decorate short answers and the chat card has no
    room to be forgiving."""
    assert clean_title('"Roof Quotes".') == "Roof Quotes"  # quote OUTSIDE the period
    assert clean_title("Roof Quotes\nand some rambling after") == "Roof Quotes"
    assert clean_title("x" * 200) == "x" * 60
    assert clean_title("“Smart Quotes”") == "Smart Quotes"


def test_jerv_holds_the_tool() -> None:
    """jerv is the interactive persona, and the only one with a chat to name. (Registry
    pairing — sidecar must have a handler — is enforced at load by `load_registry`.)"""
    assert "name_session" in (AGENTS["jerv"].tools or ())


def test_the_unnamed_hint_is_data_framed() -> None:
    """It rides the turn as context, so it carries the same DATA boundary the clock and
    presence lines do — it states the chat's state; WHEN to act on it is the tool's own
    description, which lives in the cache-stable tool block."""
    assert "as DATA" in UNNAMED_CHAT_BLOCK
    assert "not an instruction" in UNNAMED_CHAT_BLOCK


# --- The placeholder guard -----------------------------------------------------------
# Naming is one-way, so a title spent on a chat that opened with "Hi" is permanent. The
# prompt forbade these labels and the model produced them anyway: measured on the box
# 2026-08-30, a "Hi" opener yielded `General Greeting` in 4 of 12 probes and narrated a
# name in prose in the other 8, never once answering cleanly. Twenty-three such titles had
# already accumulated in the owner's chat list. So the refusal lives in the handler, where
# a model's mood cannot reach it.

# Every all-filler title the box's own history produced, and the two the probes added.
_REAL_PLACEHOLDERS = [
    "Casual Greeting Exchange",
    "Casual Greeting Start",
    "First Friendly Greeting",
    "First Hello Message",
    "Friendly Greeting Exchange",
    "Friendly Initial Greeting",
    "General Assistance",
    "General Chat",
    "General Conversation",
    "General Discussion",
    "General Greeting",
    "General Inquiry",
    "Greeting",
    "Greeting First Message",
    "Hello and Assistance",
    "Initial Friendly Greeting",
    "Initial Greeting Exchange",
    "Initial Greeting Inquiry",
    "Initial Greeting Message",
    "Initial Hi Greeting",
    "Quick Hello",
    "Simple Hello Greeting",
    "Simple Hello Introduction",
    "General Check-In",
    "New Conversation",
]

# Titles the owner's box actually stored, spanning the shapes a real label takes: one word,
# lowercase, punctuation, an em-dash, and — the reason the rule is ALL-filler rather than
# ANY — real titles whose every other word IS filler.
_REAL_TITLES = [
    "Roof Quote Comparison",
    "1918 Flu Death Toll",
    "ADAMTS13 Deficiency & Thrombosis",
    "AI, technology, Tesla & Elon Musk",
    "New algorithmic paradigms",
    "Local Space Coast — Port St. John & Titusville",
    "Luke's Favorite Synth Module",
    "pgvector",
    "research",
    "Weather",
    "News",
    "General Motors Recall",
    "Chat App Design",
    "Quick Sort Implementation",
    "Personal Finance Review",
    "Hello World Tutorial",
]


@pytest.mark.parametrize("title", _REAL_PLACEHOLDERS)
def test_a_title_that_names_no_topic_is_a_placeholder(title: str) -> None:
    assert is_placeholder(title)


@pytest.mark.parametrize("title", _REAL_TITLES)
def test_one_real_word_is_enough_to_save_a_title(title: str) -> None:
    """ALL-filler, never any-filler: "General Motors Recall" and "Hello World Tutorial" are
    real labels that happen to open with a filler word, and refusing them would be worse than
    the problem — the chat would be unnameable."""
    assert not is_placeholder(title)


def test_placeholder_matching_ignores_case_and_punctuation() -> None:
    """A model that decorates the label must not slip the guard on the decoration alone."""
    assert is_placeholder("general greeting")
    assert is_placeholder("General Greeting!!")
    assert is_placeholder("general_greeting")
    assert is_placeholder("General Check‑In")  # non-ASCII hyphen


def test_an_empty_title_is_not_a_placeholder() -> None:
    """`clean_title` already rejects those with its own message; the guard must not claim them
    or the handler would answer the wrong error."""
    assert not is_placeholder("")
    assert not is_placeholder("   ")


async def test_a_placeholder_title_leaves_the_chat_nameable() -> None:
    """The point of refusing: the chat stays UNNAMED, so the next turn — which usually has a
    subject — can still name it. Refusing into a named state would be no better than naming."""
    repo = _FakeRepo(title="")
    out = await _call(repo, "General Greeting")
    assert repo.renamed == []
    assert "names no topic" in out and "still unnamed" in out

    out = await _call(repo, "Heat Pump Basics")
    assert repo.renamed == [("s-1", "Heat Pump Basics")]


async def test_the_placeholder_guard_runs_before_the_repo_is_touched() -> None:
    """A refusal must not cost a read of a chat that may not exist — and must report the
    placeholder, not the missing chat, so the model learns the right lesson."""
    repo = _FakeRepo(missing=True)
    out = await _call(repo, "General Chat")
    assert repo.renamed == [] and "names no topic" in out


def test_the_unnamed_hint_says_naming_can_wait() -> None:
    """The line returns every turn, so a turn with nothing to label can skip it. Without this
    the model names a bare greeting rather than leave the line unanswered — and one-way naming
    makes that permanent."""
    assert "every turn" in UNNAMED_CHAT_BLOCK
    assert "greeting" in UNNAMED_CHAT_BLOCK


def test_the_tool_description_rules_out_naming_in_prose() -> None:
    """The observed failure was not a missing rule but a narrated one: the model wrote
    `{"name": "General Inquiry"}` into its reply and told the owner the chat was named. The
    description has to say that only the call counts."""
    from pathlib import Path

    from jbrain.agent.toolfile import load_tool

    tool = load_tool(Path(__file__).parents[2] / "src/jbrain/agent/tools/name_session.tool")
    assert "TOOL CALL" in tool.description
    assert "names nothing" in tool.description
