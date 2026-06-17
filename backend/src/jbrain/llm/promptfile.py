"""Load an LLM prompt from a co-located `.prompt` file (YAML frontmatter + body).

A prompt is ONE artifact: its prose, the output JSON schema it expects, the
capability tier it needs (model strength — never a concrete model id), a token
budget, and a `version` stamped onto every record the prompt produces. Bumping
the version makes a corpus re-run a budgeted migration instead of silent drift
(docs/ANALYSIS.md "Reprocessing"), so the version travels WITH the prose.

The body is a template with `{{ name }}` variables. A deliberately tiny
renderer (not Jinja) substitutes only those tokens and leaves every other brace
alone, so the literal `{` of a JSON example in the prose is never mistaken for a
variable. Validation runs at load time: a missing field, an unknown tier, or an
undeclared template variable fails startup, never a live call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Capability tiers a prompt may request; jbrain.llm.router maps each to a
# concrete provider:model, so a prompt file never names a model.
STRENGTHS = frozenset({"high", "low", "vision", "embedding"})

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class PromptError(ValueError):
    """A prompt file is missing, malformed, references an unknown tier, or uses
    an undeclared template variable — raised at load time so a bad prompt fails
    fast at startup, never mid-call."""


@dataclass(frozen=True)
class PromptFile:
    name: str
    version: str
    strength: str
    body: str
    description: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    inputs: tuple[str, ...] = ()
    output_format: str | None = None
    output_schema: dict[str, Any] | None = None
    domain_guidance: dict[str, str] = field(default_factory=dict)
    # Loop 4 governance flag (docs/LOOP4_PROMPT_TOOL_EDIT_PLAN.md): opt-in, default
    # False — only an explicitly-marked prompt is eligible for prompt/tool self-edit,
    # and even then the SELF_EDIT_LOCKED deny-set wins (non-neg #12). Not part of the
    # prompt's behavior, so it never enters the version-bump content digest.
    self_editable: bool = False

    def render(self, **variables: Any) -> str:
        """The body with every declared `{{ var }}` substituted. Every input the
        file declares must be supplied (StrictUndefined semantics) — a missing
        value is an error, never a silently blank prompt."""
        missing = [name for name in self.inputs if name not in variables]
        if missing:
            raise PromptError(f"{self.name}: missing template vars {missing}")
        return _VAR.sub(lambda m: str(variables[m.group(1)]), self.body)


def load_prompt(path: Path) -> PromptFile:
    """Parse and validate a `.prompt` file. Fails (PromptError) on a malformed
    frontmatter, a missing required field, an unknown strength tier, or a body
    variable the frontmatter never declared."""
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(raw)
    if match is None:
        raise PromptError(f"{path}: missing '---' YAML frontmatter block")
    meta = yaml.safe_load(match.group(1)) or {}
    # The single trailing newline is file hygiene (every editor adds one), not
    # part of the prompt; a prompt that truly needs a final blank line adds two.
    body = match.group(2)
    if body.endswith("\n"):
        body = body[:-1]
    if not isinstance(meta, dict):
        raise PromptError(f"{path}: frontmatter is not a mapping")

    for required in ("name", "version", "strength"):
        if not meta.get(required):
            raise PromptError(f"{path}: frontmatter missing required field {required!r}")
    strength = str(meta["strength"])
    if strength not in STRENGTHS:
        raise PromptError(
            f"{path}: unknown strength {strength!r}; expected one of {sorted(STRENGTHS)}"
        )

    inputs = tuple(meta.get("input") or ())
    used = {m.group(1) for m in _VAR.finditer(body)}
    undeclared = used - set(inputs)
    if undeclared:
        raise PromptError(f"{path}: body uses undeclared template vars {sorted(undeclared)}")

    output = meta.get("output") or {}
    schema = output.get("schema")
    if schema is not None and not isinstance(schema, dict):
        raise PromptError(f"{path}: output.schema must be a mapping (JSON Schema)")

    return PromptFile(
        name=str(meta["name"]),
        version=str(meta["version"]),
        strength=strength,
        body=body,
        description=str(meta.get("description", "")),
        config=dict(meta.get("config") or {}),
        inputs=inputs,
        output_format=output.get("format"),
        output_schema=schema,
        domain_guidance=dict(meta.get("domain_guidance") or {}),
        self_editable=bool(meta.get("self_editable", False)),
    )
