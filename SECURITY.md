# Security policy

swapback is a privacy control, so a bypass is a security bug: any way for a known value or a detected pattern to
reach the upstream unscrubbed, a request forwarded after a scrub error, a real value restored into a tool call that the README says keeps its
placeholders, or a restore that leaks one user's value into another context.

## Supported versions

| Version | Supported |
|---|---|
| 0.4.x | Yes (fixes go into the latest 0.4 release) |
| 0.3.x | No (please upgrade to the latest 0.4 release) |
| 0.2.x | No (0.3.0 keeps placeholders in egress tool calls; please upgrade) |
| 0.1.x (as pii-scrubber) | No |

## Reporting a vulnerability

Please report privately, not in a public issue:

1. Go to the repository's **Security** tab and choose **Report a vulnerability** (GitHub private vulnerability
   reporting).
   If the button is not there yet (it appears once private reporting is switched on for the public repository),
   open a public issue that only asks for a private contact, with no details of the problem.
2. Include the version or commit, your config with **synthetic values only**, a minimal request body that reproduces it,
   and what reached the upstream (the mock upstream in `examples/mock_upstream.py` records it in `mock_seen.jsonl`).

Never send real personal data, real API keys or a real `vault.json` in a report.

You can expect an acknowledgement within 7 days and a fix or a plan within 30 days for confirmed issues. We will credit
you in the changelog unless you prefer not to be named.

## Scope notes

Documented limits are not vulnerabilities on their own: names that were never listed (there is no NER), formats no
pattern covers, streaming formats the docs do not list as restored (such as legacy chat `function_call` deltas), tool calls the
restore policy lets through by design (see "Tool calls" in [docs/guide.md](docs/guide.md#tool-calls)), and non-JSON bodies. A case where the code does not do what the README or the docs say
it does is in scope.

## What swapback does not protect against

swapback reduces what a model provider sees. It is not a sandbox and not a guarantee of anonymity. It does not protect
against:

- **Values you did not list and no pattern covers.** There is no NER model: an unlisted name in free text, most
  non-US national IDs and addresses, and free-form secrets go out as written. See [docs/limits.md](docs/limits.md).
- **Re-encoded values.** Base64, hex, rot13 and similar transforms of a listed value are not decoded before sending.
- **Inference from context.** A placeholder hides the value, not the facts around it. A provider can still read the
  rest of the prompt, and a unique enough context can identify a person.
- **Anyone who can reach the proxy port or read your state directory.** The proxy has no authentication of its own,
  and `vault.json` holds the real values (mode `0600`). Keep it on loopback and treat the state directory as
  sensitive.
- **Tools you allow.** A tool on the `tool_restore` allow list gets real values back by design; a prompt-injected model
  can use it as you configured it.
- **Traffic that does not go through it.** Other clients, other APIs, telemetry, and requests your agent makes on its
  own are outside its view.
