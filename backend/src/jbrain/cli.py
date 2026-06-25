"""Operator CLI, run inside the api container (`python -m jbrain.cli <cmd>`).

The owner key is printed exactly once and stored only as a hash; there is no
way to display it again — `reset-owner-key` is the recovery path.
"""

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import get_settings
from jbrain.queue import SYSTEM_CTX
from jbrain.settings_store import SqlSettingsStore


def _print_key_block(key: str) -> None:
    print()
    print("=" * 64)
    print("  YOUR OWNER KEY — copy it to paper now; it cannot be shown again")
    print()
    print(f"    {key}")
    print()
    print("  Lost keys are reset with: jbrain reset-owner-key")
    print("=" * 64)
    print()


async def _rotate() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        repo = SqlAuthRepo(async_sessionmaker(engine, expire_on_commit=False))
        key = await service.rotate_owner_key(repo)
        _print_key_block(key)
    finally:
        await engine.dispose()


async def _print_provision_ids() -> None:
    """Print the install queue (one catalog id per line) for the update one-shot.
    Owner-scoped (settings RLS is is_owner()); empty output is the normal 'nothing
    queued' case, so the caller treats a clean exit with no lines as no-op."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        store = SqlSettingsStore(async_sessionmaker(engine, expire_on_commit=False))
        for model_id in await store.llm_local_provision_requested(SYSTEM_CTX):
            print(model_id)
    finally:
        await engine.dispose()


async def _clear_provision_ids() -> None:
    """Empty the install queue — called by the update one-shot after a successful
    provision so a completed install stops re-appearing as queued."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        store = SqlSettingsStore(async_sessionmaker(engine, expire_on_commit=False))
        await store.set_llm_local_provision_requested(SYSTEM_CTX, [])
    finally:
        await engine.dispose()


async def _print_remove_ids() -> None:
    """Print the uninstall queue (one catalog id per line) for the update one-shot.
    Owner-scoped (settings RLS is is_owner()); empty output is the normal 'nothing
    queued' case, so the caller treats a clean exit with no lines as no-op."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        store = SqlSettingsStore(async_sessionmaker(engine, expire_on_commit=False))
        for model_id in await store.llm_local_remove_requested(SYSTEM_CTX):
            print(model_id)
    finally:
        await engine.dispose()


async def _clear_remove_ids() -> None:
    """Empty the uninstall queue — called by the update one-shot after a successful
    pass so a completed uninstall stops re-appearing as queued."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        store = SqlSettingsStore(async_sessionmaker(engine, expire_on_commit=False))
        await store.set_llm_local_remove_requested(SYSTEM_CTX, [])
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jbrain-cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the owner principal and print the owner key")
    sub.add_parser("reset-owner-key", help="revoke the owner key and print a new one")
    sub.add_parser("local-provision-ids", help="print the local-model install queue")
    sub.add_parser("local-provision-clear", help="empty the local-model install queue")
    sub.add_parser("local-remove-ids", help="print the local-model uninstall queue")
    sub.add_parser("local-remove-clear", help="empty the local-model uninstall queue")
    args = parser.parse_args(argv)

    if args.command in ("init", "reset-owner-key"):
        asyncio.run(_rotate())
        return 0
    if args.command == "local-provision-ids":
        asyncio.run(_print_provision_ids())
        return 0
    if args.command == "local-provision-clear":
        asyncio.run(_clear_provision_ids())
        return 0
    if args.command == "local-remove-ids":
        asyncio.run(_print_remove_ids())
        return 0
    if args.command == "local-remove-clear":
        asyncio.run(_clear_remove_ids())
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
