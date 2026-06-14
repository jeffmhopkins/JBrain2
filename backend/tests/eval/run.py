"""CLI for the real-Grok eval: `uv run python -m tests.eval.run [--db] [id-filter]`.

The opt-in quality gate to run BEFORE shipping a prompt change. Needs
JBRAIN_XAI_API_KEY. Prints per-case PASS / FAIL / ADVISORY, the failures, and the
token cost. Exit code is non-zero if any non-advisory case fails — so it can gate
a release. Advisory cases (genuinely debatable "correct" answer) report but never
fail the run.

`--db` runs the full chain through apply_intent against a throwaway Postgres
testcontainer and asserts on the COMMITTED graph (dispositions, supersession
closure, resolve-to-existing, domain floors) — same two Grok calls, more gate.
Needs Docker. The graph is reset between cases so they don't contaminate.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable

from jbrain.config import Settings
from jbrain.llm import build_router
from jbrain.llm.types import LlmUsage
from tests.eval.assertions import check_case, check_case_db
from tests.eval.cases import Case, load_corpus
from tests.eval.runner import run_case

_PRICE_IN, _PRICE_OUT = 1.25, 2.50  # $/M tokens, grok-4.3


class _Tally:
    def __init__(self) -> None:
        self.inp = self.out = self.calls = 0

    async def record(self, *, task: str, provider: str, model: str, usage: LlmUsage) -> None:
        self.inp += usage.input_tokens
        self.out += usage.output_tokens
        self.calls += 1


async def _evaluate(cases: list[Case], run_one: Callable[[Case], Awaitable[list[str]]]) -> int:
    failed: list[str] = []
    advisory_failed: list[str] = []
    for case in cases:
        try:
            fails = await run_one(case)
        except Exception as exc:  # noqa: BLE001 - the eval surfaces whatever happens
            fails = [f"RAISED {type(exc).__name__}: {exc}"]
        tag = "ADVISORY" if case.advisory else ("FAIL" if fails else "PASS")
        if fails and case.advisory:
            advisory_failed.append(case.id)
        elif fails:
            failed.append(case.id)
        marker = "ok " if not fails else "XX "
        print(f"{marker}[{tag:8}] {case.id} ({case.category})")
        for f in fails:
            print(f"      - {f}")
    print(f"\n{len(cases)} cases · {len(failed)} hard-fail · {len(advisory_failed)} advisory-miss")
    if failed:
        print("HARD FAILURES:", ", ".join(failed))
    return 1 if failed else 0


async def _run_db_mode(cases: list[Case], router: object) -> int:
    """Each case through apply_intent against a throwaway Postgres, asserting the
    committed graph. The graph is truncated between cases so a minted/seeded
    entity from one case can't resolve a later case's mention."""
    import argparse
    import tempfile

    import sqlalchemy
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from tests.conftest import docker_available, pgvector_container
    from tests.eval.runner import run_case_db

    if not docker_available():
        print("--db needs a Docker daemon for the Postgres testcontainer.")
        return 2

    with pgvector_container() as pg, tempfile.TemporaryDirectory() as tmp:
        admin = sqlalchemy.create_engine(
            pg.get_connection_url(driver="psycopg"), isolation_level="AUTOCOMMIT"
        )
        with admin.connect() as conn:
            conn.execute(text("CREATE ROLE jbrain_app LOGIN PASSWORD 'app_test_pw'"))
        async_url = pg.get_connection_url(driver="asyncpg")
        cfg = Config("alembic.ini")
        cfg.cmd_opts = argparse.Namespace(x=[f"database_url={async_url}"])
        command.upgrade(cfg, "head")
        host, port = pg.get_container_host_ip(), pg.get_exposed_port(5432)
        app_url = f"postgresql+asyncpg://jbrain_app:app_test_pw@{host}:{port}/{pg.dbname}"
        engine = create_async_engine(app_url, poolclass=NullPool)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        def _reset() -> None:
            with admin.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT schemaname, tablename FROM pg_tables WHERE schemaname"
                        " NOT IN ('pg_catalog', 'information_schema')"
                        " AND tablename != 'alembic_version'"
                    )
                ).all()
                if rows:
                    targets = ", ".join(f'"{s}"."{t}"' for s, t in rows)
                    conn.execute(text(f"TRUNCATE {targets} CASCADE"))

        async def run_one(case: Case) -> list[str]:
            _reset()
            commit = await run_case_db(router, case, maker=maker, tmp_path=tmp)  # type: ignore[arg-type]
            return check_case_db(case, commit)

        try:
            return await _evaluate(cases, run_one)
        finally:
            await engine.dispose()
            admin.dispose()


async def main() -> int:
    settings = Settings()
    if not settings.xai_api_key:
        print("JBRAIN_XAI_API_KEY not set — the real-Grok eval is opt-in.")
        return 2
    args = sys.argv[1:]
    db_mode = "--db" in args
    flt = next((a for a in args if not a.startswith("--")), "")
    cases = [c for c in load_corpus() if not flt or flt in c.id]
    tally = _Tally()
    router = build_router(settings, recorder=tally)

    async def run_one_intent(case: Case) -> list[str]:
        intent, plan = await run_case(router, case)
        return check_case(case, intent, plan)

    code = await _run_db_mode(cases, router) if db_mode else await _evaluate(cases, run_one_intent)
    cost = tally.inp / 1e6 * _PRICE_IN + tally.out / 1e6 * _PRICE_OUT
    print(f"{tally.calls} calls ~${cost:.3f}{' · DB-mode' if db_mode else ''}")
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
