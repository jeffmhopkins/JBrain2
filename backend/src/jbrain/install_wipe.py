"""One-time install reset for the W3.3 cutover (`python -m jbrain.install_wipe`).

A deliberate, DESTRUCTIVE one-shot: drop and rebuild the schema, clear blob +
backup storage, and enable the v3 integrate pipeline for this install — so the
old single-shot graph and any stale blobs are gone and notes flow through the
new pipeline from a clean slate (the redesign always assumed a wipe).

Three guards make it impossible to misfire:
  1. `JBRAIN_WIPE_ON_FIRST_DEPLOY` must be set — off by default, so a normal
     deploy never wipes.
  2. A sentinel file on the persistent blob volume — written last, so the wipe
     runs EXACTLY once per install even if the flag stays set.
  3. It needs the superuser `JBRAIN_MIGRATION_DATABASE_URL` (the RLS-bound app
     role can't drop the schema) — the same privileged URL the migrate one-shot
     uses; absent it, the wipe refuses rather than half-runs.

Run it as a compose one-shot BEFORE bringing the stack up on a fresh install.
The app/worker never import this; it is a standalone entrypoint.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from jbrain.config import Settings, get_settings

log = structlog.get_logger()

SENTINEL_NAME = ".install_wiped"
MIGRATION_URL_ENV = "JBRAIN_MIGRATION_DATABASE_URL"


def should_wipe(*, enabled: bool, sentinel_exists: bool) -> bool:
    """Pure decision: wipe only when explicitly enabled AND not already done."""
    return enabled and not sentinel_exists


def _sentinel_path(settings: Settings) -> Path:
    return Path(settings.blob_dir) / SENTINEL_NAME


def clear_dir(path: str) -> None:
    """Empty a storage directory's CONTENTS, keeping the directory itself (it is
    a mounted volume). A missing directory is a no-op."""
    root = Path(path)
    if not root.exists():
        return
    for child in root.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


async def _drop_schema(migration_url: str) -> None:
    # The app schema holds every table (migration 0001); alembic_version sits in
    # public. Dropping both, then rebuilding, is a clean full reset that leaves
    # the jbrain_app role (created by the DB init, not migrations) untouched.
    engine = create_async_engine(migration_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS app CASCADE"))
            await conn.execute(text("DROP TABLE IF EXISTS public.alembic_version"))
    finally:
        await engine.dispose()


def _rebuild_schema() -> None:
    # Mirror the migrate one-shot: alembic reads JBRAIN_MIGRATION_DATABASE_URL
    # from the env (migrations/env.py), so no -x override is needed here.
    cfg = Config("alembic.ini")
    cfg.cmd_opts = argparse.Namespace(x=[])
    command.upgrade(cfg, "head")


def main() -> int:
    settings = get_settings()
    sentinel = _sentinel_path(settings)
    if not should_wipe(enabled=settings.wipe_on_first_deploy, sentinel_exists=sentinel.exists()):
        log.info(
            "install_wipe.skipped",
            enabled=settings.wipe_on_first_deploy,
            already_wiped=sentinel.exists(),
        )
        return 0

    migration_url = os.environ.get(MIGRATION_URL_ENV)
    if not migration_url:
        log.error("install_wipe.refused", reason=f"{MIGRATION_URL_ENV} not set")
        return 1

    log.warning(
        "install_wipe.starting",
        blob_dir=settings.blob_dir,
        backups_dir=settings.backups_dir,
    )
    asyncio.run(_drop_schema(migration_url))
    clear_dir(settings.blob_dir)
    clear_dir(settings.backups_dir)
    _rebuild_schema()

    # Sentinel LAST: a failure before here leaves the wipe un-marked, so a re-run
    # finishes the reset rather than skipping a half-done one.
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(datetime.now(UTC).isoformat())
    log.warning("install_wipe.done", sentinel=str(sentinel))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
