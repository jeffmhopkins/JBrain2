"""The `browse_trace` step view: what the browse sub-agent did on a site, for the OWNER.

Binding mock `docs/mocks/browse-trace/a-timeline.html` (owner pick 2026-10-06). One rung per
model turn, plus the host's own work (the opening navigation, an extraction on the start
page, the closing fact check), each opening onto its commands, its timings and the turn's
Thinking / Actions / Page-it-saw panels.

Two rules decide everything here:

- **Untrusted, and inert.** Every string a page could have shaped — titles, element names,
  excerpts, notes that quote the page, the model's reasoning and its call (which echo the
  page) — passes `browse_policy.quarantine` (invisible characters stripped and folded first,
  then markup, links and addresses removed) and is capped before it is stored. The PWA renders
  each one as plain text. URLs shown are the HOST-observed addresses, through `safe_url`.
- **Bounded.** A stored chat must not grow with a busy page: every field has a cap, and the
  whole payload has one (`MAX_VIEW_CHARS`); past it the oldest turns lose their excerpts, then
  their reasoning and calls, then their command detail, and the view says it was trimmed.

The plain-language lines ("Clicked “Showtimes”") are written HERE from the action and the
element, never by the model. None of this reaches jerv: `render_for_caller` is unchanged.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from jbrain.agent import browse_gate
from jbrain.agent import browse_policy as policy
from jbrain.agent.browse import OUTCOME_TEXT, BrowseRun, BrowseStep, BrowseTurn
from jbrain.agent.contracts import ViewPayload

VIEW = "browse_trace"
# Per field. A line the owner reads at a glance; an element as the snapshot names it; a host
# note (a refusal's reason, the fact check); the panels' bodies.
LINE_CHARS = 160
ELEMENT_CHARS = 160
NOTE_CHARS = 300
REASONING_CHARS = 1_500
CALL_CHARS = 1_200
ANSWER_CHARS = policy.MAX_ANSWER_CHARS
EXCERPT_LINES = 12
EXCERPT_LINE_CHARS = 200
TITLE_CHARS = 160
URL_CHARS = 300
NOT_RUN_SHOWN = 5
# The whole payload, as JSON. Room for a full 30-step run with its panels at the caps above;
# a run past it is trimmed oldest-first (see `_fit`).
MAX_VIEW_CHARS = 48_000

# Why the run needed a browser at all (browse_gate's reasons), as the opening rung's badge.
GATE_BADGE = {
    browse_gate.JS_SHELL: "JS shell",
    browse_gate.GATED: "location picker",
    browse_gate.THIN: "thin page",
    browse_gate.BLOCKED: "blocked to a fetch",
}
# The lines `PageView.render` and `page_delta` lead a view with.
_VIEW_HEAD = ("URL:", "Title:", "HTTP status:", "Page:", "[Same page;", "Changed or new:")
_HOST_N = 0
_EXTRACTS = frozenset({"extract", "extract_first"})


# Sanitising reads every character, so a field is cut to a few times its cap FIRST: a busy
# 30-step run otherwise spends seconds cleaning text it then throws away. The slack leaves
# room for what the cleaning removes.
_PRECUT = 4


def _clean(text: str, cap: int) -> str:
    """Page- or model-shaped text as one inert line."""
    cleaned = policy.quarantine(text[: cap * _PRECUT], cap=cap * 2)
    return " ".join(cleaned.split())[:cap]


def _block(text: str, cap: int) -> str:
    """Page- or model-shaped text as inert lines (the panels keep their line breaks)."""
    return policy.quarantine(text[: cap * _PRECUT], cap=cap)


def _excerpt(view: str) -> tuple[str, int]:
    """The first lines of the view the model chose from, each capped; and its line count."""
    lines = [line for line in view.splitlines() if line.strip()]
    # The view's own head (address, title, the delta's preamble) is shown beside the excerpt
    # already; the excerpt is the page.
    while lines and lines[0].startswith(_VIEW_HEAD):
        lines.pop(0)
    # "..." not "…": the folding `quarantine` repeats would widen "…" to three characters.
    shown = [
        line if len(line) <= EXCERPT_LINE_CHARS else line[: EXCERPT_LINE_CHARS - 3] + "..."
        for line in (policy.strip_invisible(raw) for raw in lines[:EXCERPT_LINES])
    ]
    return _block("\n".join(shown), EXCERPT_LINES * (EXCERPT_LINE_CHARS + 1)), len(lines)


def _url(url: str) -> str:
    safe = policy.safe_url(url) if url else None
    return safe[:URL_CHARS] if safe else ""


def _path(url: str) -> str:
    """A host-observed address as the rail shows it: its path, or the site for `/`."""
    safe = _url(url)
    if not safe:
        return ""
    parts = urlsplit(safe)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    return path if path not in ("", "/") else (parts.hostname or "")


def _site(url: str) -> str:
    safe = _url(url)
    host = urlsplit(safe).hostname if safe else None
    return host.removeprefix("www.") if host else ""


def _quoted(name: str) -> str:
    return f"“{name}”" if name else ""


def _element_name(element: str) -> str:
    """The quoted name out of `role "name"`, or the role alone."""
    if '"' in element:
        return element.split('"', 1)[1].rsplit('"', 1)[0]
    return element


def command_text(step: BrowseStep) -> str:
    """One command in plain words, written by the host from the action and its element."""
    name = _clean(_element_name(step.element), LINE_CHARS // 2)
    target = _quoted(name) if name else (f"[{step.args['index']}]" if "index" in step.args else "")
    value = _clean(str(step.args.get("value", step.args.get("text", ""))), 60)
    action = step.action
    if action == "click":
        return f"Clicked {target}".rstrip()
    if action in ("type", "type_text"):
        return f"Typed {_quoted(value)} into {target}".rstrip()
    if action in ("select", "select_option"):
        picked = value or _clean(" ".join(map(str, step.args.get("values", []))), 60)
        return f"Picked {_quoted(picked)} in {target}".rstrip()
    if action == "enter":
        return "Pressed Enter"
    if action == "press_key":
        return f"Pressed {_clean(str(step.args.get('key', '')), 20)}".rstrip()
    if action in ("goto", "navigate"):
        asked = str(step.args.get("value") or step.args.get("url") or "")
        return f"Went to {_path(step.url) or _path(asked) or 'a new address'}"
    if action in ("back", "go_back"):
        return "Went back"
    if action == "read":
        return "Read the page" + (f" from {_quoted(value)}" if value else "")
    if action == "done":
        return "Wrote the answer"
    if action == "finish":
        return "Said the page shows the answer"
    if action == "give_up":
        return "Gave up"
    if action == "wait_for":
        return "Waited for the page"
    if action == "snapshot":
        return "Looked at the page again"
    if action == "tabs":
        return "Switched tabs"
    if action == "(none)":
        return "Chose no action"
    if action == "act":
        return "Sent commands the host could not run"
    return f"Asked for {_clean(action, 40)}"


def _status(step: BrowseStep) -> str:
    if step.ok:
        return "ok"
    if step.note.startswith("the browser reported an error") or step.note == "loop":
        return "failed"
    return "refused"


def _command(step: BrowseStep, url_before: str) -> dict[str, Any]:
    index = step.args.get("index")
    ref = step.args.get("ref")
    # The fast loop's index is the host's own int; a B1 ref is what the model sent.
    handle = f"[{index}]" if isinstance(index, int) else (_clean(str(ref), 16) if ref else "")
    element = _clean(step.element, ELEMENT_CHARS)
    moved = (
        step.ok
        and bool(step.url)
        and bool(url_before)
        and step.url.split("#", 1)[0] != url_before.split("#", 1)[0]
    )
    out: dict[str, Any] = {
        "status": _status(step),
        "text": command_text(step),
        "element": f"{handle} {element}".strip() if element else handle,
        "note": _clean(step.note, NOTE_CHARS),
        "ms": step.browser_ms,
    }
    if step.refound:
        out["refound"] = True
    if step.settle_ms:
        out["settle_ms"] = step.settle_ms
    if moved:
        out["moved_to"] = _path(step.url)
    return out


def _summary(commands: list[dict[str, Any]]) -> str:
    done = [c["text"] for c in commands if c["status"] != "not_run"]
    if not done:
        return "Ran nothing"
    # "Clicked “Tue Oct 6”, clicked “Showtimes”": one sentence, so later verbs go lower case.
    head = ", ".join([done[0], *(t[:1].lower() + t[1:] for t in done[1:2])])
    more = f" +{len(done) - 2} more" if len(done) > 2 else ""
    return _clean(head, LINE_CHARS) + more


def _rung_status(commands: list[dict[str, Any]]) -> str:
    states = [c["status"] for c in commands if c["status"] != "not_run"]
    if states and all(s == "ok" for s in states):
        return "ok"
    if any(s == "ok" for s in states):
        return "part"
    return "bad"


def _badges(commands: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for key, kind, word in (
        ("refound", "re", "re-found"),
        ("refused", "ref", "refused"),
        ("failed", "ref", "failed"),
        ("not_run", "warn", "not run"),
    ):
        count = sum(
            1 for c in commands if (c.get("refound") if key == "refound" else c["status"] == key)
        )
        if count:
            out.append({"kind": kind, "text": f"{count} {word}"})
    return out


def _turn_panels(turn: BrowseTurn | None) -> dict[str, Any]:
    if turn is None:
        return {}
    excerpt, total = _excerpt(turn.page_view)
    out: dict[str, Any] = {
        "reasoning": _block(turn.reasoning, REASONING_CHARS),
        "call": _block(turn.call, CALL_CHARS),
    }
    if excerpt:
        out["page"] = {
            "title": _clean(turn.page_title, TITLE_CHARS),
            "url": _url(turn.page_url),
            "changes": turn.page_changes,
            "tokens": turn.page_tokens,
            "excerpt": excerpt,
            "lines_shown": min(total, EXCERPT_LINES),
            "lines_total": max(total, turn.page_lines),
        }
    return out


def _model_rung(
    n: int, steps: list[BrowseStep], turn: BrowseTurn | None, url_before: str
) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    prev = url_before
    for step in steps:
        commands.append(_command(step, prev))
        prev = step.url or prev
    for label in (turn.not_run if turn else [])[:NOT_RUN_SHOWN]:
        commands.append({"status": "not_run", "text": _clean(label, LINE_CHARS), "element": ""})
    last = next((s for s in reversed(steps) if s.url), None)
    first = steps[0] if steps else None
    model_ms = max((s.model_ms for s in steps), default=0)
    browser_ms = sum(s.browser_ms for s in steps)
    settle_ms = sum(s.settle_ms for s in steps)
    rung: dict[str, Any] = {
        "n": n,
        "kind": "model",
        "summary": _summary(commands),
        "status": _rung_status(commands),
        "badges": _badges(commands),
        "title": _clean(last.title, TITLE_CHARS) if last else "",
        "url": _url(last.url) if last else "",
        "ms": model_ms + browser_ms + settle_ms,
        "commands": commands,
        "nums": {
            "model_ms": model_ms,
            "browser_ms": browser_ms,
            "settle_ms": settle_ms,
            "page_tokens": last.snapshot_tokens if last else 0,
            "prompt_tokens": first.prompt_tokens if first else 0,
            "cached_tokens": first.cached_tokens if first else 0,
            "output_tokens": first.output_tokens if first else 0,
        },
    }
    rung.update(_turn_panels(turn))
    return rung


def _host_rung(steps: list[BrowseStep], gate_reason: str | None) -> dict[str, Any]:
    """The work the host did before the first turn: the start page, and an extraction tried
    on it. No model choice, so no reasoning, no call."""
    commands: list[dict[str, Any]] = []
    for step in steps:
        if step.action in _EXTRACTS:
            text = "Tried reading the answer off the start page"
            commands.append(
                {
                    "status": "ok" if step.ok else "refused",
                    "text": text,
                    "element": "",
                    "note": _clean(step.note, NOTE_CHARS),
                    "ms": step.model_ms,
                }
            )
        else:
            commands.append(_command(step, ""))
    nav = next((s for s in steps if s.action == "navigate"), None)
    site = _site(nav.url) if nav else ""
    badges = _badges(commands)
    if gate_reason in GATE_BADGE:
        badges.insert(0, {"kind": "warn", "text": GATE_BADGE[gate_reason]})
    return {
        "n": _HOST_N,
        "kind": "host",
        "summary": f"Opened {_quoted(site)}" if site else "Opened the start page",
        "status": "ok" if nav is not None and nav.ok else "bad",
        "badges": badges,
        "title": _clean(nav.title, TITLE_CHARS) if nav else "",
        "url": _url(nav.url) if nav else "",
        "ms": sum(s.browser_ms + s.model_ms for s in steps),
        "commands": commands,
        "nums": {
            "browser_ms": sum(s.browser_ms for s in steps),
            "page_tokens": nav.snapshot_tokens if nav else 0,
        },
    }


def _check(run: BrowseRun, closing: BrowseStep | None, extracts: list[BrowseStep]) -> dict:
    """The closing rung: the answer and the host's own check of it, and whether the
    separate no-thinking extraction ran (B1's `finish` always extracts; fast only when the
    check fails `done`'s answer)."""
    rows: list[dict[str, Any]] = []
    if closing is not None and closing.action == "done":
        rows.append(
            {
                "label": "done's answer",
                "text": _clean(closing.note, NOTE_CHARS),
                "ok": closing.ok,
            }
        )
    for step in extracts:
        rows.append(
            {
                "label": "extraction",
                "text": (
                    f"read the page's full text, no thinking · {step.model_ms} ms ·"
                    f" {step.prompt_tokens} tok in · {step.output_tokens} out"
                ),
                "ok": None,
                "ms": step.model_ms,
                "prompt_tokens": step.prompt_tokens,
                "output_tokens": step.output_tokens,
            }
        )
        rows.append({"label": "its answer", "text": _clean(step.note, NOTE_CHARS), "ok": step.ok})
    if not extracts:
        passed = closing is not None and closing.action == "done" and closing.ok
        rows.append(
            {
                "label": "extraction",
                "text": "not needed — done's answer passed" if passed else "did not run",
                "ok": None,
            }
        )
    if run.answered:
        label = (
            "Read the answer off the page, then checked it"
            if extracts
            else "Checked the answer against the page"
        )
    else:
        label = OUTCOME_TEXT.get(run.outcome, run.outcome).capitalize()
    return {
        "summary": label,
        "outcome": run.outcome,
        "answered": run.answered,
        "verified": run.verified,
        "ms": sum(s.model_ms for s in extracts),
        "answer": _block(run.answer, ANSWER_CHARS),
        "rows": rows,
        "extracted": bool(extracts),
        "error": _clean(run.error, NOTE_CHARS),
    }


def build_view(run: BrowseRun, gate_reason: str | None = None) -> ViewPayload:
    """The `browse_trace` view of a finished run. `gate_reason` is why browse_gate let the
    run start (the fetch found a JS shell, a picker…), shown on the opening rung."""
    turns = {t.n: t for t in run.turns}
    groups: dict[int, list[BrowseStep]] = {}
    extracts: list[BrowseStep] = []
    host: list[BrowseStep] = []
    for step in run.steps:
        if step.n == _HOST_N:
            if step.action == "extract_first" and run.answered and step.ok:
                extracts.append(step)
            else:
                host.append(step)
        elif step.action in _EXTRACTS:
            extracts.append(step)
        else:
            groups.setdefault(step.n, []).append(step)
    rungs: list[dict[str, Any]] = []
    if host:
        rungs.append(_host_rung(host, gate_reason))
    url = next((s.url for s in host if s.url), "")
    closing: BrowseStep | None = None
    for n in sorted(set(groups) | {t for t in turns if t != _HOST_N}):
        steps = groups.get(n, [])
        rungs.append(_model_rung(n, steps, turns.get(n), url))
        url = next((s.url for s in reversed(steps) if s.url), url)
        if steps and steps[-1].action in ("done", "finish"):
            closing = steps[-1]
    first = next((s.url for s in run.steps if s.url), run.final_url)
    data: dict[str, Any] = {
        "site": _site(first) or _site(run.final_url),
        "loop": run.loop,
        "elapsed_ms": run.elapsed_ms,
        "pages": len(run.sources),
        "steps": rungs,
        "check": _check(run, closing, extracts),
    }
    return ViewPayload(view=VIEW, surface="inline", data=_fit(data))


def _size(data: dict[str, Any]) -> int:
    return len(json.dumps(data, ensure_ascii=False))


def _fit(data: dict[str, Any]) -> dict[str, Any]:
    """Hold the payload under `MAX_VIEW_CHARS`, dropping the bulkiest detail of the OLDEST
    turns first — the end of a run is what the owner opens it to check."""
    if _size(data) <= MAX_VIEW_CHARS:
        return data
    data["trimmed"] = True
    rungs: list[dict[str, Any]] = data["steps"]
    for fields in (("page",), ("reasoning", "call"), ("commands",)):
        for rung in rungs:
            for name in fields:
                if name == "commands" and len(rung.get("commands", [])) > 1:
                    rung["commands"] = rung["commands"][:1]
                elif name != "commands":
                    rung.pop(name, None)
            if _size(data) <= MAX_VIEW_CHARS:
                return data
    # A pathological run: keep the first and last rungs and the check.
    while len(rungs) > 2 and _size(data) > MAX_VIEW_CHARS:
        rungs.pop(1)
    return data
