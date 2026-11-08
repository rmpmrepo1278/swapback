"""pii_scrubber.core: swap known personal values and PII patterns for stable placeholders before a chat request leaves
your machine, and put the real values back in the response.

Plain functions over OpenAI-style JSON; the HTTP proxy (pii_scrubber.proxy) is a thin adapter. No ML/NER: what is
swapped is what you list (known values, private terms) plus what the pattern detectors find. Never logs or prints a
real value: counts and types only.

Pieces
  Known values  a YAML/JSON file you write (known_file): {TYPE: [values]} or [[value, TYPE], ...]. Matched with
                pyahocorasick, case-insensitive, whole words. PHONE entries are expanded to their common formats.
  Private terms an optional YAML file (private_terms_file): `terms:` phrases or regexes -> «PRIVATE_TERM_n»,
                `figures:` money amounts expanded to every written format ($1.5B, 1,500M, USD 1.5 billion, ...)
                -> «PRIVATE_FIGURE_n».
  Patterns      phones via phonenumbers; email, Luhn card, IBAN, US SSN, IP, URL via Presidio's predefined recognizer
                patterns (vendored); API keys and tokens via the gitleaks default rules (vendored); a few local rules
                where no library exists (street address, keyword-anchored account number / date of birth, order no.).
  Vault         value -> «TYPE_n», one stable map in state_dir/vault.json (0600, local only).
  Restore       exact «TYPE_n», and bare TYPE_n when it is in the vault; streamed text holds back a possibly split
                placeholder until the next chunk; tool-call arguments get JSON-escaped values.
  Fail closed   any error while scrubbing raises ScrubError (the proxy answers 503 and nothing is forwarded); after the
                swap the outgoing strings are re-checked for known values and the request is blocked if one is left.
  Paths         a known value that is one segment of a filesystem path (/home/<user>/..., ~/...) is left alone, so
                agents' file tools keep working; restore keeps the original case of every value.
CLI: pii-scrubber stats | health | canary | scrub-file IN.json | check-config
"""
import json, os, re, sys, threading, time, hashlib, tempfile

L, R = "\u00ab", "\u00bb"
TYPES = ("PRIVATE_FIGURE", "PRIVATE_TERM", "PERSON", "EMAIL", "PHONE", "ADDRESS", "SSN", "CARD", "IBAN", "ACCOUNT", "DOB",
         "ORDER", "IP", "HOST", "SECRET", "PROFILE", "ORG", "ID", "FAMILY")
PH_RE = re.compile(L + r"([A-Z]+(?:_[A-Z]+)*)_(\d+)" + R)
JS_PH_RE = re.compile(r"\\u00[aA][bB]([A-Z]+(?:_[A-Z]+)*)_(\d+)\\u00[bB][bB]")    # «» escaped inside JSON text
BARE_RE = re.compile(r"(?<![A-Za-z0-9_" + L + r"])(" + "|".join(TYPES) + r")_(\d+)(?![0-9A-Za-z_" + R + r"])")
CANARY = [("Zorblat Quenneville", "PERSON"), ("zorblat.canary@example.org", "EMAIL")]   # fake, used by `canary`
DEFAULTS = {"mode": "on", "listen": "127.0.0.1:8787", "upstream": "https://api.openai.com", "allow_remote": False,
            "known_file": "", "private_terms_file": "", "state_dir": "", "alert_webhook": "", "alert_min_interval_s": 3600,
            "log_requests": True, "restore_bare": True, "patterns": {}, "phone_regions": ["US"],
            "url_swap": ["linkedin.com/in/"], "extra_secret_rules": "", "cache_items": 4000, "canary_model": "",
            "upstream_timeout_s": 900,
            "system_note": "Note: values written like \u00abPERSON_3\u00bb or \u00abEMAIL_1\u00bb are privacy stand-ins that the "
                           "user's own device swaps back to the real value. Use them exactly as written, including in "
                           "tool arguments, and do not mention them."}
ENV = {"mode": "PII_SCRUBBER_MODE", "listen": "PII_SCRUBBER_LISTEN", "upstream": "PII_SCRUBBER_UPSTREAM",
       "known_file": "PII_SCRUBBER_KNOWN_FILE", "private_terms_file": "PII_SCRUBBER_PRIVATE_TERMS",
       "state_dir": "PII_SCRUBBER_STATE_DIR", "alert_webhook": "PII_SCRUBBER_ALERT_WEBHOOK",
       "canary_model": "PII_SCRUBBER_CANARY_MODEL"}
PATH_KEYS = ("known_file", "private_terms_file", "state_dir", "extra_secret_rules")


class ScrubError(Exception):
    pass


# ---------------------------------------------------------------- config
def config_path():
    p = os.environ.get("PII_SCRUBBER_CONFIG")
    if p:
        return p
    return "pii-scrubber.yaml" if os.path.exists("pii-scrubber.yaml") else ""


def load_cfg(path=None):
    """defaults <- YAML config file (PII_SCRUBBER_CONFIG, or ./pii-scrubber.yaml) <- PII_SCRUBBER_* env vars.
    Relative file paths in the config file are relative to that file."""
    import yaml
    c = dict(DEFAULTS)
    path = path if path is not None else config_path()
    base = os.getcwd()
    if path:
        with open(path, encoding="utf-8") as f:
            c.update(yaml.safe_load(f) or {})
        base = os.path.dirname(os.path.abspath(path))
    for k in PATH_KEYS:
        v = c.get(k)
        if v:
            c[k] = os.path.join(base, os.path.expanduser(str(v)))
    for k, env in ENV.items():
        if os.environ.get(env):
            c[k] = os.path.abspath(os.path.expanduser(os.environ[env])) if k in PATH_KEYS else os.environ[env]
    m = c.get("mode")
    c["mode"] = "on" if m is True else "off" if m is False else str(m).strip().lower()   # YAML reads on/off as bools
    if c["mode"] not in ("off", "watch", "on"):
        raise ValueError("mode must be on, watch or off")
    return c


def state_dir(cfg):
    d = cfg.get("state_dir") or os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
                                             "pii-scrubber")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _write_private(path, obj):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ---------------------------------------------------------------- patterns
def iban_ok(v):
    v = v.replace(" ", "").upper()
    if not 15 <= len(v) <= 34:
        return False
    r = v[4:] + v[:4]
    try:
        return int("".join(str(int(c, 36)) for c in r)) % 97 == 1
    except ValueError:
        return False


SUFFIX = (r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Way|Ln|Lane|Dr|Drive|Ct|Court|Pl|Place|Ter|Terrace|Pkwy|"
          r"Parkway|Hwy|Highway|Cir|Circle|Loop|Trl|Trail)")
PATTERNS = [   # (TYPE, regex, group). Only types no maintained library covers; the rest are in scrub_detect.py
    ("HOST", re.compile(r"(?i)(?<![A-Za-z0-9\-.])[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)*\.ts\.net\b"), 0),
    ("ORDER", re.compile(r"(?<![\d-])\d{3}-\d{7}-\d{7}(?![\d-])"), 0),
    ("DOB", re.compile(r"(?i)\b(?:born|dob|d\.o\.b\.?|date of birth|birthday|birth date)\b[\s:,\-]*(?:on\s+|is\s+)?"
                       r"(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|[A-Z][a-z]{2,8}\.? \d{1,2},? \d{4}|\d{4}-\d{2}-\d{2})"), 1),
    ("ACCOUNT", re.compile(r"(?i)\b(?:account|acct|routing|a/c)\s*(?:no\.?|number|num|#|id)?\s*[:#]?\s*((?:\d[ -]?){6,16}\d)\b"), 1),
    ("ADDRESS", re.compile(r"\b\d{1,6}(?:-\d{1,4})?\s+(?:[NSEW]{1,2}\.?\s+)?(?:\d{1,3}(?:st|nd|rd|th)\s+|[A-Z][a-z]+\s+){1,3}"
                           + SUFFIX + r"\b\.?(?:\s+(?:NE|NW|SE|SW|[NSEW])\b)?(?:,?\s+(?:Apt|Unit|Suite|Ste|#)\.?\s*[A-Za-z0-9\-]+)?"), 0),
    # gitleaks targets source code and misses tokens inside URL query strings (?..._token=<uuid>&...)
    ("SECRET", re.compile(r"(?i)[?&][\w.-]*(?:token|key|secret|sig|auth|code|passw)[\w.-]*="
                          r"((?=[^&#\s\"']*\d)(?=[^&#\s\"']*[A-Za-z])[A-Za-z0-9_\-.%~+/]{16,})"), 1),
]
# cheap substring gates: a pattern runs only when one of its literals is in the lowercased text
GATES = {0: (".ts.net",), 2: ("born", "dob", "d.o.b", "birth"), 3: ("acc", "routing", "a/c"), 5: ("token", "key", "secret",
         "sig", "auth", "code", "passw")}
SKIP_IP = re.compile(r"^(?:127\.|0\.0\.0\.0|255\.|1\.1\.1\.1$|8\.8\.[48]\.[48]$)")


def _valid(t, v):        # local policy on top of the library checks
    if t == "IP":
        if SKIP_IP.match(v) or ":" in v and v.startswith("::"):
            return False
        p = v.split("/")[0].split(".")
        return not (len(p) == 4 and int(p[0]) < 10 and all(int(x) < 50 for x in p[1:]))   # 1.2.3.4-style versions
    return True


_DET = {}
DEFAULT_EXTRA_RULES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "extra_rules.toml")


def detectors(extra=None):
    """library detectors, built once per process per extra-rules file (gitleaks compile ~50 ms)."""
    extra = extra or DEFAULT_EXTRA_RULES
    if extra not in _DET:
        from . import detect as d
        _DET[extra] = (d, d.Presidio(iban_ok=iban_ok), d.Gitleaks(extra=extra))
    return _DET[extra]


# ---------------------------------------------------------------- known values
def _norm_key(v):
    return re.sub(r"\s+", " ", v.strip().lower())


def _phone_variants(p):
    d = re.sub(r"\D", "", p)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) != 10:
        return [p] if len(d) >= 7 else []
    a, b, c = d[:3], d[3:6], d[6:]
    return [d, "1" + d, "+1" + d, f"{a}-{b}-{c}", f"({a}) {b}-{c}", f"{a}.{b}.{c}", f"+1 {a} {b} {c}", f"+1 ({a}) {b}-{c}",
            f"+1-{a}-{b}-{c}", f"{a} {b} {c}", f"+1 {a}-{b}-{c}"]


def load_known(path):
    """[(value, TYPE)] from a YAML or JSON file: {TYPE: [values]}, [[value, TYPE], ...] or {"known": [...]}.
    PHONE values are expanded to their common written formats. Raises ScrubError on a malformed file."""
    import yaml
    try:
        with open(path, encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
    except Exception as e:
        raise ScrubError("known_file unreadable: %s" % type(e).__name__) from None
    if isinstance(d, dict) and "known" in d:
        d = d["known"]
    pairs = []
    if isinstance(d, dict):
        for t, vals in d.items():
            for v in vals or []:
                pairs.append((v, t))
    elif isinstance(d, list):
        for x in d:
            if isinstance(x, dict):
                pairs.append((x.get("value"), x.get("type", "PERSON")))
            else:
                pairs.append(tuple(x))
    else:
        raise ScrubError("known_file must be a mapping or a list")
    out, seen = [], set()
    for v, t in pairs:
        t = str(t or "PERSON").upper()
        if t not in TYPES:
            raise ScrubError("known_file: unknown type %s" % t)
        v = str(v or "").strip()
        if len(v) < 2:
            continue
        for x in (_phone_variants(v) if t == "PHONE" else [v]):
            k = (_norm_key(x), t)
            if k not in seen:
                seen.add(k)
                out.append((x, t))
    return out


# ---------------------------------------------------------------- custom private terms
# Things that are not PII but must never reach a provider (a codename, an internal figure). Generic: the values live
# only in your private_terms_file; figures are dollar amounts expanded here to every written format.
_CUR = r"(?:US\$|USD\s?|\$)\s?"
_UNITS = ((1e9, r"(?:billion|bn|bil|b)"), (1e6, r"(?:million|mm|mn|mil|m)"))


def _num_re(x, opt_zero=True):
    s = ("%.6f" % x).rstrip("0").rstrip(".")
    ip, _, dp = s.partition(".")
    g = []
    while len(ip) > 3:
        g.insert(0, ip[-3:])
        ip = ip[:-3]
    g.insert(0, ip)
    ipr = ",?".join(g)
    if ipr == "0" and dp:
        ipr = "0?"
    if dp:
        return ipr + r"\." + dp + "0*"
    return ipr + (r"(?:\.0+)?" if opt_zero else r"(?:\.00)?")


def figure_regex(amount, need_currency=False):
    """$1.5B, 1.5 billion, $1,500M, USD 1500 million, $1,500,000,000 ... for one dollar amount. Never a bare digit run,
    never a longer or different number (no digit, dot or comma may touch either end)."""
    a = float(amount)
    forms = []
    for unit, suf in _UNITS:
        n = a / unit
        if n >= 0.1 and abs(round(n, 3) - n) < 1e-9 * max(1.0, n):
            forms.append(_num_re(round(n, 3)) + r"[\s-]?(?i:" + suf + r")(?![A-Za-z0-9&])")   # not M&A
    alts = []
    if forms:
        alts.append((_CUR if need_currency else "(?:" + _CUR + ")?") + "(?:" + "|".join(forms) + ")")
    alts.append(_CUR + _num_re(a, opt_zero=False) + r"(?![\d.,]?\d)")
    return r"(?<![\w.,])(?:" + "|".join(alts) + ")"


def figure_gates(amount):
    """literal strings, one of which every figure_regex(amount) match contains once commas are dropped (a cheap skip)."""
    a, out = float(amount), []
    for unit, _suf in _UNITS:
        n = a / unit
        if n >= 0.1 and abs(round(n, 3) - n) < 1e-9 * max(1.0, n):
            s = ("%.6f" % round(n, 3)).rstrip("0").rstrip(".")
            out.append(s[1:] if s.startswith("0.") else s)
    out.append(("%.2f" % a).rstrip("0").rstrip("."))
    return sorted(set(out))


def build_private_terms(path):
    """-> ([[TYPE, regex, gates], ...], {"by_type": {...}, "bad": n}) from a private terms file:
        terms:   ["Project Bluebird", {"regex": "blue ?bird v\\d"}]   -> PRIVATE_TERM (case-insensitive, whole words)
        figures: [2500000, {"amount": 42e6, "need_currency": true}]     -> PRIVATE_FIGURE (all written formats)
    A bad entry is counted, not fatal. Counts only; never prints a value."""
    import yaml
    try:
        with open(path, encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
    except Exception as e:
        raise ScrubError("private_terms_file unreadable: %s" % type(e).__name__) from None
    rules, by, bad = [], {}, 0
    for x in d.get("terms") or []:
        try:
            if isinstance(x, dict) and "regex" in x:
                body = str(x["regex"])
            else:
                body = r"\s+".join(re.escape(w) for w in str(x.get("value") if isinstance(x, dict) else x).split())
            rx = r"(?i:(?<![\w-])(?:" + body + r")(?![\w-]))"
            re.compile(rx)
        except Exception:  # noqa: BLE001
            bad += 1
            continue
        rules.append(["PRIVATE_TERM", rx, None])
        by["PRIVATE_TERM"] = by.get("PRIVATE_TERM", 0) + 1
    for x in d.get("figures") or []:
        try:
            x = x if isinstance(x, dict) else {"amount": x}
            rx = figure_regex(x["amount"], bool(x.get("need_currency")))
            re.compile(rx)
        except Exception:  # noqa: BLE001
            bad += 1
            continue
        rules.append(["PRIVATE_FIGURE", rx, figure_gates(x["amount"])])
        by["PRIVATE_FIGURE"] = by.get("PRIVATE_FIGURE", 0) + 1
    return rules, {"by_type": by, "bad": bad}


# ---------------------------------------------------------------- filesystem paths
# A known name that is one segment of a filesystem path (/home/<user>/x, ~/x, a/b/<name>/c) is a folder name, not a
# person: swapping it breaks agent tool calls (read_file on a path whose case changed). URLs and domains still swap.
_PATH_STOP = frozenset(" \t\r\n\"'`<>()[]{},;|=*")
_DOMAIN_HEAD = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?::\d+)?/")


def in_fs_path(text, s, e):
    """True when text[s:e] is a whole segment of a POSIX-looking path token."""
    seg = text[s:e]
    if not seg or any(c in _PATH_STOP or c == "/" for c in seg):
        return False
    before = text[s - 1] if s else ""
    after = text[e] if e < len(text) else ""
    if before != "/" and after != "/":
        return False
    a, b, n = s, e, len(text)
    while a > 0 and text[a - 1] not in _PATH_STOP:
        a -= 1
    while b < n and text[b] not in _PATH_STOP:
        b += 1
    tok = text[a:b]
    if "://" in tok or "@" in tok or tok.lower().startswith("www."):
        return False                                   # a URL or an address, not a file
    if tok.startswith(("/", "~", "./", "../", "\\/")):
        return True                                    # absolute, home-relative or explicit relative path
    if _DOMAIN_HEAD.match(tok):
        return False                                   # example.com/<name>
    return tok.count("/") >= 2                         # a/b/c relative path; "Jane/Ozzie" stays a name pair


class Scrubber:
    def __init__(self, cfg=None, known=None, vault_path=None, private=None):
        """known: [(value, TYPE)] to use instead of cfg known_file; private: rules from build_private_terms() to use
        instead of cfg private_terms_file. The fake canary values are always part of the known list."""
        self.cfg = cfg or load_cfg()
        self.lock = threading.RLock()
        self.sdir = state_dir(self.cfg)
        self.vault_path = vault_path or os.path.join(self.sdir, "vault.json")
        self.known_path = self.cfg.get("known_file") or ""
        self.private_path = self.cfg.get("private_terms_file") or ""
        self.known_mtime = None
        self.cache = {}
        self.verified = set()
        self.private = []
        self._known_fixed, self._private_fixed = known is not None, private is not None
        self._load_vault()
        if known is not None:
            self._set_known(list(known) + CANARY)
        if private is not None:
            self._set_private(private)
        self.reload_known(force=True)
        self.on = self.cfg.get("patterns") or {}
        self.patterns = [(t, p, g, GATES.get(i)) for i, (t, p, g) in enumerate(PATTERNS) if self.on.get(t, True)]
        self.det, self.presidio, self.gitleaks = detectors(self.cfg.get("extra_secret_rules"))
        self.presidio.url_keep = tuple(self.cfg.get("url_swap") or ("linkedin.com/in/",))
        self.regions = tuple(self.cfg.get("phone_regions") or ("US", "IN"))

    # vault ---------------------------------------------------------
    def _load_vault(self):
        try:
            v = json.load(open(self.vault_path, encoding="utf-8"))
        except (OSError, ValueError):
            v = {}
        self.fwd = v.get("fwd") or {}            # norm key -> placeholder
        self.rev = v.get("rev") or {}            # placeholder -> value
        self.ctr = v.get("ctr") or {}
        self.dirty = False

    def save_vault(self):
        with self.lock:
            if self.dirty:
                _write_private(self.vault_path, {"fwd": self.fwd, "rev": self.rev, "ctr": self.ctr})
                self.dirty = False

    def placeholder(self, value, t):
        k = t + "|" + _norm_key(value) if t not in ("PHONE",) else "PHONE|" + re.sub(r"\D", "", value)[-10:]
        with self.lock:
            ph = self.fwd.get(k)
            if ph is not None and self.rev.get(ph) != value:     # phones too: each written form keeps its own stand-in
                k += "\x00" + value               # a case/spacing variant gets its own stand-in: restore is exact
                ph = self.fwd.get(k)
            if ph is None:
                n = int(self.ctr.get(t, 0)) + 1
                self.ctr[t] = n
                ph = f"{L}{t}_{n}{R}"
                self.fwd[k] = ph
                self.rev[ph] = value
                self.dirty = True
            return ph

    # known ---------------------------------------------------------
    def _set_known(self, known):
        self.known = {}
        for v, t in known:
            self.known[_norm_key(v)] = (v, t)
        self.known_m = detectors()[0].KnownMatcher(list(self.known))
        self.cache = {}
        self.verified = set()

    def _set_private(self, rules):
        by, gates = {}, {}           # one alternation per type: one pass over the text each, not one per entry
        for r in rules or []:
            t, rx, g = (list(r) + [None])[:3]
            by.setdefault(t, []).append("(?:%s)" % rx)
            gates[t] = None if (not g or (t in gates and gates[t] is None)) else gates.get(t, []) + list(g)
        # gates: skip a type's regex when none of its literals is in the text (commas dropped); None = always run
        self.private = [(t, re.compile("|".join(v)), tuple(gates[t]) if gates[t] else None) for t, v in by.items()]
        self.cache = {}
        self.verified = set()

    def _private_hits(self, text, low=None):
        if not self.private:
            return
        low2 = (low if low is not None else text.lower()).replace(",", "")    # gates only test containment
        for t, rx, g in self.private:
            if g and not any(x in low2 for x in g):
                continue
            for m in rx.finditer(text):
                if m.end() > m.start():
                    yield m.start(), m.end(), t, m.group(0)

    def _mtimes(self):
        out = []
        for p in (self.known_path, self.private_path):
            if not p:
                out.append(None)
                continue
            try:
                out.append(os.stat(p).st_mtime)
            except OSError:
                raise ScrubError("configured file is missing: %s" % os.path.basename(p)) from None
        return tuple(out)

    def reload_known(self, force=False):
        """re-read known_file / private_terms_file when they change. A configured file that is missing or unreadable
        raises ScrubError, so the proxy fails closed instead of sending text it was told to protect."""
        if self._known_fixed and self._private_fixed:
            return
        mt = self._mtimes()
        if not force and mt == self.known_mtime:
            return
        with self.lock:
            if not self._known_fixed:
                self._set_known((load_known(self.known_path) if self.known_path else []) + CANARY)
            if not self._private_fixed:
                self._set_private(build_private_terms(self.private_path)[0] if self.private_path else [])
            self.known_mtime = mt

    # spans ---------------------------------------------------------
    def spans(self, text):
        sp = []
        low = self.det.lower_same_len(text)
        for s, e, k in self._known_hits(text, low):
            v, t = self.known.get(k, (text[s:e], "PERSON"))
            sp.append((s, e, t, 0, text[s:e]))       # the surface form, so restore keeps its case
        for s, e, t, v in self._private_hits(text, low):
            sp.append((s, e, t, 0, v))
        found = self.presidio.find(text, low) + self.gitleaks.find(text, low)
        if any(c.isdigit() for c in text):
            found += self.det.find_phones(text, self.regions)
        for s, e, t, v in found:
            if self.on.get(t, True) and e > s and _valid(t, v):
                sp.append((s, e, t, 1, v))
        for t, p, g, gate in self.patterns:
            if gate and not any(x in low for x in gate):
                continue
            for m in p.finditer(text):
                s, e = m.span(g)
                v = text[s:e]
                if e <= s or not _valid(t, v):
                    continue
                sp.append((s, e, t, 1, v))
        sp.sort(key=lambda x: (x[0], x[3], -(x[1] - x[0])))
        res, last = [], -1
        for s, e, t, pr, v in sp:
            if s >= last:
                res.append((s, e, t, v))
                last = e
            elif res and e > res[-1][1] and s == res[-1][0]:
                res[-1] = (s, e, t, v)
                last = e
        return res

    def scrub_text(self, text, counts=None):
        if not text or len(text) < 3:
            return text
        h = hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=16).digest()
        c = self.cache.get(h)
        if c is not None:
            if counts is not None:
                for t, n in c[1].items():
                    counts[t] = counts.get(t, 0) + n
            return c[0]
        out, pos, local = [], 0, {}
        for s, e, t, v in self.spans(text):
            out.append(text[pos:s])
            out.append(self.placeholder(text[s:e], t))   # vault keeps the exact surface form: restore is byte for byte (fix2)
            local[t] = local.get(t, 0) + 1
            pos = e
        out.append(text[pos:])
        r = "".join(out)
        if len(self.cache) > int(self.cfg.get("cache_items", 4000)):
            self.cache.clear()
            self.verified.clear()
        self.cache[h] = (r, local)
        if counts is not None:
            for t, n in local.items():
                counts[t] = counts.get(t, 0) + n
        return r

    def leftover_known(self, text):
        """egress check: known values still present (count only)."""
        if not text:
            return 0
        return len(self._known_hits(text)) + sum(1 for _x in self._private_hits(text))

    def _known_hits(self, text, low=None):
        """known-value hits, minus those that are a segment of a filesystem path (/home/<user>/..., ~/...)."""
        hits = self.known_m.find(text, low)
        if hits and "/" in text:
            hits = [h for h in hits if not in_fs_path(text, h[0], h[1])]
        return hits

    def path_kept(self, text):
        """how many known-value hits in text were left alone because they sit inside a filesystem path."""
        if not text or "/" not in text:
            return 0
        return sum(1 for s, e, _k in self.known_m.find(text) if in_fs_path(text, s, e))

    # request -------------------------------------------------------
    SKIP_KEYS = {"model", "role", "type", "tool_call_id", "id", "tool_choice", "response_format", "stop", "format",
                 "reasoning_effort", "service_tier", "user"}

    def scrub_request(self, body, counts=None):
        """mutates and returns body; raises ScrubError on anything unexpected (fail closed)."""
        counts = {} if counts is None else counts
        left = [0]

        def walk(o, key=None, parent=None):
            if isinstance(o, dict):
                is_fn = "parameters" in o or "arguments" in o
                for k in list(o):
                    v = o[k]
                    if k in self.SKIP_KEYS or (k == "name" and is_fn):
                        if isinstance(v, str):
                            left[0] += self.leftover_known(v)
                        continue
                    if k == "url" and isinstance(v, str) and v.startswith("data:"):
                        continue
                    o[k] = walk(v, k, o)
                return o
            if isinstance(o, list):
                return [walk(x, key, parent) for x in o]
            if isinstance(o, str):
                s = self.scrub_text(o, counts)
                h = hashlib.blake2b(s.encode("utf-8", "surrogatepass"), digest_size=16).digest()
                if h not in self.verified:
                    n = self.leftover_known(s)
                    left[0] += n
                    if not n:
                        self.verified.add(h)
                return s
            return o
        try:
            self.reload_known()
            if isinstance(body, dict):                # /v1/embeddings input, /v1/completions prompt as token ids: unreadable
                for k in ("input", "prompt"):
                    v = body.get(k)
                    if isinstance(v, int) or (isinstance(v, list) and any(
                            isinstance(x, int) or (isinstance(x, list) and any(isinstance(y, int) for y in x)) for x in v)):
                        raise ScrubError("token-id %s cannot be checked" % k)
            walk(body)
            note = self.cfg.get("system_note")
            msgs = body.get("messages") if isinstance(body, dict) else None
            if note and isinstance(msgs, list) and msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system" \
                    and isinstance(msgs[0].get("content"), str) and note not in msgs[0]["content"]:
                msgs[0]["content"] += "\n\n" + note
        except ScrubError:
            raise
        except Exception as e:
            raise ScrubError("scrub failed: %s" % type(e).__name__) from None
        if left[0]:
            raise ScrubError("egress check: %d known value(s) left after the swap" % left[0])
        self.save_vault()
        return body

    # restore -------------------------------------------------------
    def _val(self, ph, js):
        v = self.rev.get(ph)
        if v is None:
            return None
        return json.dumps(v, ensure_ascii=False)[1:-1] if js else v

    def restore_text(self, text, js=False, stats=None):
        if not text or ("_" not in text):
            return text

        def rep(m):
            v = self._val(m.group(0), js)
            if v is None:
                if stats is not None:
                    stats["unknown"] = stats.get("unknown", 0) + 1
                return m.group(0)
            if stats is not None:
                stats["restored"] = stats.get("restored", 0) + 1
            return v
        text = PH_RE.sub(rep, text)
        if js and "\\u00" in text:
            text = JS_PH_RE.sub(lambda m: self._val(f"{L}{m.group(1)}_{m.group(2)}{R}", True) or m.group(0), text)
        if self.cfg.get("restore_bare", True):
            def rep2(m):
                ph = f"{L}{m.group(1)}_{m.group(2)}{R}"
                v = self._val(ph, js)
                if v is None:
                    return m.group(0)
                if stats is not None:
                    stats["restored"] = stats.get("restored", 0) + 1
                return v
            text = BARE_RE.sub(rep2, text)
        return text

    def restore_obj(self, o, stats=None, key=None):
        if isinstance(o, dict):
            return {k: self.restore_obj(v, stats, k) for k, v in o.items()}
        if isinstance(o, list):
            return [self.restore_obj(x, stats, key) for x in o]
        if isinstance(o, str):
            return self.restore_text(o, js=(key == "arguments"), stats=stats)
        return o

    def stream_restorer(self, js=False, stats=None):
        return StreamRestorer(self, js, stats)


_BARE_TAIL = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z_]*\d*)$")
_TYPE_PREFIXES = {(t + "_")[:i] for t in TYPES for i in range(1, len(t) + 2)}
_BARE_FULL = re.compile(r"(?:" + "|".join(TYPES) + r")_\d+")


class StreamRestorer:
    """feed(chunk) -> text safe to emit now; a tail that may be a split placeholder waits for the next chunk."""

    def __init__(self, scr, js=False, stats=None):
        self.s, self.js, self.stats, self.buf = scr, js, stats, ""

    def _cut(self, b):
        i = b.rfind(L)
        if i != -1 and R not in b[i:] and len(b) - i <= 24:
            return i
        if self.s.cfg.get("restore_bare", True):
            m = _BARE_TAIL.search(b[-24:])
            if m:
                w = m.group(1)
                if w in _TYPE_PREFIXES or _BARE_FULL.fullmatch(w):
                    return len(b) - len(w)
        return len(b)

    def feed(self, chunk):
        if not chunk:
            return chunk
        b = self.buf + chunk
        cut = self._cut(b)
        self.buf = b[cut:]
        return self.s.restore_text(b[:cut], self.js, self.stats)

    def flush(self):
        b, self.buf = self.buf, ""
        return self.s.restore_text(b, self.js, self.stats)


def restore_sse_event(scr, ev, state):
    """ev = parsed chat.completion.chunk dict; state = per-stream dict of restorers. Mutates ev."""
    for ch in ev.get("choices") or []:
        i = ch.get("index", 0)
        d = ch.get("delta") or {}
        for f in ("content", "reasoning_content", "reasoning"):
            if isinstance(d.get(f), str):
                r = state.setdefault((i, f), scr.stream_restorer(False, state.setdefault("_stats", {})))
                d[f] = r.feed(d[f])
        for tc in d.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if isinstance(fn.get("arguments"), str):
                r = state.setdefault((i, "tc", tc.get("index", 0)), scr.stream_restorer(True, state.setdefault("_stats", {})))
                fn["arguments"] = r.feed(fn["arguments"])
        if ch.get("finish_reason"):
            _flush_choice(scr, ch, i, state)
    return ev


def _flush_choice(scr, ch, i, state):
    d = ch.setdefault("delta", {})
    for k, r in list(state.items()):
        if not isinstance(k, tuple) or k[0] != i:
            continue
        rest = r.flush()
        if not rest:
            continue
        if k[1] == "tc":
            tcs = d.setdefault("tool_calls", [])
            tc = next((t for t in tcs if t.get("index", 0) == k[2]), None)
            if tc is None:
                tc = {"index": k[2], "function": {"arguments": ""}}
                tcs.append(tc)
            fn = tc.setdefault("function", {})
            fn["arguments"] = (fn.get("arguments") or "") + rest
        else:
            d[k[1]] = (d.get(k[1]) or "") + rest


def flush_all(scr, state, template=None):
    """leftovers at [DONE]/EOF -> one synthetic chunk (or None)."""
    ev = dict(template or {})
    ev["choices"] = []
    idx = sorted({k[0] for k in state if isinstance(k, tuple)})
    for i in idx:
        ch = {"index": i, "delta": {}, "finish_reason": None}
        _flush_choice(scr, ch, i, state)
        if ch["delta"]:
            ev["choices"].append(ch)
    return ev if ev["choices"] else None


# ---------------------------------------------------------------- alerts (optional generic webhook)
def alert(cfg, text):
    """POST {"text": "pii-scrubber: ..."} to cfg alert_webhook (Slack/Discord-compatible incoming webhook), at most once
    per alert_min_interval_s. The text never includes a value. Returns a status word."""
    import urllib.request
    url = cfg.get("alert_webhook")
    if not url:
        return "no_webhook"
    p = os.path.join(state_dir(cfg), "alert_last.json")
    try:
        last = json.load(open(p)).get("ts", 0)
    except (OSError, ValueError):
        last = 0
    if time.time() - last < float(cfg.get("alert_min_interval_s", 3600)):
        return "rate_limited"
    try:
        req = urllib.request.Request(url, method="POST", data=json.dumps({"text": "pii-scrubber: " + text[:300]}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            ok = 200 <= r.status < 300
        _write_private(p, {"ts": time.time(), "ok": ok})
        return "sent" if ok else "refused"
    except Exception as e:
        return "error:" + type(e).__name__


# ---------------------------------------------------------------- health + canary
def _http(method, url, body=None, headers=None, timeout=120):
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read() or b"{}")


def health(cfg=None):
    cfg = cfg or load_cfg()
    try:
        return _http("GET", "http://%s/scrubber/health" % cfg["listen"], timeout=5)[1]
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}


def canary(cfg=None, model=None, auth=None):
    """Send a fake person + email through the running proxy and prove the provider got placeholders only:
    1) raw mode (X-Scrubber-Raw: 1): the provider's echo must not hold the fake values;
    2) normal mode: the swap-back must give the fake values back. auth: Authorization header value
    (default: "Bearer $OPENAI_API_KEY" when set)."""
    cfg = cfg or load_cfg()
    model = model or cfg.get("canary_model")
    res = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": cfg["mode"], "health": health(cfg)}
    if not model:
        res.update(error="set canary_model (config) or PII_SCRUBBER_CANARY_MODEL", **{"pass": False})
        return res
    fake = [v for v, _ in CANARY]
    msg = {"model": model, "stream": False, "max_tokens": 80, "temperature": 0,
           "messages": [{"role": "system", "content": "You copy text exactly. Output only the copied text."},
                        {"role": "user", "content": "Copy this line exactly: Contact %s at %s about the canary." % tuple(fake)}]}
    url = "http://%s/v1/chat/completions" % cfg["listen"]
    auth = auth or ("Bearer " + os.environ["OPENAI_API_KEY"] if os.environ.get("OPENAI_API_KEY") else None)
    h = {"Authorization": auth} if auth else {}
    try:
        _, raw = _http("POST", url, json.loads(json.dumps(msg)), dict(h, **{"X-Scrubber-Raw": "1"}))
        rt = ((raw.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        res["provider_saw_fake"] = any(f.lower() in rt.lower() for f in fake)
        res["provider_echoed_placeholder"] = bool(re.search(L + r"(PERSON|EMAIL)_\d+" + R, rt))
        _, rest = _http("POST", url, json.loads(json.dumps(msg)), h)
        tt = ((rest.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        res["restored_fake_back"] = any(f.lower() in tt.lower() for f in fake)
    except Exception as e:
        res["error"] = "request: " + type(e).__name__
    res["pass"] = cfg["mode"] == "on" and "error" not in res and not res.get("provider_saw_fake") \
        and bool(res.get("restored_fake_back"))
    _write_private(os.path.join(state_dir(cfg), "canary.json"), res)
    if not res["pass"] and cfg["mode"] == "on":
        alert(cfg, "canary FAILED (%s)" % (", ".join(k for k in ("provider_saw_fake", "error") if res.get(k)) or "check"))
    return res


def main(argv=None):
    argv = sys.argv if argv is None else argv
    cfg = load_cfg()
    cmd = argv[1] if len(argv) > 1 else "stats"
    if cmd == "stats":
        s = Scrubber(cfg)
        print(json.dumps({"mode": cfg["mode"], "known": len(s.known), "private_rules": len(s.private),
                          "vault": len(s.rev), "ctr": s.ctr}))
    elif cmd == "check-config":
        out = {"mode": cfg["mode"], "listen": cfg["listen"], "upstream": cfg["upstream"], "state_dir": state_dir(cfg)}
        if cfg.get("known_file"):
            k = load_known(cfg["known_file"])
            by = {}
            for _, t in k:
                by[t] = by.get(t, 0) + 1
            out["known_by_type"] = by
        if cfg.get("private_terms_file"):
            out["private"] = build_private_terms(cfg["private_terms_file"])[1]
        print(json.dumps(out))
    elif cmd == "scrub-file":
        s = Scrubber(cfg)
        counts = {}
        t0 = time.perf_counter()
        with open(argv[2], encoding="utf-8") as f:
            body = json.load(f)
        s.scrub_request(body, counts)
        print(json.dumps({"counts": counts, "ms": round((time.perf_counter() - t0) * 1000, 2)}))
    elif cmd == "health":
        print(json.dumps(health(cfg)))
    elif cmd == "canary":
        r = canary(cfg)
        print(json.dumps(r))
        sys.exit(0 if r["pass"] else 1)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
