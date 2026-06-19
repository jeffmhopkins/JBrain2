"""Agent selection: the persona registry that sets each session's prompt, tool
allowlist, and knowledge-base access (docs/ASSISTANT.md "Agent selection")."""

import hashlib

from jbrain.agent.agents import (
    AGENT_NAMES,
    AGENTS,
    DEFAULT_AGENT,
    JERV_TOOLS,
    WEB_TOOLS,
    agent_for,
    is_agent,
)


def test_three_agents_are_defined() -> None:
    assert frozenset({"curator", "teacher", "jerv"}) == AGENT_NAMES
    assert DEFAULT_AGENT == "curator"


def test_curator_is_the_full_brain_default() -> None:
    """curator keeps the original Full Brain system prompt and every in-scope tool
    (allow=None), and reads the knowledge base — i.e. today's behavior unchanged."""
    curator = AGENTS["curator"]
    assert curator.tools is None
    assert curator.reads_knowledge_base is True
    assert curator.version == "agent-system-v4"


def test_teacher_is_a_tool_less_socratic_tutor() -> None:
    """teacher has no tools (an empty allowlist) and no knowledge-base access — it
    teaches only from the conversation."""
    teacher = AGENTS["teacher"]
    assert teacher.tools == frozenset()
    assert teacher.reads_knowledge_base is False


def test_jerv_is_a_sandboxed_web_chatbot() -> None:
    """jerv may call the web tools, the dataless clock, and the owner-approved
    coarse-location read; it reads no knowledge base."""
    jerv = AGENTS["jerv"]
    assert jerv.tools == JERV_TOOLS == WEB_TOOLS | {"current_time", "current_location"}
    assert jerv.reads_knowledge_base is False


def test_agent_for_falls_back_to_curator() -> None:
    assert agent_for("jerv").name == "jerv"
    # An unknown/old/malformed stored value never breaks a turn — it runs as curator.
    assert agent_for("nonesuch").name == DEFAULT_AGENT
    assert agent_for("").name == DEFAULT_AGENT


def test_is_agent() -> None:
    assert is_agent("curator") and is_agent("teacher") and is_agent("jerv")
    assert not is_agent("editor")


def test_persona_prompts_pinned_to_their_versions() -> None:
    """Each persona prompt carries a safety policy (the data/instruction boundary,
    the tutor's no-cheating rule, jerv's sandbox); editing one must be a deliberate
    version bump, like every .prompt file (DEVELOPMENT.md)."""
    pins = {
        "curator": (
            "agent-system-v4",
            "9d86df3adb7be857a153015a9da2aeb93a48eb17f1807651fa206e52efe61772",
        ),
        "teacher": (
            "agent-teacher-v1",
            "e457d7504be94746132de7cc0c7b50fa1567867b3573a64ddfe6030b45909b16",
        ),
        "jerv": (
            "agent-jerv-v6",
            "fd71e5781b6bff0b0a6cb28acfa7acb232362fbc498733755c35a98bf566e48b",
        ),
    }
    assert set(pins) == AGENT_NAMES
    for name, (version, digest) in pins.items():
        profile = AGENTS[name]
        assert profile.version == version
        assert hashlib.sha256(profile.prompt.encode()).hexdigest() == digest
