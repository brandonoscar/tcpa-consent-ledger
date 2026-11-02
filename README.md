# tcpa-consent-ledger

[![CI](https://github.com/brandonoscar/tcpa-consent-ledger/actions/workflows/ci.yml/badge.svg)](https://github.com/brandonoscar/tcpa-consent-ledger/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

SMS consent that fails closed, for apps that text people through Twilio:

- **A consent ledger.** A phone number in your CRM is not consent. A text needs a consent record for that sender, with the exact language the person agreed to, unless it's a reply to a message they sent that sender in the last 24 hours.
- **STOP that always works.** One STOP blocks every sender, covers people who never consented (a lead who texted first), matches the number however it was typed, and is caught in everyday wording ("Stop.", "please stop", "unsubscribe me"). START restores only what STOP revoked, for the sender it was sent to.
- **A history that proves both directions.** Every grant, revocation, STOP and START is appended to an event log that is never updated or deleted.
- **Quiet hours on the recipient's clock, by state.** The federal 8 AM to 9 PM is a floor: Florida, Oklahoma, Washington and Maryland end at 8 PM, Connecticut runs 9 AM to 8 PM, and Texas starts at 9 AM (noon on Sunday). An unknown state gets the intersection of those rules.
- **Verified webhooks and strict templates.** Inbound requests must carry Twilio's signature, and a replayed one changes nothing. Every outbound text is a registered template with its blanks filled, and carries STOP language.

Standard library only (plus `tzdata` on Windows, which ships no time zone database); a Postgres store is an optional extra. Nothing in it is specific to any industry.

> **Not legal advice.** The rules here err strict and name the statutes they come from, but they are no substitute for your own counsel, and the list of state rules is not complete. The quiet-hours windows are configurable (`QuietHoursRules`) because laws change.

## Try it

```bash
git clone https://github.com/brandonoscar/tcpa-consent-ledger
cd tcpa-consent-ledger
pip install -e .
python examples/demo.py
```

```text
1. Pat agreed to texts on our web form.
    -> SENT to +14155550134: Acme: Order 1042 is shipped. Reply STOP to opt out, HELP for help.
2. A Miami customer: at noon Pacific it is 3 PM in Miami, which is fine...
    -> SENT to +13055550199: Acme: Order 2001 is delayed. Reply STOP to opt out, HELP for help.
   ...but at 5:30 PM Pacific it is 8:30 PM in Miami, past Florida's 8 PM.
    -> BLOCKED (quiet_hours): outside the recipient's local window (8:00 AM-8:00 PM)
3. Pat replies STOP through the signed webhook.
    <- +14155550134 texted 'Stop' (keyword: stop)
    -> BLOCKED (opted_out): this number replied STOP and has not opted back in
4. A lead texts us first, so a reply needs no consent row...
    <- +13125550188 texted 'Do you ship to Canada?' (keyword: None)
    -> SENT to +13125550188: Acme: We do. Want a quote? Reply STOP to opt out.
   ...until they say STOP. The hand-typed number is matched too.
    <- +13125550188 texted 'opt out' (keyword: stop)
    -> BLOCKED (opted_out): this number replied STOP and has not opted back in
5. Pat changes their mind and texts START.
    <- +14155550134 texted 'START' (keyword: start)
    -> SENT to +14155550134: Acme: Order 1042 is delivered. Reply STOP to opt out, HELP for help.
```

## Install

Not on PyPI yet. Install a release from GitHub:

```bash
pip install "tcpa-consent-ledger @ git+https://github.com/brandonoscar/tcpa-consent-ledger@v0.1.0"
```

For the Postgres store, add the extra after the name: `tcpa-consent-ledger[postgres] @ git+...`.

## Usage

```python
from consent_ledger import ConsentLedger, Template, TemplateRegistry, TwilioTransport, send_sms
from consent_ledger.postgres import PostgresConsentStore

ledger = ConsentLedger(PostgresConsentStore(pool))
templates = TemplateRegistry(
    {
        "order_update": Template(
            "Acme: Order {order} is {status}. Reply STOP to opt out, HELP for help."
        ),
    }
)

# When someone opts in (your web form, a signed paper form):
await ledger.grant(
    "acme",
    phone,
    source="web_form",
    language=CONSENT_TEXT_THEY_SAW,
    evidence={"ip": ip, "user_agent": ua},
)

# Sending runs every check first and raises SmsBlocked with a reason:
await send_sms(
    ledger=ledger,
    transport=TwilioTransport(account_sid=..., auth_token=..., from_number="+18885550100"),
    templates=templates,
    sender="acme",
    to=phone,
    template_id="order_update",
    params={"order": "1042", "status": "shipped"},
    enabled=CAMPAIGN_APPROVED,
)
```

Your inbound webhook (FastAPI shown; any framework works):

```python
from consent_ledger import EMPTY_TWIML, WebhookRejected, handle_inbound_webhook


@app.post("/sms/inbound")
async def inbound(request: Request):
    params = dict(await request.form())
    try:
        message = await handle_inbound_webhook(
            ledger,
            auth_token=os.environ["TWILIO_AUTH_TOKEN"],
            url="https://example.com/sms/inbound",  # the URL in the Twilio console, not request.url
            params=params,
            signature=request.headers.get("X-Twilio-Signature"),
            sender_for_number=sender_for,  # your To-number -> sender lookup
        )
    except WebhookRejected:
        return Response(status_code=403)
    # message.keyword is "stop", "start", "help" or None (an ordinary message);
    # message.replayed is True for a MessageSid seen before, which changed nothing.
    return Response(EMPTY_TWIML, media_type="text/xml")
```

The ledger's history of a number, oldest first:

```python
for event in await ledger.history(phone):
    # kind: granted, revoked, restored, opted_out or opted_in; sender is None
    # for an opt-out or opt-in, which is about the person, not one sender.
    print(event.at, event.kind, event.sender, event.source, event.detail)
```

## What a check decides, in order

| Reason | When |
|---|---|
| `unverifiable` | the number can't be parsed, or the store couldn't be read. "We couldn't check" never means "so we sent it". |
| `opted_out` | the number said STOP, to any sender. Checked first, so the refusal never reads "try later". |
| `unsupported_region` | not a +1 number: a US campaign can't text it at all. |
| `no_consent` / `revoked` | no consent from this person for this sender. |
| `no_conversation` | a reply (`conversational=True`) with no message from this person to this sender in the last 24 hours. |
| `quiet_hours` | outside the recipient's local window; the detail names the window applied. |

`send_sms` also refuses with `disabled` until you pass `enabled=True`: ship dark, and turn texting on when the carriers approve your 10DLC campaign.

## Rules worth knowing

- **Consent is per sender; opt-out is per person.** If you send for several brands or customers, each needs its own consent, but one STOP silences all of them. An over-broad block is a customer-service problem; a missed one is a violation.
- **Nothing is deleted, and nothing is lost.** The current consent and opt-out rows change; the event log behind them only grows (`ledger.history(phone)`), and on Postgres a trigger refuses any UPDATE, DELETE or TRUNCATE on it. A second STOP keeps the original opt-out date (the evidence of how long you'd been told to stop) and is logged too, and a STOP after a START blocks again.
- **START never creates consent.** It restores only consent a STOP revoked, and only for the sender the START was sent to. A sender's own revocation (a settings page) stays revoked, and someone who never opted in is still not opted in. YES is not START: it's the commonest answer to an ordinary question.
- **STOP errs broad.** The FCC lets a person revoke by any reasonable means. STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT, REVOKE and OPT OUT count in any case, punctuation or width, as do the Spanish ALTO, PARAR and CANCELAR. So does a message whose first word is one of them, a message of four words or fewer that contains one, or one that asks in so many words ("stop sending these", "remove me"). Twilio's `OptOutType` counts too. A longer message that merely uses the word ("Can I cancel my order?") is for a person to read.
- **A reply needs a conversation.** `conversational=True` skips the consent check only when the person wrote to that sender within `conversation_window` (24 hours by default). No statute sets that window; it's a policy choice, so set your own.
- **Every spelling of a number is checked.** A STOP that arrives as `+14155550134` blocks `(415) 555-0134` typed into a CRM, and digits in any script (full-width ones from a phone keyboard) are the same number. Across spellings the latest change wins. A foreign number is never given US spellings, so `+44 20 7946 0134` can't block the US number that shares its last ten digits.
- **A recorded time zone adds a check; it never removes one.** The area code's clock still counts, and the recorded zone is read with the unknown-state rule, because `America/Chicago` covers Texas and Illinois and only Texas has a Sunday rule. A wrong zone, like a server's UTC default, can then only block a text. Zones that name no place (`UTC`, `EST`) are refused when consent is recorded.
- **Where the area code puts someone.** A single-zone state or territory (Guam, Puerto Rico...) gets its zone. A state that spans zones (Florida's 850, Indiana, Michigan, Arizona's 928 with the Navajo Nation's daylight time) is checked on both the Eastern and Pacific clocks, Alaska on both of its own. An area code the table doesn't know (Canada, the Caribbean, a new overlay) gets only the hours that are legal from Newfoundland to Hawaii, a few a day; add the ones you text with `QuietHoursRules(area_codes=...)`. A missing area code costs hours; a wrong one costs an illegal text.
- **Your own state rules add to the defaults.** `QuietHoursRules(states=...)` merges over the built-in ones, so adding Connecticut can't drop Florida, and the unknown-state window is recomputed as the intersection of all of them.
- **The webhook URL you verify against is the one in the Twilio console.** A proxy can rewrite the request's own URL. An unsigned request could forge a START, which is forged consent, and a signed one replayed later could undo a newer STOP, so a `MessageSid` seen before changes nothing. A failing To-number lookup still records the STOP.
- **The transport never follows a redirect.** Following one would send your account's credentials to whatever host it names. An answer without a message id is an error.
- **Templates are strict.** Unknown or missing placeholders, line breaks in a value (Unicode's line and paragraph separators too) and a free-text blank carrying its own "reply START" are all errors. What code can't judge is whether the prose in a free-text blank fits your registered use case, so keep those blanks few and put a person in front of them.

## Storage

`InMemoryConsentStore` is for tests and demos. `PostgresConsentStore` (the `postgres` extra) creates four tables from `consent_ledger.postgres.SCHEMA_SQL`: current consent and opt-outs, the event log, and inbound messages' metadata (never their text). The two logs are append-only, enforced by triggers. Row-level security is on for all four with no policies, and Supabase's `anon` and `authenticated` roles get no grants, so the public API key can neither read a number nor lift an opt-out; your server connects as the owner or a role that bypasses RLS (the service role). Any other database works by implementing the `ConsentStore` protocol; the test suite runs every ledger test against both stores, so a third one can reuse it.

## Development

```bash
pip install -e ".[dev]"
pytest                                   # Postgres tests skip without a database
CONSENT_LEDGER_TEST_DSN=postgresql://postgres@localhost:5432/postgres pytest
ruff check . && ruff format --check . && mypy
```

Pull requests are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Report security problems privately, as [SECURITY.md](SECURITY.md) describes, not in an issue. Changes are listed in [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE).
