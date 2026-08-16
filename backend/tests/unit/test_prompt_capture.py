"""The assembled prompt a run last sent to the model.

In memory and bounded on purpose: an assembled agent prompt is a verbatim copy of
everything the turn retrieved — including whatever crossed the health, finance and
location domains — so it is not written to a table that backups would then carry.
"""

from jbrain.agent.prompt_capture import MAX_RUNS, forget, prompt_for, record_prompt


def messages(*texts: str) -> list[dict[str, object]]:
    return [{"role": "user", "content": t} for t in texts]


def test_keeps_what_was_sent() -> None:
    record_prompt(
        "run_a", system="you are jerv", messages=messages("size the heat pump"), tools=["web.fetch"]
    )

    kept = prompt_for("run_a")

    assert kept is not None
    assert kept.system == "you are jerv"
    assert kept.messages[0]["content"] == "size the heat pump"
    assert kept.tools == ["web.fetch"]
    assert kept.truncated is False
    forget("run_a")


def test_keeps_the_latest_round_and_counts_them() -> None:
    """A ReAct turn sends several prompts; the latest carries every earlier tool result
    and so is the fullest account of what the model was looking at."""
    record_prompt("run_b", system="s", messages=messages("first"), tools=[])
    record_prompt("run_b", system="s", messages=messages("first", "then a tool result"), tools=[])

    kept = prompt_for("run_b")

    assert kept is not None
    assert kept.round_index == 2
    assert len(kept.messages) == 2
    forget("run_b")


def test_clips_a_huge_prompt_and_says_so() -> None:
    record_prompt("run_c", system="s", messages=messages("x" * 500_000), tools=[])

    kept = prompt_for("run_c")

    assert kept is not None
    assert kept.truncated is True
    # The reported size is the REAL one, so a clipped prompt reads as clipped rather
    # than as a short prompt.
    assert kept.message_chars == 500_000
    assert len(kept.messages[0]["content"]) < 500_000
    forget("run_c")


def test_forgets_the_oldest_runs() -> None:
    for i in range(MAX_RUNS + 5):
        record_prompt(f"run_evict_{i}", system="s", messages=messages("m"), tools=[])

    assert prompt_for("run_evict_0") is None
    assert prompt_for(f"run_evict_{MAX_RUNS + 4}") is not None
    for i in range(MAX_RUNS + 5):
        forget(f"run_evict_{i}")


def test_a_run_without_an_id_records_nothing() -> None:
    # A headless probe or a test has no run; capturing under a blank key would collide.
    record_prompt(None, system="s", messages=messages("m"), tools=[])
    record_prompt("", system="s", messages=messages("m"), tools=[])

    assert prompt_for("") is None


def test_unknown_run_has_no_prompt() -> None:
    # Predates a restart, evicted, or never reached a model call — the surface says so.
    assert prompt_for("run_never_seen") is None
