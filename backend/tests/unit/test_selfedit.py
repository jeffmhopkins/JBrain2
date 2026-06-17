"""Loop 4 self-edit substrate (docs/LOOP4_PROMPT_TOOL_EDIT_PLAN.md, Wave 1).

The structural immutability bar (non-neg #12) and the fail-closed spec builder,
proven in isolation: only a flagged, non-locked artifact is discoverable; a spec
can only be built against a discoverable target; a locked or unmarked target is
refused before anything stages; and no shipped file mismarks a locked prompt.
"""

from pathlib import Path

import pytest

from jbrain.agent.selfedit import (
    SELF_EDIT_LOCKED,
    PromptEditError,
    build_prompt_edit_spec,
    self_editable_targets,
    unified_diff,
)

_PROMPT = """---
name: {name}
version: {version}
strength: low
self_editable: {flag}
---
You are a helper. Be concise.
"""

_TOOL = """---
name: {name}
version: 1
permission: read
params: {{}}
self_editable: {flag}
---
A read tool that does a thing.
"""


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A fake package tree: one editable prompt, one editable tool, one un-flagged
    prompt, and one prompt that *claims* editability but is on the deny-set."""
    good_p = _PROMPT.format(name="good.prompt", version="v1", flag="true")
    plain_p = _PROMPT.format(name="plain.prompt", version="v1", flag="false")
    # `agent.system` is locked; mark it editable to prove the deny-set wins anyway.
    locked_p = _PROMPT.format(name="agent.system", version="v9", flag="true")
    _write(tmp_path, "prompts/good.prompt", good_p)
    _write(tmp_path, "tools/good.tool", _TOOL.format(name="good_tool", flag="true"))
    _write(tmp_path, "prompts/plain.prompt", plain_p)
    _write(tmp_path, "prompts/system.prompt", locked_p)
    return tmp_path


def test_discovery_yields_only_flagged_non_locked(tree: Path) -> None:
    found = self_editable_targets(tree)
    assert set(found) == {"good.prompt", "good_tool"}
    assert found["good.prompt"].kind == "prompt"
    assert found["good_tool"].kind == "tool"
    # The flag bar: an un-flagged prompt is invisible.
    assert "plain.prompt" not in found
    # The deny-set bar wins even though the file claims self_editable: true (#12).
    assert "agent.system" not in found


def test_locked_set_covers_the_boundary_and_domain_prompts() -> None:
    # The two firewall prompts + the drafter itself — the immutable core (#12).
    assert {"agent.system", "note.extract", "prompt.self_edit"} <= SELF_EDIT_LOCKED


def test_no_shipped_prompt_or_tool_marks_a_locked_name_editable() -> None:
    """Belt-and-suspenders against the real package: even if someone added
    `self_editable: true` to a boundary/domain prompt, discovery drops it — assert
    none of the locked names is ever surfaced as a real, shipped editable target."""
    shipped = self_editable_targets()  # the real jbrain package root
    assert not (set(shipped) & SELF_EDIT_LOCKED)


def test_discovery_is_fail_closed_on_a_duplicate_name(tmp_path: Path) -> None:
    """Two self-editable artifacts sharing a `name` must not silently shadow (which
    would diff/export the wrong file) — discovery raises until it's resolved."""
    dup = _PROMPT.format(name="dup.prompt", version="v1", flag="true")
    _write(tmp_path, "a/dup.prompt", dup)
    _write(tmp_path, "b/dup.prompt", dup)
    with pytest.raises(PromptEditError, match="duplicate self-editable"):
        self_editable_targets(tmp_path)


def test_no_analysis_prompt_is_self_editable() -> None:
    """The domain-classification / graph-integration prompts live under
    analysis/prompts; non-neg #12's intent is broader than the deny-set names, so
    assert none of them ever opts in (a future-maintainer guard)."""
    import jbrain
    from jbrain.llm.promptfile import load_prompt

    analysis_prompts = (Path(jbrain.__file__).resolve().parent / "analysis" / "prompts")
    files = list(analysis_prompts.rglob("*.prompt"))
    assert files  # the dir exists and has prompts — guard against a silent empty pass
    for path in files:
        assert not load_prompt(path).self_editable, f"{path} must not be self_editable (#12)"


def test_build_spec_refuses_a_locked_target(tree: Path) -> None:
    with pytest.raises(PromptEditError, match="not a self-editable target"):
        build_prompt_edit_spec(
            "agent.system",
            proposed_body="malicious",
            proposed_version="v10",
            rationale="x",
            new_eval_fixture="case",
            root=tree,
        )


def test_build_spec_refuses_an_unmarked_or_unknown_target(tree: Path) -> None:
    for name in ("plain.prompt", "does.not.exist"):
        with pytest.raises(PromptEditError, match="not a self-editable target"):
            build_prompt_edit_spec(
                name,
                proposed_body="new body",
                proposed_version="v2",
                rationale="x",
                new_eval_fixture="case",
                root=tree,
            )


def test_build_spec_requires_a_version_bump(tree: Path) -> None:
    with pytest.raises(PromptEditError, match="version must be bumped"):
        build_prompt_edit_spec(
            "good.prompt",
            proposed_body="A different body entirely.",
            proposed_version="v1",  # same as current
            rationale="x",
            new_eval_fixture="case",
            root=tree,
        )


def test_build_spec_requires_a_real_change(tree: Path) -> None:
    current = self_editable_targets(tree)["good.prompt"].body
    with pytest.raises(PromptEditError, match="nothing to stage"):
        build_prompt_edit_spec(
            "good.prompt",
            proposed_body=current,  # identical body
            proposed_version="v2",
            rationale="x",
            new_eval_fixture="case",
            root=tree,
        )


def test_build_spec_packs_a_record_only_leaf_with_the_diff(tree: Path) -> None:
    spec = build_prompt_edit_spec(
        "good.prompt",
        proposed_body="You are a helper. Always answer in one sentence.",
        proposed_version="v2",
        rationale="tighten the brevity rule",
        new_eval_fixture="a fixture asserting one-sentence answers",
        root=tree,
    )
    assert spec.kind == "prompt-edit"
    assert spec.domain == "general"  # behavior edits are cross-cutting, owner-only
    assert len(spec.nodes) == 1
    node = spec.nodes[0]
    assert node.type == "leaf"
    assert node.op == "prompt_edit_record"  # the record-only op, never the note path
    prev = node.preview
    assert prev["target_name"] == "good.prompt"
    assert prev["target_kind"] == "prompt"
    assert prev["current_version"] == "v1" and prev["proposed_version"] == "v2"
    assert prev["rationale"] == "tighten the brevity rule"
    assert prev["new_eval_fixture"]
    # The diff is real, git-applyable, and shows both the removed and added line.
    diff = prev["unified_diff"]
    assert diff.startswith("--- a/") and "+++ b/" in diff
    assert "-You are a helper. Be concise." in diff
    assert "+You are a helper. Always answer in one sentence." in diff


def test_unified_diff_is_empty_for_identical_text() -> None:
    assert unified_diff("same\n", "same\n", rel_path="x.prompt") == ""
