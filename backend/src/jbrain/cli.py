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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jbrain-cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the owner principal and print the owner key")
    sub.add_parser("reset-owner-key", help="revoke the owner key and print a new one")
    args = parser.parse_args(argv)

    if args.command in ("init", "reset-owner-key"):
        asyncio.run(_rotate())
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
