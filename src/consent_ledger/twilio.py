"""Twilio: verify inbound webhooks, apply STOP/START, send through the REST API.

Standard library only (no Twilio SDK).

**Webhook signatures.** Twilio signs each webhook: the full URL as configured
in the console, followed by every POST parameter in sorted key order (key then
value, no separators), HMAC-SHA1 with your auth token, base64. Verify against
the URL you configured, never the one the request claims: a proxy can rewrite
the latter. A missing token, URL or header, or one that is not ASCII, is
invalid, never an exception. An unsigned inbound request could forge a START,
which is forged consent; a signed one replayed later could undo a newer STOP,
so the ledger refuses a ``MessageSid`` it has seen before.

**Inbound routing.** The ``To`` number says which of your senders the message
is for. When it maps to none, or the lookup fails, the STOP is still honoured
(an opt-out is about the person, not about you), recorded as received by that
number. Twilio's ``OptOutType`` parameter, when it says STOP, makes the
message a STOP whatever the body says.

**Sending.** The transport never follows a redirect: it would carry your
account's credentials to whatever host the redirect names. An answer without
a message id is an error, not a quiet success.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from http.client import HTTPMessage
from typing import IO

from consent_ledger.keywords import Keyword
from consent_ledger.ledger import ConsentLedger
from consent_ledger.phone import redact

logger = logging.getLogger(__name__)

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response/>'
API_BASE = "https://api.twilio.com/2010-04-01"


def compute_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    payload = url + "".join(f"{key}{params[key] or ''}" for key in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def validate_signature(
    *, auth_token: str, url: str, params: Mapping[str, str], signature: str | None
) -> bool:
    """True only when ``signature`` is Twilio's for this URL and these params."""
    if not auth_token or not url or not signature:
        return False
    expected = compute_signature(auth_token, url, params).encode()
    # Compare bytes: a non-ASCII header is a mismatch, not a TypeError.
    return hmac.compare_digest(expected, signature.encode("utf-8", errors="replace"))


class WebhookRejected(Exception):
    """Answer the request with 403 and do nothing else."""

    status = 403


@dataclass(frozen=True)
class InboundMessage:
    from_number: str
    to_number: str
    body: str
    message_sid: str
    #: Your sender for the ``To`` number, or ``None`` when it maps to none.
    sender: str | None
    keyword: Keyword | None
    #: The ``MessageSid`` was seen before; the ledger changed nothing.
    replayed: bool = False


SenderLookup = Callable[[str], str | None] | Callable[[str], Awaitable[str | None]]


async def handle_inbound_webhook(
    ledger: ConsentLedger,
    *,
    auth_token: str,
    url: str,
    params: Mapping[str, str],
    signature: str | None,
    sender_for_number: SenderLookup,
) -> InboundMessage:
    """Verify, route by ``To``, and apply STOP/START. Raises
    :class:`WebhookRejected` for a bad signature, before anything is read."""
    if not validate_signature(auth_token=auth_token, url=url, params=params, signature=signature):
        raise WebhookRejected("invalid Twilio signature")
    to_number = (params.get("To") or "").strip()
    from_number = (params.get("From") or "").strip()
    body = params.get("Body") or ""
    message_sid = params.get("MessageSid") or ""
    sender: str | None
    try:
        found = sender_for_number(to_number)
        sender = await found if inspect.isawaitable(found) else found
    except Exception:
        # The STOP still counts; it is recorded as received by the number.
        logger.warning("sender lookup for %s failed", redact(to_number), exc_info=True)
        sender = None
    result = await ledger.handle_inbound(
        from_number=from_number,
        body=body,
        received_by=sender or f"number:{to_number}",
        message_sid=message_sid,
        opt_out_type=params.get("OptOutType"),
    )
    return InboundMessage(
        from_number, to_number, body, message_sid, sender, result.keyword, result.replayed
    )


@dataclass(frozen=True)
class SendReceipt:
    sid: str
    status: str
    to: str


class TwilioSendError(RuntimeError):
    """Twilio refused or failed the send. Carries Twilio's code and message,
    never the auth token."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: following one would resend the Authorization
    header to whatever host it names."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


class TwilioTransport:
    """Sends one message through Twilio's REST API.

    Give either ``from_number`` or ``messaging_service_sid``. Each of your
    senders should text from its own number, so a reply comes back to a
    number that routes to the same sender; a shared number mixes two
    customers' conversations in both directions.
    """

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        from_number: str | None = None,
        messaging_service_sid: str | None = None,
        status_callback: str | None = None,
        timeout_s: float = 15.0,
        api_base: str = API_BASE,
    ) -> None:
        if not account_sid or not auth_token:
            raise ValueError("account_sid and auth_token are required")
        self._sid = account_sid
        self._token = auth_token
        self.from_number = from_number
        self.messaging_service_sid = messaging_service_sid
        self.status_callback = status_callback
        self.timeout_s = timeout_s
        self.api_base = api_base.rstrip("/")

    def __repr__(self) -> str:  # never print the token
        return f"TwilioTransport(account_sid={self._sid!r}, from_number={self.from_number!r})"

    async def send(self, *, to: str, body: str, from_number: str | None = None) -> SendReceipt:
        return await asyncio.to_thread(self._send, to, body, from_number)

    def _send(self, to: str, body: str, from_number: str | None) -> SendReceipt:
        form = {"To": to, "Body": body}
        sender = from_number or self.from_number
        if sender:
            form["From"] = sender
        elif self.messaging_service_sid:
            form["MessagingServiceSid"] = self.messaging_service_sid
        else:
            raise ValueError("no sender: pass from_number or messaging_service_sid")
        if self.status_callback:
            form["StatusCallback"] = self.status_callback
        request = urllib.request.Request(
            f"{self.api_base}/Accounts/{self._sid}/Messages.json",
            data=urllib.parse.urlencode(form).encode(),
            method="POST",
            headers={
                "Authorization": "Basic "
                + base64.b64encode(f"{self._sid}:{self._token}".encode()).decode(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with _OPENER.open(request, timeout=self.timeout_s) as response:
                data = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode(errors="replace")
            try:
                parsed = json.loads(detail)
                detail = f"code {parsed.get('code')}: {parsed.get('message')}"
            except ValueError:
                pass
            raise TwilioSendError(f"Twilio returned {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TwilioSendError(f"could not reach Twilio: {exc}") from None
        except ValueError:
            raise TwilioSendError("Twilio's answer was not JSON") from None
        sid = data.get("sid") if isinstance(data, dict) else None
        if not sid:
            # Without a message id there is no proof Twilio took the message.
            raise TwilioSendError("Twilio's answer carried no message sid")
        return SendReceipt(sid=str(sid), status=str(data.get("status", "")), to=to)


__all__ = [
    "API_BASE",
    "EMPTY_TWIML",
    "InboundMessage",
    "SendReceipt",
    "TwilioSendError",
    "TwilioTransport",
    "WebhookRejected",
    "compute_signature",
    "handle_inbound_webhook",
    "validate_signature",
]
