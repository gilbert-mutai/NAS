"""Fixtures for tests that need a real PostgreSQL database.

These are skipped unless ``NAS_TEST_DATABASE_URL`` is set, so the default test
run works on any machine and in CI without a database. To run them:

    docker compose up -d
    createdb -h localhost -p 5434 -U nas nas_test   # once
    export NAS_TEST_DATABASE_URL=postgresql+asyncpg://nas:nas@localhost:5434/nas_test
    pytest -m integration

The schema is created from Alembic migrations rather than ``metadata.create_all``,
so these tests also verify that the migrations actually produce the schema the
ORM expects.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession

from nas.db.session import Database
from tests.conftest import build_settings

TEST_DATABASE_URL_VAR = "NAS_TEST_DATABASE_URL"


def _require_database_url() -> str:
    url = os.environ.get(TEST_DATABASE_URL_VAR)
    if not url:
        pytest.skip(f"{TEST_DATABASE_URL_VAR} is not set")
    return url


@pytest.fixture(scope="session")
def integration_database_url() -> str:
    return _require_database_url()


@pytest.fixture(scope="session")
def migrated_database(integration_database_url: str) -> Iterator[str]:
    """Apply migrations to the test database once per session.

    Running the real migrations (rather than ``metadata.create_all``) means these
    tests also prove that ``downgrade`` works and that the migrations produce the
    schema the ORM expects.

    ``NAS_DATABASE_URL`` is restored afterwards. A session fixture that leaves the
    process environment mutated silently changes the behaviour of every later test
    — including one that asserts the variable is *absent*.
    """
    from nas.core.config import get_settings

    config = Config("alembic.ini")
    previous = os.environ.get("NAS_DATABASE_URL")
    # env.py reads the URL from settings, so point settings at the test database.
    os.environ["NAS_DATABASE_URL"] = integration_database_url
    get_settings.cache_clear()

    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        yield integration_database_url
        command.downgrade(config, "base")
    finally:
        if previous is None:
            os.environ.pop("NAS_DATABASE_URL", None)
        else:
            os.environ["NAS_DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest.fixture
async def db(migrated_database: str) -> AsyncIterator[Database]:
    database = Database(build_settings(database_url=migrated_database))
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def session(db: Database) -> AsyncIterator[AsyncSession]:
    """A session whose changes are always rolled back, keeping tests isolated."""
    async with db.session_factory() as session:
        yield session
        await session.rollback()
