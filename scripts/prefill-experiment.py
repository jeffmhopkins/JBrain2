#!/usr/bin/env python
"""Build and cost a MODE-(b) tool prefill for jerv, and emit a ready-to-POST
`/api/debug/tool-probe` body so the surface can be tested on the live box with no terminal.

Mode (b) (`docs/plans/TOOL_CATALOG_PLAN.md` §4) keeps every tool CALLABLE — its
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

MEASURED on the live box (2026-08-17, gpt-oss-120b, hot core = web_search + web_fetch). Raw
numbers in `scratchpad/prefill-probe-results.json`.

    mode                 mean input_tokens   vs today   16-case fixture
    today                           22,694       100%   14/16
    b (authored v2)                 11,567      51.0%   14/16
    b, no hot core                  10,451      46.1%   worse (see below)
    b-strict                        11,747      51.8%   —

**Selection parity at half the prefill.** Neither mode dominates: `today` misses the FL-licence
case (it picks `public_records`, which covers medical NPI licences) and the court-records case;
mode (b) misses the court-records case and one noisy science-search case. The single case both
miss is a baseline defect, not a mode effect. On a narrower 7-case fixture mode (b) scored 7/7
against today's 5/7 — that gap did not survive widening, which is the expected direction for a
fixture written alongside the thing it measures.

Five results to carry forward.

(1) **n=1 is noise, and the baseline is not exempt.** `today` scores 3/5 on the categorical-chart
case across repeats. An early pass read a 3-sample difference as a regression; it reversed on the
next run. Repeat any case n>=5 before believing a difference — this runner is single-shot by design.

(2) **Summary WORDING is high-leverage and counter-intuitive.** One line, three phrasings, n=5 on
the same prompt: "Render a categorical breakdown…" 1/5, "Graph numbers you already have BY
CATEGORY…" 2/5, "Show numbers you already have as a bar graph…" 4/5. The winner carries the noun
phrase the owner would type ("as a graph") where the request will match it. Author to the phrasing
the request arrives in, not the vocabulary the codebase uses — and probe each line rather than
trusting a style rule, this one included. A first authored pass made `render_bars` WORSE than
having no summary at all (0/5 vs the derived fallback's 4/5).

(3) **The hot core is justified on accuracy, not just preference.** Dropping it saves a further
~1,115 tokens and makes selection worse (`science_search` 3/3 -> 2/3, `weather_history` scattered
across three answers). The hypothesis that a hot tool's long description out-competes a one-line
summary regardless of fit was tested and REFUTED.

(4) **The b-strict prose gate does not work.** `tool_explain` was never called once, in any mode,
though every deferred summary in b-strict ended "Call tool_explain first." A mandatory
explain-before-use step must be enforced in the HANDLER (refuse-or-auto-explain on first call),
like the scout's search budget. Prose the model may skip is not a gate.

(5) **Watch the fixture as hard as the result.** Three of sixteen cases were wrong on first
writing, all the same way: when a probe contains a deictic ("here", "my", "that"), the tool that
RESOLVES it is a legitimate first step and belongs in `expect`. A fixture bug reads exactly like a
model failure.

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
    # v2. Every line rewritten after the render_bars variant test (see below): lead with the
    # NOUN PHRASE the owner would actually type, quote the literal phrasings the request will
    # arrive in, avoid codebase vocabulary ("render", "artifact", "categorical breakdown") and
    # avoid ALL-CAPS emphasis — the caps variant scored worse than the plain one.
    # web_search / web_fetch are normally HOT (full description). These entries exist so the
    # no-hot-core build can be probed — the test of whether a hot tool's 3.5k chars of prose
    # out-competes a one-line summary regardless of which tool actually fits.
    "web_search": "Search the web — the general lookup when nothing more specific fits. For current news use news_search; for papers, science_search.",
    "web_fetch": "Open a specific URL and read what that page says. Never guess or build a URL yourself, and a search FORM you could not submit is not evidence a record is absent.",
    "analyze_image": "Look at a picture and answer a question about it — 'what's in this photo', 'what does this screenshot show'.",
    "analyze_video": "Watch an attached video and say what it shows and what is said. For the words only, transcribe is far faster.",
    "canvas": "Mark up a photo or draw a figure — 'put a red box around this', 'circle the X', 'draw me a diagram'.",
    "check_channel": "Check a YouTube channel for new uploads not analysed yet — 'anything new from X', 'check the channels'.",
    "compare_images": "Put two or more pictures side by side and answer a question — 'what changed', 'which one is better'.",
    "crop_regions": "Cut pieces out of a picture, each returned as its own image — 'crop out every label', 'pull that part out'.",
    "current_location": "Where the owner is right now — 'where am I', 'what's near me'. Gives a city name unless asked for more.",
    "current_time": "The time right now, or the time somewhere else — 'what time is it in Tokyo'. Today's date is already in your context.",
    "deep_produce": "A researched plan, comparison table, brief, differential or timeline — 'make me a plan for X', 'compare these in a table'.",
    "deep_research": "A deep, cited write-up on an open question — 'research X', 'do a deep dive on', 'write me a report about'.",
    "deepest_research": "An hours-long background research run that answers later, not now. Rare — deep_research handles almost everything.",
    "edit_image": "Change a picture you already have — 'make it night', 'remove the car', 'same but in blue'.",
    "external_video": "Look in the owner's analysed YouTube videos — 'what did that video say', 'what do my videos say about X'.",
    "fetch_image": "Get a picture off the web so you can actually look at it. Needed before analyze_image on anything online.",
    "generate_image": "Make a new picture from a description — 'draw me a', 'generate an image of'. Never for plotting data.",
    "grab_frame": "Take a screenshot of a video at one moment — 'what's on screen at 4:20', 'grab that frame'.",
    "grokipedia": "Look a topic up in Grokipedia, xAI's encyclopedia — background on a subject, not current news.",
    "hurricane": "Check whether a hurricane or tropical storm is near a place — 'is anything coming', 'is there a storm'.",
    "news_feed": "Today's headlines from trusted feeds by topic — space, ai_tech, national, economy, world, local.",
    "news_search": "Search the news — 'what happened with X', 'latest on Y'. Better than web_search for anything current.",
    "ocr": "Read the exact text off an attached picture or PDF — a screenshot of an error, a receipt, a scanned page.",
    "portal_search": "Search a state government website by name — 'is X registered in Florida', 'does X hold a Florida license'.",
    "public_records": "Look a person up in free national records — other names they have used, court cases, medical licences, federal actions.",
    "query_server_metrics": "How the server is doing — 'how's the box', 'is it running hot' — CPU, memory, disk, GPU, fans.",
    "read_artifact": "Re-read a page you already fetched this chat, or keep reading a long one from where you stopped.",
    "read_plan": "Read this conversation's plan and which step you are on. Check it before working a step.",
    "remove_external_video": "Ask the owner to confirm deleting one video from their library — 'delete that video'.",
    "remove_research_report": "Ask the owner to confirm deleting one saved report — 'delete that report'.",
    # Wording matters more than content here, and this line is the proof. Three variants were
    # probed n=5 on "I counted 12 launches by SpaceX, 4 by ULA and 2 by Rocket Lab — show me
    # that as a graph": a verb-led version ("Graph numbers you already have BY CATEGORY…") got
    # render_bars 2/5, a "Render a categorical breakdown…" version 1/5, and this one — which
    # puts the NOUN PHRASE "a bar graph" where the owner's own words are — 4/5. Match the
    # phrasing the request will arrive in, not the vocabulary the codebase uses.
    "render_bars": "Show numbers you already have as a bar graph — one bar per category. For 'show me that as a graph', 'how many per Y', 'compare X across Y' when the x-axis is categories, not dates.",
    "render_chart": "Show numbers you already have as a line chart over time — 'plot this trend', 'graph this over the months', when the x-axis is dates.",
    "research_report": "Look in the owner's saved research reports — 'what did that report say', or to build on an earlier one.",
    "science_search": "Search papers and studies — 'what does the research say', 'any studies on X'.",
    "show_canvas": "Show the owner the figure you drew. Until you call this, they have seen nothing.",
    "show_external_video": "Play one library video for the owner with its timeline — 'show me that video'.",
    "show_research_report": "Show the owner a saved report as a card — 'show me that report'.",
    "spawn_subagent": "Send several sandboxed helpers to research parts of a question at once, then write the answer from what they find.",
    "transcribe": "Get the words out of an attached voice memo or video — 'what does this say', 'transcribe this'.",
    "weather": "The weather for a place — 'what's it like out', 'will it rain', or the week ahead.",
    "weather_history": "What the weather was on a past date or range — 'how hot was it here last July'.",
    "write_plan": "Write or update this conversation's plan — only when the owner asks for one. You can never approve it yourself.",
    "write_plan_result": "Record what a plan step found; this also ticks the step off. Call it after every step.",
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
    # --- widened 2026-08-17. A seven-case fixture flatters whatever it was built alongside;
    # these cover tools the first pass never exercised, and add the case class it was missing
    # entirely: requests where the RIGHT answer is no tool at all. A surface that over-calls is
    # as broken as one that mis-routes, and only these cases can catch it.
    {
        "probe": "is it going to rain in Titusville tomorrow?",
        "expect": ("weather",),
        "tests": "forecast picks weather, not a web search",
    },
    {
        # "here" makes a current_location read a legitimate FIRST step, exactly as in the storm
        # case below — the first version of this expectation omitted it and scored a reasonable
        # plan as a miss. Third fixture bug of this kind: when a probe contains a deictic
        # ("here", "my", "that"), the resolving tool is a valid answer.
        "probe": "how hot did it get here last July?",
        "expect": ("weather_history", "current_location"),
        "tests": "PAST weather is a different tool from the forecast",
    },
    {
        "probe": "is there a storm headed our way?",
        "expect": ("hurricane", "weather", "current_location"),
        "tests": "tropical-cyclone lookup, or a location read first",
    },
    {
        "probe": "I attached a screenshot of an error — what exactly does it say?",
        "expect": ("ocr",),
        "tests": "LITERAL text picks the deterministic OCR, not the vision model",
    },
    {
        "probe": "how's the box doing? is it running hot?",
        "expect": ("query_server_metrics",),
        "tests": "host telemetry, not a web search about servers",
    },
    {
        "probe": "are there any studies on creatine and cognition?",
        "expect": ("science_search",),
        "tests": "scholarly literature picks science_search over web_search",
    },
    {
        "probe": "has anyone ever sued Acme Holdings LLC?",
        "expect": ("public_records", "portal_search"),
        "tests": "court records are the NATIONAL registry, the twin of the state-portal case",
    },
    {
        "probe": "thanks, that's exactly what I needed",
        "expect": ("(none)",),
        "tests": "NO-TOOL control — a surface that over-calls is as broken as one that mis-routes",
    },
    {
        "probe": "what's the capital of France?",
        "expect": ("(none)",),
        "tests": "NO-TOOL control — general knowledge needs no search",
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
