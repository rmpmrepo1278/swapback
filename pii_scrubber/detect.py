"""Library-backed detectors behind pii_scrubber.core. Each detector returns spans (start, end, TYPE, value); core.py
owns the rest (placeholders, the vault, streaming restore, fail-closed).
- PHONE: phonenumbers.PhoneNumberMatcher (configurable regions; '+' numbers parse in any region). Formatted numbers
  only, so bare digit runs (order ids, timestamps) are never swapped.
- EMAIL / CARD / IBAN / SSN / IP / URL: Presidio's predefined PatternRecognizer regex + context words, vendored in
  vendor/presidio/patterns.json (presidio-analyzer requires spaCy and imports it with the package; see vendor/README.md).
- SECRET: gitleaks default rules (vendor/gitleaks/gitleaks.toml, pinned release) compiled to Python re.
- known values: pyahocorasick automaton (case-insensitive, whole words, leftmost-longest)."""
import ipaddress, json, math, os, re, sys, tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "vendor")
import ahocorasick
import phonenumbers


_ESC = re.compile(r"\\[nrt]")     # JSON-style escapes inside tool output, blanked for detection (same length)


def lower_same_len(text):
    low = text.lower()
    return low if len(low) == len(text) else "".join(c if len(c.lower()) != 1 else c.lower() for c in text)


def _automaton(words):
    a = ahocorasick.Automaton()
    for w in words:
        a.add_word(w, w)
    a.make_automaton()
    return a


# ---------------------------------------------------------------- known list (pyahocorasick)
class KnownMatcher:
    def __init__(self, keys):
        self.a = _automaton(keys) if keys else None

    def find(self, text, low=None):
        """[(start, end, key)] whole-word, leftmost-longest, non-overlapping."""
        if self.a is None or not text:
            return []
        low = low or lower_same_len(text)
        n, hits = len(text), []
        for end, k in self.a.iter(low):
            s, e = end - len(k) + 1, end + 1
            if (s and (text[s - 1].isalnum() or text[s - 1] in "_@.")) or (e < n and (text[e].isalnum() or text[e] in "_@")):
                continue
            hits.append((s, e, k))
        hits.sort(key=lambda h: (h[0], h[0] - h[1]))
        out, last = [], -1
        for h in hits:
            if h[0] >= last:
                out.append(h)
                last = h[1]
        return out


# ---------------------------------------------------------------- phone numbers (phonenumbers)
_PHONE_CAND = re.compile(r"(?<![\w+])\+?\(?\d[\d \t().\-]{5,22}\d(?!\w)")
_SEP = re.compile(r"[\s().\-]")
_NOT_PHONE = re.compile(r"\(?\d{1,3}(?:\.\d{1,3}){3}\)?|\(?(?:19|20)\d\d[-/.]\d\d?[-/.]\d\d?\b.*")   # IPv4, ISO dates


def find_phones(text, regions=("US", "IN")):
    out = []
    text = _ESC.sub("  ", text)
    for m in _PHONE_CAND.finditer(text):
        chunk = m.group(0)
        if sum(c.isdigit() for c in chunk) < 7:
            continue
        if not chunk.startswith("+") and not _SEP.search(chunk):
            continue                                   # a bare digit run (job id, timestamp) is not a phone
        for region in regions:
            got = [pm for pm in phonenumbers.PhoneNumberMatcher(chunk, region, leniency=phonenumbers.Leniency.VALID)
                   if (pm.raw_string.startswith("+") or _SEP.search(pm.raw_string.strip("()")))
                   and not _NOT_PHONE.fullmatch(pm.raw_string)]
            if got:
                out += [(m.start() + pm.start, m.start() + pm.end, "PHONE", pm.raw_string) for pm in got]
                break
    return out


# ---------------------------------------------------------------- Presidio pattern recognizers (vendored)
def luhn(d):
    s = 0
    for i, ch in enumerate(reversed(d)):
        x = int(ch) * (2 if i % 2 else 1)
        s += x - 9 if x > 9 else x
    return s % 10 == 0


def _ssn_invalid(v):      # UsSsnRecognizer.invalidate_result (presidio-analyzer 2.2.366), ported as is
    if len({c for c in v if c in ".- "}) > 1:
        return True
    d = "".join(c for c in v if c.isdigit())
    if all(d[0] == c for c in d) or d[3:5] == "00" or d[5:] == "0000":
        return True
    return d[:3] in ("000", "666") or d in ("123456789", "987654320", "078051120")


def _ip_invalid(v):       # IpRecognizer.invalidate_result, ported
    try:
        ipaddress.ip_interface(v)
        return False
    except ValueError:
        return True


_V6_FULL = re.compile(r"(?:[0-9A-Fa-f]{1,4}:){7}")
_NO_TRUE = ("SSN", "IP", "URL", "EMAIL")       # types whose validator never confirms: weak patterns need context


class Presidio:
    def __init__(self, path=None, iban_ok=None, url_keep=("linkedin.com/in/",), min_score=0.5):
        d = json.load(open(path or os.path.join(VENDOR, "presidio", "patterns.json"), encoding="utf-8"))
        self.version, self.min_score, self.url_keep, self.iban_ok = d["version"], min_score, url_keep, iban_ok
        self.iban_fmt = {k: re.compile(v) for k, v in d["iban_regex_per_country"].items()}
        self.rules = []
        for r in d["recognizers"]:
            fl = 0
            for f in r["flags"].split("|"):
                fl |= getattr(re, f)
            if r["type"] in ("EMAIL", "CARD", "SSN", "IP"):
                fl &= ~re.IGNORECASE         # case-symmetric patterns: same matches, ~2x faster in Python re
            for name, rx, score in r["patterns"]:
                self.rules.append((r["type"], re.compile(rx, fl), score, tuple(r["context"]), name))

    def _validate(self, t, v):
        """True = confirmed (Presidio raises the score to max), False = rejected, None = keep the pattern score."""
        if t == "CARD":
            d = re.sub(r"[ -]", "", v)
            return luhn(d) if d.isdigit() else False
        if t == "IBAN":
            c = re.sub(r"[ -]", "", v)
            return bool(self.iban_ok and self.iban_ok(c) and c[:2] in self.iban_fmt and self.iban_fmt[c[:2]].match(c))
        if t == "EMAIL":      # Presidio checks the domain with tldextract; we require an alphabetic TLD instead
            return None if re.search(r"\.[A-Za-z]{2,}$", v) else False
        if t == "SSN" and _ssn_invalid(v):
            return False
        if t == "IP" and _ip_invalid(v):
            return False
        return None

    def find(self, text, low):
        out = []
        text = _ESC.sub("  ", text)          # a literal \n before a value is not a word character (same length)
        url_on = any(k in low for k in self.url_keep)
        v6_on = "::" in text or _V6_FULL.search(text) is not None
        for t, rx, score, ctx, name in self.rules:
            if (t == "URL" and not url_on) or (name.startswith("IPv6") or name.startswith("IPv4_") and not v6_on):
                continue                     # speed gates: no match is possible without these substrings
            if score < self.min_score and t in _NO_TRUE and not any(c.strip() in low for c in ctx):
                continue                     # would be dropped below for want of a context word
            for m in rx.finditer(text):
                s, e = m.span()
                while e > s and text[s] in "\"'" and text[e - 1] in "\"'":
                    s, e = s + 1, e - 1
                if t == "URL" and "\\" in text[s:e]:
                    e = text.index("\\", s)        # a JSON-escaped newline is not part of the URL
                while e > s and text[s] == "|":
                    s += 1
                v = text[s:e]
                if not v:
                    continue
                if t == "URL":
                    if not any(k in v.lower() for k in self.url_keep):
                        continue
                    t2 = "PROFILE"
                else:
                    t2 = t
                ok = self._validate(t, v)
                if ok is False:
                    continue
                if ok is None and score < self.min_score:      # weak pattern: needs a context word in the 5 words before
                    before = low[max(0, s - 80):s].split()[-5:]
                    if not any(c.strip() in w for w in before for c in ctx):
                        continue
                out.append((s, e, t2, v))
        return out


# ---------------------------------------------------------------- gitleaks default rules (vendored)
_POSIX = {"[:alnum:]": "A-Za-z0-9", "[:alpha:]": "A-Za-z", "[:digit:]": "0-9", "[:space:]": r"\s", "[:word:]": r"\w",
          "[:upper:]": "A-Z", "[:lower:]": "a-z", "[:xdigit:]": "0-9A-Fa-f", "[:punct:]": r"!-/:-@\[-`{-~"}


def go_regex(rx):
    """Go RE2 -> Python re. A mid-pattern (?i) becomes a whole-pattern flag (slightly broader)."""
    for k, v in _POSIX.items():
        rx = rx.replace(k, v)
    rx = rx.replace(r"\z", r"\Z")
    fl = 0
    if "(?i)" in rx:
        rx, fl = rx.replace("(?i)", ""), re.I
    return re.compile(rx, fl)


def _entropy(s):
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in (s.count(ch) for ch in set(s))) if n else 0.0


def _allow_list(a):
    return {"target": a.get("regexTarget", "secret"), "rx": [go_regex(x) for x in a.get("regexes", [])],
            "stop": [w.lower() for w in a.get("stopwords", [])], "and": a.get("condition", "OR").upper() == "AND"}


class Gitleaks:
    def __init__(self, path=None, extra=None):
        path = path or os.path.join(VENDOR, "gitleaks", "gitleaks.toml")
        d = tomllib.load(open(path, "rb"))
        if extra and os.path.exists(extra):      # extra rules, same format, for keys gitleaks has no rule for
            d["rules"] = d.get("rules", []) + tomllib.load(open(extra, "rb")).get("rules", [])
        self.rules, self.skipped, self.path_only, kw = [], {}, 0, {}
        self.allow = [_allow_list(a) for a in ([d["allowlist"]] if "allowlist" in d else []) + d.get("allowlists", [])]
        for r in d.get("rules", []):
            if not r.get("regex"):
                self.path_only += 1
                continue
            try:
                rx = go_regex(r["regex"])
                allow = [_allow_list(a) for a in r.get("allowlists", [])]
            except re.error as e:
                self.skipped[r["id"]] = str(e)[:60]
                continue
            i = len(self.rules)
            self.rules.append((r["id"], rx, int(r.get("secretGroup", 0)), float(r.get("entropy", 0)), allow))
            for k in r.get("keywords") or ["\0always"]:
                kw.setdefault(k.lower(), []).append(i)
        self.always = kw.pop("\0always", [])
        self.kw = kw
        self.kw_a = _automaton(list(kw))

    def _allowed(self, secret, match, line):
        for a in self.allow_now:
            tgt = {"match": match, "line": line}.get(a["target"], secret)
            checks = []
            if a["rx"]:
                checks.append(any(r.search(tgt) for r in a["rx"]))
            if a["stop"]:
                checks.append(any(w in secret.lower() for w in a["stop"]))
            if checks and (all(checks) if a["and"] else any(checks)):
                return True
        return False

    def _windows(self, text, low, hits, full, near=False):
        if full:
            return [(0, len(text))]
        ws = []
        for p in sorted(hits):      # keyword-anchored rules: a match starts <= 50 chars before its keyword
            a, b = max(0, p - (80 if near else 250)), min(len(text), p + (400 if near else 2000))
            if ws and a <= ws[-1][1]:
                ws[-1] = (ws[-1][0], max(ws[-1][1], b))
            else:
                ws.append((a, b))
        return ws

    def find(self, text, low):
        hits = {i: [] for i in self.always}
        for end, k in self.kw_a.iter(low):
            for i in self.kw[k]:
                hits.setdefault(i, []).append(end - len(k) + 1)
        out = []
        for i in sorted(hits):
            rid, rx, grp, ent, allow = self.rules[i]
            self.allow_now = allow + self.allow
            full = i in self.always or "PRIVATE" in rx.pattern or r"[\s\S" in rx.pattern
            for a, b in self._windows(text, low, hits[i], full, rid.startswith("generic")):
                out += self._scan(text, a, b, rid, rx, grp, ent)
        return out

    def _scan(self, text, a, b, rid, rx, grp, ent):
        out = []
        if True:
            for m in rx.finditer(text, a, b):
                g = grp if grp else next((j for j in range(1, (m.lastindex or 0) + 1) if m.group(j)), 0)
                s, e = m.span(g)
                secret = text[s:e]
                if not secret:
                    continue
                if ent and (_entropy(secret) <= ent or (rid.startswith("generic") and not re.search(r"\d", secret))):
                    continue
                ls, le = text.rfind("\n", 0, m.start()) + 1, text.find("\n", m.end())
                if self._allowed(secret, m.group(0), text[ls:le if le >= 0 else len(text)]):
                    continue
                out.append((s, e, "SECRET", secret))
        return out


def versions():
    from importlib.metadata import version
    return {p: version(p) for p in ("phonenumbers", "pyahocorasick")}