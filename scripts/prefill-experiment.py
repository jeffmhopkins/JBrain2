#!/usr/bin/env python
"""Build and cost a MODE-(b) tool prefill for jerv, and emit a ready-to-POST
`/api/debug/tool-probe` body so the surface can be tested on the live box with no terminal.

Mode (b) (`docs/proposed/TOOL_CATALOG_PLAN.md` §4) keeps every tool CALLABLE — its
`input_schema` stays in the turn's tool array — and defers only the verbose usage prose to
an on-demand `tool_explain(name)`. That is the safe half of the catalog design: nothing is
armed mid-turn, so there is no KV-prefix invalidation, no extra hop on the common path, and
none of the native-tool-calling contradiction that gates W2/W3 there.

A HOT CORE keeps its full description inline. The owner's call is `web_search` + `web_fetch`:
they are the tools jerv actually reaches for, and `web_fetch` carries the densest safety prose
in the surface (never guess a URL; a search FORM is not evidence of absence). Keeping both hot
costs ~4% of today's spend — see the accounting this script prints.

Two constraints the build honours, both discovered the hard way:

1. **Some prose IS the schema.** `analyze_stream`'s `mode`/`captions` allowed values live in
   the description because a JSON-Schema `enum` on that object deterministically segfaults the
   gpt-oss harmony/GBNF path (see its sidecar's NOTE, and the regression test in
   `tests/unit/test_agent_readtools.py`). Deferring that text would leave the model guessing at
   legal values, so a CONSTRAINT_CARRIER keeps its full description regardless of hotness.
2. **A model that doesn't know what it doesn't know won't ask.** The one-line summary has to
   carry the *when to use*, not the *what it is*. This script derives summaries mechanically
   from the first sentence, which is the WEAK version — an authored `summary:` field (the
   metadata half of catalog W0, and forward-compatible here: an authored field wins if present)
   will read better. So a probe result from this script is a LOWER BOUND on mode (b)'s accuracy,
   which is the right direction for an experiment to err.

Usage:
    scripts/prefill-experiment.py --report
    scripts/prefill-experiment.py --mode b --probe "what's the weather in titusville" > body.json
    scripts/prefill-experiment.py --mode b-strict --hot web_search,web_fetch --report

Then POST the emitted body to `/api/debug/tool-probe` from the owner debug console
(`docs/runbooks/DEBUG_ACCESS.md`) and compare `tool_calls` against the same prompt run with
`--mode today`. No handler ever runs; only schemas reach the model.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend" / "src"))

from jbrain.agent.agents import JERV_TOOLS  # noqa: E402
from jbrain.agent.toolfile import load_tool  # noqa: E402

TOOLS_DIR = ROOT / "backend" / "src" / "jbrain" / "agent" / "tools"

# Tools whose DESCRIPTION carries a constraint the schema cannot express (see module note).
# These keep their full prose in every mode; deferring it would remove a validation, not a doc.
CONSTRAINT_CARRIERS = frozenset({"analyze_stream"})

DEFAULT_HOT = ("web_search", "web_fetch")

# Appended to a deferred tool's summary in `b-strict`. The gate is enforced at the HANDLER
# (refuse-or-auto-explain on first call), never by withholding the schema — withholding is
# mode (a), which cannot work under grammar-constrained native tool-calling because the model
# has no token to emit for a tool that is absent from the array.
STRICT_SUFFIX = " Call tool_explain first."

EXPLAIN_TOOL = {
    "name": "tool_explain",
    "description": (
        "Read the full use-guide for one tool — what it does, when to reach for it, its "
        "parameters with an example, and its pitfalls. The tool list you have gives each tool "
        "a one-line summary and its exact parameters; call this when the summary is not enough "
        "to be sure this is the right tool or to fill its arguments correctly. It reads a "
        "guide only: it never runs the tool."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The tool name to explain, exactly as listed."}
        },
        "required": ["name"],
    },
}


# The discriminating probe set. A generic prompt ("what's the weather") proves nothing — every
# mode gets it right. Each case below is a routing decision that the DEFERRED prose is what
# currently teaches, so a mode-(b) regression should show up here first. `expect` is the tool
# the live surface should choose; `tests` names the rule that would be lost.
SUITE = [
    {
        "probe": "what did that SpaceX video I analysed say about the launch date?",
        "expect": ("external_video", "deep_research", "deep_produce"),
        "tests": "library-vs-web routing — a specific analysed video's transcript is not on the web",
    },
    {
        "probe": "look up whether Frank Collige holds a Florida insurance license",
        "expect": ("portal_search",),
        "tests": "state portal vs web_search — a plain fetch sees only the empty search FORM",
    },
    {
        "probe": "plot the number of launches by provider this year",
        "expect": ("render_bars",),
        "tests": "categorical x-axis picks bars, not render_chart (which is time-only)",
    },
    {
        "probe": "chart how my daily step count changed over the last three months",
        "expect": ("render_chart",),
        "tests": "dated series picks the line chart — the pair that used to be split",
    },
    {
        "probe": "I attached a voice memo — what does it say?",
        "expect": ("transcribe",),
        "tests": "words-only picks transcribe, not the frame-sampling analyze_video",
    },
    {
        "probe": "give me a thorough writeup on how stablecoins hold their peg",
        "expect": ("deep_research",),
        "tests": "the deepest ladder — a bounded run, not the hours-long background one",
    },
    {
        "probe": "what's the article at https://spaceflightnow.com about today?",
        "expect": ("web_fetch", "news_feed", "web_search"),
        "tests": "hot-core control — this should be unaffected by any deferral",
    },
]


def summarize(toolfile, cap: int = 130) -> str:
    """The always-on one-liner. Prefers an authored `summary:` sidecar field when one exists
    (forward-compatible with catalog W0a); otherwise falls back to the description's first
    sentence, trimmed. The fallback is deliberately crude — see the module note."""
    authored = getattr(toolfile.spec, "summary", None)
    if isinstance(authored, str) and authored.strip():
        return authored.strip()
    flat = " ".join(toolfile.description.split())
    cut = flat.find(". ")
    if 0 < cut < cap:
        return flat[: cut + 1]
    return flat[:cap].rstrip() + ("…" if len(flat) > cap else "")


def build(mode: str, hot: frozenset[str]) -> list[dict]:
    """The tool array for a mode. `today` is the live shape (full description + params)."""
    out: list[dict] = []
    for name in sorted(JERV_TOOLS):
        path = TOOLS_DIR / f"{name}.tool"
        if not path.exists():  # config-gated tool with no sidecar on this box
            continue
        tf = load_tool(path)
        full = mode == "today" or name in hot or name in CONSTRAINT_CARRIERS
        if full:
            desc = tf.description
        else:
            desc = summarize(tf)
            if mode == "b-strict":
                desc += STRICT_SUFFIX
        out.append({"name": name, "description": desc, "input_schema": tf.spec.params})
    if mode != "today":
        out.append(EXPLAIN_TOOL)
    return out


def cost(tools: list[dict]) -> tuple[int, int]:
    """(chars, approx tokens) of the serialized tool payload."""
    chars = sum(len(t["description"]) + len(json.dumps(t["input_schema"])) for t in tools)
    return chars, chars // 4


def report(hot: frozenset[str]) -> None:
    base = build("today", hot)
    base_chars, base_tok = cost(base)
    print(f"hot core: {', '.join(sorted(hot))}")
    print(f"constraint carriers (always full): {', '.join(sorted(CONSTRAINT_CARRIERS))}\n")
    print(f"{'mode':10s} {'tools':>6s} {'chars':>9s} {'~tokens':>9s} {'vs today':>9s}")
    for mode in ("today", "b", "b-strict"):
        tools = build(mode, hot)
        chars, tok = cost(tools)
        print(f"{mode:10s} {len(tools):6d} {chars:9,d} {tok:9,d} {100 * chars / base_chars:8.0f}%")

    # What mode (b) cannot reach: params must stay for a tool to remain callable.
    params = sum(len(json.dumps(t["input_schema"])) for t in base)
    print(
        f"\nparams floor: {params:,} chars (~{params // 4:,} tok) = {100 * params / base_chars:.0f}%"
        " of today — mode (b) cannot go below this without dynamic arming (mode (a), gated)."
    )
    fat = sorted(base, key=lambda t: -len(json.dumps(t["input_schema"])))[:5]
    print("fattest PARAMS (the next lever after prose, and a different list than fattest tools):")
    for t in fat:
        print(f"  {t['name']:18s} {len(json.dumps(t['input_schema'])):6,d}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("today", "b", "b-strict"), default="b")
    ap.add_argument("--hot", default=",".join(DEFAULT_HOT), help="comma-separated hot-core tool names")
    ap.add_argument("--probe", help="user_text for a /api/debug/tool-probe body written to stdout")
    ap.add_argument("--report", action="store_true", help="print the token accounting and exit")
    ap.add_argument(
        "--suite",
        action="store_true",
        help="emit one /api/debug/tool-probe body per SUITE case, as JSON lines "
        "(each line also carries `_expect`/`_tests` for scoring — strip before POSTing)",
    )
    args = ap.parse_args()

    hot = frozenset(n for n in args.hot.split(",") if n)
    unknown = hot - JERV_TOOLS
    if unknown:
        ap.error(f"unknown hot-core tool(s): {sorted(unknown)}")

    if args.suite:
        tools = build(args.mode, hot)
        for case in SUITE:
            json.dump(
                {
                    "user_text": case["probe"],
                    "task": "agent.turn",
                    "tools": [],
                    "raw_tools": tools,
                    "_expect": list(case["expect"]),
                    "_tests": case["tests"],
                },
                sys.stdout,
            )
            sys.stdout.write("\n")
        return

    if args.report or not args.probe:
        report(hot)
        return

    tools = build(args.mode, hot)
    json.dump(
        {"user_text": args.probe, "task": "agent.turn", "tools": [], "raw_tools": tools},
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
