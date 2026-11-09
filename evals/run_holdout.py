#!/usr/bin/env python3
"""Score pii-scrubber's PATTERN detectors on evals/holdout3.json (64 synthetic cases).

Patterns only: no known values and no private terms are configured, so this measures what the scrubber finds on its
own. Names are expected to be missed (there is no NER: list the names you care about in known.yaml).

For each case:
  positive  caught      every expected codepoint is inside a swapped span (any type)
            typed       ... and the swapped type matches the case's category (see CATEGORY_MAP)
  negative  untouched   the text goes out exactly as written
  all       round trip  restore(scrub(text)) == text, byte for byte
            utility     every utility span survives verbatim in the scrubbed text
            over        codepoints swapped outside the expected spans

Usage:  python evals/run_holdout.py [--json] [--markdown] [--misses] [--file evals/holdout3.json]
"""
import argparse, json, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from pii_scrubber import core  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
# holdout category -> scrubber types that count as the right type (frozen; not tuned to the labels)
CATEGORY_MAP = {"PERSON": {"PERSON"}, "EMAIL": {"EMAIL"}, "PHONE": {"PHONE"}, "ADDRESS": {"ADDRESS"}, "SSN": {"SSN"},
                "IP_ADDRESS": {"IP"}, "DATE_OF_BIRTH": {"DOB"}, "SECRET": {"SECRET", "ID", "ACCOUNT"}}


def score(path=None):
    path = path or os.path.join(HERE, "holdout3.json")
    cases = json.load(open(path, encoding="utf-8"))["cases"]
    with tempfile.TemporaryDirectory() as d:
        sc = core.Scrubber(dict(core.DEFAULTS, mode="on", state_dir=d), known=[])
        rows = []
        for c in cases:
            text = c["text"]
            forms = core.Forms() if hasattr(core, "Forms") else None
            out = sc.scrub_text(text, forms=forms) if forms is not None else sc.scrub_text(text)
            back = sc.restore_text(out, forms=forms) if forms is not None else sc.restore_text(out)
            sp = [(x[0], x[1], x[2]) for x in sc.spans(text)]
            cov = {}
            for s, e, t in sp:
                for i in range(s, e):
                    cov[i] = t
            exp = set()
            caught = typed = True
            for x in c["expected_spans"]:
                ok = CATEGORY_MAP.get(x["category"], {x["category"]})
                for i in range(x["start"], x["end"]):
                    exp.add(i)
                    if i not in cov:
                        caught = typed = False
                    elif cov[i] not in ok:
                        typed = False
            rows.append({"id": c["id"], "kind": c["kind"], "category": (c.get("tags") or ["?"])[0] if c["kind"] == "positive"
                         else "NEGATIVE", "caught": caught and c["kind"] == "positive", "typed": typed and c["kind"] == "positive",
                         "untouched": out == text, "round_trip": back == text,
                         "utility": all(u["text"] in out for u in c.get("utility_spans") or []),
                         "over": sum(1 for i in cov if i not in exp), "found": sorted({t for _s, _e, t in sp}),
                         "text": text, "out": out})
    return rows


def summary(rows):
    by = {}
    for r in rows:
        b = by.setdefault(r["category"], {"cases": 0, "caught": 0, "typed": 0, "untouched": 0})
        b["cases"] += 1
        b["caught"] += r["caught"]
        b["typed"] += r["typed"]
        b["untouched"] += r["untouched"]
    pos = [r for r in rows if r["kind"] == "positive"]
    neg = [r for r in rows if r["kind"] != "positive"]
    return {"positives": len(pos), "caught": sum(r["caught"] for r in pos), "typed": sum(r["typed"] for r in pos),
            "negatives": len(neg), "negatives_untouched": sum(r["untouched"] for r in neg),
            "round_trip": sum(r["round_trip"] for r in rows), "cases": len(rows),
            "utility": sum(r["utility"] for r in rows), "over_redacted_cases": sum(1 for r in rows if r["over"]),
            "by_category": by}


def markdown(s):
    lines = ["| Category | Caught | Right type |", "|---|---|---|"]
    for k in sorted(s["by_category"], key=lambda k: (k == "NEGATIVE", k)):
        b = s["by_category"][k]
        if k == "NEGATIVE":
            continue
        lines.append("| %s | %d/%d | %d/%d |" % (k, b["caught"], b["cases"], b["typed"], b["cases"]))
    lines.append("| **All positives** | **%d/%d** | **%d/%d** |" % (s["caught"], s["positives"], s["typed"], s["positives"]))
    lines.append("")
    lines.append("Negatives left untouched: %d/%d. Round trip exact: %d/%d. Utility text kept: %d/%d."
                 % (s["negatives_untouched"], s["negatives"], s["round_trip"], s["cases"], s["utility"], s["cases"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--misses", action="store_true", help="list positives not fully caught (synthetic text)")
    a = ap.parse_args(argv)
    rows = score(a.file)
    s = summary(rows)
    if a.json:
        print(json.dumps(s, indent=2))
    else:
        print(markdown(s))
    if a.misses:
        for r in rows:
            if (r["kind"] == "positive" and not r["caught"]) or (r["kind"] != "positive" and not r["untouched"]):
                print("%s %-8s found=%s  %s" % (r["id"], r["category"], ",".join(r["found"]) or "-", r["text"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
