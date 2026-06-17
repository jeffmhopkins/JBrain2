"""Loop 4 — prompt/tool self-edit substrate (docs/LOOP4_PROMPT_TOOL_EDIT_PLAN.md).

The roadmap's most security-sensitive deliverable, built **propose-only and
fail-closed**. A self-edit is a STAGED diff the owner applies as a real PR off-box
— never a file write, never a runtime apply (non-neg #6; the box is air-gapped from
git). Two independent structural bars make the data/instruction-boundary and
domain-classification prompts physically untargetable (non-neg #12):

1. an opt-in `self_editable` frontmatter flag (default False), and
2. the `SELF_EDIT_LOCKED` deny-set below, which wins even if a file is mismarked.

The ONLY way to build a `prompt-edit` ProposalSpec is `build_prompt_edit_spec`,
which resolves its target through `self_editable_targets` — so a spec can never be
staged against a locked, unmarked, or unknown artifact. The diff is computed here
from the current first-party body; it is **data**, never executable.
"""

from __future__ import annotations

import difflib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from jbrain.agent.proposals import NodeSpec, ProposalSpec
from jbrain.agent.toolfile import ToolFileError, load_tool
from jbrain.llm.promptfile import PromptError, load_prompt

# Prompts whose prose IS the firewall: the data/instruction boundary
# (`agent.system`) and domain classification (`note.extract`), plus the Loop-4
# drafter itself (`prompt.self_edit`) — you cannot self-edit the self-editor. Keyed
# by artifact `name`. Locked even if a file is mistakenly flagged `self_editable`:
# the belt to the flag's suspenders (non-neg #12). Editing this set is a human code
# change behind normal PR review — it is code, not data, and unreachable from a
# drafting turn.
SELF_EDIT_LOCKED: frozenset[str] = frozenset({"agent.system", "note.extract", "prompt.self_edit"})


class PromptEditError(ValueError):
    """A self-edit was attempted against a locked/unmarked/unknown target, with no
    version bump, or with no actual change — raised before anything is staged so the
    bar is fail-closed."""


@dataclass(frozen=True)
class EditableTarget:
    """A self-editable `.prompt`/`.tool` the drafter may read and propose a diff to.
    Discovery only ever yields targets that pass BOTH bars, so holding one is proof
    of eligibility."""

    kind: str  # "prompt" | "tool"
    name: str
    rel_path: str  # path relative to the jbrain package root, for the diff + preview
    version: str  # the current version (str for prompts, str(int) for tools)
    body: str  # the current first-party body the drafter reads as input


def _package_root() -> Path:
    import jbrain

    return Path(jbrain.__file__).resolve().parent


def self_editable_targets(root: Path | None = None) -> dict[str, EditableTarget]:
    """Discover every `.prompt`/`.tool` under `root` (default: the jbrain package)
    that is `self_editable` AND not in `SELF_EDIT_LOCKED`, keyed by artifact name.
    The two filters are the structural immutability bar; a malformed sidecar that
    fails to load is skipped (it is not a valid edit target), never crashing
    discovery. Two filters make this fail-closed:

    - a path that resolves OUTSIDE `base` (e.g. a symlink escaping the package) is
      ineligible — discovery never surfaces a target it can't honestly locate;
    - a duplicate `self_editable` name across files raises (a config error must not
      silently let one file shadow another, which would diff/export the wrong one).
    """
    base = (root or _package_root()).resolve()
    targets: dict[str, EditableTarget] = {}

    def _add(name: str, target: EditableTarget) -> None:
        if name in targets:
            raise PromptEditError(
                f"duplicate self-editable artifact name {name!r}"
                f" ({targets[name].rel_path} and {target.rel_path}) — refusing to"
                " stage until the collision is resolved"
            )
        targets[name] = target

    for path in sorted(base.rglob("*.prompt")):
        if not _within(path, base):
            continue
        try:
            pf = load_prompt(path)
        except PromptError:
            continue
        if not pf.self_editable or pf.name in SELF_EDIT_LOCKED:
            continue
        _add(pf.name, EditableTarget("prompt", pf.name, _rel(path, base), pf.version, pf.body))
    for path in sorted(base.rglob("*.tool")):
        if not _within(path, base):
            continue
        try:
            tf = load_tool(path)
        except ToolFileError:
            continue
        if not tf.self_editable or tf.spec.name in SELF_EDIT_LOCKED:
            continue
        _add(
            tf.spec.name,
            EditableTarget(
                "tool", tf.spec.name, _rel(path, base), str(tf.spec.version), tf.description
            ),
        )
    return targets


def _within(path: Path, base: Path) -> bool:
    return path.resolve().is_relative_to(base)


def _rel(path: Path, base: Path) -> str:
    return str(path.resolve().relative_to(base))


def unified_diff(old: str, new: str, *, rel_path: str) -> str:
    """A git-applyable unified diff old→new for the owner's preview (and their
    off-box `git apply`). Pure data — it never executes here."""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            lineterm="",
        )
    )


# Shapes a self-edit draft must never introduce — the egress/exfiltration surfaces
# #9 forbids in agent output, applied here to the proposed body so a drafter tricked
# by a poisoned failure-mode can't smuggle one past the structural gate. The owner
# review is the terminal gate; this is defense-in-depth before staging.
_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\([^)]*\)")
_URL = re.compile(r"https?://", re.IGNORECASE)
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def lint_proposed_body(body: str) -> list[str]:
    """Reject a proposed prompt/tool body that introduces an external-load or markup
    surface (#9): markdown links/images, bare URLs, or HTML tags. Returns a list of
    violation reasons (empty = clean). Pure and total, so the injection suite can pin
    it; the real guarantee is still the allowlist bar + owner review, this is the
    belt against a drafter coaxed into an exfil-shaped draft."""
    reasons: list[str] = []
    if _MARKDOWN_LINK.search(body):
        reasons.append("introduces a markdown link/image (an external-load surface, #9)")
    if _URL.search(body):
        reasons.append("introduces a URL (an external-load surface, #9)")
    if _HTML_TAG.search(body):
        reasons.append("introduces an HTML tag (a markup-injection surface, #9)")
    return reasons


def build_prompt_edit_spec(
    target_name: str,
    *,
    proposed_body: str,
    proposed_version: str,
    rationale: str,
    new_eval_fixture: str,
    root: Path | None = None,
) -> ProposalSpec:
    """The ONLY constructor of a `prompt-edit` ProposalSpec — fail-closed by
    construction. It resolves `target_name` through `self_editable_targets` (so a
    locked/unmarked/unknown target raises before staging), requires a version bump
    and a real change, and packs a single record-only leaf whose preview IS the
    deliverable: the diff + bumped version + rationale + a new eval fixture for the
    applied branch's CI. Behavior edits are cross-cutting, so the proposal is
    `general`-domain and (at the call site) owner-principal — the only principal RLS
    lets stage one."""
    target = self_editable_targets(root).get(target_name)
    if target is None:
        raise PromptEditError(
            f"{target_name!r} is not a self-editable target (locked, unmarked, or unknown)"
            " — refusing to stage"
        )
    proposed_version = proposed_version.strip()
    if not proposed_version:
        raise PromptEditError(f"{target_name}: a proposed version is required")
    # A change of version, not strict ordering: prompt versions are opaque strings
    # (`agent-system-v4`). The monotonic-bump semantics are enforced where they can
    # be — the applied branch's CI digest-pin guard — not here (Fork B).
    if proposed_version == target.version:
        raise PromptEditError(
            f"{target_name}: version must be bumped from {target.version!r} (got the same)"
        )
    diff = unified_diff(target.body, proposed_body, rel_path=target.rel_path)
    if not diff.strip():
        raise PromptEditError(f"{target_name}: proposed body is identical — nothing to stage")
    preview = {
        "target_kind": target.kind,
        "target_name": target.name,
        "target_path": target.rel_path,
        "current_version": target.version,
        "proposed_version": proposed_version,
        "unified_diff": diff,
        "rationale": rationale.strip(),
        "new_eval_fixture": new_eval_fixture.strip(),
    }
    node = NodeSpec(
        id=str(uuid.uuid4()),
        type="leaf",
        op="prompt_edit_record",
        label=f"{target.name}: {target.version} → {proposed_version}",
        preview=preview,
    )
    return ProposalSpec(
        kind="prompt-edit",
        domain="general",
        title=f"edit {target.kind} {target.name} → {proposed_version}",
        nodes=[node],
        provenance={"source": "self-edit"},
    )
