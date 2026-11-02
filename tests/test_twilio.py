from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from consent_ledger import ConsentLedger, InMemoryConsentStore
from consent_ledger.twilio import (
    TwilioSendError,
    TwilioTransport,
    WebhookRejected,
    compute_signature,
    handle_inbound_webhook,
    validate_signature,
)

TOKEN = "test-auth-token-not-real"
URL = "https://example.com/sms/inbound"
PARAMS = {
    "From": "+14155550134",
    "To": "+18885550100",
    "Body": "STOP",
    "MessageSid": "SM00000000000000000000000000000001",
}


def test_signature_round_trip_and_tampering() -> None:
    signature = compute_signature(TOKEN, URL, PARAMS)
    assert validate_signature(auth_token=TOKEN, url=URL, params=PARAMS, signature=signature)
    assert not validate_signature(
        auth_token=TOKEN, url=URL, params={**PARAMS, "Body": "START"}, signature=signature
    )
    assert not validate_signature(
        auth_token=TOKEN, url=URL + "?x=1", params=PARAMS, signature=signature
    )
    assert not validate_signature(auth_token="other", url=URL, params=PARAMS, signature=signature)


@pytest.mark.parametrize(
    ("token", "url", "signature"),
    [("", URL, "sig"), (TOKEN, "", "sig"), (TOKEN, URL, None), (TOKEN, URL, "")],
)
def test_missing_inputs_are_invalid_not_errors(token: str, url: str, signature: str | None) -> None:
    assert not validate_signature(auth_token=token, url=url, params=PARAMS, signature=signature)


@pytest.mark.parametrize("signature", ["\u00e9", "s\u00efgnature", "\ud800"])
def test_a_non_ascii_signature_is_invalid_not_an_error(signature: str) -> None:
    # Finding 11: hmac.compare_digest raised TypeError on a non-ASCII header.
    assert not validate_signature(auth_token=TOKEN, url=URL, params=PARAMS, signature=signature)


def test_the_signature_recipe() -> None:
    # URL, then each key and value in sorted key order, HMAC-SHA1, base64.
    import hashlib
    import hmac

    payload = (
        URL + "BodySTOPFrom+14155550134MessageSidSM00000000000000000000000000000001To+18885550100"
    )
    expected = base64.b64encode(
        hmac.new(TOKEN.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()
    assert compute_signature(TOKEN, URL, PARAMS) == expected


async def test_a_bad_signature_is_rejected_before_anything_is_read() -> None:
    ledger = ConsentLedger(InMemoryConsentStore())
    with pytest.raises(WebhookRejected):
        await handle_inbound_webhook(
            ledger,
            auth_token=TOKEN,
            url=URL,
            params=PARAMS,
            signature="forged",
            sender_for_number=lambda number: "acme",
        )
    assert not await ledger.store.is_opted_out(["+14155550134"])


async def test_a_signed_stop_to_an_unknown_number_is_still_honoured() -> None:
    ledger = ConsentLedger(InMemoryConsentStore())
    message = await handle_inbound_webhook(
        ledger,
        auth_token=TOKEN,
        url=URL,
        params=PARAMS,
        signature=compute_signature(TOKEN, URL, PARAMS),
        sender_for_number=lambda number: None,
    )
    assert message.keyword == "stop"
    assert message.sender is None
    assert (await ledger.check("acme", "+14155550134", conversational=True)).reason == "opted_out"


async def test_a_failing_sender_lookup_still_honours_the_stop() -> None:
    # Finding 7: a lookup that raised lost the STOP.
    def lookup(number: str) -> str | None:
        raise ConnectionError("routing service down")

    ledger = ConsentLedger(InMemoryConsentStore())
    message = await handle_inbound_webhook(
        ledger,
        auth_token=TOKEN,
        url=URL,
        params=PARAMS,
        signature=compute_signature(TOKEN, URL, PARAMS),
        sender_for_number=lookup,
    )
    assert (message.keyword, message.sender) == ("stop", None)
    assert await ledger.store.is_opted_out(["+14155550134"])


async def test_twilios_opt_out_type_is_honoured() -> None:
    params = {**PARAMS, "Body": "thanks, that is everything for today", "OptOutType": "STOP"}
    ledger = ConsentLedger(InMemoryConsentStore())
    message = await handle_inbound_webhook(
        ledger,
        auth_token=TOKEN,
        url=URL,
        params=params,
        signature=compute_signature(TOKEN, URL, params),
        sender_for_number=lambda number: "acme",
    )
    assert message.keyword == "stop"
    assert await ledger.store.is_opted_out(["+14155550134"])


async def test_a_replayed_signed_start_is_ignored() -> None:
    # Finding 11: replaying a signed START after a newer STOP lifted the block.
    ledger = ConsentLedger(InMemoryConsentStore())
    start = {**PARAMS, "Body": "START", "MessageSid": "SM0"}
    stop = {**PARAMS, "Body": "STOP", "MessageSid": "SM1"}
    for params in (start, stop, start):
        message = await handle_inbound_webhook(
            ledger,
            auth_token=TOKEN,
            url=URL,
            params=params,
            signature=compute_signature(TOKEN, URL, params),
            sender_for_number=lambda number: "acme",
        )
    assert message.replayed
    assert await ledger.store.is_opted_out(["+14155550134"])


async def test_an_async_sender_lookup() -> None:
    async def lookup(number: str) -> str | None:
        return {"+18885550100": "acme"}.get(number)

    params = {**PARAMS, "Body": "where is my order?"}
    message = await handle_inbound_webhook(
        ConsentLedger(InMemoryConsentStore()),
        auth_token=TOKEN,
        url=URL,
        params=params,
        signature=compute_signature(TOKEN, URL, params),
        sender_for_number=lookup,
    )
    assert (message.sender, message.keyword) == ("acme", None)


class FakeTwilio(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []  # noqa: RUF012
    reply: tuple[int, dict[str, Any]] = (201, {"sid": "SM123", "status": "queued"})
    location: str | None = None

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        FakeTwilio.requests.append(
            {
                "path": self.path,
                "auth": self.headers["Authorization"],
                "form": dict(urllib.parse.parse_qsl(self.rfile.read(length).decode())),
            }
        )
        status, body = FakeTwilio.reply
        payload = json.dumps(body).encode()
        self.send_response(status)
        if FakeTwilio.location:
            self.send_header("Location", FakeTwilio.location)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def fake_twilio() -> Iterator[str]:
    FakeTwilio.requests = []
    server = HTTPServer(("127.0.0.1", 0), FakeTwilio)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/2010-04-01"
    finally:
        server.shutdown()


async def test_the_transport_posts_the_message(fake_twilio: str) -> None:
    FakeTwilio.reply = (201, {"sid": "SM123", "status": "queued"})
    transport = TwilioTransport(
        account_sid="AC_test", auth_token=TOKEN, from_number="+18885550100", api_base=fake_twilio
    )
    receipt = await transport.send(to="+14155550134", body="Acme: hi. Reply STOP to opt out.")
    assert (receipt.sid, receipt.status) == ("SM123", "queued")
    request = FakeTwilio.requests[0]
    assert request["path"] == "/2010-04-01/Accounts/AC_test/Messages.json"
    assert request["auth"] == "Basic " + base64.b64encode(f"AC_test:{TOKEN}".encode()).decode()
    assert request["form"] == {
        "To": "+14155550134",
        "From": "+18885550100",
        "Body": "Acme: hi. Reply STOP to opt out.",
    }


async def test_a_twilio_error_names_the_code_and_never_the_token(fake_twilio: str) -> None:
    FakeTwilio.reply = (400, {"code": 21211, "message": "Invalid 'To' Phone Number"})
    transport = TwilioTransport(
        account_sid="AC_test",
        auth_token=TOKEN,
        messaging_service_sid="MG_test",
        api_base=fake_twilio,
    )
    with pytest.raises(TwilioSendError) as error:
        await transport.send(to="+10000000000", body="x")
    assert "21211" in str(error.value)
    assert TOKEN not in str(error.value)
    assert TOKEN not in repr(transport)
    assert FakeTwilio.requests[0]["form"]["MessagingServiceSid"] == "MG_test"


class Sink(BaseHTTPRequestHandler):
    """Another host. It must never see the account's credentials."""

    seen: list[str | None] = []  # noqa: RUF012

    def do_GET(self) -> None:
        Sink.seen.append(self.headers.get("Authorization"))
        payload = json.dumps({"sid": "SM-elsewhere", "status": "queued"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_POST = do_GET

    def log_message(self, *args: Any) -> None:
        pass


async def test_a_redirect_is_never_followed_with_the_credentials(fake_twilio: str) -> None:
    # Finding 12: urllib followed a 302 and resent the Authorization header to
    # the host it named, then reported success with an empty sid.
    Sink.seen = []
    sink = HTTPServer(("127.0.0.1", 0), Sink)
    thread = threading.Thread(target=sink.serve_forever, daemon=True)
    thread.start()
    try:
        FakeTwilio.reply = (302, {})
        FakeTwilio.location = f"http://127.0.0.1:{sink.server_port}/elsewhere"
        transport = TwilioTransport(
            account_sid="AC_test",
            auth_token=TOKEN,
            from_number="+18885550100",
            api_base=fake_twilio,
        )
        with pytest.raises(TwilioSendError, match="302"):
            await transport.send(to="+14155550134", body="x")
        assert Sink.seen == []
    finally:
        FakeTwilio.location = None
        sink.shutdown()


async def test_an_answer_without_a_message_sid_is_an_error(fake_twilio: str) -> None:
    FakeTwilio.reply = (200, {"status": "queued"})
    transport = TwilioTransport(
        account_sid="AC_test", auth_token=TOKEN, from_number="+18885550100", api_base=fake_twilio
    )
    with pytest.raises(TwilioSendError, match="no message sid"):
        await transport.send(to="+14155550134", body="x")


def test_a_transport_needs_credentials() -> None:
    with pytest.raises(ValueError, match="required"):
        TwilioTransport(account_sid="", auth_token=TOKEN)
