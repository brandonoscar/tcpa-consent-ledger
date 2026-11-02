"""SMS consent that fails closed.

A consent and opt-out ledger, STOP/START/HELP handling, recipient-local
quiet hours with per-state rules, strict registered templates, and Twilio
webhook verification and sending. Not legal advice: see the README.
"""

from consent_ledger.keywords import classify
from consent_ledger.ledger import (
    DEFAULT_CONVERSATION_WINDOW,
    ConsentLedger,
    Decision,
    InboundResult,
)
from consent_ledger.phone import normalize_e164, phones_match, redact, spellings
from consent_ledger.quiet_hours import (
    DEFAULT_RULES,
    DEFAULT_STATE_RULES,
    QuietHoursRules,
    StateRule,
    describe_window,
    may_send_now,
    validate_zone,
)
from consent_ledger.sender import SmsBlocked, send_sms
from consent_ledger.stores import (
    ConsentEvent,
    ConsentRecord,
    ConsentStore,
    InboundRecord,
    InMemoryConsentStore,
    OptOutRecord,
)
from consent_ledger.templates import Template, TemplateRegistry
from consent_ledger.twilio import (
    EMPTY_TWIML,
    SendReceipt,
    TwilioTransport,
    WebhookRejected,
    handle_inbound_webhook,
    validate_signature,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_CONVERSATION_WINDOW",
    "DEFAULT_RULES",
    "DEFAULT_STATE_RULES",
    "EMPTY_TWIML",
    "ConsentEvent",
    "ConsentLedger",
    "ConsentRecord",
    "ConsentStore",
    "Decision",
    "InMemoryConsentStore",
    "InboundRecord",
    "InboundResult",
    "OptOutRecord",
    "QuietHoursRules",
    "SendReceipt",
    "SmsBlocked",
    "StateRule",
    "Template",
    "TemplateRegistry",
    "TwilioTransport",
    "WebhookRejected",
    "__version__",
    "classify",
    "describe_window",
    "handle_inbound_webhook",
    "may_send_now",
    "normalize_e164",
    "phones_match",
    "redact",
    "send_sms",
    "spellings",
    "validate_signature",
    "validate_zone",
]
