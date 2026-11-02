"""What the Postgres schema guarantees by itself, whoever connects."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from consent_ledger.ledger import ConsentLedger
from consent_ledger.postgres import SCHEMA_SQL, PostgresConsentStore

from .conftest import postgres_schema

pytestmark = pytest.mark.postgres

TABLES = ("sms_consent", "sms_opt_outs", "sms_consent_events", "sms_inbound")
NOON_SF = datetime(2026, 7, 15, 19, 0, tzinfo=UTC)
LANGUAGE = "Yes, text me. Reply STOP to opt out."


async def like_supabase(admin: Any, schema: str) -> None:
    """Supabase's API roles, and its default grant of every new table to them.
    Roles are cluster-wide: created only if missing, never dropped."""
    await admin.execute(
        """
        do $$
        begin
          if not exists (select 1 from pg_roles where rolname = 'anon') then
            create role anon nologin noinherit;
          end if;
          if not exists (select 1 from pg_roles where rolname = 'authenticated') then
            create role authenticated nologin noinherit;
          end if;
        end
        $$;
        """
    )
    await admin.execute(
        f"alter default privileges in schema {schema} grant all on tables to anon, authenticated"
    )


async def test_every_table_has_rls_and_the_api_roles_hold_nothing() -> None:
    # Finding 9: the tables were created without row-level security, so on a
    # default Supabase project the public key could lift an opt-out.
    async with postgres_schema(like_supabase) as (pool, schema), pool.acquire() as conn:
        rls = await conn.fetch(
            """
                select c.relname, c.relrowsecurity from pg_class c
                 where c.relnamespace = $1::regnamespace and c.relkind = 'r'
                """,
            schema,
        )
        assert {row["relname"]: row["relrowsecurity"] for row in rls} == dict.fromkeys(TABLES, True)
        for role in ("anon", "authenticated"):
            for table in TABLES:
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    held = await conn.fetchval(
                        "select has_table_privilege($1, $2, $3)",
                        role,
                        f"{schema}.{table}",
                        privilege,
                    )
                    assert held is False, (role, table, privilege)


@pytest.mark.parametrize(
    "statement",
    [
        "update sms_consent_events set source = 'rewritten'",
        "delete from sms_consent_events",
        "truncate sms_consent_events",
        "update sms_inbound set received_by = 'someone else'",
        "delete from sms_inbound",
        "truncate sms_inbound",
    ],
)
async def test_the_logs_are_append_only(statement: str) -> None:
    import asyncpg

    async with postgres_schema() as (pool, _schema):
        ledger = ConsentLedger(PostgresConsentStore(pool), clock=lambda: NOON_SF)
        await ledger.grant("acme", "+14155550134", source="web_form", language=LANGUAGE)
        await ledger.handle_inbound(
            from_number="+14155550134", body="STOP", received_by="acme", message_sid="SM1"
        )
        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.RaiseError, match="append-only"):
                await conn.execute(statement)
        assert len(await ledger.history("+14155550134")) == 3


async def test_the_schema_can_be_applied_again() -> None:
    async with postgres_schema(like_supabase) as (pool, schema), pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
        triggers = await conn.fetch(
            """
                select c.relname, t.tgname from pg_trigger t
                  join pg_class c on c.oid = t.tgrelid
                 where c.relnamespace = $1::regnamespace and not t.tgisinternal
                 order by 1, 2
                """,
            schema,
        )
        assert [(row["relname"], row["tgname"]) for row in triggers] == [
            ("sms_consent_events", "sms_consent_events_append_only"),
            ("sms_consent_events", "sms_consent_events_no_truncate"),
            ("sms_inbound", "sms_inbound_append_only"),
            ("sms_inbound", "sms_inbound_no_truncate"),
        ]
