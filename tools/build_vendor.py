"""build_vendor: regenerate swapback/vendor/presidio/patterns.json from a presidio-analyzer wheel (no spaCy needed).
presidio-analyzer requires spaCy as a hard dependency and imports it at package import, so its predefined
PatternRecognizer patterns + context words are vendored verbatim (MIT, Microsoft). usage: build_vendor.py <wheel>"""
import hashlib, importlib.abc, importlib.util, json, os, re, sys, types, zipfile, tempfile

WANT = [("generic/email_recognizer.py", "EmailRecognizer", "EMAIL"),
        ("generic/credit_card_recognizer.py", "CreditCardRecognizer", "CARD"),
        ("generic/iban_recognizer.py", "IbanRecognizer", "IBAN"),
        ("country_specific/us/us_ssn_recognizer.py", "UsSsnRecognizer", "SSN"),
        ("generic/ip_recognizer.py", "IpRecognizer", "IP"),
        ("generic/url_recognizer.py", "UrlRecognizer", "URL")]


class Pattern:
    def __init__(self, name, regex, score):
        self.name, self.regex, self.score = name, regex, score


class _Any:
    def __init__(self, *a, **k):
        self.kw = k
    MIN_SCORE, MAX_SCORE = 0, 1.0

    @staticmethod
    def sanitize_value(text, pairs):
        for a, b in pairs:
            text = text.replace(a, b)
        return text


class _Stub(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in ("presidio_analyzer", "tldextract", "regex") and name not in sys.modules:
            return importlib.util.spec_from_loader(name, self, is_package=True)

    def create_module(self, spec):
        m = types.ModuleType(spec.name)
        m.__path__ = []
        m.__getattr__ = lambda attr: Pattern if attr == "Pattern" else (re if attr == "re" else getattr(re, attr, _Any))
        return m

    def exec_module(self, m):
        pass


def main(wheel):
    tmp = tempfile.mkdtemp()
    zipfile.ZipFile(wheel).extractall(tmp)
    meta = open(next(os.path.join(tmp, d, "METADATA") for d in os.listdir(tmp) if d.endswith(".dist-info"))).read()
    version = re.search(r"^Version: (.+)$", meta, re.M).group(1)
    lic = re.search(r"^License-Expression: (.+)$", meta, re.M).group(1)
    sys.meta_path.insert(0, _Stub())
    base = os.path.join(tmp, "presidio_analyzer", "predefined_recognizers")
    ip = importlib.util.spec_from_file_location("iban_patterns", os.path.join(base, "generic", "iban_patterns.py"))
    ibp = importlib.util.module_from_spec(ip)
    ip.loader.exec_module(ibp)
    for n in ("presidio_analyzer.predefined_recognizers.generic.iban_patterns",
              "presidio_analyzer.predefined_recognizers.iban_patterns"):
        sys.modules[n] = ibp
    out = {"source": "presidio-analyzer", "version": version, "license": lic,
           "wheel_sha256": hashlib.sha256(open(wheel, "rb").read()).hexdigest(), "recognizers": []}
    for rel, cls, typ in WANT:
        spec = importlib.util.spec_from_file_location("v_" + cls, os.path.join(base, rel))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        C = getattr(mod, cls)
        flags = "DOTALL|MULTILINE" if typ == "IBAN" else "DOTALL|MULTILINE|IGNORECASE"   # PatternRecognizer default
        out["recognizers"].append({"type": typ, "class": cls, "file": rel, "flags": flags,
                                   "patterns": [[p.name, p.regex, p.score] for p in C.PATTERNS],
                                   "context": list(C.CONTEXT)})
        if typ == "IBAN":
            out["iban_regex_per_country"] = dict(ibp.regex_per_country)
            out["iban_bos_eos"] = [ibp.BOS, ibp.EOS]
    return out


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1]), indent=1))
