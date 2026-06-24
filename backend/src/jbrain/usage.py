"""LLM token accounting: the SQL usage recorder and the Ops usage card data.

Costs are estimated at query time from the config price table
(docs/ANALYSIS.md "Cost estimates"): a price-table update re-prices history,
and models missing from the table contribute tokens but never a guessed
dollar figure.
"""

import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import TracebackType
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm.types import LlmUsage

# llm_usage is owner-only telemetry; the recorder is the owner's own
# machinery regardless of which process (api or worker) hosts it.
USAGE_CTX = SessionContext(principal_id="llm-usage", principal_kind="owner")

DAYS_WINDOW = 30

# Per-unit-of-work token tally. The recorder is the SINGLE chokepoint every LLM
# call passes through, so a caller (the worker, around one job) opens a scope and
# the recorder tallies that scope's tokens here. A ContextVar is task-local and
# propagates through `await`, so a job's nested calls land in its own counter even
# if another job runs concurrently. None = no scope active (the tally is skipped).
_token_tally: ContextVar[list[int] | None] = ContextVar("llm_token_tally", default=None)


class TokenScope:
    """Scope the running LLM-token tally to a unit of work (one worker job). Every
    `SqlUsageRecorder.record` inside the scope adds to `total`; reads it after."""

    def __enter__(self) -> "TokenScope":
        self._acc: list[int] = [0]
        self._token = _token_tally.set(self._acc)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _token_tally.reset(self._token)

    @property
    def total(self) -> int:
        return self._acc[0]


class SqlUsageRecorder:
    """UsageRecorder backed by app.llm_usage; one row per adapter call."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def record(self, *, task: str, provider: str, model: str, usage: LlmUsage) -> None:
        # Tally into the active per-job TokenScope (if any) so the run-log can show
        # the tokens that job spent — the recorder is the one place every call meets.
        acc = _token_tally.get()
        if acc is not None:
            acc[0] += usage.input_tokens + usage.output_tokens
        async with scoped_session(self._maker, USAGE_CTX) as session:
            await session.execute(
                text(
                    "INSERT INTO app.llm_usage"
                    " (id, task, provider, model, input_tokens, output_tokens)"
                    " VALUES (:id, :task, :provider, :model, :inp, :out)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "task": task,
                    "provider": provider,
                    "model": model,
                    "inp": usage.input_tokens,
                    "out": usage.output_tokens,
                },
            )


@dataclass(frozen=True)
class UsageRow:
    """One (day, task, provider, model) aggregate from app.llm_usage."""

    day: date
    task: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int


def cost_usd(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    prices: dict[str, dict[str, float]],
) -> float | None:
    """Estimated dollars for one model's tokens; None when the model (or a
    rate) is missing from the table — never a guessed price."""
    entry = prices.get(f"{provider}:{model}")
    if entry is None or "input_per_m" not in entry or "output_per_m" not in entry:
        return None
    return input_tokens / 1e6 * entry["input_per_m"] + output_tokens / 1e6 * entry["output_per_m"]


def _bucket(rows: list[UsageRow], prices: dict[str, dict[str, float]]) -> dict[str, Any]:
    costs = [cost_usd(r.provider, r.model, r.input_tokens, r.output_tokens, prices) for r in rows]
    priced = [c for c in costs if c is not None]
    return {
        "input_tokens": sum(r.input_tokens for r in rows),
        "output_tokens": sum(r.output_tokens for r in rows),
        # Unpriceable models are omitted from the estimate (tokens still
        # counted above); null when nothing at all is priceable.
        "cost_usd": round(sum(priced), 6) if priced else None,
    }


def summarize_usage(
    rows: list[UsageRow], prices: dict[str, dict[str, float]], today: date
) -> dict[str, Any]:
    """The /api/ops/llm-usage payload, computed from pre-aggregated rows."""
    month_start = today.replace(day=1)
    window_start = today - timedelta(days=DAYS_WINDOW - 1)
    month_rows = [r for r in rows if r.day >= month_start]

    by_task: dict[str, list[UsageRow]] = {}
    for row in month_rows:
        by_task.setdefault(row.task, []).append(row)

    by_day: dict[date, list[UsageRow]] = {}
    for row in rows:
        if row.day >= window_start:
            by_day.setdefault(row.day, []).append(row)

    return {
        "today": _bucket([r for r in rows if r.day == today], prices),
        "month": _bucket(month_rows, prices),
        "by_task": [
            {"task": task, **_bucket(task_rows, prices)}
            for task, task_rows in sorted(
                by_task.items(),
                key=lambda kv: sum(r.input_tokens + r.output_tokens for r in kv[1]),
                reverse=True,
            )
        ],
        "days": [
            {"date": day.isoformat(), **_bucket(day_rows, prices)}
            for day, day_rows in sorted(by_day.items())
        ],
    }


async def usage_summary(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    prices: dict[str, dict[str, float]],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    today = today or datetime.now(UTC).date()
    # The month can start more than 30 days back (and vice versa on the 1st):
    # fetch the union window once and slice in Python.
    since = min(today.replace(day=1), today - timedelta(days=DAYS_WINDOW - 1))
    async with scoped_session(maker, ctx) as session:
        records = (
            await session.execute(
                text(
                    """
                    SELECT (created_at AT TIME ZONE 'UTC')::date AS day,
                           task, provider, model,
                           sum(input_tokens)::bigint AS input_tokens,
                           sum(output_tokens)::bigint AS output_tokens
                    FROM app.llm_usage
                    WHERE created_at >= :since
                    GROUP BY 1, 2, 3, 4
                    """
                ),
                {"since": datetime(since.year, since.month, since.day, tzinfo=UTC)},
            )
        ).all()
    rows = [
        UsageRow(
            day=r.day,
            task=r.task,
            provider=r.provider,
            model=r.model,
            input_tokens=int(r.input_tokens),
            output_tokens=int(r.output_tokens),
        )
        for r in records
    ]
    return summarize_usage(rows, prices, today)
