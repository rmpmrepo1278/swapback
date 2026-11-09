"""swapback.known: build or extend your known-values file (known.yaml) from a contacts export.

    swapback known init --vcf contacts.vcf --csv people.csv [--scan prompts.jsonl] [--out known.yaml]

Sources: vCard (.vcf, 2.1 / 3.0 / 4.0: FN or N, EMAIL, TEL, ADR street), CSV (Google and Outlook contact exports, any
file with name / email / phone / address columns, or plain value,type rows) and other known files (--yaml). New values
are merged into the existing file without duplicates; the file is written 0600. --scan reads past prompts (text or
JSONL) and writes candidate names, emails and phones to a separate suggestions file for you to review; suggestions are
never added on their own. Prints counts only; --show also prints the values (on your own terminal)."""
import csv, io, json, os, quopri, re, sys, tempfile

from . import core as S

ORDER = ("PERSON", "FAMILY", "EMAIL", "PHONE", "ADDRESS", "ORG", "ID")
EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
# given names that are also everyday words: with --first-names they are skipped, since the match is case-insensitive
WORDLIKE = frozenset("""will may mark bill rose june april august grace hope joy faith art sue pat gene ray rob frank
jack dawn summer autumn chase miles max lane drew rich sky river sunny star penny ruby amber jade iris lily daisy
ivy holly hunter mason carter cash case dean don dot earl gay guy hazel king major neil nick page price reed rocky
sandy skip son stone storm terry victor wade ward warren wes will win young bo al ed ash bob""".split())
NAME_STOP = frozenset("""the this that these those a an and or but if then i we you he she they it my our your their
his her its monday tuesday wednesday thursday friday saturday sunday january february march april may june july
august september october november december dear hi hello hey thanks thank please note re fw fwd subject from to
cc mr mrs ms dr new old good best regards kind did does do can could would should will ask tell email call
send also remind text meet invite reply message ping book schedule cancel check find get let see""".split())
# single-word contact names that are relations or roles, not names
SKIP_SINGLE = WORDLIKE | frozenset("""mom mum dad mother father mama papa grandma grandpa nana sister brother wife husband
home work office doctor dentist plumber landlord boss babysitter nanny vet pharmacy school gym taxi uber""".split())


# ---------------------------------------------------------------- vCard
def _unfold(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"=\n", "=\x00", text)                          # quoted-printable soft breaks stay joined
    text = re.sub(r"\n[ \t]", "", text)                            # RFC 6350 folding
    return [l.replace("=\x00", "=\n") for l in text.split("\n")]


def _vval(params, value):
    p = params.upper()
    if "QUOTED-PRINTABLE" in p:
        cs = re.search(r"CHARSET=([\w-]+)", p)
        value = quopri.decodestring(value.replace("=\n", "").encode("latin-1", "replace")).decode(
            (cs.group(1) if cs else "utf-8").lower(), "replace")
    return value


def _unesc(v):
    return re.sub(r"\\([\\,;nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v).strip()


def _split(v, sep=";"):
    parts, cur, i = [], "", 0
    while i < len(v):
        if v[i] == "\\" and i + 1 < len(v):
            cur += v[i:i + 2]
            i += 2
            continue
        if v[i] == sep:
            parts.append(cur)
            cur = ""
        else:
            cur += v[i]
        i += 1
    parts.append(cur)
    return [_unesc(p) for p in parts]


def parse_vcf(text, first_names=False):
    """[(value, TYPE)] from vCard text."""
    out, card = [], None
    for line in _unfold(text):
        if not line.strip():
            continue
        name, _, value = line.partition(":")
        name = re.sub(r"^item\d+\.", "", name, flags=re.I)
        prop, _, params = name.partition(";")
        prop = prop.upper()
        if prop == "BEGIN" and value.strip().upper() == "VCARD":
            card = {"fn": None, "n": None, "vals": []}
            continue
        if card is None:
            continue
        if prop == "END":
            fn = card["fn"]
            given = None
            if card["n"]:
                fam, giv = (card["n"] + ["", ""])[:2]
                given = giv.split()[0] if giv.strip() else None
                if not fn:
                    fn = " ".join(x for x in (giv, fam) if x).strip()
            if fn and not (" " not in fn and fn.lower() in SKIP_SINGLE):
                out.append((fn, "PERSON"))
            if first_names and given and given.lower() not in WORDLIKE and fn and given != fn:
                out.append((given, "PERSON"))
            out += card["vals"]
            card = None
            continue
        value = _vval(params, value)
        if prop == "FN":
            card["fn"] = _unesc(value) or None
        elif prop == "N":
            card["n"] = _split(value)
        elif prop == "EMAIL":
            v = _unesc(value)
            if EMAIL_RE.fullmatch(v):
                card["vals"].append((v, "EMAIL"))
        elif prop == "TEL":
            v = re.sub(r"^tel:", "", _unesc(value), flags=re.I)
            if len(re.sub(r"\D", "", v)) >= 7:
                card["vals"].append((v, "PHONE"))
        elif prop == "ADR":
            parts = _split(value) + [""] * 7
            street = parts[2].replace("\n", ", ").strip()
            if len(street) >= 6 and re.search(r"\d", street):
                card["vals"].append((street, "ADDRESS"))
    return out


# ---------------------------------------------------------------- CSV
def _col_kind(h):
    h = h.strip().lower()
    if "type" in h or "label" in h:
        return None
    if "e-mail" in h or "email" in h:
        return "EMAIL"
    if any(w in h for w in ("phone", "mobile", "tel", "cell")):
        return "PHONE"
    if "street" in h or h.endswith("formatted") or h in ("address", "home address", "work address", "mailing address"):
        return "ADDRESS"
    if h in ("first name", "given name", "firstname", "first", "given"):
        return "GIVEN"
    if h in ("last name", "family name", "lastname", "surname", "last", "family"):
        return "FAMILY_NAME"
    if h in ("name", "full name", "fullname", "display name", "displayname", "contact", "contact name", "person"):
        return "PERSON"
    return None


def parse_csv(text, first_names=False):
    """[(value, TYPE)] from CSV text: a contacts export with a header row, or value,type rows."""
    rows = [r for r in csv.reader(io.StringIO(text.lstrip("\ufeff"))) if any(c.strip() for c in r)]
    if not rows:
        return []
    out = []
    head = [c.strip() for c in rows[0]]
    if len(head) >= 2 and head[0].lower() == "value" and head[1].lower() == "type":
        rows = rows[1:]
        head = None
    elif all(_col_kind(h) is None for h in head):
        head = None
    if head is None:                                               # value,type rows
        for r in rows:
            if len(r) >= 2 and r[1].strip().upper() in S.TYPES and r[0].strip():
                out.append((r[0].strip(), r[1].strip().upper()))
        return out
    kinds = [_col_kind(h) for h in head]
    for r in rows[1:]:
        given = family = ""
        full = None
        vals_row = []
        for k, c in zip(kinds, r):
            c = c.strip()
            if not k or not c:
                continue
            vals = [x.strip() for x in c.split(":::")]             # Google packs several values into one cell
            if k == "GIVEN":
                given = c
            elif k == "FAMILY_NAME":
                family = c
            elif k == "PERSON":
                full = full or c
            elif k == "EMAIL":
                vals_row += [(v, "EMAIL") for v in vals if EMAIL_RE.fullmatch(v)]
            elif k == "PHONE":
                vals_row += [(v, "PHONE") for v in vals if len(re.sub(r"\D", "", v)) >= 7]
            elif k == "ADDRESS":
                vals_row += [(v.replace("\n", ", "), "ADDRESS") for v in vals if len(v) >= 6 and re.search(r"\d", v)]
        name = full or " ".join(x for x in (given, family) if x)
        if name and not (" " not in name and name.lower() in SKIP_SINGLE):
            out.append((name, "PERSON"))
        g = given.split()[0] if given.strip() else (full.split()[0] if full and " " in full else "")
        if first_names and g and g.lower() not in WORDLIKE and g != name:
            out.append((g, "PERSON"))
        out += vals_row
    return out


# ---------------------------------------------------------------- known file I/O
def read_known_raw(path):
    """{TYPE: [values]} as written (no phone expansion); {} when the file does not exist."""
    import yaml
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    if isinstance(d, dict) and "known" in d:
        d = d["known"]
    out = {}
    if isinstance(d, dict):
        for t, vals in d.items():
            for v in vals or []:
                out.setdefault(str(t).upper(), []).append(str(v))
    elif isinstance(d, list):
        for x in d:
            v, t = (x.get("value"), x.get("type", "PERSON")) if isinstance(x, dict) else (list(x) + ["PERSON"])[:2]
            out.setdefault(str(t or "PERSON").upper(), []).append(str(v))
    else:
        raise S.ScrubError("known file must be a mapping or a list")
    return out


def _key(v, t):
    if t == "PHONE":
        d = re.sub(r"\D", "", v)
        return t, d[1:] if len(d) == 11 and d.startswith("1") else d
    return t, S._canon(v)


def merge(existing, pairs):
    """adds new (value, TYPE) pairs to {TYPE: [values]} in place; returns ({TYPE: added}, skipped_duplicates)."""
    seen = {_key(v, t) for t, vals in existing.items() for v in vals}
    added, dup = {}, 0
    for v, t in pairs:
        v = re.sub(r"\s+", " ", str(v)).strip()
        t = t.upper()
        if len(v) < 2 or t not in S.TYPES:
            continue
        k = _key(v, t)
        if k in seen:
            dup += 1
            continue
        seen.add(k)
        existing.setdefault(t, []).append(v)
        added[t] = added.get(t, 0) + 1
    return added, dup


def write_known(path, data):
    import yaml
    order = [t for t in ORDER if t in data] + sorted(t for t in data if t not in ORDER)
    body = ("# Known values for swapback (built by `swapback known init`). Private: keep this file out of git.\n" +
            yaml.safe_dump({t: data[t] for t in order}, allow_unicode=True, sort_keys=False, default_flow_style=False))
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".known-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ---------------------------------------------------------------- suggestions from past prompts
def _strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


def scan_texts(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    texts = []
    for line in raw.splitlines():
        s = line.strip()
        if s[:1] in "{[":
            try:
                texts += list(_strings(json.loads(s)))
                continue
            except ValueError:
                pass
        texts.append(line)
    return texts


NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?(?:\s+[A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?){1,2})\b")


def suggest(texts, known_pairs, regions=("US",), min_count=2):
    """{TYPE: [candidates]}: emails and phones seen at least once, capitalised two- or three-word names seen at least
    min_count times, none already known."""
    import phonenumbers
    known = {_key(v, t) for v, t in known_pairs}
    counts = {}

    def add(v, t):
        k = _key(v, t)
        if k not in known:
            c = counts.setdefault(k, [v, t, 0])
            c[2] += 1
    for text in texts:
        for m in EMAIL_RE.finditer(text):
            add(m.group(0), "EMAIL")
        for m in phonenumbers.PhoneNumberMatcher(text, (regions or ["US"])[0]):
            add(m.raw_string, "PHONE")
        for m in NAME_RE.finditer(text):
            words = m.group(1).split()
            while words and words[0].lower() in NAME_STOP:
                words = words[1:]
            while words and words[-1].lower() in NAME_STOP:
                words = words[:-1]
            if len(words) >= 2:
                add(" ".join(words), "PERSON")
    out = {}
    for v, t, n in sorted(counts.values(), key=lambda x: -x[2]):
        if t == "PERSON" and n < min_count:
            continue
        out.setdefault(t, []).append(v)
    return out


# ---------------------------------------------------------------- command
def add_parser(sub):
    p = sub.add_parser("known", help="build or extend known.yaml from contacts (counts only)",
                       description="Build or extend your known-values file.")
    ks = p.add_subparsers(dest="known_cmd", metavar="ACTION", title="actions", required=True)
    i = ks.add_parser("init", help="merge contacts (vCard, CSV) into known.yaml; --scan suggests more",
                      description=__doc__.split("\n\n", 1)[1], formatter_class=__import__("argparse").RawDescriptionHelpFormatter)
    i.add_argument("--vcf", action="append", default=[], metavar="FILE", help="vCard export (.vcf); repeatable")
    i.add_argument("--csv", action="append", default=[], metavar="FILE", help="CSV export; repeatable")
    i.add_argument("--yaml", action="append", default=[], metavar="FILE",
                   help="another known file to merge, e.g. a suggestions file you reviewed and edited; repeatable")
    i.add_argument("--scan", action="append", default=[], metavar="FILE",
                   help="past prompts (text or JSONL) to scan for candidates; written to the suggestions file only")
    i.add_argument("--out", metavar="FILE", help="known file to write (default: known_file from the config, else "
                                                 "./known.yaml)")
    i.add_argument("--suggestions", metavar="FILE", help="where --scan writes candidates (default: "
                                                         "known.suggestions.yaml next to --out)")
    i.add_argument("--first-names", action="store_true",
                   help="also add each contact's first name on its own (skips names that are everyday words)")
    i.add_argument("--min-count", type=int, default=2, help="--scan: times a name must appear (default 2)")
    i.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    i.add_argument("--show", action="store_true", help="also print the added values and suggestions")
    return p


def run(args, cfg=None):
    cfg = cfg or {}
    out = args.out or cfg.get("known_file") or "known.yaml"
    if not (args.vcf or args.csv or args.yaml or args.scan):
        print("swapback known init: give at least one of --vcf, --csv, --yaml, --scan", file=sys.stderr)
        return 2
    pairs, sources = [], {}
    for kind, files in (("vcf", args.vcf), ("csv", args.csv), ("yaml", args.yaml)):
        for fp in files:
            if kind == "yaml":
                got = [(v, t) for t, vals in read_known_raw(fp).items() for v in vals]
            else:
                with open(fp, encoding="utf-8-sig", errors="replace") as f:
                    text = f.read()
                got = (parse_vcf if kind == "vcf" else parse_csv)(text, args.first_names)
            sources[os.path.basename(fp)] = len(got)
            pairs += got
    existing = read_known_raw(out)
    before = sum(len(v) for v in existing.values())
    added, dup = merge(existing, pairs)
    res = {"known_file": os.path.abspath(out), "read_per_file": sources, "existing": before,
           "added": added, "duplicates_skipped": dup, "total": sum(len(v) for v in existing.values())}
    if added and not args.dry_run:
        write_known(out, existing)
    res["written"] = bool(added and not args.dry_run)
    sugg = {}
    if args.scan:
        texts = []
        for fp in args.scan:
            texts += scan_texts(fp)
        sugg = suggest(texts, [(v, t) for t, vals in existing.items() for v in vals],
                       cfg.get("phone_regions") or ["US"], args.min_count)
        sp = args.suggestions or os.path.join(os.path.dirname(os.path.abspath(out)), "known.suggestions.yaml")
        res["suggestions"] = {t: len(v) for t, v in sugg.items()}
        if sugg and not args.dry_run:
            write_known(sp, sugg)
            with open(sp, encoding="utf-8") as f:
                body = f.read().split("\n", 1)[1]
            fd = os.open(sp, os.O_WRONLY | os.O_TRUNC)
            os.write(fd, ("# Suggestions from `swapback known init --scan`: NOT used by the proxy. Review, delete what "
                          "is not personal,\n# then merge: swapback known init --yaml " + os.path.basename(sp) + "\n"
                          + body).encode("utf-8"))
            os.close(fd)
            res["suggestions_file"] = os.path.abspath(sp)
    if args.show:
        added_vals = {t: existing[t][-n:] for t, n in added.items()}
        res["added_values"] = added_vals
        if sugg:
            res["suggested_values"] = sugg
    print(json.dumps(res, ensure_ascii=False, indent=None if not args.show else 1))
    return 0
