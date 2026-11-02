# Security policy

## Reporting a vulnerability

Report it privately on GitHub: open the **Security** tab and choose **Report a vulnerability**. Please don't open a public issue for it.

Say what you ran, what happened and what should have happened. Expect a first reply within a week. Once a fix ships, the advisory is published and credits you, unless you'd rather not be named.

## What counts

- A message that `check` allows to a number that opted out, never consented, or is inside quiet hours.
- A reply that should opt someone out and doesn't.
- A forged Twilio webhook that passes signature validation.
- A template that renders text it should have refused.

## Supported versions

Only the latest release gets fixes.
