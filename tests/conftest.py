from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from consent_ledger.ledger import ConsentLedger
from consent_ledger.stores import ConsentStore, InMemoryConsentStore

DSN = os.environ.get("CONSENT_LEDGER_TEST_DSN", "")
REQUIRE_POSTGRES = os.environ.get("CONSENT_LEDGER_REQUIRE_POSTGRES") == "1"

#: A Wednesday, 18:00 UTC: 14:00 in New York, 11:00 in Los Angeles.
WEDNESDAY_18_UTC = datetime(2026, 7, 15, 18, 0, tzinfo=UTC)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if DSN:
        return
    if REQUIRE_POSTGRES:
        raise pytest.UsageError("CONSENT_LEDGER_REQUIRE_POSTGRES=1 but no CONSENT_LEDGER_TEST_DSN")
    skip = pytest.mark.skip(reason="set CONSENT_LEDGER_TEST_DSN to run the Postgres tests")
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip)


@contextlib.asynccontextmanager
async def postgres_schema(
    before_tables: Callable[[Any, str], Awaitable[None]] | None = None,
) -> AsyncIterator[tuple[Any, str]]:
    """A pool whose search_path is a schema of its own, with the tables made.
    ``before_tables(admin, schema)`` runs first, for a test that needs the
    database set up the way a host (Supabase) sets it up."""
    import asyncpg

    from consent_ledger.postgres import create_schema

    schema = f"t_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(DSN)
    await admin.execute(f"create schema {schema}")
    pool: Any = await asyncpg.create_pool(
        DSN, min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    try:
        if before_tables is not None:
            await before_tables(admin, schema)
        await create_schema(pool)
        yield pool, schema
    finally:
        await pool.close()
        await admin.execute(f"drop schema {schema} cascade")
        await admin.close()


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[ConsentStore]:
    """Each test gets an empty store; with a DSN, also a Postgres store in a
    schema of its own, so the two implementations cannot drift apart."""
    if request.param == "memory":
        yield InMemoryConsentStore()
        return
    from consent_ledger.postgres import PostgresConsentStore

    async with postgres_schema() as (pool, _schema):
        yield PostgresConsentStore(pool)


class Clock:
    def __init__(self, now: datetime = WEDNESDAY_18_UTC) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def ledger(store: ConsentStore, clock: Clock) -> ConsentLedger:
    return ConsentLedger(store, clock=clock)
