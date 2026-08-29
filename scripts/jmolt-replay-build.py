#!/usr/bin/env python3
"""Build a /api/debug/replay body from a REAL jmolt sitting.

The point of replaying from the record rather than from invented stubs: a sitting's tool
results are what the night actually observed, so an unmodified replay should reproduce
what the night actually did. That is the validation step. A counterfactual then differs
from a real, reproduced sitting by exactly one edit, and `matched_recorded` says where the
model stopped following the night it had.

    # 1. dump the sitting (read-only SQL, via the debug console)
    scripts/debug-connect.sh sql "$(python3 scripts/jmolt-replay-build.py --sql \
        --session <uuid> --sitting 7)" > /tmp/sitting.json

    # 2. turn it into a replay body, optionally editing the prologue
    python3 scripts/jmolt-replay-build.py --from-dump /tmp/sitting.json \
        --system-file prompt.txt --out /tmp/replay.json \
        --drop "The posts themselves"          # a counterfactual: remove one block

    # 3. run it
    scripts/debug-connect.sh replay --body-file /tmp/replay.json

`--drop` removes a block from the prologue by its leading text; `--replace OLD=NEW` swaps
one string. Both operate on the REAL prologue, so a condition is one documented edit away
from ground truth rather than a fresh piece of writing.
"""

from __future__ import annotations

import argparse
import json
import sys

# Tools jmolt actually holds at night. Kept explicit rather than read from the registry so
# a replay pins the tool set it was run with.
JMOLT_TOOLS = [
    "moltbook",
    "current_time",
    "time_left",
    "scratch_list",
    "scratch_read",
    "scratch_write",
    "scratch_manage",
    "journal",
    "moltbook_post",
    "moltbook_comment",
    "moltbook_vote",
    "moltbook_social",
    "moltbook_profile_update",
]


def dump_sql(session_id: str, sitting: int) -> str:
    """SELECT one sitting's prologue and its observed tool calls + results.

    Turns persist at sitting END and a sitting writes one user row and one assistant row,
    so ordering by created_at and taking the Nth pair is the sitting. `tools` carries the
    calls with their `summary` — the result text the model actually saw."""
    return (
        "select t.content::text as prologue, coalesce(a.tools::text,'[]') as tools from"
        " (select content, created_at, row_number() over (order by created_at) as n"
        f"  from app.agent_turns where session_id='{session_id}' and role='user') t"
        " join (select tools, created_at, row_number() over (order by created_at) as n"
        f"  from app.agent_turns where session_id='{session_id}' and role='assistant') a"
        f" on a.n = t.n where t.n = {sitting}"
    )


def build(dump: dict, system: str, drops: list[str], replaces: list[str]) -> dict:
    rows = dump.get("rows") or []
    if not rows:
        sys.exit("no rows in dump — check the session id and sitting number")
    prologue, tools_json = rows[0][0], rows[0][1]

    for lead in drops:
        idx = prologue.find(lead)
        if idx < 0:
            sys.exit(f"--drop text not found in the prologue: {lead!r}")
        # A block runs to the next blank line, which is how the prologue separates them.
        end = prologue.find("\n\n", idx)
        prologue = prologue[:idx] + (prologue[end + 2 :] if end > 0 else "")
    for pair in replaces:
        old, _, new = pair.partition("=")
        if old not in prologue:
            sys.exit(f"--replace source not found in the prologue: {old!r}")
        prologue = prologue.replace(old, new)

    stubs = []
    for entry in json.loads(tools_json):
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        stubs.append(
            {
                "name": entry["name"],
                "result": str(entry.get("summary") or ""),
                "is_error": not entry.get("ok", True),
            }
        )
    return {
        "task": "agent.turn",
        "system": system,
        "user_text": prologue,
        "tools": JMOLT_TOOLS,
        "stubs": stubs,
        "max_steps": 12,
        "max_tokens": 2048,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sql", action="store_true", help="print the dump SQL and exit")
    ap.add_argument("--session")
    ap.add_argument("--sitting", type=int, default=1)
    ap.add_argument("--from-dump", help="JSON from the sql command")
    ap.add_argument("--system-file", help="file holding the system prompt body")
    ap.add_argument("--out")
    ap.add_argument("--drop", action="append", default=[], help="remove a prologue block")
    ap.add_argument("--replace", action="append", default=[], help="OLD=NEW in the prologue")
    args = ap.parse_args()

    if args.sql:
        if not args.session:
            sys.exit("--sql needs --session")
        print(dump_sql(args.session, args.sitting))
        return
    if not args.from_dump:
        sys.exit("need --from-dump (or --sql)")
    with open(args.from_dump, encoding="utf-8") as fh:
        dump = json.load(fh)
    system = ""
    if args.system_file:
        with open(args.system_file, encoding="utf-8") as fh:
            system = fh.read()
    body = build(dump, system, args.drop, args.replace)
    out = json.dumps(body, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out)
        print(f"wrote {args.out}: {len(body['stubs'])} stubs, {len(body['user_text'])} chars")
    else:
        print(out)


if __name__ == "__main__":
    main()
