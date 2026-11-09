# Contributing

Thanks for helping. This project is small on purpose, so the bar for a change is that it keeps the proxy simple, local
and fail-closed.

## Set up

```bash
git clone https://github.com/rmpmrepo1278/pii-scrubber.git && cd pii-scrubber
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
pytest -q
python examples/smoke.py
```

Python 3.11 or newer is required (the gitleaks rules are read with `tomllib`).

## Ground rules

- **Synthetic data only.** Tests, examples, issues and screenshots must use invented values: `example.com` / `.invalid`
  domains, 555-01xx phone numbers, documentation IP ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24). Never paste
  a real request log or vault.
- **Fail closed stays the default.** A change must not let an unchecked body reach the upstream. If something cannot be
  scrubbed, it is blocked.
- **No new runtime dependencies** without an issue first. Three is the budget today.
- **Every behaviour change has a test** in `tests/`. Detection changes should add a synthetic case.
- **Describe what the code does.** README claims must match the code; limits are documented, not hidden.

## Pull requests

1. Open an issue for anything bigger than a small fix, so we can agree on the approach.
2. Keep the PR focused. Update `CHANGELOG.md` under "Unreleased".
3. Make sure `pytest -q` and `python examples/smoke.py` pass locally; CI runs the tests on Python 3.11 to 3.13.

## Reporting security issues

Do not open a public issue for a leak or bypass. See [SECURITY.md](SECURITY.md).

By contributing you agree that your contributions are licensed under the MIT License and that you follow the
[Code of Conduct](CODE_OF_CONDUCT.md).
