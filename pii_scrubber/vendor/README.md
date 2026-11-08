# Vendored detector data

## presidio/patterns.json: Presidio predefined pattern recognizers
- Source: presidio-analyzer 2.2.366 (https://github.com/microsoft/presidio), MIT license (presidio/LICENSE).
- Recognizers: EmailRecognizer, CreditCardRecognizer, IbanRecognizer (+ regex_per_country), UsSsnRecognizer,
  IpRecognizer, UrlRecognizer: patterns, scores and context words copied verbatim by tools/build_vendor.py.
- Why vendored: presidio-analyzer requires spaCy and imports it with the package; this project needs only the regex
  recognizers. Their validation logic (Luhn, IBAN checksum, SSN invalidation) is ported in detect.py; Presidio's email
  domain check (tldextract) is replaced by an alphabetic-TLD check.
- Refresh: `pip download --no-deps presidio-analyzer==X` then
  `python tools/build_vendor.py <wheel> > pii_scrubber/vendor/presidio/patterns.json`.

## gitleaks/gitleaks.toml: gitleaks default rules
- Source: https://raw.githubusercontent.com/gitleaks/gitleaks/v8.30.1/config/gitleaks.toml (tag v8.30.1), MIT license
  (gitleaks/LICENSE). 221 regex rules compile in Python `re`; 1 path-only rule does not apply to chat text.

## extra_rules.toml
Extra secret rules in gitleaks format (OpenRouter keys, Tailscale keys, HTTP bearer values) for keys gitleaks v8.30.1
has no rule for. Point `extra_secret_rules` at your own file to replace it.
