"""A durable ``ConsentStore`` on Postgres (the ``postgres`` extra, asyncpg).

Create the tables once with :data:`SCHEMA_SQL` (or :func:`create_schema`);
running it again changes nothing. It makes four tables:

* ``sms_consent`` and ``sms_opt_outs``: the current state, which checks read;
* ``sms_consent_events``: every grant, revocation, restoration, opt-out and
  opt-in, appended in the same transaction as the change it records;
* ``sms_inbound``: inbound messages' metadata (never their text), which proves
  a conversation the person started and catches a replayed message id.

The two log tables are append-only: a trigger refuses any UPDATE, DELETE or
TRUNCATE on them. Every table has row-level security enabled with no
policies, and on a database with Supabase's ``anon`` and ``authenticated``
roles their grants are revoked, so the public API key can neither read a
phone number nor lift an opt-out. Your server connects as the table owner or
a role that bypasses RLS (Supabase's service role). CI runs it on Postgres 16.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from consent_ledger.stores import (
    ConsentEvent,
    ConsentRecord,
    InboundRecord,
    OptOutRecord,
    plain_json,
)

SCHEMA_SQL = """
create table if not exists sms_consent (
    sender            text        not null,
    phone             text        not null,  -- E.164
    source            text        not null,  -- how consent was collected
    consented_at      timestamptz not null,
    evidence          jsonb       not null default '{}'::jsonb,
    timezone          text,                  -- IANA zone, when known
    revoked_at        timestamptz,
    revocation_source text,
    revocation_kind   text check (revocation_kind in ('sender', 'opt_out')),
    primary key (sender, phone),
    check ((revoked_at is null) = (revocation_kind is null))
);
create index if not exists sms_consent_phone_idx on sms_consent (phone);

create table if not exists sms_opt_outs (
    phone         text        primary key,  -- E.164
    opted_out_at  timestamptz not null,
    received_by   text        not null,
    source        text        not null,
    evidence_sid  text,
    evidence_body text,
    resumed_at    timestamptz
);

create table if not exists sms_consent_events (
    id     bigint      generated always as identity primary key,
    at     timestamptz not null,
    kind   text        not null
           check (kind in ('granted', 'revoked', 'restored', 'opted_out', 'opted_in')),
    phone  text        not null,
    sender text,                             -- null: about the person, not one sender
    source text        not null,
    detail jsonb       not null default '{}'::jsonb
);
create index if not exists sms_consent_events_phone_idx on sms_consent_events (phone, at, id);

create table if not exists sms_inbound (
    id          bigint      generated always as identity primary key,
    at          timestamptz not null,
    phone       text        not null,
    received_by text        not null,
    message_sid text,
    keyword     text
);
create unique index if not exists sms_inbound_sid_idx
    on sms_inbound (message_sid) where message_sid is not null;
create index if not exists sms_inbound_lookup_idx on sms_inbound (received_by, phone, at);

create or replace function sms_append_only() returns trigger language plpgsql as $fn$
begin
  raise exception '% is append-only: record a new event instead', tg_table_name;
end
$fn$;

do $do$
declare
  t text;
begin
  foreach t in array array['sms_consent_events', 'sms_inbound'] loop
    if not exists (
      select 1 from pg_trigger where tgname = t || '_append_only' and tgrelid = t::regclass
    ) then
      execute format(
        'create trigger %I before update or delete on %I '
        'for each row execute function sms_append_only()', t || '_append_only', t);
      execute format(
        'create trigger %I before truncate on %I '
        'for each statement execute function sms_append_only()', t || '_no_truncate', t);
    end if;
  end loop;
end
$do$;

alter table sms_consent enable row level security;
alter table sms_opt_outs enable row level security;
alter table sms_consent_events enable row level security;
alter table sms_inbound enable row level security;

do $do$
declare
  r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format(
        'revoke all on sms_consent, sms_opt_outs, sms_consent_events, sms_inbound from %I', r);
    end if;
  end loop;
end
$do$;
"""

_CONSENT_COLUMNS = (
    "sender, phone, source, consented_at, evidence, timezone, "
    "revoked_at, revocation_source, revocation_kind"
)


async def create_schema(pool: Any) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        loaded = json.loads(value)
        return dict(loaded) if isinstance(loaded, dict) else {}
    return dict(value or {})


def _consent(row: Any) -> ConsentRecord:
    return ConsentRecord(
        sender=row["sender"],
        phone=row["phone"],
        source=row["source"],
        consented_at=row["consented_at"],
        evidence=_json(row["evidence"]),
        timezone=row["timezone"],
        revoked_at=row["revoked_at"],
        revocation_source=row["revocation_source"],
        revocation_kind=row["revocation_kind"],
    )


_LOG = """
insert into sms_consent_events (at, kind, phone, sender, source, detail)
values ($1, $2, $3, $4, $5, $6::jsonb)
"""


class PostgresConsentStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def get_consent(self, sender: str, phones: Sequence[str]) -> ConsentRecord | None:
        async with self._pool.acquire() as conn:
            # The most recent change wins across spellings; a revocation wins a tie.
            row = await conn.fetchrow(
                f"""
                select {_CONSENT_COLUMNS} from sms_consent
                 where sender = $1 and phone = any($2::text[])
                 order by coalesce(revoked_at, consented_at) desc, (revoked_at is not null) desc
                 limit 1
                """,
                sender,
                list(phones),
            )
            return _consent(row) if row is not None else None

    async def grant(self, record: ConsentRecord) -> ConsentRecord:
        evidence = plain_json(record.evidence)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"""
                insert into sms_consent as c ({_CONSENT_COLUMNS})
                values ($1, $2, $3, $4, $5::jsonb, $6, null, null, null)
                on conflict (sender, phone) do update
                   set source = excluded.source,
                       consented_at = excluded.consented_at,
                       evidence = excluded.evidence,
                       timezone = excluded.timezone,
                       revoked_at = null,
                       revocation_source = null,
                       revocation_kind = null
                 where c.revoked_at is not null
                returning {_CONSENT_COLUMNS}
                """,
                record.sender,
                record.phone,
                record.source,
                record.consented_at,
                json.dumps(evidence),
                record.timezone,
            )
            if row is None:  # already active: keep the original grant, take a new zone
                row = await conn.fetchrow(
                    f"""
                    update sms_consent set timezone = coalesce($3, timezone)
                     where sender = $1 and phone = $2
                    returning {_CONSENT_COLUMNS}
                    """,
                    record.sender,
                    record.phone,
                    record.timezone,
                )
            await conn.execute(
                _LOG,
                record.consented_at,
                "granted",
                record.phone,
                record.sender,
                record.source,
                json.dumps({"evidence": evidence, "timezone": record.timezone}),
            )
            return _consent(row)

    async def _revoke(
        self, where: str, args: Sequence[Any], *, at: datetime, source: str, kind: str
    ) -> int:
        n = len(args)
        async with self._pool.acquire() as conn:
            count = await conn.fetchval(
                f"""
                with changed as (
                    update sms_consent
                       set revoked_at = ${n + 1}, revocation_source = ${n + 2},
                           revocation_kind = ${n + 3}
                     where {where} and revoked_at is null
                    returning sender, phone
                ), logged as (
                    insert into sms_consent_events (at, kind, phone, sender, source, detail)
                    select ${n + 1}, 'revoked', phone, sender, ${n + 2},
                           jsonb_build_object('kind', ${n + 3}::text)
                      from changed
                )
                select count(*) from changed
                """,
                *args,
                at,
                source,
                kind,
            )
            return int(count)

    async def revoke_all(self, phones: Sequence[str], *, at: datetime, source: str) -> int:
        return await self._revoke(
            "phone = any($1::text[])", [list(phones)], at=at, source=source, kind="opt_out"
        )

    async def revoke(self, sender: str, phones: Sequence[str], *, at: datetime, source: str) -> int:
        return await self._revoke(
            "sender = $1 and phone = any($2::text[])",
            [sender, list(phones)],
            at=at,
            source=source,
            kind="sender",
        )

    async def restore_revoked(
        self, sender: str, phones: Sequence[str], *, at: datetime, source: str
    ) -> int:
        async with self._pool.acquire() as conn:
            count = await conn.fetchval(
                """
                with changed as (
                    update sms_consent
                       set revoked_at = null, revocation_source = null, revocation_kind = null
                     where sender = $1 and phone = any($2::text[]) and revocation_kind = 'opt_out'
                    returning sender, phone
                ), logged as (
                    insert into sms_consent_events (at, kind, phone, sender, source)
                    select $3, 'restored', phone, sender, $4 from changed
                )
                select count(*) from changed
                """,
                sender,
                list(phones),
                at,
                source,
            )
            return int(count)

    async def is_opted_out(self, phones: Sequence[str]) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "select 1 from sms_opt_outs where phone = any($1::text[]) and resumed_at is null",
                list(phones),
            )
            return row is not None

    async def record_opt_out(self, record: OptOutRecord) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            # A repeat STOP keeps the original date (the evidence of how long
            # you had been told to stop); a STOP after a START blocks again.
            await conn.execute(
                """
                insert into sms_opt_outs as o
                    (phone, opted_out_at, received_by, source, evidence_sid, evidence_body)
                values ($1, $2, $3, $4, $5, $6)
                on conflict (phone) do update
                   set opted_out_at = excluded.opted_out_at,
                       received_by = excluded.received_by,
                       source = excluded.source,
                       evidence_sid = excluded.evidence_sid,
                       evidence_body = excluded.evidence_body,
                       resumed_at = null
                 where o.resumed_at is not null
                """,
                record.phone,
                record.opted_out_at,
                record.received_by,
                record.source,
                record.evidence_sid,
                record.evidence_body,
            )
            await conn.execute(
                _LOG,
                record.opted_out_at,
                "opted_out",
                record.phone,
                None,
                record.source,
                json.dumps(
                    {
                        "received_by": record.received_by,
                        "evidence_sid": record.evidence_sid,
                        "evidence_body": record.evidence_body,
                    }
                ),
            )

    async def resume(
        self, phones: Sequence[str], *, at: datetime, source: str, received_by: str
    ) -> int:
        async with self._pool.acquire() as conn:
            count = await conn.fetchval(
                """
                with changed as (
                    update sms_opt_outs set resumed_at = $2
                     where phone = any($1::text[]) and resumed_at is null
                    returning phone
                ), logged as (
                    insert into sms_consent_events (at, kind, phone, sender, source, detail)
                    select $2, 'opted_in', phone, null, $3,
                           jsonb_build_object('received_by', $4::text)
                      from changed
                )
                select count(*) from changed
                """,
                list(phones),
                at,
                source,
                received_by,
            )
            return int(count)

    async def record_inbound(self, record: InboundRecord) -> bool:
        async with self._pool.acquire() as conn:
            inserted = await conn.fetchval(
                """
                insert into sms_inbound (at, phone, received_by, message_sid, keyword)
                values ($1, $2, $3, $4, $5)
                on conflict (message_sid) where message_sid is not null do nothing
                returning id
                """,
                record.at,
                record.phone,
                record.received_by,
                record.message_sid or None,
                record.keyword,
            )
            return inserted is not None

    async def seen_message(self, message_sid: str) -> bool:
        if not message_sid:
            return False
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "select 1 from sms_inbound where message_sid = $1", message_sid
            )
            return row is not None

    async def last_inbound(self, sender: str, phones: Sequence[str]) -> datetime | None:
        async with self._pool.acquire() as conn:
            value = await conn.fetchval(
                """
                select max(at) from sms_inbound
                 where received_by = $1 and phone = any($2::text[])
                """,
                sender,
                list(phones),
            )
            return value if isinstance(value, datetime) else None

    async def history(self, phones: Sequence[str]) -> list[ConsentEvent]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                select at, kind, phone, sender, source, detail from sms_consent_events
                 where phone = any($1::text[])
                 order by at, id
                """,
                list(phones),
            )
            return [
                ConsentEvent(
                    at=row["at"],
                    kind=row["kind"],
                    phone=row["phone"],
                    sender=row["sender"],
                    source=row["source"],
                    detail=_json(row["detail"]),
                )
                for row in rows
            ]


__all__ = ["SCHEMA_SQL", "PostgresConsentStore", "create_schema"]
