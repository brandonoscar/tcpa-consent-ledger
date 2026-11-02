"""Where consent, opt-outs and their history live.

Two current-state records, kept apart on purpose:

* **Consent** is per sender (the brand, account or customer of yours that
  texts) and per phone: a person agreed to hear from that sender. Revoking
  sets ``revoked_at`` and says why (``revocation_kind``: the sender's own
  revocation, or an opt-out), so START can restore exactly what STOP took.
* **Opt-outs** are per phone, whoever sent the STOP. They cover people with no
  consent row at all (a lead who texted you first), which a revocation alone
  cannot: there is no row to revoke. A START sets ``resumed_at``; a later STOP
  blocks again.

The current-state rows answer "may we text?" quickly and change over time.
**The history is the event log**: every grant, revocation, restoration,
opt-out and opt-in is appended to it with its time and source, and nothing in
it is ever updated or deleted. That log, not the current rows, is what proves
both directions of every change (``ConsentLedger.history``).

Inbound messages are kept too (who wrote to which sender, when, and the
message's id, never its text). They prove a conversation the person started,
and an id seen twice is a replay, not a new message.

Lookups take every spelling of a number (``phone.spellings``), so a STOP
recorded in E.164 blocks a send to the same number typed by hand.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, Literal, Protocol

RevocationKind = Literal["sender", "opt_out"]
EventKind = Literal["granted", "revoked", "restored", "opted_out", "opted_in"]


@dataclasses.dataclass(frozen=True)
class ConsentRecord:
    sender: str
    phone: str
    #: How consent was collected: "web_form", "paper_form", "keyword_optin"...
    source: str
    consented_at: datetime
    #: Server-stamped capture evidence: the exact language shown, who recorded
    #: it, IP and user agent. Never taken from the client as is. Plain JSON.
    evidence: dict[str, Any] = dataclasses.field(default_factory=dict)
    #: The recipient's IANA zone, when known. It adds a quiet-hours check; it
    #: never removes the area code's.
    timezone: str | None = None
    revoked_at: datetime | None = None
    revocation_source: str | None = None
    #: "sender": revoked for this sender alone (a settings page). "opt_out":
    #: revoked by a STOP, which a START may restore.
    revocation_kind: RevocationKind | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    @property
    def changed_at(self) -> datetime:
        """When this row last changed: its grant, or its revocation."""
        return self.revoked_at or self.consented_at


@dataclasses.dataclass(frozen=True)
class OptOutRecord:
    phone: str
    opted_out_at: datetime
    #: The sender (or the number) the STOP arrived at.
    received_by: str
    source: str
    evidence_sid: str | None = None
    evidence_body: str | None = None
    resumed_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.resumed_at is None


@dataclasses.dataclass(frozen=True)
class ConsentEvent:
    """One change, as it happened. Appended, never updated or deleted."""

    at: datetime
    kind: EventKind
    phone: str
    #: The sender a consent event is about; ``None`` for an opt-out or opt-in,
    #: which is about the person.
    sender: str | None
    source: str
    detail: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class InboundRecord:
    """An inbound message's metadata. The text itself is not kept."""

    phone: str
    received_by: str
    at: datetime
    message_sid: str | None = None
    keyword: str | None = None


def plain_json(value: Mapping[str, Any]) -> dict[str, Any]:
    """``value`` as plain JSON: datetimes and dates become ISO strings, and
    anything else JSON can't hold is an error before anything is stored, so
    both stores keep exactly the same evidence."""

    def default(obj: object) -> str:
        if isinstance(obj, datetime | date):
            return obj.isoformat()
        raise TypeError(f"evidence must be plain JSON; {type(obj).__name__} is not")

    result = json.loads(json.dumps(dict(value), default=default))
    assert isinstance(result, dict)
    return result


class ConsentStore(Protocol):
    async def get_consent(self, sender: str, phones: Sequence[str]) -> ConsentRecord | None:
        """The row for this sender under any of ``phones``. Across spellings,
        the most recent change wins, and a revocation wins a tie."""
        ...

    async def grant(self, record: ConsentRecord) -> ConsentRecord:
        """Insert, or re-grant a revoked row. An active row keeps its original
        grant; a time zone in ``record`` replaces the row's. Logs the grant."""
        ...

    async def revoke(self, sender: str, phones: Sequence[str], *, at: datetime, source: str) -> int:
        """Revoke one sender's active consent (kind "sender")."""
        ...

    async def revoke_all(self, phones: Sequence[str], *, at: datetime, source: str) -> int:
        """Revoke every sender's active consent for the number (kind "opt_out")."""
        ...

    async def restore_revoked(
        self, sender: str, phones: Sequence[str], *, at: datetime, source: str
    ) -> int:
        """START: restore this sender's rows that an opt-out revoked, and no
        others. Never creates consent."""
        ...

    async def is_opted_out(self, phones: Sequence[str]) -> bool: ...

    async def record_opt_out(self, record: OptOutRecord) -> None:
        """Keep the original date while already opted out; re-block after a
        START. Every STOP is logged, repeats included."""
        ...

    async def resume(
        self, phones: Sequence[str], *, at: datetime, source: str, received_by: str
    ) -> int:
        """Lift an active opt-out. Logs each one lifted."""
        ...

    async def record_inbound(self, record: InboundRecord) -> bool:
        """Keep an inbound message. False, and nothing kept, when its
        ``message_sid`` was recorded before."""
        ...

    async def seen_message(self, message_sid: str) -> bool: ...

    async def last_inbound(self, sender: str, phones: Sequence[str]) -> datetime | None:
        """When the person last wrote to this sender, under any spelling."""
        ...

    async def history(self, phones: Sequence[str]) -> list[ConsentEvent]:
        """Every event for the number under any spelling, oldest first."""
        ...


class InMemoryConsentStore:
    """A process-local store for tests and demos. Use ``PostgresConsentStore``
    (or your own) in production: this forgets everything on restart."""

    def __init__(self) -> None:
        self.consents: dict[tuple[str, str], ConsentRecord] = {}
        self.opt_outs: dict[str, OptOutRecord] = {}
        self.events: list[ConsentEvent] = []
        self.inbound: list[InboundRecord] = []
        self._lock = asyncio.Lock()

    def _log(self, event: ConsentEvent) -> None:
        self.events.append(event)

    async def get_consent(self, sender: str, phones: Sequence[str]) -> ConsentRecord | None:
        found = [row for p in phones if (row := self.consents.get((sender, p))) is not None]
        found.sort(key=lambda row: (row.changed_at, not row.active), reverse=True)
        return found[0] if found else None

    async def grant(self, record: ConsentRecord) -> ConsentRecord:
        record = dataclasses.replace(record, evidence=plain_json(record.evidence))
        async with self._lock:
            key = (record.sender, record.phone)
            existing = self.consents.get(key)
            if existing is not None and existing.active:
                if record.timezone is not None and record.timezone != existing.timezone:
                    existing = dataclasses.replace(existing, timezone=record.timezone)
                    self.consents[key] = existing
                kept = existing
            else:
                self.consents[key] = kept = record
            self._log(
                ConsentEvent(
                    record.consented_at,
                    "granted",
                    record.phone,
                    record.sender,
                    record.source,
                    {"evidence": record.evidence, "timezone": record.timezone},
                )
            )
            return kept

    async def _revoke_where(
        self, match: Any, *, at: datetime, source: str, kind: RevocationKind
    ) -> int:
        async with self._lock:
            count = 0
            for key, row in list(self.consents.items()):
                if row.active and match(key):
                    self.consents[key] = dataclasses.replace(
                        row, revoked_at=at, revocation_source=source, revocation_kind=kind
                    )
                    self._log(ConsentEvent(at, "revoked", key[1], key[0], source, {"kind": kind}))
                    count += 1
            return count

    async def revoke_all(self, phones: Sequence[str], *, at: datetime, source: str) -> int:
        wanted = set(phones)
        return await self._revoke_where(
            lambda key: key[1] in wanted, at=at, source=source, kind="opt_out"
        )

    async def revoke(self, sender: str, phones: Sequence[str], *, at: datetime, source: str) -> int:
        wanted = set(phones)
        return await self._revoke_where(
            lambda key: key[0] == sender and key[1] in wanted, at=at, source=source, kind="sender"
        )

    async def restore_revoked(
        self, sender: str, phones: Sequence[str], *, at: datetime, source: str
    ) -> int:
        wanted = set(phones)
        async with self._lock:
            count = 0
            for key, row in list(self.consents.items()):
                if (
                    key[0] == sender
                    and key[1] in wanted
                    and not row.active
                    and row.revocation_kind == "opt_out"
                ):
                    self.consents[key] = dataclasses.replace(
                        row, revoked_at=None, revocation_source=None, revocation_kind=None
                    )
                    self._log(ConsentEvent(at, "restored", key[1], sender, source))
                    count += 1
            return count

    async def is_opted_out(self, phones: Sequence[str]) -> bool:
        return any((row := self.opt_outs.get(p)) is not None and row.active for p in phones)

    async def record_opt_out(self, record: OptOutRecord) -> None:
        async with self._lock:
            existing = self.opt_outs.get(record.phone)
            if existing is None or not existing.active:
                self.opt_outs[record.phone] = record
            # Else keep the original date: it is the evidence of how long you
            # had been told to stop. The repeat is still logged below.
            self._log(
                ConsentEvent(
                    record.opted_out_at,
                    "opted_out",
                    record.phone,
                    None,
                    record.source,
                    {
                        "received_by": record.received_by,
                        "evidence_sid": record.evidence_sid,
                        "evidence_body": record.evidence_body,
                    },
                )
            )

    async def resume(
        self, phones: Sequence[str], *, at: datetime, source: str, received_by: str
    ) -> int:
        async with self._lock:
            count = 0
            for phone in phones:
                row = self.opt_outs.get(phone)
                if row is not None and row.active:
                    self.opt_outs[phone] = dataclasses.replace(row, resumed_at=at)
                    self._log(
                        ConsentEvent(
                            at, "opted_in", phone, None, source, {"received_by": received_by}
                        )
                    )
                    count += 1
            return count

    async def record_inbound(self, record: InboundRecord) -> bool:
        async with self._lock:
            if record.message_sid and any(
                row.message_sid == record.message_sid for row in self.inbound
            ):
                return False
            self.inbound.append(record)
            return True

    async def seen_message(self, message_sid: str) -> bool:
        return bool(message_sid) and any(row.message_sid == message_sid for row in self.inbound)

    async def last_inbound(self, sender: str, phones: Sequence[str]) -> datetime | None:
        wanted = set(phones)
        times = [r.at for r in self.inbound if r.received_by == sender and r.phone in wanted]
        return max(times, default=None)

    async def history(self, phones: Sequence[str]) -> list[ConsentEvent]:
        wanted = set(phones)
        found = [event for event in self.events if event.phone in wanted]
        # Stable: events at the same moment keep the order they happened in.
        return sorted(found, key=lambda event: event.at)


__all__ = [
    "ConsentEvent",
    "ConsentRecord",
    "ConsentStore",
    "EventKind",
    "InMemoryConsentStore",
    "InboundRecord",
    "OptOutRecord",
    "RevocationKind",
    "plain_json",
]
