"""A persona's soul as the box writes it (jbrain.growbot.souls): the owner's edits and imports are
cleaned the way the creature cleans an import (soul.js `sanitizeSoulCfg`), and the memory
surfaces stay the creature's."""

from __future__ import annotations

import copy

import pytest

from jbrain.growbot import souls


def _grown() -> dict[str, object]:
    return {
        "soul_format": 3,
        "rev": 41,
        "writeId": "tab:41:abc",
        "identity": "I am Sprout. I live on the desk by the window and I like the blue mug.",
        "identityCore": "My person held me the first morning.",
        "goals": {"wants": ["walk to the kettle"], "selfName": "Sprout"},
        "scratch": {"state": "watching the window", "rules": ["don't walk off the desk"]},
        "log": [{"t": 1, "txt": "Saw the blue mug."}],
        "persona": {"constitution": "You are a small creature."},
        "meta": {"name": "Sprout", "description": "The first one."},
    }


def test_clean_is_soul_clean() -> None:
    assert souls.clean("  a\n\tb  ==== c ") == "a b = c"
    assert souls.clean(None) == ""
    assert souls.clean("x" * 500) == "x" * 400
    assert souls.clean("abc", 2) == "ab"


def test_a_fresh_creature_is_upstreams_seed_with_a_name() -> None:
    soul = souls.fresh("  Pip  ")
    assert soul["soul_format"] == 3 and soul["bornAt"] == 0
    assert soul["meta"]["name"] == "Pip"
    # Every memory surface is left for the phone's normalizeSoul to seed.
    assert not {"identity", "goals", "scratch", "log", "persona"} & set(soul)


def test_sanitize_keeps_only_known_fields_each_capped() -> None:
    raw = {
        "persona": {
            "constitution": "x" * 7000,
            "prompt_order": ["a"],
            "output_contract": {"k": 1},
            "evil": 1,
        },
        "dream": {
            "system_prompt": "d " * 5000,
            "limits": {
                "wants_max": 99,
                "longing_step": -3,
                "add_words_max": 2.7,
                "identity_cap_chars": 5000,
                "identity_min_after_drop": 5,
                "x": 1,
            },
            "scene_vocab": ["Kitchen!", "", 3, "Desk"] + ["w"] * 50,
        },
        "harness": {"models": {"fast": "m" * 100, "dream": 7}, "max_tokens": 5000, "timing": {}},
        "body": {"legs": 2},
        "expression": {"face": "ostrich"},
        "meta": {"name": "n" * 100, "kind": "weird", "author": None},
        "identity": "never a config region",
    }
    cfg = souls.sanitize_cfg(raw)
    assert set(cfg) == {"persona", "dream", "harness", "body", "expression", "meta"}
    assert cfg["persona"] == {
        "constitution": "x" * 6000,
        "prompt_order": ["a"],
        "output_contract": {"k": 1},
    }
    assert len(cfg["dream"]["system_prompt"]) <= 8000
    # cleanDreamLimits: positive finite numbers only, clamped; a negative is dropped, so the
    # creature's default applies.
    assert cfg["dream"]["limits"] == {
        "identity_cap_chars": 1600,
        "identity_min_after_drop": 20,
        "wants_max": 4,
        "add_words_max": 2,
    }
    assert cfg["dream"]["scene_vocab"][:3] == ["kitchen", "desk", "w"]
    assert len(cfg["dream"]["scene_vocab"]) == 40
    assert cfg["harness"] == {"models": {"fast": "m" * 80}, "max_tokens": 1024, "timing": {}}
    assert cfg["meta"]["name"] == "n" * 80 and cfg["meta"]["kind"] == "template"
    assert cfg["meta"]["author"] == ""
    # A bool is not a number to clamp, and the floor holds.
    assert souls.sanitize_cfg({"harness": {"max_tokens": True}})["harness"] == {}
    assert souls.sanitize_cfg({"harness": {"max_tokens": 1}})["harness"]["max_tokens"] == 48
    zero = souls.sanitize_cfg({"dream": {"limits": {"longing_step": 0, "wants_max": float("inf")}}})
    assert zero["dream"]["limits"] == {}
    assert souls.sanitize_cfg({"dream": {"limits": {"wants_max": True}}})["dream"]["limits"] == {}
    assert souls.sanitize_cfg({"dream": {"limits": []}})["dream"] == {}
    # An integer past a double is JSON.parse's Infinity upstream, so it is dropped too.
    huge = souls.sanitize_cfg({"dream": {"limits": {"wants_max": 10**400}}})
    assert huge["dream"]["limits"] == {}


def test_a_seed_takes_the_personality_and_leaves_the_memories() -> None:
    src = _grown() | {"expression": {"face": "x"}, "dream": {"system_prompt": "dream well"}}
    soul = souls.seeded("Bolt", src, "Sprout / First week")
    assert soul["persona"] == {"constitution": "You are a small creature."}
    assert soul["expression"] == {"face": "x"} and soul["dream"] == {"system_prompt": "dream well"}
    assert soul["meta"]["name"] == "Bolt" and soul["meta"]["based_on"] == "Sprout / First week"
    assert not {"identity", "identityCore", "goals", "scratch", "log"} & set(soul)


def test_the_size_cap_holds_for_the_phone_and_for_a_file() -> None:
    big = {"soul_format": 3, "log": [{"txt": "x" * 1000} for _ in range(1100)]}
    with pytest.raises(souls.SoulTooBig):
        souls.from_phone(big)
    with pytest.raises(souls.SoulTooBig, match="at most 1 MB"):
        souls.from_file(big)


def test_the_phone_must_send_a_format_3_soul() -> None:
    assert souls.from_phone({"soul_format": 3, "x": 1}) == {"soul_format": 3, "x": 1}
    for bad in (None, [], "soul", {"soul_format": 2}, {}):
        with pytest.raises(souls.SoulError):
            souls.from_phone(bad)


def test_an_import_is_cleaned_and_loses_the_files_revision() -> None:
    raw = _grown() | {"persona": {"constitution": "  loud   voice  ", "hack": 1}}
    raw["meta"] = {"name": "Sprout", "kind": "snapshot"}
    soul = souls.from_file(raw)
    assert soul["persona"] == {"constitution": "loud voice"}
    assert "rev" not in soul and "writeId" not in soul
    # The memory surfaces travel as the file has them.
    assert soul["identityCore"] == "My person held me the first morning."
    assert soul["log"] == [{"t": 1, "txt": "Saw the blue mug."}]


@pytest.mark.parametrize(
    ("raw", "words"),
    [
        ("text", "isn't a soul file"),
        ({"hello": 1}, "isn't a soul file"),
        ({"soul_format": 2}, "older format"),
        ({"identity": "I am a v1 creature"}, "older format"),
    ],
)
def test_a_file_that_is_not_a_soul_says_so(raw: object, words: str) -> None:
    with pytest.raises(souls.SoulError, match=words):
        souls.from_file(raw)


def test_editing_a_text_region_replaces_only_its_text() -> None:
    soul = _grown() | {"persona": {"constitution": "old", "prompt_order": ["a"]}}
    before = copy.deepcopy(soul)
    out = souls.edit_region(soul, "persona", "  You are Captain Bolt.  ")
    assert out["persona"] == {"constitution": "You are Captain Bolt.", "prompt_order": ["a"]}
    assert soul == before
    assert out["identity"] == soul["identity"]
    assert souls.edit_region(soul, "dream", "Dream of boats.")["dream"] == {
        "system_prompt": "Dream of boats."
    }


@pytest.mark.parametrize(
    ("region", "value", "words"),
    [
        ("persona", "   ", "can't be empty"),
        ("persona", 7, "can't be empty"),
        ("persona", "x" * 6001, "6001 characters"),
        ("dream", "x" * 8001, "8001 characters"),
        ("meta", {"name": " "}, "Give it a name"),
        ("meta", "Sprout", "Give it a name"),
        ("expression", "[1]", "one { … } block"),
        ("harness", [1], "one { … } block"),
        ("body", {"x": "y" * 70_000}, "too big"),
        ("identity", "I am new", "at least 20 characters"),
        ("identity", 7, "has to be text"),
        ("identity", "x" * 1601, "1601 characters"),
        ("identityCore", "x" * 241, "241 characters"),
        ("identityCore", None, "has to be text"),
        ("scratch", "napping", "one { … } block"),
        ("scratch", {"state": 3, "rules": []}, "has to be text"),
        ("scratch", {"state": "up", "rules": "be kind"}, "list of lines"),
        ("scratch", {"state": "up", "rules": [f"rule {i}" for i in range(25)]}, "25 rules"),
        ("goals", {"selfName": "Pip", "wants": ["a", "b", "c", "d", "e"]}, "5 wants"),
        ("goals", {"selfName": None, "wants": []}, "name has to be text"),
        ("log", {"t": 1, "txt": "x"}, "list of at most 1000"),
        ("log", [{"t": "noon", "txt": "x"}], "a time and its words"),
        ("log", [{"t": True, "txt": "x"}], "a time and its words"),
        ("log", [{"t": 1}], "a time and its words"),
        ("mood", {"v": 1}, "can't be edited here"),
    ],
)
def test_a_bad_edit_is_refused_with_words(region: str, value: object, words: str) -> None:
    with pytest.raises(souls.SoulError, match=words):
        souls.edit_region(_grown(), region, value)


def test_editing_who_it_is_replaces_the_identity_and_stamps_it_newer() -> None:
    soul = _grown() | {"identityAt": 5}
    out = souls.edit_region(soul, "identity", "  I am Pip.\n  I love   the garden.  ")
    assert out["identity"] == "I am Pip. I love the garden."
    assert out["identityAt"] > 5
    assert out["identityCore"] == soul["identityCore"]
    assert soul["identityAt"] == 5


def test_editing_the_bond_line_may_empty_it_and_stamps_it_newer() -> None:
    out = souls.edit_region(_grown(), "identityCore", "  My person is Jeff. ")
    assert out["identityCore"] == "My person is Jeff." and out["identityAt"] > 0
    assert souls.edit_region(_grown(), "identityCore", " ")["identityCore"] == ""


def test_editing_right_now_keeps_its_mood_and_reflexes() -> None:
    scratch = {"state": "x", "rules": [], "mood": {"v": 0.3, "e": 0.5}, "reflexes": ["r"]}
    soul = _grown() | {"scratch": scratch}
    out = souls.edit_region(
        soul,
        "scratch",
        {"state": " sitting  still ", "rules": ["stay on the desk", "  ", "x" * 99]},
    )
    assert out["scratch"] == {
        "state": "sitting still",
        "rules": ["stay on the desk", "x" * 90],
        "mood": {"v": 0.3, "e": 0.5},
        "reflexes": ["r"],
    }


def test_editing_wants_renames_it_and_keeps_its_other_goals() -> None:
    soul = _grown() | {"goals": {"selfName": "Sprout", "wants": ["w"], "longing": 0.4}}
    out = souls.edit_region(
        soul, "goals", {"selfName": ' Fish "the" <Great> ', "wants": ["walk to Tamsin", ""]}
    )
    assert out["goals"] == {
        "selfName": "Fish the Great",
        "wants": ["walk to Tamsin"],
        "longing": 0.4,
    }


def test_editing_the_diary_keeps_each_lines_flags_drops_emptied_ones_and_sorts() -> None:
    soul = _grown() | {"log": [{"t": 1, "txt": "a", "amb": 1}, {"t": 2, "txt": "b", "amb": 0}]}
    out = souls.edit_region(
        soul, "log", [{"t": 5, "txt": "new"}, {"t": 2, "txt": " "}, {"t": 1, "txt": " A  fixed "}]
    )
    assert out["log"] == [{"t": 1, "txt": "A fixed", "amb": 1}, {"t": 5, "txt": "new"}]


def test_editing_meta_and_the_json_regions() -> None:
    out = souls.edit_region(_grown(), "meta", {"name": "Bolt", "description": "Brave"})
    assert out["meta"]["name"] == "Bolt" and out["meta"]["description"] == "Brave"
    out = souls.edit_region(_grown(), "harness", {"max_tokens": 9, "models": {"fast": "m"}})
    assert out["harness"] == {"max_tokens": 48, "models": {"fast": "m"}}
    out = souls.edit_region(_grown(), "body", {"legs": 2, "servo_trim": [0, 1]})
    assert out["body"] == {"legs": 2, "servo_trim": [0, 1]}
    assert souls.edit_region({}, "expression", {"face": "x"})["expression"] == {"face": "x"}


def test_rename_writes_meta_name_and_keeps_the_creatures_own_name() -> None:
    out = souls.renamed(_grown(), "Quorra's buddy")
    assert out["meta"]["name"] == "Quorra's buddy"
    assert out["goals"]["selfName"] == "Sprout"
    assert souls.renamed({}, "Pip")["meta"]["name"] == "Pip"


def test_export_is_a_snapshot_without_the_tabs_bookkeeping() -> None:
    out = souls.for_export(_grown(), "Sprout")
    assert out["meta"]["kind"] == "snapshot" and out["meta"]["name"] == "Sprout"
    assert "writeId" not in out and out["soul_format"] == 3
    assert souls.for_export({}, "Pip")["meta"]["name"] == "Pip"


def test_a_snapshots_bond_line_is_capped() -> None:
    raw = _grown() | {"identityCore": "b " * 400, "meta": {"kind": "snapshot"}}
    assert len(souls.from_file(raw)["identityCore"]) == 240
    raw = _grown() | {"identityCore": {"forged": 1}, "meta": {"kind": "snapshot"}}
    assert "identityCore" not in souls.from_file(raw)


def test_a_template_is_born_fresh() -> None:
    """soul.js soulFromFile's template branch: no bond, no diary, none of its person's choices,
    so a gallery soul never arrives with its uploader's bond line."""
    raw = _grown() | {
        "meta": {"name": "Gallery Bolt", "kind": "template"},
        "goals": {"wants": list("abcdef"), "selfName": "Bolt", "owner": "Q", "longing": 0.5},
        "scratch": {"rules": ["be brave"], "mood": {"v": 1, "e": 1}, "glows": [1]},
    }
    soul = souls.from_file(raw)
    assert soul["identityCore"] == "" and soul["log"] == [] and soul["bornAt"] == 0
    assert soul["goals"]["wants"] == list("abcd") and soul["goals"]["longing"] == 0.5
    assert "selfName" not in soul["goals"] and "owner" not in soul["goals"]
    assert soul["scratch"]["rules"] == ["be brave"] and soul["scratch"]["mood"] == {"v": 1, "e": 1}
    assert "glows" not in soul["scratch"]
    # The personality and its identity text still come with it.
    assert soul["identity"] == raw["identity"]
    bare = souls.from_file({"soul_format": 3, "goals": {"longing": True}})
    assert bare["goals"]["longing"] == 0.2 and bare["scratch"]["rules"] == []
