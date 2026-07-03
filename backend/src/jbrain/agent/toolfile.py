"""Load a tool definition from a co-located `.tool` sidecar (YAML frontmatter +
prose body), mirroring `jbrain.llm.promptfile`.

A tool is ONE artifact, like a prompt: its frontmatter is the `ToolSpec`
(contracts) — name, `version`, the arguments JSON Schema, permission class,
domains, and flags — and its body is the model-facing description, given the same
care as prompt prose. Validation runs at load time, so a malformed sidecar fails
startup, never a live call. The body+spec digest is pinned per version by a CI
guard, so changing a tool's described behavior is a deliberate version bump.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from jbrain.agent.contracts import ToolSpec

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


class ToolFileError(ValueError):
    """A `.tool` sidecar is missing, malformed, or its frontmatter fails the
    ToolSpec schema — raised at load time so a bad tool fails fast at startup."""


@dataclass(frozen=True)
class ToolFile:
    """A validated sidecar: the typed spec plus its model-facing description."""

    spec: ToolSpec
    description: str
    # Loop 4 governance flag (Loop 4 was removed; see docs/reference/ASSISTANT.md): opt-in, default
    # False. Deliberately NOT a ToolSpec field — it is governance metadata, not
    # model-facing behavior, so it stays out of the digest and a tool's editability
    # can change without forcing a version bump. The SELF_EDIT_LOCKED deny-set still
    # wins over it (non-neg #12).
    self_editable: bool = False

    @property
    def digest(self) -> str:
        """A content hash over the description and the spec, pinned per version by
        the CI guard so prose/param edits force a deliberate `version` bump."""
        blob = self.description + "\x00" + json.dumps(self.spec.model_dump(), sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()


def load_tool(path: Path) -> ToolFile:
    """Parse and validate a `.tool` sidecar. Fails (ToolFileError) on malformed
    frontmatter, a spec that violates ToolSpec, or an empty description."""
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(raw)
    if match is None:
        raise ToolFileError(f"{path}: missing '---' YAML frontmatter block")
    meta = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta, dict):
        raise ToolFileError(f"{path}: frontmatter is not a mapping")
    # The single trailing newline is file hygiene, not part of the description.
    body = match.group(2)
    if body.endswith("\n"):
        body = body[:-1]
    if not body.strip():
        raise ToolFileError(f"{path}: empty description body — the model needs one")
    # Pop the governance flag before ToolSpec (which forbids extras) so it never
    # touches the spec or the digest — it is sidecar metadata, not a tool contract.
    self_editable = bool(meta.pop("self_editable", False))
    try:
        spec = ToolSpec.model_validate(meta)
    except ValidationError as exc:
        raise ToolFileError(f"{path}: invalid tool frontmatter: {exc}") from exc
    return ToolFile(spec=spec, description=body, self_editable=self_editable)
