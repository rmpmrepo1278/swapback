# Leak test, performance and detection score

Back to the [README](../README.md).

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

0.4.15 on Python 3.13:

```text
swapback selftest: 34 made-up listed values, 257 written forms, 7 paths, 1799 requests (9.5 s)
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
  missing/broken config file, one readable line:     5/5 (100.0%)
  tool args, mixed escaping, 1-8 inner levels valid: 96/96 (100.0%)
  \u00ab-escaped placeholders in text JSON restored: 6/6 (100.0%)
  dictionary-word names in one-word HTML cells:      7/7 (100.0%)
  bad listen / port in use, one readable line:       3/3 (100.0%)
  names in one-word cells anywhere in a row/list:    194/194 (100.0%)
  example placeholders unassigned, broken YAML said: 3/3 (100.0%)
  quoted attributes between a value's parts, exact:  36/36 (100.0%)
  cell names in escaped HTML (\u003c, <\/, &lt;):    36/36 (100.0%)
  cell names reused, dotted I, quoted, many copies:  11/11 (100.0%)  (9,000 copies: 0.32 s)
  cell names 7-8 JSON levels down, raw/Go/PHP/&lt;:  32/32 (100.0%)
  cells reused, comma/CSV/| names, dotted i, | quotes: 18/18 (100.0%)  (20 KB of copies 8 levels down: 0.86 s)
  emails/phones right after \\u003e, \\u0026, \\u003c:      48/48 (100.0%)
  keys/emails/phones next to escapes (#14 F6-F8):   36/36 (100.0%)
  phones with 3+ blanks, sk_test_ words (#15):      14/14 (100.0%)
  200 KB %XX body checked in:                       0.49 s
  health: blocked when the list breaks, known count: 5/5 (100.0%)
  %XX-encoded forms back as the same value, decoded: 105/105 (100.0%)
  unlisted values caught by the pattern detectors:  6/8 (75.0%)  (reported, not a pass condition; names are never caught without a list)
PASS
```

The 0.4.10 selftest adds the review #10 forms: a listed address or name whose parts are separated by tags with
quoted attributes (`<div dir="ltr">`, `<p class>`, `<br class>`, `<td class>`, `<li data-x>`, `<span class>`, an
attribute menu with the listed pair in it) inside JSON at 1 and 2 levels, ASCII-escaped and raw (swapped, back byte
for byte); the review #9 cell layouts with the HTML escaped inside JSON (`\u003c`, `<\/td>`, `&lt;`); a name swapped out
of cells and reused in a greeting and in a tool call's `to_name` (the listed name); a name with a capital dotted I; a
`> `-quoted address; and 9,000 copies of a listed email in one string (under 10 seconds). The 0.4.11 selftest adds
the review #11 forms: names in one-word `<div dir="ltr">`, `<td class/style>`, plain `<td>` and `<li>` cells nested 7
and 8 JSON levels deep, as raw, Go-escaped (`\u003c`), PHP-escaped (`<\/td>`) and `&lt;`-entity HTML (swapped, back
byte for byte); a name taken from `<td>` cells reused in `<li>` and `<p>` (the listed name) and in a `<td>` (the cells);
a name split by a comma in prose and across CSV columns, plain and in JSON; `i` + a combining dot; a `| `-quoted
address; about 20 KB of copies of a listed address 8 JSON levels down (under 5 seconds); and the time limit
(`scrub_timeout_s`, a 503 with a one-line reason). The 0.4.15 selftest adds unlisted phones typed with 3 or more spaces between their digit groups and a JSON `\t` between them 2 and 3 levels down (swapped, back byte for byte), and `sk_test_environment.py`, which is not a Stripe key (left as written). The 0.4.13 selftest adds emails and phone numbers, listed and unlisted, right after Go-style `\u003e`, `\u0026` and `\u003c`, 1-3 JSON levels down (swapped whole, back byte for byte, valid JSON). The 0.4.12 selftest adds a name split across markdown table cells (`| 3 | Faith | Ridge | Reno |`, plain and in JSON; swapped, back byte for byte) and a non-numeric and a negative `scrub_timeout_s` (one readable line each). The 0.4.9 selftest added the review #9 forms: a listed dictionary-word name split across one-word cells or blocks in
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

Many copies of one listed value inside deeply nested JSON are checked in linear time since 0.4.11. Measured on the
release test box (Python 3.11): copies of a listed three-line address 8 JSON levels deep, 50 KB before nesting (577,000
characters as sent) in 2.2 s and 100 KB in 4.1 s (0.4.10: 11 s and 44 s for 50 and 100 KB at 8 levels, quadratic);
880 KB nested 8 levels deep (10 MB as sent) runs past the 10-second `scrub_timeout_s` and is answered `503`; the same
880 KB at 1 level takes 2.8 s.

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
