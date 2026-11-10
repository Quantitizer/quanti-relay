import os

import pytest

from relay.db import Database
from relay.db.models import Base

# The suite always runs on SQLite. Set RELAY_TEST_DATABASE_URL (postgresql+asyncpg://...) to run
# every database test against Postgres as well. That database is wiped on every test.
_BACKENDS = ["sqlite"] + (["postgres"] if os.environ.get("RELAY_TEST_DATABASE_URL") else [])


@pytest.fixture(params=_BACKENDS)
async def db(request, tmp_path):
    if request.param == "sqlite":
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'relay.db'}")
    else:
        database = Database(os.environ["RELAY_TEST_DATABASE_URL"])
        async with database.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    await database.create_all()
    yield database
    await database.close()
