"""The fast loop's page view and command shape (BROWSER_FAST_LOOP_PLAN L1). Pure.

- **Per-run monotonic indexes.** Every actionable element the model is shown gets a number,
  `[n]`, bound to its playwright ref, role and name on the document it was seen on, and
  keeps that number for the rest of the run: append-only history keeps old views in the
  prompt, so a number must never come to mean a different element. A number acts only while
  its element is on the latest page (the B1 rule); within a batch, a target the page
  re-rendered is re-found by role + name on the same document, never across a navigation.
- **The indexed view.** The pruned snapshot with each actionable line led by its number
  instead of carrying a ref, long text clipped, and runs of identical controls ("Details",
  "Details", …) collapsed. Full text is one `read` away.
- **Commands.** The `act` tool takes up to five `{do, index?, value?}` commands; `parse`
  turns the model's arguments into checked commands or says what is wrong. The action gate
  (`browse_policy`) is applied by the loop to each one, unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urldefrag

from jbrain.agent import browse_policy as policy

MAX_COMMANDS = 5
COMMANDS = ("click", "select", "type", "enter", "goto", "read", "back", "done")
# Commands that need an element number.
TARGETED = frozenset({"click", "select", "type"})
# Commands that need a value.
VALUED = frozenset({"select", "type", "goto"})
# A typed or chosen value is short; refusing a long one costs a model call, so the model is
# told the limit. `done`'s answer is never refused for length — a whole decision rewritten
# to fit is the costliest refusal there is — it is cut to `policy.MAX_ANSWER_CHARS` instead.
MAX_VALUE_CHARS = 500
# One text line's share of the indexed view: the model reads to choose; `read` has the rest.
VIEW_LINE_CHARS = 200
# A run of identical controls longer than this shows its first few and a count.
REPEAT_SHOWN = 3
# What one `read` hands the model of the page's full text.
READ_CHARS = 6_000

_REF_LINE = re.compile(
    r"^(?P<indent>\s*)- (?P<body>.*?) \[ref=(?P<ref>[A-Za-z0-9]+)\](?P<rest>.*)$"
)
_TEXT_ITEM = re.compile(r"^(?P<indent>\s*)- (?P<text>.*)$")
_INDEX = re.compile(r"\[\d+\] ")


@dataclass(frozen=True)
class Bound:
    """What an index was given to: the element as the model was shown it."""

    n: int
    ref: str
    role: str
    name: str
    doc: int


@dataclass
class IndexBook:
    """The run's numbering. `observe` is idempotent: showing the same page twice gives the
    same numbers."""

    bound: dict[int, Bound] = field(default_factory=dict)
    doc: int = 0
    _next: int = 1
    _doc_url: str | None = None
    _by_key: dict[tuple[int, str, str, str], int] = field(default_factory=dict)

    def observe(self, page: policy.PageView) -> dict[str, int]:
        """Number `page`'s actionable elements: ref → index. A new address (fragment aside)
        is a new document, whose refs mean new elements."""
        url = urldefrag(page.url)[0]
        if url != self._doc_url:
            self.doc += 1
            self._doc_url = url
        out: dict[str, int] = {}
        for ref, element in page.elements.items():
            key = (self.doc, ref, element.role, element.name)
            n = self._by_key.get(key)
            if n is None:
                n = self._next
                self._next += 1
                self._by_key[key] = n
                self.bound[n] = Bound(n, ref, element.role, element.name, self.doc)
            out[ref] = n
        return out

    def resolve(
        self, page: policy.PageView, n: int, *, refind: bool = False
    ) -> tuple[policy.Element | None, str | None]:
        """The element number `n` names on `page`, or why it cannot act. `refind` (within a
        batch, after an earlier command) re-finds a re-rendered element by role + name, only
        when exactly one matches on the same document."""
        bound = self.bound.get(n)
        if bound is None:
            return None, f"There is no element [{n}]. Use a number from the latest page."
        mapping = self.observe(page)
        element = page.elements.get(bound.ref)
        if element is not None and mapping.get(bound.ref) == n:
            return element, None
        if refind and bound.doc == self.doc:
            same = [
                e for e in page.elements.values() if e.role == bound.role and e.name == bound.name
            ]
            if len(same) == 1:
                return same[0], None
        return None, (
            f"[{n}] is not on the current page (it was on an earlier view, or the page changed"
            " under it). Use a number from the latest page."
        )


def indexed(page: policy.PageView, book: IndexBook) -> policy.PageView:
    """`page` with its outline as the indexed view — what the model reads and what a delta
    is taken against. The elements, text and readable copy (what the gate and the fact check
    use) are the page's own, untouched."""
    numbers = book.observe(page)
    lines: list[str] = []
    for line in page.outline.splitlines():
        match = _REF_LINE.match(line)
        if match is not None and match.group("ref") in numbers:
            rest = match.group("rest")
            if rest.startswith(": "):
                rest = ": " + _clip(rest[2:])
            number = numbers[match.group("ref")]
            line = f"{match.group('indent')}- [{number}] {match.group('body')}{rest}"
        else:
            text = _TEXT_ITEM.match(line)
            if text is not None:
                line = f"{text.group('indent')}- {_clip(text.group('text'))}"
        lines.append(line)
    return replace(page, outline="\n".join(_collapse(lines)))


def _clip(text: str) -> str:
    if len(text) <= VIEW_LINE_CHARS:
        return text
    return text[: VIEW_LINE_CHARS - 1].rstrip() + "…"


def _collapse(lines: list[str]) -> list[str]:
    """Runs of the same control (numbers aside) beyond `REPEAT_SHOWN` become one count line
    that lists the hidden ones' numbers, so each stays actionable."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        key = _INDEX.sub("", lines[i], count=1)
        j = i + 1
        while (
            j < len(lines) and _INDEX.search(lines[i]) and _INDEX.sub("", lines[j], count=1) == key
        ):
            j += 1
        run = j - i
        out.extend(lines[i : i + min(run, REPEAT_SHOWN)])
        if run > REPEAT_SHOWN:
            # The hidden ones keep their numbers on the count line: still reachable.
            indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            hidden = " ".join(
                m.group(0).strip()
                for line in lines[i + REPEAT_SHOWN : j]
                if (m := _INDEX.search(line)) is not None
            )
            out.append(f"{indent}- (… {run - REPEAT_SHOWN} more like the line above: {hidden})")
        i = j
    return out


def read_text(page: policy.PageView, phrase: str = "") -> str:
    """Up to `READ_CHARS` of the page's full text, from the first line containing `phrase`
    when one is given and found."""
    text = page.readable
    if phrase.strip():
        at = text.casefold().find(phrase.strip().casefold())
        if at > 0:
            text = text[text.rfind("\n", 0, at) + 1 :]
    if len(text) > READ_CHARS:
        text = (
            text[:READ_CHARS].rstrip()
            + "\n[… more on the page; read again with a phrase from further down]"
        )
    return text or "(the page has no readable text)"


@dataclass(frozen=True)
class Command:
    do: str
    index: int | None = None
    value: str = ""

    def brief(self) -> dict[str, Any]:
        out: dict[str, Any] = {"do": self.do}
        if self.index is not None:
            out["index"] = self.index
        if self.value:
            out["value"] = self.value if len(self.value) <= 300 else self.value[:299] + "…"
        return out


def parse(arguments: dict[str, Any]) -> tuple[list[Command], str | None]:
    """The `act` call's commands, checked for shape only (the gate judges them later), or
    why the call is malformed. More than `MAX_COMMANDS` is refused, not truncated: the model
    should know which of its commands would not have run."""
    raw = arguments.get("commands")
    if not isinstance(raw, list) or not raw:
        return [], "act needs `commands`: a list of 1 to 5 commands."
    if len(raw) > MAX_COMMANDS:
        return [], f"At most {MAX_COMMANDS} commands per act; nothing was run."
    out: list[Command] = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            return [], f'Command {i} is not an object like {{"do": "click", "index": 3}}.'
        do = item.get("do")
        if do not in COMMANDS:
            return [], f"Command {i}: `do` must be one of {', '.join(COMMANDS)}."
        index = _number(item.get("index"))
        if do in TARGETED and index is None:
            return [], f"Command {i}: {do} needs the element's number as `index`."
        value = item.get("value", "")
        if not isinstance(value, str):
            value = "" if value is None else str(value)
        if do in VALUED and not value.strip():
            return [], f"Command {i}: {do} needs a `value`."
        if do == "done":
            value = _cut_answer(value)
        elif len(value) > MAX_VALUE_CHARS:
            return [], (
                f"Command {i}: `value` is too long ({len(value)} characters; at most"
                f" {MAX_VALUE_CHARS})."
            )
        out.append(Command(do, index if do in TARGETED else None, value.strip()))
    return out, None


def _cut_answer(value: str) -> str:
    """`done`'s answer within `policy.MAX_ANSWER_CHARS`, cut at a line break so no item is
    left half-written (the fact check would sink a torn time)."""
    if len(value) <= policy.MAX_ANSWER_CHARS:
        return value
    head = value[: policy.MAX_ANSWER_CHARS]
    cut = head.rfind("\n")
    return head[:cut] if cut > 0 else head


def _number(raw: object) -> int | None:
    """An index as the model sent it: an int, or digits in a string ("12", "[12]")."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip(" []").isdigit():
        return int(raw.strip(" []"))
    return None
