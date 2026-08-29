"""`scripts/jmolt-replay-build.py` — the counterfactual builder.

A replay experiment is only as trustworthy as the edit that produced its condition. If
`--drop` removes the wrong span or `--replace` silently no-ops, every arm is quietly the
same arm and the result reads as a null. That failure is invisible in the output, so it is
pinned here instead.
"""

import importlib.util
import json
import pathlib

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "jmolt-replay-build.py"
_spec = importlib.util.spec_from_file_location("replay_build", _SCRIPT)
assert _spec and _spec.loader
build_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_mod)

_PROLOGUE = (
    "It is 03:09; you woke at 03:00 and about 51 minute(s) remain. This is sitting 7.\n\n"
    "WHAT YOU HAVE ALREADY DONE TONIGHT:\n- post on tech (03:06)\n\n"
    'The posts themselves, by title:\n- "Owner prompts vs emergent context" (03:06)\n\n'
    "MARCHING ORDERS: read deeply, think, tend your notes.\n"
)
_TOOLS = json.dumps(
    [
        {"name": "scratch_list", "ok": True, "summary": "- thoughts.md (275 bytes)"},
        {"name": "moltbook_post", "ok": False, "summary": "That resource was not found."},
        {"name": "no_name_entry"},
    ]
)


def _dump() -> dict:
    return {"columns": ["prologue", "tools"], "rows": [[_PROLOGUE, _TOOLS]]}


def test_stubs_carry_the_observed_results_and_their_error_flag() -> None:
    body = build_mod.build(_dump(), system="sys", drops=[], replaces=[])
    assert [s["name"] for s in body["stubs"]] == ["scratch_list", "moltbook_post", "no_name_entry"]
    assert body["stubs"][0]["result"] == "- thoughts.md (275 bytes)"
    # A failed call must replay as a failure: the night's model saw an error there, and a
    # replay that turns it into a success is no longer the sitting that happened.
    assert body["stubs"][0]["is_error"] is False
    assert body["stubs"][1]["is_error"] is True


def test_drop_removes_exactly_one_block() -> None:
    body = build_mod.build(_dump(), system="", drops=["The posts themselves"], replaces=[])
    text = body["user_text"]
    assert "The posts themselves" not in text
    assert "Owner prompts vs emergent context" not in text
    # ...and nothing either side of it, or the arm differs by more than the one edit.
    assert "WHAT YOU HAVE ALREADY DONE TONIGHT" in text
    assert "MARCHING ORDERS" in text


def test_a_drop_that_matches_nothing_is_fatal() -> None:
    """Silently leaving the prologue intact would run the control twice and report a null."""
    with pytest.raises(SystemExit):
        build_mod.build(_dump(), system="", drops=["a block that is not there"], replaces=[])


def test_a_replace_that_matches_nothing_is_fatal() -> None:
    with pytest.raises(SystemExit):
        build_mod.build(_dump(), system="", drops=[], replaces=["not present=x"])


def test_replace_edits_the_real_prologue() -> None:
    body = build_mod.build(
        _dump(), system="", drops=[], replaces=["about 51 minute(s) remain=there is time"]
    )
    assert "there is time" in body["user_text"]
    assert "51 minute(s)" not in body["user_text"]


def test_an_empty_dump_is_fatal_rather_than_an_empty_replay() -> None:
    with pytest.raises(SystemExit):
        build_mod.build({"rows": []}, system="", drops=[], replaces=[])
