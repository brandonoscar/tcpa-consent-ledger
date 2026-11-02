"""The ledger's decisions. Every test runs on each store (see conftest)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from consent_ledger.ledger import ConsentLedger
from consent_ledger.stores import ConsentRecord, ConsentStore, InMemoryConsentStore

from .conftest import Clock

PAT = "+14155550134"  # San Francisco
PAT_TYPED = "(415) 555-0134"
#: The same number typed on a full-width keyboard.
PAT_FULL_WIDTH = "".join(chr(0xFF10 + int(digit)) for digit in "4155550134")
LEAD = "+13125550188"  # Chicago, never consented
LANGUAGE = "Yes, text me order updates. Msg & data rates may apply. Reply STOP to opt out."


async def consent(ledger: ConsentLedger, sender: str = "acme", phone: str = PAT) -> None:
    await ledger.grant(sender, phone, source="web_form", language=LANGUAGE)


async def text_from(ledger: ConsentLedger, phone: str, body: str, to: str = "acme") -> None:
    await ledger.handle_inbound(from_number=phone, body=body, received_by=to)


# ── consent ─────────────────────────────────────────────────────────────────


async def test_consent_allows_a_text_in_hours(ledger: ConsentLedger) -> None:
    await consent(ledger)
    assert (await ledger.check("acme", PAT)).allowed
    assert (await ledger.check("acme", PAT_TYPED)).allowed


async def test_a_number_on_file_is_not_consent(ledger: ConsentLedger) -> None:
    decision = await ledger.check("acme", LEAD)
    assert (decision.allowed, decision.reason) == (False, "no_consent")


async def test_consent_is_per_sender(ledger: ConsentLedger) -> None:
    await consent(ledger, "acme")
    assert (await ledger.check("globex", PAT)).reason == "no_consent"


async def test_grant_needs_the_language_and_a_real_number(ledger: ConsentLedger) -> None:
    with pytest.raises(ValueError, match="language"):
        await ledger.grant("acme", PAT, source="web_form", language="  ")
    with pytest.raises(ValueError, match="not a phone number"):
        await ledger.grant("acme", "555-0134", source="web_form", language=LANGUAGE)
    record = await ledger.grant(
        "acme", PAT_TYPED, source="web_form", language=LANGUAGE, evidence={"ip": "192.0.2.10"}
    )
    assert record.phone == PAT
    assert record.evidence == {"ip": "192.0.2.10", "language": LANGUAGE}


async def test_evidence_is_plain_json_on_every_store(
    ledger: ConsentLedger, store: ConsentStore
) -> None:
    # Finding 9: a datetime in evidence raised on Postgres and not in memory.
    captured = datetime(2026, 7, 1, 9, 30, tzinfo=UTC)
    record = await ledger.grant(
        "acme", PAT, source="web_form", language=LANGUAGE, evidence={"captured_at": captured}
    )
    assert record.evidence["captured_at"] == "2026-07-01T09:30:00+00:00"
    stored = await store.get_consent("acme", [PAT])
    assert stored is not None
    assert stored.evidence["captured_at"] == "2026-07-01T09:30:00+00:00"
    with pytest.raises(TypeError, match="plain JSON"):
        await ledger.grant(
            "acme", PAT, source="web_form", language=LANGUAGE, evidence={"blob": object()}
        )


async def test_revoking_one_sender_leaves_the_others(ledger: ConsentLedger) -> None:
    await consent(ledger, "acme")
    await consent(ledger, "globex")
    assert await ledger.revoke("acme", PAT_TYPED, source="settings_page") == 1
    assert (await ledger.check("acme", PAT)).reason == "revoked"
    assert (await ledger.check("globex", PAT)).allowed


async def test_revoking_junk_is_an_error_not_a_quiet_zero(ledger: ConsentLedger) -> None:
    with pytest.raises(ValueError, match="not a phone number"):
        await ledger.revoke("acme", "4155550l34", source="settings_page")


async def test_a_regrant_after_revocation(ledger: ConsentLedger) -> None:
    await consent(ledger)
    await ledger.revoke("acme", PAT, source="settings_page")
    await consent(ledger)
    assert (await ledger.check("acme", PAT)).allowed


async def test_the_latest_change_wins_across_spellings(store: ConsentStore, clock: Clock) -> None:
    # Finding 4: an old active row under a hand-typed spelling beat a newer
    # revocation of the E.164 row.
    await store.grant(ConsentRecord("acme", PAT_TYPED, "import", datetime(2025, 1, 1, tzinfo=UTC)))
    ledger = ConsentLedger(store, clock=clock)
    await consent(ledger)
    clock.advance(minutes=5)
    await ledger.revoke("acme", PAT, source="settings_page")
    for spelling in (PAT, PAT_TYPED):
        assert (await ledger.check("acme", spelling)).reason == "revoked"


async def test_a_full_width_number_is_the_same_number(ledger: ConsentLedger) -> None:
    # Finding 4: full-width digits kept their script in "E.164", so a STOP
    # from Twilio in ASCII neither blocked it nor revoked consent under it.
    record = await ledger.grant("acme", PAT_FULL_WIDTH, source="web_form", language=LANGUAGE)
    assert record.phone == PAT
    await text_from(ledger, PAT, "STOP")
    assert (await ledger.check("acme", PAT_FULL_WIDTH)).reason == "opted_out"


# ── STOP ───────────────────────────────────────────────────────────────────


async def test_stop_blocks_every_sender_and_revokes_consent(
    ledger: ConsentLedger, store: ConsentStore
) -> None:
    await consent(ledger, "acme")
    await consent(ledger, "globex")
    result = await ledger.handle_inbound(
        from_number=PAT, body="Stop", received_by="acme", message_sid="SM0001"
    )
    assert result.keyword == "stop"
    assert result.consents_changed == 2
    for sender in ("acme", "globex", "initech"):
        decision = await ledger.check(sender, PAT)
        assert decision.reason == "opted_out"
        assert "STOP" in decision.detail
    record = await store.get_consent("acme", [PAT])
    assert record is not None
    assert (record.revocation_source, record.revocation_kind) == ("inbound_keyword", "opt_out")


@pytest.mark.parametrize("body", ["Stop.", "STOP!", "Stop please", "Unsubscribe me", "ALTO"])
async def test_stop_in_its_everyday_spellings(ledger: ConsentLedger, body: str) -> None:
    # Finding 1: only an exact whole-message match counted.
    await consent(ledger)
    assert (await ledger.handle_inbound(from_number=PAT, body=body, received_by="acme")).keyword
    assert (await ledger.check("acme", PAT)).reason == "opted_out"


async def test_twilios_opt_out_type_makes_it_a_stop(ledger: ConsentLedger) -> None:
    await consent(ledger)
    result = await ledger.handle_inbound(
        from_number=PAT,
        body="ok thanks, that is all for now",
        received_by="acme",
        opt_out_type="STOP",
    )
    assert result.keyword == "stop"
    assert (await ledger.check("acme", PAT)).reason == "opted_out"


async def test_stop_covers_a_lead_with_no_consent_row(ledger: ConsentLedger) -> None:
    await text_from(ledger, LEAD, "STOP")
    assert (await ledger.check("acme", LEAD, conversational=True)).reason == "opted_out"


async def test_a_stop_recorded_in_e164_blocks_the_hand_typed_number(ledger: ConsentLedger) -> None:
    await text_from(ledger, PAT, "STOP")
    assert (await ledger.check("acme", PAT_TYPED, conversational=True)).reason == "opted_out"
    assert (await ledger.check("acme", "415.555.0134", conversational=True)).reason == "opted_out"


async def test_a_foreign_stop_does_not_block_a_us_stranger(ledger: ConsentLedger) -> None:
    await text_from(ledger, "+442079460134", "STOP")
    await text_from(ledger, "+12079460134", "hello?")
    assert (await ledger.check("acme", "+12079460134", conversational=True)).allowed


async def test_a_second_stop_keeps_the_original_date(
    ledger: ConsentLedger, store: ConsentStore, clock: Clock
) -> None:
    await text_from(ledger, PAT, "STOP")
    first = clock.now
    clock.advance(days=3)
    await text_from(ledger, PAT, "STOP", to="globex")
    stops = [event for event in await ledger.history(PAT) if event.kind == "opted_out"]
    assert [event.at for event in stops] == [first, clock.now]
    if isinstance(store, InMemoryConsentStore):
        assert store.opt_outs[PAT].opted_out_at == first
    assert (await ledger.check("acme", PAT, conversational=True)).reason == "opted_out"


class FailingRevocation(InMemoryConsentStore):
    async def revoke_all(self, phones: Sequence[str], *, at: datetime, source: str) -> int:
        raise TimeoutError("statement timeout on sms_consent")


async def test_a_stop_holds_when_revoking_consent_fails(clock: Clock) -> None:
    # Finding 7: the opt-out was written after the revocation, so a failed
    # revocation lost the STOP and the next send went out.
    ledger = ConsentLedger(FailingRevocation(), clock=clock)
    await consent(ledger)
    with pytest.raises(TimeoutError):
        await text_from(ledger, PAT, "STOP")
    assert (await ledger.check("acme", PAT)).reason == "opted_out"


# ── START ──────────────────────────────────────────────────────────────────


async def test_start_lifts_the_block_and_restores_only_revoked_consent(
    ledger: ConsentLedger, clock: Clock
) -> None:
    await consent(ledger, "acme")
    await text_from(ledger, PAT, "STOP")
    clock.advance(hours=1)
    result = await ledger.handle_inbound(from_number=PAT, body="start", received_by="acme")
    assert result.keyword == "start"
    assert result.consents_changed == 1
    assert (await ledger.check("acme", PAT)).allowed
    # START never creates consent where there was none.
    assert (await ledger.check("globex", PAT)).reason == "no_consent"


async def test_start_never_undoes_a_senders_own_revocation(ledger: ConsentLedger) -> None:
    # Finding 2: a settings-page revocation was restored by a START.
    await consent(ledger, "acme")
    await ledger.revoke("acme", PAT, source="settings_page")
    await text_from(ledger, PAT, "STOP")
    await text_from(ledger, PAT, "START")
    assert (await ledger.check("acme", PAT)).reason == "revoked"


async def test_start_restores_only_the_sender_it_was_sent_to(ledger: ConsentLedger) -> None:
    # Finding 2: a START to globex's number restored acme's consent.
    await consent(ledger, "acme")
    await consent(ledger, "globex")
    await text_from(ledger, PAT, "STOP", to="acme")
    result = await ledger.handle_inbound(from_number=PAT, body="START", received_by="globex")
    assert result.consents_changed == 1
    assert (await ledger.check("globex", PAT)).allowed
    assert (await ledger.check("acme", PAT)).reason == "revoked"


async def test_yes_is_not_start(ledger: ConsentLedger) -> None:
    # Finding 2: "yes" (the commonest answer to anything) lifted an opt-out.
    await consent(ledger)
    await text_from(ledger, PAT, "STOP")
    result = await ledger.handle_inbound(from_number=PAT, body="yes", received_by="acme")
    assert result.keyword is None
    assert (await ledger.check("acme", PAT)).reason == "opted_out"


async def test_stop_after_start_blocks_again(ledger: ConsentLedger, clock: Clock) -> None:
    for body in ("STOP", "START", "STOP"):
        await text_from(ledger, LEAD, body)
        clock.advance(minutes=5)
    assert (await ledger.check("acme", LEAD, conversational=True)).reason == "opted_out"


async def test_a_replayed_start_cannot_undo_a_newer_stop(ledger: ConsentLedger) -> None:
    # Finding 11: a signed START replayed after a STOP lifted the block.
    for body, sid in (("START", "SM0"), ("STOP", "SM1")):
        await ledger.handle_inbound(from_number=PAT, body=body, received_by="acme", message_sid=sid)
    replay = await ledger.handle_inbound(
        from_number=PAT, body="START", received_by="acme", message_sid="SM0"
    )
    assert replay.replayed
    assert (await ledger.check("acme", PAT, conversational=True)).reason == "opted_out"


async def test_help_and_ordinary_messages_change_nothing(ledger: ConsentLedger) -> None:
    await consent(ledger)
    for body in ("HELP", "is my order late?"):
        result = await ledger.handle_inbound(from_number=PAT, body=body, received_by="acme")
        assert result.consents_changed == 0
    assert (await ledger.check("acme", PAT)).allowed


# ── history ────────────────────────────────────────────────────────────────


async def test_the_history_proves_both_directions(ledger: ConsentLedger, clock: Clock) -> None:
    # Finding 8: STOP then START overwrote the grant's date and source, and
    # STOP, START, STOP erased the first STOP and the START.
    granted_at = clock.now
    await ledger.grant(
        "acme", PAT, source="web_form", language=LANGUAGE, evidence={"ip": "192.0.2.10"}
    )
    for body in ("STOP", "START", "STOP"):
        clock.advance(days=1)
        await text_from(ledger, PAT, body)
    clock.advance(days=1)
    await consent(ledger)  # a new grant while revoked
    events = await ledger.history(PAT_TYPED)
    assert [(event.kind, event.sender) for event in events] == [
        ("granted", "acme"),
        ("opted_out", None),
        ("revoked", "acme"),
        ("opted_in", None),
        ("restored", "acme"),
        ("opted_out", None),
        ("revoked", "acme"),
        ("granted", "acme"),
    ]
    first = events[0]
    assert (first.at, first.source) == (granted_at, "web_form")
    assert first.detail["evidence"]["ip"] == "192.0.2.10"
    assert events[1].detail["received_by"] == "acme"


async def test_restoring_keeps_the_original_grant(
    ledger: ConsentLedger, store: ConsentStore, clock: Clock
) -> None:
    granted_at = clock.now
    await consent(ledger)
    clock.advance(days=1)
    await text_from(ledger, PAT, "STOP")
    clock.advance(days=1)
    await text_from(ledger, PAT, "START")
    record = await store.get_consent("acme", [PAT])
    assert record is not None
    assert (record.consented_at, record.source, record.active) == (granted_at, "web_form", True)


# ── conversations ──────────────────────────────────────────────────────────


async def test_a_reply_needs_the_conversation_they_started(
    ledger: ConsentLedger, clock: Clock
) -> None:
    # Finding 10: conversational=True skipped consent on the caller's word.
    assert (await ledger.check("acme", LEAD, conversational=True)).reason == "no_conversation"
    await text_from(ledger, LEAD, "Do you ship to Canada?")
    assert (await ledger.check("acme", LEAD, conversational=True)).allowed
    # Their message was to acme, not to globex.
    assert (await ledger.check("globex", LEAD, conversational=True)).reason == "no_conversation"
    clock.advance(hours=25)
    assert (await ledger.check("acme", LEAD, conversational=True)).reason == "no_conversation"


async def test_the_conversation_window_is_configurable(store: ConsentStore, clock: Clock) -> None:
    ledger = ConsentLedger(store, clock=clock, conversation_window=timedelta(hours=1))
    await text_from(ledger, LEAD, "Is the store open?")
    clock.advance(minutes=59)
    assert (await ledger.check("acme", LEAD, conversational=True)).allowed
    clock.advance(minutes=2)
    assert (await ledger.check("acme", LEAD, conversational=True)).reason == "no_conversation"


# ── quiet hours through the ledger ─────────────────────────────────────────


async def test_a_recorded_zone_adds_a_check_and_never_removes_one(store: ConsentStore) -> None:
    # Finding 5: a recorded zone replaced the area code's, so a zone of UTC sent
    # at 03:00 in San Francisco. Now both clocks must agree.
    late = datetime(2026, 7, 16, 2, 0, tzinfo=UTC)  # 19:00 Los Angeles, 22:00 New York
    ledger = ConsentLedger(store, clock=lambda: late)
    # A New York number whose owner lives in Los Angeles: 19:00 is fine there,
    # but New York's clock still counts.
    await ledger.grant(
        "acme", "+16465550134", source="web_form", language=LANGUAGE, timezone="America/Los_Angeles"
    )
    blocked = await ledger.check("acme", "+16465550134")
    assert blocked.reason == "quiet_hours"
    assert "America/New_York" in blocked.detail
    # A San Francisco number whose owner lives in New York: 22:00 there.
    await ledger.grant(
        "acme", PAT, source="web_form", language=LANGUAGE, timezone="America/New_York"
    )
    assert (await ledger.check("acme", PAT)).reason == "quiet_hours"


async def test_a_zone_that_names_no_place_is_refused(ledger: ConsentLedger) -> None:
    for zone in ("UTC", "EST", "Etc/GMT+5", "Mars/Olympus_Mons"):
        with pytest.raises(ValueError, match="time zone"):
            await ledger.grant("acme", PAT, source="web_form", language=LANGUAGE, timezone=zone)


async def test_a_recorded_zone_reads_with_the_unknown_state_rule(store: ConsentStore) -> None:
    # Finding 5: a California number recorded in Chicago was texted Sunday at
    # 10:30 there, when the person may well be in Texas (noon on Sundays).
    sunday_1030_central = datetime(2026, 7, 19, 15, 30, tzinfo=UTC)
    ledger = ConsentLedger(store, clock=lambda: sunday_1030_central)
    await ledger.grant(
        "acme", PAT, source="web_form", language=LANGUAGE, timezone="America/Chicago"
    )
    assert (await ledger.check("acme", PAT)).reason == "quiet_hours"


async def test_granting_again_takes_the_new_zone(
    ledger: ConsentLedger, store: ConsentStore
) -> None:
    # Finding 5: granting again with the right zone returned the old row.
    await ledger.grant(
        "acme", PAT, source="web_form", language=LANGUAGE, timezone="America/New_York"
    )
    again = await ledger.grant(
        "acme", PAT, source="web_form", language=LANGUAGE, timezone="America/Los_Angeles"
    )
    assert again.timezone == "America/Los_Angeles"
    stored = await store.get_consent("acme", [PAT])
    assert stored is not None
    assert stored.timezone == "America/Los_Angeles"


async def test_a_foreign_number_is_unsupported(ledger: ConsentLedger) -> None:
    assert (await ledger.check("acme", "+442079460134")).reason == "unsupported_region"


async def test_junk_is_unverifiable(ledger: ConsentLedger) -> None:
    assert (await ledger.check("acme", "call me maybe")).reason == "unverifiable"
    with pytest.raises(ValueError, match="not a phone number"):
        await ledger.opt_out("12", received_by="acme", source="manual")


class BrokenStore(InMemoryConsentStore):
    def __init__(self, broken: str) -> None:
        super().__init__()
        self.broken = broken

    async def is_opted_out(self, phones: Sequence[str]) -> bool:
        if self.broken == "opt_outs":
            raise ConnectionError("database down")
        return await super().is_opted_out(phones)

    async def get_consent(self, sender: str, phones: Sequence[str]) -> ConsentRecord | None:
        if self.broken == "consent":
            raise ConnectionError("database down")
        return await super().get_consent(sender, phones)

    async def last_inbound(self, sender: str, phones: Sequence[str]) -> datetime | None:
        if self.broken == "inbound":
            raise ConnectionError("database down")
        return await super().last_inbound(sender, phones)


@pytest.mark.parametrize("broken", ["opt_outs", "consent", "inbound"])
async def test_a_store_that_cannot_answer_means_do_not_send(broken: str) -> None:
    ledger = ConsentLedger(BrokenStore(broken), clock=lambda: datetime(2026, 7, 15, 18, tzinfo=UTC))
    await ledger.grant("acme", PAT, source="web_form", language=LANGUAGE)
    await text_from(ledger, PAT, "hi")
    decision = await ledger.check("acme", PAT, conversational=broken == "inbound")
    assert (decision.allowed, decision.reason) == (False, "unverifiable")
