"""Roster gate for the PWA's "Worked" step polish (frontend/src/agent/toolSummary.ts).

Every tool the agent can run renders in the owner's Worked strip, and a tool that
skips its polish shows up as a raw snake_case row with no visible target (the
jmolt_observe regression this gate came from). So each `.tool` sidecar must
register, in toolSummary.ts:

- a friendly STEP_LABELS entry ("Observed jmolt", never `jmolt_observe`), and
- an inline-arg policy: INLINE_ARGS keys naming the call's human-readable target
  (query/url/action/…, shown to the right of the label), or an explicit NO_INLINE
  opt-out when every argument is an opaque id / boolean / structured blob.

Parsed here with regexes over the literal maps — the frontend file is the single
source of truth and this test only keeps it honest against the sidecar roster
(docs/reference/ASSISTANT.md "Tools as `.tool` sidecars")."""

import re
from pathlib import Path

from jbrain.agent.toolfile import load_tool

_REPO = Path(__file__).resolve().parents[3]
_TOOLS_DIR = _REPO / "backend" / "src" / "jbrain" / "agent" / "tools"
_SUMMARY_TS = _REPO / "frontend" / "src" / "agent" / "toolSummary.ts"

# Step names the backend emits that are not `.tool` sidecars (e.g. the synthetic
# "queued" job step). They may appear in the frontend maps without a sidecar.
_SYNTHETIC = {"queued"}


def _summary_src() -> str:
    return _SUMMARY_TS.read_text(encoding="utf-8")


def _block(src: str, start: str, end: str) -> str:
    begin = src.index(start) + len(start)
    return src[begin : src.index(end, begin)]


def _step_labels(src: str) -> set[str]:
    body = _block(src, "const STEP_LABELS: Record<string, string> = {", "\n};")
    return set(re.findall(r"^\s*(\w+):", body, re.MULTILINE))


def _inline_args(src: str) -> dict[str, list[str]]:
    body = _block(src, "const INLINE_ARGS: Record<string, readonly string[]> = {", "\n};")
    return {
        name: re.findall(r'"([^"]+)"', keys)
        for name, keys in re.findall(r"^\s*(\w+): \[([^\]]*)\]", body, re.MULTILINE)
    }


def _no_inline(src: str) -> set[str]:
    body = _block(src, "const NO_INLINE: ReadonlySet<string> = new Set([", "\n]);")
    return set(re.findall(r'"([^"]+)"', body))


def _roster() -> dict[str, dict[str, object]]:
    """Every sidecar's name -> its params `properties` map."""
    tools = {}
    for path in sorted(_TOOLS_DIR.glob("*.tool")):
        tool = load_tool(path)
        props = tool.spec.params.get("properties") or {}
        tools[tool.spec.name] = props
    assert tools, f"no .tool sidecars found under {_TOOLS_DIR}"
    return tools


def test_every_tool_has_a_friendly_step_label() -> None:
    labels = _step_labels(_summary_src())
    # `lookup_*` tools are labelled by the stepLabel prefix rule, not the map.
    missing = [n for n in _roster() if n not in labels and not n.startswith("lookup_")]
    assert not missing, (
        "these tools would render as raw snake_case rows in the Worked strip — add a "
        f"STEP_LABELS entry in {_SUMMARY_TS.relative_to(_REPO)}: {missing}"
    )


def test_every_tool_declares_an_inline_arg_policy() -> None:
    src = _summary_src()
    with_keys = set(_inline_args(src))
    opted_out = _no_inline(src)
    roster = set(_roster())
    missing = sorted(roster - with_keys - opted_out)
    assert not missing, (
        "these tools have no inline-arg policy — list the human-readable target key(s) "
        "in INLINE_ARGS, or add the tool to NO_INLINE if every argument is opaque "
        f"({_SUMMARY_TS.relative_to(_REPO)}): {missing}"
    )
    both = sorted(with_keys & opted_out)
    assert not both, f"these tools are in both INLINE_ARGS and NO_INLINE — pick one: {both}"


def test_inline_arg_keys_exist_in_each_tool_schema() -> None:
    roster = _roster()
    bad = []
    for name, keys in _inline_args(_summary_src()).items():
        if name in _SYNTHETIC or name not in roster:
            continue
        unknown = [k for k in keys if k not in roster[name]]
        if unknown:
            bad.append((name, unknown))
    assert not bad, f"INLINE_ARGS names keys the tool schema doesn't have: {bad}"


def test_frontend_maps_carry_no_stale_tools() -> None:
    src = _summary_src()
    known = set(_roster()) | _SYNTHETIC
    stale = sorted((set(_step_labels(src)) | set(_inline_args(src)) | _no_inline(src)) - known)
    assert not stale, (
        "these entries in toolSummary.ts no longer match any .tool sidecar (renamed or "
        f"removed tool?): {stale}"
    )
