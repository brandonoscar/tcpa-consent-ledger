# Contributing

Bug reports and pull requests are welcome. For anything bigger than a fix, open an issue first so we can agree on the approach before you build it.

## Set up

```bash
git clone https://github.com/brandonoscar/tcpa-consent-ledger
cd tcpa-consent-ledger
pip install -e ".[dev]"
```

## Before you open a pull request

```bash
ruff check . && ruff format --check . && mypy
pytest
```

The Postgres store's tests skip without a database. To run them, point `CONSENT_LEDGER_TEST_DSN` at a throwaway Postgres 16 (`postgresql://postgres@localhost:5432/postgres`). CI runs them on every pull request.

- **A change comes with its tests.** A bug fix includes a test that fails without it.
- **Fail closed.** When the code can't tell whether something is safe, it refuses. An unknown is never a pass.
- **Keep the dependencies where they are.** The core has no dependencies. Anything new goes in an extra (like `postgres`) and is imported only by the module that needs it.
- **No real personal data**, in tests, examples or issues: `example.com` addresses, 555-01xx phone numbers, made-up names.
- **Add a line to `CHANGELOG.md`** under Unreleased.

## Versions

Releases follow [semantic versioning](https://semver.org/). Before 1.0, a minor release may change the API, and the changelog says how.
