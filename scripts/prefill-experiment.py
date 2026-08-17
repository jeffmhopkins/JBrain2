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

MEASURED on the live box (2026-08-17, gpt-oss-120b, hot core = web_search + web_fetch);
raw runs in `scratchpad/prefill-probe-results.json`:

    mode        mean input_tokens   vs today
    today                  22,694       100%
    b                      11,502      50.7%
    b-strict               11,747      51.8%

Mode (b) halves the prefill with no selection change on six of seven cases.

**A retracted conclusion, kept here because the mistake is instructive.** A first pass measured
the corrected chart case (#3) at `render_bars` 3/3 under `today` and 2/3 under the derived
summaries, and concluded that authored summaries were a hard prerequisite. It did not replicate:
the next run had `b-derived` at 3/3 and the AUTHORED build at 2/3 — the reverse. Case #3 is
simply unstable under mode (b), and an n=3 comparison cannot tell an unstable case from a
summary-quality effect. Do not read a 3-sample difference off this harness as a finding; the
`--suite` runner is single-shot per case by design, so repeat a case before believing it.

What DID replicate, and is the more interesting result: the FL-license case (#2) chooses
`public_records` — the wrong tool, since that source covers medical NPI licences, not insurance
— 5/5 under `today` AND 5/5 under derived summaries, but picks `portal_search` correctly under
the AUTHORED summary. A short, aimed summary routed BETTER than 2.5k chars of full description.
That is the case for authored summaries: not that deferral breaks routing, but that most of a
long description is not doing routing work at all.

Three more results to carry forward. (1) `char // 4` OVERESTIMATES this
tokenizer by ~22% (27,748 predicted vs 22,694 measured), so the `--report` figures are a
conservative upper bound; trust `input_tokens` from a probe over the report. (2) **b-strict's
prose gate was ignored entirely** — `tool_explain` was never called once, in any mode, even
though every deferred summary in b-strict ended "Call tool_explain first." A mandatory
explain-before-use step therefore has to be enforced in the HANDLER (refuse-or-auto-explain on
first call), exactly like the scout's search budget, which is engine-enforced for the same
reason. Prose the model is free to skip is not a gate.

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


# Authored when-to-use summaries, kept HERE rather than in the sidecars so the wording can be
# iterated against the probe without 40+ `version:` bumps and pin-hash updates on every pass.
# Once a set proves out, it migrates into a `summary:` sidecar field (catalog W0a) and this map
# empties. The rule they follow — learned from the 3/3 -> 2/3 regression the derived summaries
# caused — is LEAD WITH WHEN, NOT WHAT: the trigger phrasing an owner would actually type comes
# first, because a description that opens by defining itself yields a summary that cannot route.
AUTHORED = {
    "analyze_image": "Answer a question about what an image SHOWS (attached, generated, or fetched), using the local vision model.",
    "analyze_video": "Understand an attached video — what it shows AND says. Use when the visuals matter; for words only, transcribe is far cheaper.",
    "canvas": "Draw on a photo or a blank sheet: 'put a red box around this', 'point at the X', annotate, sketch a diagram.",
    "check_channel": "Find a YouTube channel's new uploads not yet in the library. For 'check the channels', 'anything new from X'.",
    "compare_images": "Answer a question comparing two or more images side by side: 'what changed', 'which is better', 'spot the difference'.",
    "crop_regions": "Cut parts out of one image, each returned as its own saveable picture: 'crop out every label', 'export each face'.",
    "current_location": "Where the owner is right now — for 'where am I' or anything 'near me'. Returns a city name by default.",
    "current_time": "A fresh date/time reading, or the time in another zone. Today's date is already in your context each turn.",
    "deep_produce": "Research a topic and get a PLAN, TABLE, BRIEF, DIFFERENTIAL or TIMELINE — when the useful output is not a prose report.",
    "deep_research": "Research an open, multi-source question and get a cited report: 'research X', 'do a deep dive', 'write me a report on'.",
    "deepest_research": "Escalation only — an hours-long BACKGROUND run that returns an acknowledgement now, not a report. Prefer deep_research.",
    "edit_image": "Change an existing image: 'make it night', 'remove the car', 'same but in blue'.",
    "external_video": "Search or read the owner's analysed-YouTube library: 'what did that video say', 'what do my videos say about X'.",
    "fetch_image": "Fetch a web image's bytes so you can SEE it — web_fetch strips images, so this comes first for any picture on the web.",
    "generate_image": "Make a brand-new picture from a description: 'draw me', 'generate an image of'. Never for plotting data.",
    "grab_frame": "Take a still from a video (URL or attachment) at a timestamp: 'screenshot it at 4:20', 'what's on screen when'.",
    "grokipedia": "Search and read xAI's Grokipedia — articles, sections, citations. Background reference, not current news.",
    "hurricane": "Check for an active tropical cyclone near a place: 'is anything coming', 'is there a hurricane'.",
    "news_feed": "Today's articles from curated per-topic feeds (space, ai_tech, national, economy, world, local) — the daily brief's source.",
    "news_search": "Search recent NEWS for dated article leads: 'what happened with X', 'latest on Y'. Prefer over web_search for current events.",
    "ocr": "Read the LITERAL text out of an attached image or PDF, deterministically — error screenshots, receipts, scanned documents.",
    "portal_search": "Query a STATE government portal by name — a state business registry or a state LICENSE lookup: 'is X registered', 'does X hold a license'.",
    "public_records": "Look up a PERSON in free NATIONAL registries — aliases, court records, medical-provider (NPI) licenses, federal actions.",
    "query_server_metrics": "How the box itself is doing — CPU, memory, disk, GPU, fans: 'how's the server', 'is it running hot'.",
    "read_artifact": "Re-read or continue a page you already fetched this chat, from cache — for paging a long transcript across turns.",
    "read_plan": "Read this conversation's plan and where you are in it. Call it before working a step.",
    "remove_external_video": "Stage the removal of one library video for the owner's approval: 'delete that video'.",
    "remove_research_report": "Stage the removal of one saved report for the owner's approval: 'delete that report'.",
    "render_bars": "Graph numbers you already have BY CATEGORY — 'plot/graph/chart this by X', 'how many per Y', 'compare X across Y'. Bars, not a time line.",
    "render_chart": "Graph numbers you already have OVER TIME — 'plot/graph/chart this trend' when the x-axis is dates. Lines, not categories.",
    "research_report": "Search or re-read the owner's saved deep-research reports: 'what did that report say', or to build on an earlier run.",
    "science_search": "Search scholarly literature — arXiv, PubMed, Scholar — for paper leads: 'what does the research say', 'any studies on'.",
    "show_canvas": "Show the owner the canvas you drew. Until you call this, they have seen nothing.",
    "show_external_video": "Show the owner a library video as its analysis card: 'show me that video' — not for reading what it said.",
    "show_research_report": "Show the owner a saved report as its rich card: 'show me that report' — not for reading it yourself.",
    "spawn_subagent": "Fan a task out to web-sandboxed sub-agents and compose their summaries yourself — for breadth you'd otherwise search serially.",
    "transcribe": "Get the WORDS of an attached audio or video clip. Much cheaper and faster than analyze_video when the visuals don't matter.",
    "weather": "Current conditions or the 7-day forecast for a place: 'what's the weather', 'will it rain'.",
    "weather_history": "PAST weather for a place and date range, with the heat index computed: 'how hot was it last July'.",
    "write_plan": "Write or re-status this conversation's plan — ONLY when the owner asks for one. You may never approve a plan yourself.",
    "write_plan_result": "Record a finished plan step: appends your synthesis AND ticks the box. Call it after every step.",
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
        # Data supplied inline ON PURPOSE. The first draft of this case asked the model to
        # "plot launches by provider this year" and scored web_search as a miss — but a gather
        # step first is CORRECT, so the fixture was wrong, not the model. Supplying the numbers
        # isolates the chart-shape decision, which is what this case exists to test.
        "probe": (
            "I counted 12 launches by SpaceX, 4 by ULA and 2 by Rocket Lab this year — "
            "show me that as a graph"
        ),
        "expect": ("render_bars",),
        "tests": "categorical x-axis picks bars, not render_chart (which is time-only)",
    },
    {
        # Likewise: the first draft asked about "my daily step count", which jerv is KB-blind to
        # — answering with NO tool call was correct, and the fixture scored it a miss.
        "probe": (
            "plot this trend: 2026-01-01 180 lb, 2026-02-01 178 lb, 2026-03-01 175 lb, "
            "2026-04-01 174 lb"
        ),
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


def derive(toolfile, cap: int = 130) -> str:
    """The crude first-sentence fallback — i.e. exactly what a MISSING summary looks like. Kept
    as its own mode so the authored-vs-derived delta stays measurable on every re-probe."""
    flat = " ".join(toolfile.description.split())
    cut = flat.find(". ")
    if 0 < cut < cap:
        return flat[: cut + 1]
    return flat[:cap].rstrip() + ("…" if len(flat) > cap else "")


def summarize(toolfile, cap: int = 130) -> str:
    """The always-on one-liner. Prefers an authored `summary:` sidecar field when one exists
    (forward-compatible with catalog W0a); otherwise falls back to the description's first
    sentence, trimmed. The fallback is deliberately crude — see the module note."""
    authored = getattr(toolfile.spec, "summary", None) or AUTHORED.get(toolfile.spec.name)
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
            desc = derive(tf) if mode == "b-derived" else summarize(tf)
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
    for mode in ("today", "b-derived", "b", "b-strict"):
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
    ap.add_argument("--mode", choices=("today", "b", "b-derived", "b-strict"), default="b")
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
