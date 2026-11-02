from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from consent_ledger import (
    ConsentLedger,
    InMemoryConsentStore,
    SendReceipt,
    SmsBlocked,
    Template,
    TemplateRegistry,
    send_sms,
)

TEMPLATES = TemplateRegistry(
    {
        "order_update": Template(
            "Acme: Order {order} is {status}. Reply STOP to opt out, HELP for help."
        ),
        "notice": Template(
            "Acme: {notice} Reply STOP to opt out.", free_text=frozenset({"notice"})
        ),
    }
)
NOON_SF = datetime(2026, 7, 15, 19, 0, tzinfo=UTC)  # 12:00 in San Francisco
PAT = "+14155550134"
LANGUAGE = "Yes, text me order updates. Reply STOP to opt out."


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    async def send(self, *, to: str, body: str, from_number: str | None = None) -> SendReceipt:
        self.sent.append((to, body, from_number))
        return SendReceipt(sid=f"SM{len(self.sent):04d}", status="queued", to=to)


def test_render_is_strict() -> None:
    assert (
        TEMPLATES.render("order_update", {"order": "1042", "status": "shipped"})
        == "Acme: Order 1042 is shipped. Reply STOP to opt out, HELP for help."
    )
    with pytest.raises(ValueError, match="unknown template"):
        TEMPLATES.render("promo", {})
    with pytest.raises(ValueError, match="missing"):
        TEMPLATES.render("order_update", {"order": "1042"})
    with pytest.raises(ValueError, match="unexpected"):
        TEMPLATES.render("order_update", {"order": "1", "status": "x", "coupon": "SAVE"})
    with pytest.raises(ValueError, match="control characters"):
        TEMPLATES.render("order_update", {"order": "1042", "status": "shipped\nAcme: WIN"})


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029", "\x7f", "\t", "\r"])
def test_every_kind_of_line_break_is_refused(separator: str) -> None:
    # Finding 14: NEL and Unicode's line and paragraph separators got through.
    with pytest.raises(ValueError, match="control characters"):
        TEMPLATES.render("order_update", {"order": "1", "status": f"shipped{separator}Acme: WIN"})


@pytest.mark.parametrize(
    "notice",
    [
        "Reply START to keep getting deals!",
        # Finding 14: quotes, "with" and txt-speak got through.
        'Reply "START" for deals',
        "Reply with YES for deals",
        "Txt START 4 deals",
        "Text back Y to join",
    ],
)
def test_free_text_may_not_carry_its_own_opt_out_language(notice: str) -> None:
    with pytest.raises(ValueError, match="opt-out or opt-in"):
        TEMPLATES.render("notice", {"notice": notice})


def test_free_text_that_only_mentions_replying_is_fine() -> None:
    assert TEMPLATES.render("notice", {"notice": "The office is closed Monday."}).startswith(
        "Acme: The office"
    )
    assert TEMPLATES.render("notice", {"notice": "Send your forms by noon."})


def test_length_and_registry_rules() -> None:
    with pytest.raises(ValueError, match="over 320"):
        TEMPLATES.render("notice", {"notice": "x" * 400})
    with pytest.raises(ValueError, match="Reply STOP"):
        TemplateRegistry({"bad": Template("Acme: {x}")})
    with pytest.raises(ValueError, match="named placeholders"):
        TemplateRegistry({"bad": Template("Acme: {0} Reply STOP.")})
    with pytest.raises(ValueError, match="unknown blanks"):
        TemplateRegistry({"bad": Template("Acme: {x} Reply STOP.", free_text=frozenset({"y"}))})


async def make_ledger() -> ConsentLedger:
    ledger = ConsentLedger(InMemoryConsentStore(), clock=lambda: NOON_SF)
    await ledger.grant("acme", PAT, source="web_form", language=LANGUAGE)
    return ledger


async def test_send_runs_every_check_then_sends() -> None:
    ledger = await make_ledger()
    transport = FakeTransport()
    logged: list[tuple[str, str, str, str]] = []

    async def log(sender: str, phone: str, template_id: str, receipt: SendReceipt) -> None:
        logged.append((sender, phone, template_id, receipt.sid))

    receipt = await send_sms(
        ledger=ledger,
        transport=transport,
        templates=TEMPLATES,
        sender="acme",
        to="(415) 555-0134",
        template_id="order_update",
        params={"order": "1042", "status": "shipped"},
        enabled=True,
        from_number="+14155550100",
        log=log,
    )
    assert receipt.sid == "SM0001"
    assert transport.sent == [
        (PAT, "Acme: Order 1042 is shipped. Reply STOP to opt out, HELP for help.", "+14155550100")
    ]
    assert logged == [("acme", PAT, "order_update", "SM0001")]


def test_send_cannot_be_told_another_time() -> None:
    # Finding 13: send_sms(now=noon) sent a text at a real 03:00.
    assert "now" not in inspect.signature(send_sms).parameters


async def test_send_checks_the_ledgers_own_clock() -> None:
    three_am_sf = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    ledger = ConsentLedger(InMemoryConsentStore(), clock=lambda: three_am_sf)
    await ledger.grant("acme", PAT, source="web_form", language=LANGUAGE)
    transport = FakeTransport()
    with pytest.raises(SmsBlocked) as blocked:
        await send_sms(
            ledger=ledger,
            transport=transport,
            templates=TEMPLATES,
            sender="acme",
            to=PAT,
            template_id="order_update",
            params={"order": "1", "status": "x"},
            enabled=True,
        )
    assert blocked.value.reason == "quiet_hours"
    assert transport.sent == []


async def test_send_is_dark_by_default() -> None:
    transport = FakeTransport()
    with pytest.raises(SmsBlocked) as blocked:
        await send_sms(
            ledger=await make_ledger(),
            transport=transport,
            templates=TEMPLATES,
            sender="acme",
            to=PAT,
            template_id="order_update",
            params={"order": "1", "status": "x"},
        )
    assert blocked.value.reason == "disabled"
    assert transport.sent == []


async def test_a_blocked_recipient_never_reaches_the_transport() -> None:
    ledger = await make_ledger()
    await ledger.handle_inbound(from_number=PAT, body="STOP", received_by="acme")
    transport = FakeTransport()
    with pytest.raises(SmsBlocked) as blocked:
        await send_sms(
            ledger=ledger,
            transport=transport,
            templates=TEMPLATES,
            sender="acme",
            to=PAT,
            template_id="order_update",
            params={"order": "1", "status": "x"},
            enabled=True,
        )
    assert blocked.value.reason == "opted_out"
    assert transport.sent == []


async def test_a_logging_failure_does_not_report_a_failed_send() -> None:
    async def broken_log(*args: object) -> None:
        raise RuntimeError("log table down")

    transport = FakeTransport()
    receipt = await send_sms(
        ledger=await make_ledger(),
        transport=transport,
        templates=TEMPLATES,
        sender="acme",
        to=PAT,
        template_id="order_update",
        params={"order": "1", "status": "x"},
        enabled=True,
        log=broken_log,
    )
    assert receipt.status == "queued"
    assert len(transport.sent) == 1
