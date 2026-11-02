# Changelog

Notable changes, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [semantic versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-02

First release.

### Added

- `ConsentLedger`: grant, revoke, opt out and opt back in, and a `check` that says whether a message may go and, if not, why.
- STOP, START and HELP handling for inbound replies: STOP in everyday wording and Twilio's `OptOutType`; START restores only what STOP revoked, for the sender it was sent to.
- An append-only history of every grant, revocation, opt-out and opt-in (`ConsentLedger.history`).
- Replies in a conversation need a recent inbound message from the person (`conversation_window`); a replayed message id changes nothing.
- Quiet hours on the recipient's clock, with stricter state rules, the US territories' own zones, and the hours legal everywhere when the area code is unknown. A recorded time zone adds a check.
- Phone number normalisation to E.164, reading digits in any script.
- Message templates checked when registered (`TemplateRegistry`).
- Twilio: signature validation, an inbound webhook handler and a small REST transport that never follows a redirect. `send_sms` sends nothing until it is enabled.
- An in-memory store and a Postgres store with row-level security and append-only logs.

[Unreleased]: https://github.com/brandonoscar/tcpa-consent-ledger/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/brandonoscar/tcpa-consent-ledger/releases/tag/v0.1.0
