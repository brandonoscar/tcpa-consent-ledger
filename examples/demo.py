"""A walk through the ledger, offline. Every number is from the 555-01xx range
reserved for fiction, and the transport prints instead of sending.

    python examples/demo.py
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import UTC, datetime

from consent_ledger import (
    ConsentLedger,
    InMemoryConsentStore,
    SendReceipt,
    SmsBlocked,
    Template,
    TemplateRegistry,
    handle_inbound_webhook,
    send_sms,
)
from consent_ledger.twilio import compute_signature

AUTH_TOKEN = "demo-token-not-real"
WEBHOOK_URL = "https://example.com/sms/inbound"
OUR_NUMBER = "+18885550100"
PAT = "+14155550134"  # San Francisco
LEAD = "(312) 555-0188"  # Chicago, typed by hand into a CRM
MIAMI = "+13055550199"
#: Twilio gives every message its own id; the ledger ignores an id it has seen.
MESSAGE_SIDS = (f"SM{n:032d}" for n in itertools.count(1))

TEMPLATES = TemplateRegistry(
    {
        "order_update": Template(
            "Acme: Order {order} is {status}. Reply STOP to opt out, HELP for help."
        ),
        "reply": Template("Acme: {text} Reply STOP to opt out.", free_text=frozenset({"text"})),
    }
)


class PrintTransport:
    async def send(self, *, to: str, body: str, from_number: str | None = None) -> SendReceipt:
        print(f"    -> SENT to {to}: {body}")
        return SendReceipt(sid="SM-demo", status="queued", to=to)


async def attempt(ledger: ConsentLedger, to: str, template_id: str, **params: str) -> None:
    try:
        await send_sms(
            ledger=ledger,
            transport=PrintTransport(),
            templates=TEMPLATES,
            sender="acme",
            to=to,
            template_id=template_id,
            params=params,
            enabled=True,
            conversational=template_id == "reply",
        )
    except SmsBlocked as blocked:
        print(f"    -> BLOCKED ({blocked.reason}): {blocked.detail}")


async def inbound(ledger: ConsentLedger, from_number: str, body: str) -> None:
    params = {"From": from_number, "To": OUR_NUMBER, "Body": body, "MessageSid": next(MESSAGE_SIDS)}
    message = await handle_inbound_webhook(
        ledger,
        auth_token=AUTH_TOKEN,
        url=WEBHOOK_URL,
        params=params,
        signature=compute_signature(AUTH_TOKEN, WEBHOOK_URL, params),
        sender_for_number=lambda number: "acme" if number == OUR_NUMBER else None,
    )
    print(f"    <- {from_number} texted {body!r} (keyword: {message.keyword})")


async def main() -> None:
    noon_in_sf = datetime(2026, 7, 15, 19, 0, tzinfo=UTC)
    ledger = ConsentLedger(InMemoryConsentStore(), clock=lambda: noon_in_sf)

    print("1. Pat agreed to texts on our web form.")
    await ledger.grant(
        "acme",
        PAT,
        source="web_form",
        language="Yes, text me about my orders. Msg & data rates may apply. Reply STOP to opt out.",
        evidence={"ip": "192.0.2.10", "user_agent": "demo"},
    )
    await attempt(ledger, PAT, "order_update", order="1042", status="shipped")

    print("2. A Miami customer: at noon Pacific it is 3 PM in Miami, which is fine...")
    await ledger.grant("acme", MIAMI, source="paper_form", language="Text me. Reply STOP to end.")
    await attempt(ledger, MIAMI, "order_update", order="2001", status="delayed")
    print("   ...but at 5:30 PM Pacific it is 8:30 PM in Miami, past Florida's 8 PM.")
    ledger.clock = lambda: datetime(2026, 7, 16, 0, 30, tzinfo=UTC)
    await attempt(ledger, MIAMI, "order_update", order="2001", status="delayed")
    ledger.clock = lambda: noon_in_sf

    print("3. Pat replies STOP through the signed webhook.")
    await inbound(ledger, PAT, "Stop")
    await attempt(ledger, PAT, "order_update", order="1042", status="delivered")

    print("4. A lead texts us first, so a reply needs no consent row...")
    await inbound(ledger, "+13125550188", "Do you ship to Canada?")
    await attempt(ledger, LEAD, "reply", text="We do. Want a quote?")
    print("   ...until they say STOP. The hand-typed number is matched too.")
    await inbound(ledger, "+13125550188", "opt out")
    await attempt(ledger, LEAD, "reply", text="Sorry to see you go.")

    print("5. Pat changes their mind and texts START.")
    await inbound(ledger, PAT, "START")
    await attempt(ledger, PAT, "order_update", order="1042", status="delivered")


if __name__ == "__main__":
    asyncio.run(main())
