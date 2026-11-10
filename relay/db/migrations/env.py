"""Alembic environment: async engine, URL from relay settings, metadata from relay.db.models."""
import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from relay.config import settings
from relay.db.models import Base

target_metadata = Base.metadata


def _url() -> str:
    return context.config.attributes.get("url") or settings.database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:
    # render_as_batch: SQLite can't ALTER most things in place; batch mode copies the table.
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
