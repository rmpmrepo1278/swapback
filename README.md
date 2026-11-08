# pii-scrubber

A small local proxy that keeps personal data out of LLM requests. It sits between any OpenAI-compatible client and its
provider, swaps known values and detected PII for stable placeholders like `«PERSON_1»` before the request leaves your
machine, and puts the real values back in the response, including streamed responses and tool-call arguments.

```
your app / agent  ->  pii-scrubber (127.0.0.1:8787)  ->  any OpenAI-compatible API
                  <-  real values restored            <-  sees «PERSON_1», «PHONE_1», ...
```

## What it does

- **Swaps before the call.** Values you list (names, emails, phones, addresses, IDs) plus what the detectors find:
  phone numbers (`phonenumbers`), email / credit card (Luhn) / IBAN / US SSN / IP / profile URLs (Presidio's predefined
  recognizer patterns), API keys and tokens (the gitleaks default rules), and a few local rules (street addresses,
  keyword-anchored account numbers and dates of birth, order numbers).
- **Custom private terms.** Codenames and internal figures that are not PII but must not leave: phrases or regexes become
  `«PRIVATE_TERM_n»`, and money amounts are matched in every written form (`$2.47B`, `2.47 billion`, `$2,470M`,
  `USD 2,470,000,000`) as `«PRIVATE_FIGURE_n»`.
- **Restores after.** Placeholders in the reply are swapped back to the exact original text, case included. Streams are
  restored chunk by chunk; a placeholder split across chunks is held back until it is complete. Tool-call arguments get
  JSON-escaped values. A system note tells the model to use the placeholders as written.
- **Stable placeholders.** One local vault (`vault.json`, mode 0600) maps each value to the same placeholder across
  requests, so multi-turn conversations stay coherent.
- **Fails closed.** Any error while scrubbing answers `503` and nothing is forwarded. After the swap every outgoing string
  is re-checked for known values; if one is left, the request is blocked. A known-values file that is configured but
  missing or broken also blocks requests.
- **Leaves file paths alone.** A known name that is one segment of a filesystem path (`/home/jane/...`, `~/jane/...`) is
  not swapped, so agents' file tools keep working. URLs and domains are still scrubbed.
- **Logs counts, never values.** `requests.jsonl` records types and counts per request.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .                                  # or: pip install -e ".[test]"
cp pii-scrubber.example.yaml pii-scrubber.yaml    # set upstream, listen, known_file
cp known.example.yaml known.yaml                  # put your real values here (git-ignored)
pii-scrubber-proxy                                # listens on 127.0.0.1:8787
```

Point any OpenAI-compatible client at it. Paths, headers (including `Authorization`) and models pass through
unchanged:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-...")   # your provider key, forwarded as is
```

```bash
curl http://127.0.0.1:8787/v1/chat/completions -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Draft a note to Jane Q Doe at (425) 555-0182"}]}'
```

Try it with no provider at all: `python examples/smoke.py` starts a mock upstream and the proxy, and shows what the
upstream saw (placeholders) next to what the client got (real values).

## Config

`pii-scrubber.yaml` (or the file named by `PII_SCRUBBER_CONFIG`); every key also has an env override.

| key | env | default | |
|---|---|---|---|
| `mode` | `PII_SCRUBBER_MODE` | `on` | `on` swap + restore; `watch` count only; `off` pass through |
| `listen` | `PII_SCRUBBER_LISTEN` | `127.0.0.1:8787` | loopback only unless `allow_remote: true` |
| `upstream` | `PII_SCRUBBER_UPSTREAM` | `https://api.openai.com` | any OpenAI-compatible base URL |
| `known_file` | `PII_SCRUBBER_KNOWN_FILE` | none | YAML/JSON: `{TYPE: [values]}` or `[[value, TYPE], ...]` |
| `private_terms_file` | `PII_SCRUBBER_PRIVATE_TERMS` | none | `terms:` and `figures:` (see `private_terms.example.yaml`) |
| `state_dir` | `PII_SCRUBBER_STATE_DIR` | `~/.local/state/pii-scrubber` | vault and request log |
| `alert_webhook` | `PII_SCRUBBER_ALERT_WEBHOOK` | none | POSTs `{"text": ...}` (Slack/Discord-style) on a block or failed canary |
| `canary_model` | `PII_SCRUBBER_CANARY_MODEL` | none | model for `pii-scrubber canary` |

Also: `phone_regions`, `url_swap`, `patterns` (turn single detectors off), `extra_secret_rules`, `log_requests`,
`alert_min_interval_s`. Relative paths are relative to the config file. The known-values and private-terms files are
re-read when they change; no restart needed.

CLI: `pii-scrubber check-config` (counts per type, never values), `pii-scrubber stats`, `pii-scrubber health`,
`pii-scrubber scrub-file request.json`, `pii-scrubber canary` (sends a fake name and email through the running proxy and
checks the provider saw only placeholders and the client got the values back).

## Limits

- **No NER.** It swaps what you list and what the patterns match. A name you never listed, written in free text, goes
  out as written. Keep `known_file` current.
- **Whole words, case-insensitive.** `Jane` does not match `Janet`. Spelling variants and nicknames need their own entries.
- **Streaming.** The restore holds back at most one partial placeholder (up to 24 characters) per stream, so chunks can
  arrive slightly regrouped; the text is unchanged. Only OpenAI-style `chat.completion.chunk` SSE events are restored;
  other streaming formats pass through with placeholders left in.
- **JSON bodies only.** Multipart uploads and binary bodies pass through untouched; `data:` URLs (images) are not scanned.
- **The model sees placeholders.** Answers that need the real value's content (spell this name, count the digits of
  this phone number) will be wrong or vague.
- **One vault for all conversations.** Placeholders are stable across requests and conversations on one machine.
- **Single process, local use.** It is meant to run next to the client on loopback.

## Latency

Measured by `tests/test_latency.py` (`pytest -s tests/test_latency.py`) on a synthetic chat request of about
19,000 characters with 13 messages, on a laptop CPU (AMD Ryzen 7 4700U, Python 3.13):

| | median | max (20 runs) |
|---|---|---|
| first time a text is seen (cold) | 7.1 ms | 10.1 ms |
| repeated text (cached, typical for a growing chat history) | 0.13 ms | 0.14 ms |

Each text is scrubbed once and cached by hash, so in a multi-turn conversation only the newest messages cost the cold
time.

## Development

```bash
pip install -e ".[test]" && pytest -q
python examples/smoke.py
```

The vendored rule sets live in `pii_scrubber/vendor` (see its README and `THIRD_PARTY_NOTICES.md`);
`tools/build_vendor.py` regenerates the Presidio patterns from a presidio-analyzer wheel.

## License

MIT, see `LICENSE`. Bundled rule data: gitleaks (MIT) and Microsoft Presidio (MIT), see `THIRD_PARTY_NOTICES.md`.
