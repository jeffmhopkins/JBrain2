import asyncio
import os

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from jbrain.config import get_settings
from jbrain.models import Base

target_metadata = Base.metadata


def _database_url() -> str:
    # Precedence: -x database_url=... (tests) > the migrate service's
    # superuser URL > app settings. Migrations need DDL rights the RLS-bound
    # app role deliberately lacks.
    from_x = context.get_x_argument(as_dictionary=True).get("database_url")
    return from_x or os.environ.get("JBRAIN_MIGRATION_DATABASE_URL") or get_settings().database_url


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
        await connection.commit()
    await engine.dispose()


def run_migrations_offline() -> None:
    context.configure(url=_database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
