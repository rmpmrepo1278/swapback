<p align="center">
  <img src="docs/assets/banner.svg" alt="swapback: keep personal data out of LLM requests, restore it byte-exact in the reply" width="100%">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg" alt="Python 3.11 | 3.12 | 3.13">
  <a href="https://github.com/rmpmrepo1278/swapback/actions/workflows/tests.yml"><img src="https://github.com/rmpmrepo1278/swapback/actions/workflows/tests.yml/badge.svg" alt="tests"></a>
  <img src="https://img.shields.io/badge/dependencies-3-brightgreen.svg" alt="3 dependencies">
</p>

<p align="center"><b>A local proxy that swaps personal data for placeholders before an LLM call and restores it, byte-exact, in the reply.</b></p>

---

## Why

Agents and chat apps send everything to the model provider: names, emails, phone numbers, account numbers, API keys
pasted into a log. Most of the time the model does not need the real value, only a stable stand-in for it.

`swapback` sits on loopback between your client and an OpenAI-compatible API. It replaces the values you list and
the patterns it detects with placeholders like `«PERSON_1»`, forwards the request, and puts the exact original text back
into the answer, including streamed answers and tool-call arguments. If anything goes wrong while scrubbing, the request
is blocked, not sent.

- **Byte-exact restore.** The reply gets back the string as you wrote it: same case, same phone format, same spacing.
- **Fails closed.** A scrub error, an unreadable body or a known value still present after the swap answers `503`.
- **Agent-neutral.** Works with any client that lets you set a base URL. Every tool's arguments are restored, not a
  fixed list of tools.
- **No model needed.** Exact matching plus patterns. No NER model, no GPU, three small dependencies.

<p align="center"><img src="docs/assets/before-after.svg" alt="The app sends real values, the provider sees placeholders, the app gets the real values back" width="100%"></p>

## Quickstart (60 seconds, no API key)

Needs Python 3.11 or newer. This runs the proxy in front of a bundled mock LLM that echoes your message, so you can see
both sides.

```bash
git clone https://github.com/rmpmrepo1278/swapback.git && cd swapback
python3 -m venv .venv && . .venv/bin/activate && pip install -e .

python examples/mock_upstream.py 8009 &                            # fake LLM on :8009
SWAPBACK_CONFIG=examples/quickstart.yaml swapback-proxy &  # proxy on :8787
sh examples/curl_demo.sh
```

Output:

```text
== reply your app gets back (values restored)
{"id": "m", "object": "chat.completion", ... "content": "You said: Draft a reply to Jane Q Doe (jdoe@corp.example) and ask her to call (425) 555-0182."} ...

== same request, streamed (SSE), restored chunk by chunk
You said: Draft a reply to Jane Q Doe (jdoe@corp.example) and ask her to call (425) 555-0182.

== what the upstream (the "provider") actually received
Draft a reply to «PERSON_1» («EMAIL_1») and ask her to call «PHONE_1».
```

Stop both with `kill %1 %2`.

### Set it up for a real provider

```bash
cp swapback.example.yaml swapback.yaml
cp known.example.yaml known.yaml                    # then EDIT it: your names, emails, phones (see Configuration)
cp private_terms.example.yaml private_terms.yaml    # optional: codenames and money figures
```

In `swapback.yaml`:

```yaml
upstream: https://api.openai.com      # a base URL WITHOUT /v1: the proxy appends the client's own path
known_file: known.yaml
private_terms_file: private_terms.yaml   # leave "" if you skipped the private terms file
```

Edit `known.yaml` **before** you start the proxy. The example values are fake, and a configured `known_file` that is
missing blocks every request (`503`, fail-closed). Then:

```bash
swapback check-config     # counts per type, never values
swapback-proxy            # listens on 127.0.0.1:8787
```

Point your client at the proxy. It keeps its usual path; only the host changes:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-...")   # your key is forwarded as is
```

`upstream` is joined with the client's path, so `upstream: https://api.openai.com/v1` would request `/v1/v1/...`.
Other providers work the same way: `upstream: https://openrouter.ai` with client base URL
`http://127.0.0.1:8787/api/v1`; `upstream: https://api.anthropic.com` with an Anthropic SDK base URL of
`http://127.0.0.1:8787`.

## Use with agents

Any agent that lets you set a provider base URL can sit behind the proxy. Tested end to end with
[OpenClaw](https://github.com/openclaw/openclaw) 2026.10.7: a fresh clone, the steps above and one provider block gave a
scrubbed chat in a few minutes; replies, streamed replies and tool calls came back with the real values.

```json5
// OpenClaw config (openclaw.json)
{
  models: { providers: { scrub: {
    baseUrl: "http://127.0.0.1:8787/v1",   // swapback
    apiKey: "${PROVIDER_API_KEY}",         // your provider key; the proxy forwards it unchanged
    api: "openai-completions",
    models: [{ id: "your-model", name: "your-model via swapback", reasoning: false, input: ["text"],
               cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 128000, maxTokens: 4096 }],
  } } },
  agents: { defaults: { model: { primary: "scrub/your-model" } } },
}
```

`api: "openai-completions"` was tested against a live model. `api: "anthropic-messages"` works from 0.1.1, which
restores Anthropic Messages streams (before, streamed replies in that mode kept their placeholders): set `baseUrl` to
`http://127.0.0.1:8787` (no `/v1`) and the proxy's `upstream` to an Anthropic-compatible endpoint. That mode was checked
end to end with OpenClaw against a mock Anthropic upstream, not yet against a live Claude model.

**What a block looks like in an agent.** When the proxy blocks a request (fail-closed), it answers `503` with a
`scrubber_error` message. Most agents retry a 503 several times and then show a generic error; OpenClaw retries for
about a minute and then says it couldn't connect to the AI service. The reason is not shown in the agent. To see it:

```bash
swapback health                          # mode, counters, last_error
tail -n 5 ~/.local/state/swapback/requests.jsonl   # one line per request: status, blocked, error, swap counts
```

The usual causes are a missing or broken `known_file` / `private_terms_file`, a non-JSON or compressed body on a model
endpoint, or a known value still present after the swap.

**Check what the provider saw.** `requests.jsonl` records counts and types, never the text. To prove the provider got
placeholders only, run `swapback canary` (set `canary_model` first): it sends a fake name and email through the
running proxy, checks the provider's raw answer (`X-Scrubber-Raw: 1`) for the fake values and checks the restored answer
for them. `python examples/smoke.py` runs the same check offline against the mock upstream.

## How it works

<p align="center"><img src="docs/assets/architecture.svg" alt="Architecture: app or agent, then swapback, then the LLM API, with the restore path back" width="100%"></p>

1. **Swap.** Every string in the JSON request body is scanned. Values from your known-values file are matched as whole
   words, case-insensitively, in their common written variants (a phone number also matches `(425) 555-0182`,
   `425.555.0182`, `+14255550182`, ...; a multi-word value also matches with its words joined by non-breaking or
   repeated spaces, tabs, zero-width characters, `.`, `_`, `-` or `%20`, so `Jane.Q.Doe`, `JANE Q DOE` and
   `Jane%20Q%20Doe` are the same person under one placeholder). Detectors then catch what you did not list:
   - phone numbers via [`phonenumbers`](https://github.com/daviddrysdale/python-phonenumbers);
   - email, credit card (Luhn), IBAN, US SSN, IP address and profile URLs, from
     [Microsoft Presidio](https://github.com/microsoft/presidio)'s predefined recognizer patterns;
   - API keys and tokens, from the [gitleaks](https://github.com/gitleaks/gitleaks) default rules;
   - a few local rules: street addresses, keyword-anchored account numbers and dates of birth, order numbers, and
     gap rules for emails written as `name [at] example [dot] org` or with a full-width `＠` or hidden zero-width
     characters, and IPv6 addresses.
2. **Re-check.** After the swap, every outgoing string is checked again for known values, with the same variant rules.
   If one is left, the request is blocked.
3. **Forward or block.** Paths, headers (including `Authorization`) and the model name pass through unchanged. Any error
   while scrubbing answers `503` with a `scrubber_error`, and nothing is forwarded.
4. **Restore.** Placeholders in the reply are swapped back to the original text: plain JSON responses anywhere in the
   body, OpenAI-style `chat.completion.chunk` SSE streams and Anthropic Messages streams (`text_delta`, and
   `input_json_delta` for `tool_use`), delta by delta. A placeholder split across chunks is held back until complete.
   Values inside tool-call arguments are JSON-escaped. Each variant comes back exactly as it was written in the request.

Placeholders are stable: a local vault (`vault.json`, mode `0600`) maps each value to the same placeholder across
requests, so multi-turn conversations stay coherent. A short system note asks the model to keep placeholders as written.

Known names that are one segment of a filesystem path (`/home/jane/notes.md`) are left alone, so agents' file tools keep
working. URLs and domains are still scrubbed.

## Configuration

Copy `swapback.example.yaml` to `swapback.yaml`, or point `SWAPBACK_CONFIG` at any file. Every key is optional
and has an environment override. Relative paths are relative to the config file. A minimal working setup is in
[`examples/`](examples/).

| key | env | default | |
|---|---|---|---|
| `mode` | `SWAPBACK_MODE` | `on` | `on` swap + restore; `watch` count only, forward the original; `off` pass through |
| `listen` | `SWAPBACK_LISTEN` | `127.0.0.1:8787` | loopback only unless `allow_remote: true` |
| `upstream` | `SWAPBACK_UPSTREAM` | `https://api.openai.com` | any OpenAI-compatible base URL (OpenRouter, LiteLLM, vLLM, llama.cpp, ...) |
| `known_file` | `SWAPBACK_KNOWN_FILE` | none | your known values; configured but missing means requests are blocked |
| `private_terms_file` | `SWAPBACK_PRIVATE_TERMS` | none | codenames and figures that must not leave (below) |
| `state_dir` | `SWAPBACK_STATE_DIR` | `~/.local/state/swapback` | vault and request log |
| `alert_webhook` | `SWAPBACK_ALERT_WEBHOOK` | none | POSTs `{"text": ...}` when a request is blocked or the canary fails |
| `canary_model` | `SWAPBACK_CANARY_MODEL` | none | model used by `swapback canary` |

Renamed from pii-scrubber in 0.2.0: the old `PII_SCRUBBER_*` variables are still read when the `SWAPBACK_*` one is unset, but they are deprecated.

Also: `phone_regions`, `url_swap`, `patterns` (turn single detectors off, e.g. `{IP: false}`), `extra_secret_rules`,
`log_requests`, `alert_min_interval_s`.

**Known values** (`known.example.yaml`). The exact strings to swap, by type. The file is re-read when it changes; no
restart needed.

```yaml
PERSON:
  - Jane Q Doe
EMAIL:
  - jdoe@corp.example
PHONE:
  - "425-555-0182"        # also matches (425) 555-0182, 425.555.0182, +14255550182, ...
```

**Private terms** (`private_terms.example.yaml`). Things that are not PII but must not leave: phrases or regexes become
`«PRIVATE_TERM_n»`, and money amounts are matched in every written form (`$2.47B`, `2.47 billion`, `$2,470M`) as
`«PRIVATE_FIGURE_n»`.

```yaml
terms:
  - Project Bluebird
  - {regex: "11[\\s-]*FTEs?"}
figures:
  - 2470000000
```

**CLI.** `swapback check-config` (counts per type, never values), `swapback stats`, `swapback health`,
`swapback scrub-file request.json`, and `swapback canary`, which sends a fake name and email through the running
proxy and checks that the provider saw only placeholders and the client got the values back. `GET /scrubber/health` on
the proxy returns counters. Every command takes `--help` and `--version`; `swapback-proxy --help` lists the settings it reads and exits without starting.

## Comparison

Other open-source projects do the same job. This table reflects each project's own README and docs as read on
2026-11-09; "unclear" means we could not confirm it there. Corrections are welcome.

| | **swapback** | [PasteGuard][pg] | [Maskit][mk] | [Veil][vl] |
|---|---|---|---|---|
| License | MIT | Apache-2.0 | AGPL-3.0 | Apache-2.0 |
| Stack | Python 3.11+, 3 deps, stdlib HTTP server | TypeScript on Bun + GLiNER detector service; Docker | Python engine + desktop app; Docker | Python 3.11+ (PyPI, Homebrew) |
| Needs an ML model | No | Yes: GLiNER for names and places ([docs][pg-sem]) | Ships a local ONNX NER model | Optional: 46 MB ONNX NER |
| Your own list of values | Yes, typed, reloaded live | Yes: `denylist` patterns ([docs][pg-mask]) | Yes: custom words and regexes | Yes: seeded from your data sources |
| Restores values in replies | Yes, byte-exact as written | Yes ("supported placeholders") | Yes | Yes |
| Streaming restore | OpenAI chat-completions and Anthropic Messages SSE | Yes | Yes (SSE) | Unclear per API |
| Tool-call arguments | Restored for every tool, JSON-escaped | Unclear | Unclear | Per-tool policy: unknown and network tools get pseudonyms |
| On a scrub error | Fails closed (`503`) + egress re-check | Unclear | Unclear; documents a passthrough that forwards plaintext when masking is off or the app exits | Fails closed (`502`) + second guard |
| APIs | OpenAI-compatible; Anthropic Messages (JSON and streaming) | OpenAI Chat + Responses, Anthropic, Codex | OpenAI, DeepSeek, Anthropic | Anthropic Messages, OpenAI Chat + Responses, Bedrock, Vertex, more |
| Beyond text | No | Browser extension, dashboard, route-to-local-LLM mode | Image/PDF OCR redaction, browser extension, dashboard | Encrypted vault, DB seeding, shell-safe escaping, MCP DB tool |

**Where swapback fits:** you want something small you can read in an afternoon, that never needs a model, restores
the exact original text for any agent's tools, and blocks rather than leaks. **Where others are stronger:** names you
never listed (PasteGuard, Maskit and Veil's NER), more APIs and streaming formats (all three), images and documents
(Maskit), and coding-agent hardening such as escaping restored values for shells (Veil).

[pg]: https://github.com/sgasser/pasteguard
[pg-mask]: https://pasteguard.com/docs/concepts/mask-mode
[pg-sem]: https://github.com/sgasser/pasteguard#how-detection-works
[mk]: https://github.com/xiaYuTian11/maskit
[vl]: https://github.com/lemonadesaltbagel/veil

## Limits

- **No NER.** It swaps what you list and what the patterns match. A name you never listed, written in free text, goes
  out as written. Keep the known-values file current.
- **Pattern coverage is what the rules cover.** Phone numbers, emails, cards (Luhn), IBANs, US SSNs, IPs, profile URLs,
  common API keys and a few local rules. National IDs other than the US SSN, free-form addresses, dates of birth in
  most label forms and many other formats are not detected unless you list them. See the measured score below.
- **Whole words, case-insensitive.** `Jane` does not match `Janet`. Separator and case variants of a listed multi-word
  value are caught (see How it works); nicknames, misspellings and other spellings need their own entries.
- **Variants and restore.** All written variants of one listed value share one placeholder. A reply gets back the forms
  written in the same request, in order of occurrence; a reply with more mentions than the request reuses the last one.
  With no request context (`restore_text` on its own, the CLI) the spelling from your known file comes back.
- **Streaming.** OpenAI `chat.completion.chunk` and Anthropic Messages streams are restored. Anthropic `thinking`
  deltas are left as sent, since they are signed. Other streams, including the OpenAI Responses API, pass through with
  placeholders left in. Non-streaming JSON replies of any shape are restored. The restore holds back at most one partial
  placeholder (up to 24 characters) per stream, so chunks can arrive slightly regrouped; the text is unchanged.
- **JSON bodies only.** Multipart uploads and binary bodies pass through untouched; `data:` URLs (images) are not scanned.
  Token-id inputs to embeddings and completions are blocked, since they cannot be checked.
- **The model sees placeholders.** Answers that depend on the real value (spell this name, count the digits) will be
  wrong or vague.
- **One vault, one machine.** Placeholders are stable across all conversations on one machine. It is a single-process
  tool meant to run next to the client on loopback.

## Performance

Measured by `tests/test_latency.py` on a synthetic chat request of about 19,000 characters with 13 messages, on a
laptop CPU (AMD Ryzen 7 4700U, Python 3.13):

| | median | max (20 runs) |
|---|---|---|
| first time a text is seen (cold) | 7.1 ms | 10.1 ms |
| repeated text (cached, typical for a growing chat history) | 0.13 ms | 0.14 ms |

## Security notes

- The proxy listens on loopback and refuses other addresses unless you set `allow_remote: true`. It has no
  authentication of its own; anything that can reach the port can use it.
- Your provider key passes through in the `Authorization` header and is not stored.
- The vault (`vault.json`) holds the real values in plain JSON, readable only by your user (`0600`). Treat the state
  directory as sensitive.
- The request log records counts and types per request, never values.
- `X-Scrubber-Raw: 1` returns the provider's answer without the restore. The canary uses it to prove what the provider saw.
- Report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

## Detection score (patterns only)

`evals/holdout3.json` holds 64 synthetic cases (invented values only): 42 with something to catch, 22 that must pass
untouched. `python evals/run_holdout.py` scores the pattern detectors with **no known values listed**, so it measures
what the scrubber finds on its own. A case counts as caught only if every character of the expected value is swapped.
These numbers are not tuned to the holdout; the 0.1.1 rules are generic (bracketed at/dot emails, full-width `＠` and
zero-width characters in emails, IPv6).

| Category | 0.1.0 | 0.1.1 |
|---|---|---|
| EMAIL | 1/5 | 5/5 |
| IP address | 2/4 | 4/4 |
| SSN | 2/2 | 2/2 |
| PHONE | 2/5 | 2/5 |
| ADDRESS | 2/6 | 2/6 |
| SECRET (keys, passwords, passport/licence IDs) | 1/9 | 1/9 |
| DATE OF BIRTH | 0/5 | 0/5 |
| PERSON (no NER, by design) | 0/6 | 0/6 |
| **All 42 positives** | **10/42** | **16/42** |
| Negatives left untouched | 22/22 | 22/22 |
| Round trip exact | 64/64 | 64/64 |

Read it plainly: with nothing listed, the patterns catch well-formed contact data and miss most free-form secrets,
names, dates of birth and non-US addresses. The two phone misses are UK fictional-range numbers (07700 900xxx) that
the phone library rejects as invalid. **List what matters to you in `known.yaml` and `private_terms.yaml`;** listed
values are caught in every written variant above.

## Development

```bash
pip install -e ".[test]"
pytest -q                      # 203 tests, including the holdout score as a regression floor
python evals/run_holdout.py    # the table above (--misses lists the cases not caught)
python examples/smoke.py       # end-to-end: mock upstream + proxy, plain and streamed
```

The vendored rule sets live
in `swapback/vendor` (see `THIRD_PARTY_NOTICES.md`); `tools/build_vendor.py` regenerates the Presidio patterns.

See [CONTRIBUTING.md](CONTRIBUTING.md) and the [changelog](CHANGELOG.md).

## Roadmap

- ~~Restore Anthropic Messages streams.~~ Done in 0.1.1.
- ~~Catch separator and case variants of listed values (NBSP, `.`, `_`, `-`, `%20`, all caps) under one placeholder.~~
  Done in 0.1.1.
- Restore OpenAI Responses API streams.
- Optional, pluggable NER for names you never listed, off by default.
- More national ID and address patterns, with tests from synthetic data.
- Packaged release on PyPI.

Ideas and pull requests are welcome; open an issue first for anything large.

## License

MIT, see [LICENSE](LICENSE). Bundled rule data: gitleaks (MIT) and Microsoft Presidio (MIT), see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
