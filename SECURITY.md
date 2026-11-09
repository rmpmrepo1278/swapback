# Security policy

pii-scrubber is a privacy control, so a bypass is a security bug: any way for a known value or a detected pattern to
reach the upstream unscrubbed, a request forwarded after a scrub error, or a restore that leaks one user's value into
another context.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Reporting a vulnerability

Please report privately, not in a public issue:

1. Go to the repository's **Security** tab and choose **Report a vulnerability** (GitHub private vulnerability
   reporting).
2. Include the version or commit, your config with **synthetic values only**, a minimal request body that reproduces it,
   and what reached the upstream (the mock upstream in `examples/mock_upstream.py` records it in `mock_seen.jsonl`).

Never send real personal data, real API keys or a real `vault.json` in a report.

You can expect an acknowledgement within 7 days and a fix or a plan within 30 days for confirmed issues. We will credit
you in the changelog unless you prefer not to be named.

## Scope notes

Documented limits are not vulnerabilities on their own: names that were never listed (there is no NER), formats no
pattern covers, non-OpenAI streaming formats, and non-JSON bodies. A case where the code does not do what the README says
it does is in scope.
