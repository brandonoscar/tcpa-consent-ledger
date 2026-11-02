"""The consent ledger: who may be texted, by whom, and when.

Every answer fails closed. A number that cannot be parsed, a store that
cannot be read and a time zone that cannot be resolved all mean "do not
send", because "we could not check" must never mean "so we sent it".

The check order, and why:

1. **Opted out**, for any sender. One STOP silences every sender: an
   over-broad block is a customer-service problem, a missed one is a
   violation. Checked first so the refusal says so, rather than "try later".
2. **Unsupported region**: not a +1 number. A US campaign cannot text it at
   all, which is a different answer from "not at this hour".
3. **Consent** for this sender. A phone number in your CRM is not consent.
   A reply in a conversation the person started needs no consent row, but it
   needs the conversation: an inbound message from them to this sender within
   ``conversation_window``.
4. **Quiet hours**, on the recipient's clock, per state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from consent_ledger.keywords import Keyword, classify
from consent_ledger.phone import is_nanp, normalize_e164, redact, spellings
from consent_ledger.quiet_hours import (
    DEFAULT_RULES,
    QuietHoursRules,
    describe_window,
    may_send_now,
    validate_zone,
)
from consent_ledger.stores import (
    ConsentEvent,
    ConsentRecord,
    ConsentStore,
    InboundRecord,
    OptOutRecord,
    plain_json,
)

logger = logging.getLogger(__name__)

BlockReason = Literal[
    "unverifiable",  # the number cannot be parsed, or the store could not be read
    "opted_out",
    "unsupported_region",
    "no_consent",
    "revoked",
    "no_conversation",
    "quiet_hours",
]

_DETAIL: dict[str, str] = {
    "unverifiable": "could not verify this number may be texted, so it was not",
    "opted_out": "this number replied STOP and has not opted back in",
    "unsupported_region": "this number is outside the US/Canada (+1) and cannot be texted",
    "no_consent": "no consent on record for this sender",
    "revoked": "consent for this sender was revoked",
    "no_conversation": "no recent message from this number to this sender to reply to",
}

#: How long after the person's last message a reply still counts as part of
#: the conversation they started. A policy choice: no statute sets it.
DEFAULT_CONVERSATION_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: BlockReason | None = None
    detail: str = ""

    @staticmethod
    def allow() -> Decision:
        return Decision(True)

    @staticmethod
    def block(reason: BlockReason, detail: str = "") -> Decision:
        return Decision(False, reason, detail or _DETAIL.get(reason, reason))


@dataclass(frozen=True)
class InboundResult:
    """What an inbound message did to the ledger."""

    keyword: Keyword | None
    phone: str
    #: Consent rows revoked or restored by this message.
    consents_changed: int = 0
    #: The message id was seen before: a replay or a retry. Nothing changed.
    replayed: bool = False


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ConsentLedger:
    def __init__(
        self,
        store: ConsentStore,
        *,
        rules: QuietHoursRules = DEFAULT_RULES,
        clock: Callable[[], datetime] = _utcnow,
        conversation_window: timedelta = DEFAULT_CONVERSATION_WINDOW,
    ) -> None:
        self.store = store
        self.rules = rules
        self.clock = clock
        self.conversation_window = conversation_window

    # ── recording ────────────────────────────────────────────────────────

    async def grant(
        self,
        sender: str,
        phone: str,
        *,
        source: str,
        language: str,
        evidence: Mapping[str, Any] | None = None,
        timezone: str | None = None,
    ) -> ConsentRecord:
        """Record that ``phone`` agreed to texts from ``sender``.

        ``language`` is the exact consent text the person saw. Without it the
        record proves nothing, so it is required. ``evidence`` must be plain
        JSON (datetimes are stored as ISO strings). ``timezone`` must name a
        place (``America/Chicago``); it adds a quiet-hours check. Granting
        again while consent is active keeps the original grant, logs the new
        one, and takes its time zone.
        """
        canonical = normalize_e164(phone)
        if canonical is None:
            raise ValueError("not a phone number; consent was not recorded")
        if not sender:
            raise ValueError("consent needs the sender it was given to")
        if not language.strip():
            raise ValueError("record the exact consent language the person saw")
        zone = validate_zone(timezone) if timezone is not None else None
        record = ConsentRecord(
            sender=sender,
            phone=canonical,
            source=source,
            consented_at=self.clock(),
            evidence=plain_json({**dict(evidence or {}), "language": language}),
            timezone=zone,
        )
        return await self.store.grant(record)

    async def revoke(self, sender: str, phone: str, *, source: str) -> int:
        """One sender's consent revoked (the person turned texts off in your app).
        A START never restores it."""
        known = spellings(phone)
        if not known:
            raise ValueError("not a phone number; the revocation was not recorded")
        return await self.store.revoke(sender, known, at=self.clock(), source=source)

    async def opt_out(
        self,
        phone: str,
        *,
        received_by: str,
        source: str,
        evidence_sid: str | None = None,
        evidence_body: str | None = None,
    ) -> int:
        """Block the number for every sender and revoke every consent it holds.

        The opt-out is written first, so the number is blocked even if the
        revocation then fails (the error is raised after it). The revocation
        keeps the consent record honest for people who consented; the opt-out
        covers people who never did (a lead who texted first has no row).
        """
        known = spellings(phone)
        if not known:
            raise ValueError("not a phone number; the opt-out was not recorded")
        now = self.clock()
        await self.store.record_opt_out(
            OptOutRecord(
                phone=normalize_e164(phone) or phone.strip(),
                opted_out_at=now,
                received_by=received_by,
                source=source,
                evidence_sid=evidence_sid or None,
                evidence_body=(evidence_body or "").strip()[:320] or None,
            )
        )
        try:
            return await self.store.revoke_all(known, at=now, source=source)
        except Exception:
            logger.error("opt-out for %s recorded, but revoking its consent failed", redact(phone))
            raise

    async def opt_in_again(self, phone: str, *, received_by: str, source: str) -> int:
        """START: lift the block, and restore ``received_by``'s consent that an
        opt-out revoked.

        It restores nothing a sender's own revocation took, nothing for other
        senders, and it never creates consent: with no prior grant there is
        nothing to restore.
        """
        known = spellings(phone)
        if not known:
            return 0
        now = self.clock()
        await self.store.resume(known, at=now, source=source, received_by=received_by)
        return await self.store.restore_revoked(received_by, known, at=now, source=source)

    async def handle_inbound(
        self,
        *,
        from_number: str,
        body: str,
        received_by: str,
        message_sid: str = "",
        opt_out_type: str | None = None,
    ) -> InboundResult:
        """Apply STOP / START / HELP from an inbound message, and keep its
        metadata (sender, number, time, id; never the text).

        ``opt_out_type`` is Twilio's ``OptOutType`` parameter: "STOP" makes it
        a STOP whatever the body says. A ``message_sid`` seen before changes
        nothing: a replayed START must not undo a newer STOP.
        """
        keyword: Keyword | None = classify(body)
        if (opt_out_type or "").strip().upper() == "STOP":
            keyword = "stop"
        phone = normalize_e164(from_number) or from_number.strip()
        if message_sid and await self.store.seen_message(message_sid):
            logger.info("message %s from %s seen before; ignored", message_sid, redact(phone))
            return InboundResult(keyword, phone, 0, replayed=True)
        record = InboundRecord(
            phone=phone,
            received_by=received_by,
            at=self.clock(),
            message_sid=message_sid or None,
            keyword=keyword,
        )
        changed = 0
        if keyword == "stop":
            # Apply first, then remember the id: a STOP that failed halfway is
            # applied again when it is retried.
            changed = await self.opt_out(
                from_number,
                received_by=received_by,
                source="inbound_keyword",
                evidence_sid=message_sid,
                evidence_body=body,
            )
            await self.store.record_inbound(record)
            logger.info("STOP from %s via %s", redact(from_number), received_by)
        elif keyword == "start":
            # Claim the id first: a START that cannot be claimed is a replay.
            if not await self.store.record_inbound(record):
                return InboundResult(keyword, phone, 0, replayed=True)
            changed = await self.opt_in_again(
                from_number, received_by=received_by, source="inbound_keyword"
            )
            logger.info("START from %s via %s", redact(from_number), received_by)
        else:
            await self.store.record_inbound(record)
        return InboundResult(keyword, phone, changed)

    async def history(self, phone: str) -> list[ConsentEvent]:
        """Every grant, revocation, restoration, opt-out and opt-in for the
        number, oldest first. The log is append-only."""
        known = spellings(phone)
        if not known:
            raise ValueError("not a phone number")
        return await self.store.history(known)

    # ── deciding ─────────────────────────────────────────────────────────

    async def check(
        self,
        sender: str,
        phone: str,
        *,
        now: datetime | None = None,
        conversational: bool = False,
    ) -> Decision:
        """May ``sender`` text ``phone`` now?

        ``conversational=True`` is a reply in a conversation the person
        started: it needs no consent row, but it needs an inbound message from
        them to this sender within ``conversation_window``. The opt-out and
        quiet-hours floors hold either way.

        ``now`` asks about another moment (to plan a send). ``send_sms``
        always asks about the ledger's own clock.
        """
        known = spellings(phone)
        if not known:
            return Decision.block("unverifiable", "not a phone number")
        moment = now or self.clock()
        try:
            if await self.store.is_opted_out(known):
                return Decision.block("opted_out")
        except Exception:
            logger.warning("opt-out lookup failed; refusing", exc_info=True)
            return Decision.block("unverifiable")
        if not is_nanp(phone):
            return Decision.block("unsupported_region")

        zone: str | None = None
        if conversational:
            try:
                last = await self.store.last_inbound(sender, known)
            except Exception:
                logger.warning("inbound lookup failed; refusing", exc_info=True)
                return Decision.block("unverifiable")
            if last is None or not (moment - self.conversation_window <= last <= moment):
                return Decision.block("no_conversation")
        else:
            try:
                record = await self.store.get_consent(sender, known)
            except Exception:
                logger.warning("consent lookup failed; refusing", exc_info=True)
                return Decision.block("unverifiable")
            if record is None:
                return Decision.block("no_consent")
            if not record.active:
                return Decision.block("revoked")
            zone = record.timezone

        if not may_send_now(phone, moment, zone=zone, rules=self.rules):
            window = describe_window(phone, moment, zone=zone, rules=self.rules)
            return Decision.block("quiet_hours", f"outside the recipient's local window ({window})")
        return Decision.allow()


__all__ = [
    "DEFAULT_CONVERSATION_WINDOW",
    "BlockReason",
    "ConsentLedger",
    "Decision",
    "InboundResult",
]
