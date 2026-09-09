"""Measure whether the live model fills a proposed tool schema well enough to ship it.

The deterministic harness scripts a perfect model, and `evals/run.py` scores a prompt's
OUTPUT. Neither can answer the question a new tool surface raises first: will the model
actually FILL this schema? `TOOL_SURFACE.md` had to design a whole flat-scalar fallback
around not knowing, for a shape nobody had tried on this box.

So: send a candidate schema to the real model through `/api/debug/tool-probe` (which never
runs a handler) and score the call it proposes. Well-formed means every item carried all
its required non-blank string fields, and — where a schema demands a quote — that the quote
is a verbatim substring of the note. Anything less is a shape that will silently drop facts.

    JBRAIN_DEBUG_TOKEN=... uv run python -m evals.shape_probe 20

Dev-only, like the rest of `backend/evals/`, and opt-in: it costs real inference on the
box's serial GPU, roughly 30 s per sample.

**It measures the FIRST call and nothing after it.** With a full tool set attached the
first call is whatever the persona reaches for first, so a write tool the model only gets
to on its second move is invisible here. `/api/debug/replay` takes the same inline schemas
and does run multi-turn; use that once the surface has stubs worth feeding back.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

TIMEOUT_S = 300

# One note, several unambiguous facts, a few entity kinds. Deliberately ordinary: this
# measures the SCHEMA, so anything the model could reasonably disagree about would show
# up as a shape failure and confound the reading.
NOTE = (
    "Coffee with Dana Whitfield at Ritual on Valencia this morning. She just moved to the "
    "Mission from Oakland and started at Everlane as a staff engineer in March. Her partner "
    "Theo is finishing a PhD in marine biology at Berkeley. She drives a green Subaru "
    "Outback and is allergic to shellfish."
)

SYSTEM = (
    "You are reading a note the owner just wrote, so that what it means ends up in their "
    "knowledge graph. Read it and record what it says using your tools. Clear facts you "
    "record without asking. Every fact must carry a verbatim quote from the note."
)


def _field(desc: str) -> dict[str, str]:
    return {"type": "string", "description": desc}


FACT_FIELDS = {
    "subject": _field("The entity the fact is about, as written in the note."),
    "predicate": _field("A short snake_case relation, e.g. lives_in, works_at."),
    "value": _field("The other side of the relation."),
    "quote": _field("The exact span of the note supporting this fact, copied verbatim."),
}

# Each arm is (tool schema, the key holding the items, the fields each item owes).
# `None` fields means the items are plain strings.
ARMS: dict[str, tuple[dict[str, Any], str | None, list[str] | None]] = {
    "batched_objects": (
        {
            "name": "assert_fact",
            "description": "Record facts from the note. Batch up to 8 per call.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "facts": {
                        "type": "array",
                        "maxItems": 8,
                        "description": "The facts to record.",
                        "items": {
                            "type": "object",
                            "properties": FACT_FIELDS,
                            "required": list(FACT_FIELDS),
                        },
                    }
                },
                "required": ["facts"],
            },
        },
        "facts",
        list(FACT_FIELDS),
    ),
    "flat_scalar": (
        {
            "name": "assert_fact",
            "description": "Record ONE fact from the note. Call it once per fact.",
            "input_schema": {
                "type": "object",
                "properties": dict(FACT_FIELDS),
                "required": list(FACT_FIELDS),
            },
        },
        None,
        list(FACT_FIELDS),
    ),
    "resolve_objects": (
        {
            "name": "resolve_entity",
            "description": "Resolve the people, places and things the note names. Batch up to 12.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "entities": {
                        "type": "array",
                        "maxItems": 12,
                        "description": "The surfaces to resolve.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "surface": _field("The name exactly as the note writes it."),
                                "kind": _field("person, place, organization, thing or event."),
                            },
                            "required": ["surface", "kind"],
                        },
                    }
                },
                "required": ["entities"],
            },
        },
        "entities",
        ["surface", "kind"],
    ),
    # The control that separates "arrays are hard" from "arrays OF OBJECTS are hard".
    "resolve_strings": (
        {
            "name": "resolve_entity",
            "description": "Resolve the people, places and things the note names. Batch up to 12.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "surfaces": {
                        "type": "array",
                        "maxItems": 12,
                        "description": "The names, exactly as the note writes them.",
                        "items": {"type": "string"},
                    }
                },
                "required": ["surfaces"],
            },
        },
        "surfaces",
        None,
    ),
}


def _credentials() -> tuple[str, str]:
    """Same resolution `scripts/debug-connect.sh` uses: env first, then the gitignored
    file at the repo root. The payload is base64 JSON carrying the box url and the key."""
    payload = os.environ.get("JBRAIN_DEBUG_TOKEN", "").strip()
    if not payload:
        path = Path(__file__).resolve().parents[2] / ".jbrain-debug-token"
        if path.is_file():
            payload = path.read_text().strip()
    if not payload:
        sys.exit("no token: set JBRAIN_DEBUG_TOKEN or write ./.jbrain-debug-token")
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(raw)
        return str(claims["u"]).rstrip("/"), str(claims["k"])
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
        sys.exit(f"token is not a debug payload ({{u, k}} base64 JSON): {exc}")


def _probe(url: str, key: str, tool: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(
        {
            "user_text": NOTE,
            "system": SYSTEM,
            "task": "agent.turn",
            "raw_tools": [tool],
            "max_tokens": 2048,
        }
    )
    out = subprocess.run(
        [
            "curl",
            "-sS",
            "-m",
            str(TIMEOUT_S),
            "-X",
            "POST",
            "-H",
            f"Authorization: Bearer {key}",
            "-H",
            "Content-Type: application/json",
            "-d",
            body,
            f"{url}/api/debug/tool-probe",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return {"_transport": out.stderr.strip()[:200]}
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"_transport": out.stdout[:200]}


def _score(
    res: dict[str, Any], items_key: str | None, required: list[str] | None
) -> tuple[str, int, str]:
    if "_transport" in res:
        return "transport_error", 0, res["_transport"]
    if res.get("error"):
        return "llm_error", 0, str(res["error"])[:160]
    calls = res.get("tool_calls") or []
    if not calls:
        return "no_call", 0, str(res.get("text") or "")[:120]

    items: list[Any] = []
    for call in calls:
        args = call.get("arguments")
        if not isinstance(args, dict):
            return "malformed", 0, f"arguments not an object: {type(args).__name__}"
        if items_key is None:
            items.append(args)
            continue
        batch = args.get(items_key)
        if not isinstance(batch, list):
            return "malformed", 0, f"{items_key} not a list: {json.dumps(args)[:100]}"
        items.extend(batch)

    if not items:
        return "empty", 0, ""
    for item in items:
        if required is None:
            if not isinstance(item, str) or not item.strip():
                return "malformed", len(items), f"non-string item: {item!r}"
            continue
        if not isinstance(item, dict):
            return "malformed", len(items), f"item not an object: {item!r}"
        missing = [k for k in required if not isinstance(item.get(k), str) or not item[k].strip()]
        if missing:
            return "malformed", len(items), f"missing/blank {missing} in {json.dumps(item)[:120]}"
        # The quote is the whole span-attestation contract: a paraphrase cannot be located
        # in a chunk, so a plausible-looking non-verbatim quote is a silent fact loss.
        if "quote" in required and item["quote"] not in NOTE:
            return "bad_quote", len(items), f"not verbatim: {item['quote'][:80]!r}"
    return "ok", len(items), ""


def main() -> None:
    samples = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    url, key = _credentials()
    for arm, (tool, items_key, required) in ARMS.items():
        tally: Counter[str] = Counter()
        yields: list[int] = []
        failures: list[str] = []
        for i in range(samples):
            verdict, count, detail = _score(_probe(url, key, tool), items_key, required)
            tally[verdict] += 1
            if verdict == "ok":
                yields.append(count)
            elif detail:
                failures.append(f"  [{i}] {verdict}: {detail}")
            print(f"{arm} {i + 1}/{samples}: {verdict} ({count})", flush=True)
        ok = tally["ok"]
        mean = sum(yields) / len(yields) if yields else 0.0
        print(
            f"\n=== {arm}: {ok}/{samples} well-formed ({100 * ok / samples:.0f}%),"
            f" mean {mean:.1f} items per turn"
        )
        for verdict, count in tally.most_common():
            if verdict != "ok":
                print(f"    {verdict}: {count}")
        for line in failures[:6]:
            print(line)
        print(flush=True)


if __name__ == "__main__":
    main()
