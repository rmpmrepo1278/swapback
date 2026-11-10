# Limits and security notes

What swapback does not catch, and how it handles your data. Back to the [README](../README.md).

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
  a header row above it, list items, divs), and whether that HTML is written raw or escaped inside JSON (`\u003c`,
  `<\/td>`, `&lt;`). The restore gives the cells back exactly as written, quoted attributes included. Not across a row or list boundary (`</tr><tr>`, `</ul><ul>`). Such a pair
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
  `sequence_number`s are shifted so they stay unique and increasing. Non-streaming JSON replies of any shape are restored. Plain-text streams hold back at
  most one partial placeholder (up to 24 characters), so chunks can arrive slightly regrouped. A streamed reply whose
  text is itself escaped JSON is held back longer (measured: up to about 20 characters at 2 levels, up to about 300 at
  8, about one escaped placeholder) and arrives in bursts. The text is unchanged either way.
- **Long streamed replies that are themselves escaped JSON are restored slowly.** The time grows roughly with the
  square of the reply's length. Measured on a shared 4-core test box with two different test models (the time
  depends on how the model splits its stream): 10 KB in 0.4–3 s and 40 KB in 6–42 s at 2 JSON levels, 10 KB in
  0.3–18 s at 8 levels. `scrub_timeout_s` does not cover the restore, so such a reply is
  slow, not cut off. Not affected: non-streamed replies (160 KB at 2 levels in 0.3 s), plain-text streams (160 KB in
  about 4 s) and streamed tool-call arguments. It needs a reply of several thousand
  tokens that is escaped JSON, which is rare in chat.
- **A name split by a comma and a line break, across quoted CSV columns, or by a bar with no blank beside it, is blocked,
  not swapped.** `Faith,\nRidge`
  and `"Faith","Ridge"` (a quoted CSV row, or two strings next to each other in a JSON array) answer `503`: nothing
  leaks, but the blocked text stays in the chat history, so later requests that carry it are blocked too until it is
  removed. The same goes for a bar with no blank beside it (`|Faith|Ridge|`). One comma within a line (`Faith, Ridge`), an
  unquoted CSV row (`3,Faith,Ridge,Reno`) and markdown table cells with a blank on each side of the bar (`| Faith |
  Ridge |`, `Owners: Faith | Ridge`) are swapped, and come back exactly as written.
- **Part of a listed address on its own is not swapped.** A listed address is swapped whole (as listed, on one line,
  with commas, quoted, or with its lines joined by tags). One or two of its lines on their own (just the street line,
  the street line and the unit, the unit and the city line) go out as written, unless the street-address pattern
  catches the street line, and it knows only common suffixes (Street, Ave, Road, Lane ... Trail; not Crossing, Mews or
  Wharf). To protect the street line by itself, list it separately as well.
- **JSON bodies only.** Multipart uploads and binary bodies pass through untouched; `data:` URLs (images) are not scanned.
  Token-id inputs to embeddings and completions are blocked, since they cannot be checked.
- **The tool policy is a heuristic.** It reads tool names and looks for URLs. A tool with a harmless name that still
  sends data out (say `notes_sync` posting to a server) gets real values unless you `deny` it or set
  `default: keep`. A value can also leave through a tool that writes a file which something else uploads later.
- **The model sees placeholders.** Answers that depend on the real value (spell this name, count the digits) will be
  wrong or vague.
- **One vault, one machine.** Placeholders are stable across all conversations on one machine. It is a single-process
  tool meant to run next to the client on loopback.

## Security notes

- The proxy listens on loopback and refuses other addresses unless you set `allow_remote: true`. It has no
  authentication of its own; anything that can reach the port can use it.
- Your provider key passes through in the `Authorization` header and is not stored.
- The vault (`vault.json`) holds the real values in plain JSON, readable only by your user (`0600`). Treat the state
  directory as sensitive.
- Tool calls that look like network or shell egress, or carry a URL, keep placeholders by default, so a prompt-injected
  model cannot have your client send real values out through them. See [Tool calls](guide.md#tool-calls).
- The request log records counts and types per request, never values.
- `X-Scrubber-Raw: 1` returns the provider's answer without the restore. The canary uses it to prove what the provider saw.
- Report vulnerabilities privately; see [SECURITY.md](https://github.com/rmpmrepo1278/swapback/blob/main/SECURITY.md).
