"""Guards the vendored gpt-oss harmony template (deploy/chat-templates/gpt-oss-120b.jinja).

The stock harmony template renders a live `Current date` into the prompt HEAD, which broke
daily prompt-cache reuse of the whole ~29k-token persona+tools prefix (kv_prefix.py). Our copy
moves that one line to the prompt TAIL. These checks pin the SHAPE of that edit; they cannot
see upstream drift — deploy/chat-templates/README.md's diff step is the check that does.
"""

from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[3] / "deploy" / "chat-templates" / "gpt-oss-120b.jinja"


@pytest.fixture(scope="module")
def src() -> str:
    return TEMPLATE.read_text()


def test_the_live_date_is_emitted_exactly_once(src: str) -> None:
    # Moved, never duplicated or dropped — the model must still get the real date, once.
    assert src.count('strftime_now("%Y-%m-%d")') == 1


def test_the_date_sits_at_the_tail_not_the_head(src: str) -> None:
    # The whole point: the date must NOT render inside the system message (the prompt head),
    # and MUST render after the tool namespace (the tail of the static persona+tools prefix).
    date_pos = src.index('Current date: " + strftime_now')
    sys_macro = src.index("macro build_system_message")
    sys_macro_end = src.index("endmacro", sys_macro)
    tools_render = src.index('render_tool_namespace("functions", tools)')
    assert not (sys_macro < date_pos < sys_macro_end), "date must leave the system message"
    assert date_pos > tools_render, "date must render after the tools block (at the tail)"


def test_harmony_markers_survive_so_tool_parsing_still_detects_gpt_oss(src: str) -> None:
    # llama.cpp picks the gpt-oss tool-call parser when the template source contains
    # `<|channel|>` (common/chat.cpp). Only the date line moved, so every marker is intact.
    for marker in ("<|channel|>", "<|start|>", "<|end|>", "<|call|>", "Valid channels"):
        assert marker in src, marker
