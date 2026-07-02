"""CLI for the real-Grok eval: `uv run python -m tests.eval.run [--db] [--canon] [id-filter]`.

The opt-in quality gate to run BEFORE shipping a prompt change. Needs
JBRAIN_XAI_API_KEY. Prints per-case PASS / FAIL / ADVISORY, the failures, and the
token cost. Exit code is non-zero if any non-advisory case fails — so it can gate
a release. Advisory cases (genuinely debatable "correct" answer) report but never
fail the run.

`--db` runs the full chain through apply_intent against a throwaway Postgres
testcontainer and asserts on the COMMITTED graph (dispositions, supersession
closure, resolve-to-existing, domain floors) — same two Grok calls, more gate.
Needs Docker. The graph is reset between cases so they don't contaminate.

`--canon` (implies --db) additionally seeds + embeds the canonical_predicates
index (the real bootstrap job) and runs the durable predicate-alias collapse
before the arbiter, plus any requires_canon cases (alias-collapse/drift
coverage — cards never file; long-tail commits raw either way). Needs the TEI
embed container up as well as Docker + Grok.
"""

from __future__ import annotations

import asyncio
import os
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


async def _evaluate(
    cases: list[Case], run_one: Callable[[Case], Awaitable[list[str]]], *, db: bool = False
) -> int:
    failed: list[str] = []
    advisory_failed: list[str] = []
    for case in cases:
        try:
            fails = await run_one(case)
        except Exception as exc:  # noqa: BLE001 - the eval surfaces whatever happens
            fails = [f"RAISED {type(exc).__name__}: {exc}"]
        advisory = case.advisory_for(db=db)
        tag = "ADVISORY" if advisory else ("FAIL" if fails else "PASS")
        if fails and advisory:
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


def _selected(args: list[str]) -> list[Case]:
    flt = next((a for a in args if not a.startswith("--")), "")
    return [c for c in load_corpus() if not flt or flt in c.id]


def _print_cost(tally: _Tally, *, db: bool) -> None:
    cost = tally.inp / 1e6 * _PRICE_IN + tally.out / 1e6 * _PRICE_OUT
    print(f"{tally.calls} calls ~${cost:.3f}{' · DB-mode' if db else ''}")


async def _db_loop(cases: list[Case], app_url: str, tmp: str, reset, *, canon: bool = False) -> int:
    # The async engine binds to this loop, so it is created here (not in the sync
    # bootstrap). build_router's recorder tallies the same two Grok calls per case.
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from tests.eval.runner import run_case_db

    engine = create_async_engine(app_url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    tally = _Tally()
    settings = Settings()
    router = build_router(settings, recorder=tally)

    embedder = None
    embed_model = ""
    if canon:
        # Real embeddings + the canonical index seeded once via the real bootstrap
        # job (needs the TEI container). The setting now gates only the held-fact
        # suggestion picker; --canon exercises the alias collapse + that picker.
        from jbrain.embed import PredicateEmbedder, TeiEmbedClient
        from jbrain.queue import SYSTEM_CTX
        from jbrain.settings_store import PREDICATE_CANON_KEY, SqlSettingsStore

        embedder = TeiEmbedClient(settings.embed_url)
        embed_model = settings.embed_model
        await SqlSettingsStore(maker).upsert(SYSTEM_CTX, PREDICATE_CANON_KEY, True)
        await PredicateEmbedder(maker, embedder, embed_model).sync_predicates({})

    debug = bool(os.environ.get("JBRAIN_EVAL_DEBUG"))

    async def run_one(case: Case) -> list[str]:
        reset()
        commit = await run_case_db(
            router,
            case,
            maker=maker,
            tmp_path=tmp,
            embedder=embedder,
            embed_model=embed_model,
            canonicalize=canon,
        )
        if debug:
            for f in commit.facts:
                obj = f" -> {f.object_name}" if f.object_name else ""
                print(
                    f"      · {f.entity_name}.{f.predicate}{obj} = {f.value_json}"
                    f" [{f.kind}/{f.assertion}/{f.status}/{f.domain_code}]"
                )
        return check_case_db(case, commit)

    try:
        code = await _evaluate(cases, run_one, db=True)
        _print_cost(tally, db=True)
        return code
    finally:
        await engine.dispose()


def run_db_mode(args: list[str]) -> int:
    """Synchronous orchestrator for --db: the testcontainer + Alembic bootstrap
    must run OUTSIDE the event loop (Alembic's env.py drives asyncio.run itself),
    then the async per-case loop runs under one asyncio.run."""
    import argparse
    import tempfile

    import sqlalchemy
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    from tests.conftest import docker_available, pgvector_container

    if not Settings().xai_api_key:
        print("JBRAIN_XAI_API_KEY not set — the real-Grok eval is opt-in.")
        return 2
    if not docker_available():
        print("--db needs a Docker daemon for the Postgres testcontainer.")
        return 2

    canon = "--canon" in args
    cases = _selected(args)
    if not canon:
        # requires_canon cases assert alias-collapse behavior that only runs
        # with the collapse enabled, so skip them otherwise.
        cases = [c for c in cases if not c.requires_canon]
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

        def reset() -> None:
            # Wipe the per-case graph but keep reference/migration rows: domains
            # holds the seeded firewall codes a note's domain FKs to; truncating
            # it would make every health/finance/location note fail the FK. In
            # --canon mode the canonical_predicates index is seeded once up front,
            # so it is preserved too (empty and harmless to exempt otherwise).
            with admin.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT schemaname, tablename FROM pg_tables WHERE schemaname"
                        " NOT IN ('pg_catalog', 'information_schema')"
                        " AND tablename NOT IN"
                        " ('alembic_version', 'domains', 'canonical_predicates')"
                    )
                ).all()
                if rows:
                    targets = ", ".join(f'"{s}"."{t}"' for s, t in rows)
                    conn.execute(text(f"TRUNCATE {targets} CASCADE"))

        try:
            return asyncio.run(_db_loop(cases, app_url, tmp, reset, canon=canon))
        finally:
            admin.dispose()


async def main() -> int:
    settings = Settings()
    if not settings.xai_api_key:
        print("JBRAIN_XAI_API_KEY not set — the real-Grok eval is opt-in.")
        return 2
    # Canonicalization is a DB-mode behavior; intent-mode check_case can't assert
    # a requires_canon case, so skip them rather than burn Grok calls for nothing.
    cases = [c for c in _selected(sys.argv[1:]) if not c.requires_canon]
    tally = _Tally()
    router = build_router(settings, recorder=tally)

    async def run_one_intent(case: Case) -> list[str]:
        intent, plan = await run_case(router, case)
        return check_case(case, intent, plan)

    code = await _evaluate(cases, run_one_intent)
    _print_cost(tally, db=False)
    return code


if __name__ == "__main__":
    # --canon implies DB-mode (it asserts the committed graph).
    if "--db" in sys.argv[1:] or "--canon" in sys.argv[1:]:
        raise SystemExit(run_db_mode(sys.argv[1:]))
    raise SystemExit(asyncio.run(main()))
