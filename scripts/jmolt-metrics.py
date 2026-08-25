#!/usr/bin/env python3
"""jmolt's weekly observability report (docs/plans/JMOLT_PLAN.md, §5 W4).

Prints the week's rubric from jmolt's ledger, scratchpad, nights, and outbox — nights
run, actions by type, distinct agents engaged, scratchpad activity, outbox status, and
the account/integrity state. Read-only. Run it from the backend venv, e.g. weekly:

    python scripts/jmolt-metrics.py            # last 7 days, human report
    python scripts/jmolt-metrics.py --days 30 --json

Uses the same JBRAIN_DATABASE_URL the app reads (via jbrain.config.Settings), so it hits
the live box's Postgres with no extra config.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jbrain.agent.jmolt_metrics import JmoltMetrics, format_report
from jbrain.config import Settings
from jbrain.settings_store import SqlSettingsStore


async def _run(days: int, as_json: bool) -> None:
    settings = Settings()
    engine = create_async_engine(settings.database_url)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        metrics = await JmoltMetrics(maker=maker, settings_store=SqlSettingsStore(maker)).compute(
            days=days
        )
    finally:
        await engine.dispose()
    if as_json:
        print(json.dumps(dataclasses.asdict(metrics), indent=2, default=str))
    else:
        print(format_report(metrics))


def main() -> None:
    parser = argparse.ArgumentParser(description="jmolt weekly observability report")
    parser.add_argument("--days", type=int, default=7, help="window in days (default 7)")
    parser.add_argument("--json", action="store_true", help="emit raw JSON instead of a report")
    args = parser.parse_args()
    asyncio.run(_run(args.days, args.json))


if __name__ == "__main__":
    main()
