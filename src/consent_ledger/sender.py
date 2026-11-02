"""Send one text, after every check, in the right order.

1. The channel is switched on. Ship dark: on a new 10DLC campaign, nothing
   should leave before the carriers approve it, so ``enabled`` defaults to
   off and is checked before any lookup or network call.
2. The ledger's decision for this sender and recipient (opt-out, region,
   consent, quiet hours) at the ledger's own clock, which fails closed. There
   is no way to pass another time: a check at a scheduled time says nothing
   about the moment the text actually leaves.
3. The template renders strictly.
4. The transport sends. Logging afterwards is best-effort: the message has
   already left, so a logging failure must not report a failed send.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

from consent_ledger.ledger import ConsentLedger
from consent_ledger.phone import normalize_e164, redact
from consent_ledger.templates import TemplateRegistry
from consent_ledger.twilio import SendReceipt

logger = logging.getLogger(__name__)


class Transport(Protocol):
    async def send(self, *, to: str, body: str, from_number: str | None = None) -> SendReceipt: ...


class SmsBlocked(Exception):
    """Refused before any network call. ``reason`` is stable for code to read;
    ``detail`` is the sentence to show a person."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"SMS blocked ({reason}){': ' + detail if detail else ''}")


SendLog = Callable[[str, str, str, SendReceipt], Awaitable[None]]


async def send_sms(
    *,
    ledger: ConsentLedger,
    transport: Transport,
    templates: TemplateRegistry,
    sender: str,
    to: str,
    template_id: str,
    params: Mapping[str, str],
    enabled: bool = False,
    conversational: bool = False,
    from_number: str | None = None,
    log: SendLog | None = None,
) -> SendReceipt:
    """Send a templated text from ``sender`` to ``to``, or raise :class:`SmsBlocked`.

    The log callback receives ``(sender, phone, template_id, receipt)``, never
    the rendered body: it can be rebuilt from the template, and storing it
    copies personal data at rest.
    """
    if not enabled:
        raise SmsBlocked("disabled", "texting is switched off until the campaign is approved")
    decision = await ledger.check(sender, to, conversational=conversational)
    if not decision.allowed:
        logger.info("sms to %s blocked: %s", redact(to), decision.reason)
        raise SmsBlocked(decision.reason or "blocked", decision.detail)
    body = templates.render(template_id, params)
    canonical = normalize_e164(to) or to
    receipt = await transport.send(to=canonical, body=body, from_number=from_number)
    if log is not None:
        try:
            await log(sender, canonical, template_id, receipt)
        except Exception:
            logger.warning("sms sent (%s) but logging it failed", receipt.sid, exc_info=True)
    return receipt


__all__ = ["SendLog", "SmsBlocked", "Transport", "send_sms"]
