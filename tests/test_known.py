"""swapback known init: contacts (vCard, CSV) into known.yaml, merged, 0600, counts only. Made-up values only."""
import json, os, pathlib, stat, subprocess, sys

import pytest

from swapback import core as s
from swapback import known as K

FX = pathlib.Path(__file__).parent / "fixtures"


def _cli(args, cwd):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SWAPBACK_", "PII_SCRUBBER_"))}
    env["SWAPBACK_CONFIG"] = str(cwd / "missing.yaml")
    r = subprocess.run([sys.executable, "-B", "-c", "from swapback.core import main; main()", "known", "init", *args],
                       cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    return r


def test_parse_vcf_versions_folding_qp_escapes():
    got = K.parse_vcf((FX / "contacts.vcf").read_text())
    assert ("Jane Q Testperson", "PERSON") in got and ("jane.testperson@example.org", "EMAIL") in got
    assert ("+1 (425) 555-0101", "PHONE") in got and ("1234 Elm Street, Apt 5", "ADDRESS") in got
    assert ("Zo\u00eb M\u00fcller", "PERSON") in got and ("425.555.0102", "PHONE") in got      # 2.1, no FN, QP
    assert ("+1-425-555-0103", "PHONE") in got and ("Will Ozzard", "PERSON") in got           # 4.0 tel: uri
    assert not any(v == "not-an-email" or v == "12" for v, _ in got)
    assert not any("folded" in v for v, _ in got)


def test_parse_vcf_first_names_skip_wordlike():
    got = K.parse_vcf((FX / "contacts.vcf").read_text(), first_names=True)
    assert ("Jane", "PERSON") in got and ("Zo\u00eb", "PERSON") in got
    assert ("Will", "PERSON") not in got                       # an everyday word: matched case-insensitively


def test_parse_csv_google_outlook_and_value_type():
    g = K.parse_csv((FX / "google_contacts.csv").read_text())
    assert ("Priya Fakename", "PERSON") in g and ("priya.fakename@example.com", "EMAIL") in g
    assert ("pf@example.org", "EMAIL") in g and ("(425) 555-0104", "PHONE") in g and ("77 Harbor Road", "ADDRESS") in g
    assert not any(v.startswith("98000") or v in ("* Home", "Mobile", "Example Corp", "P") for v, _ in g)
    o = K.parse_csv((FX / "outlook_contacts.csv").read_text())
    assert o == [("Ravi Notreal", "PERSON"), ("ravi.notreal@example.com", "EMAIL"), ("+1 425 555 0105", "PHONE"),
                 ("500 Main St Suite 2", "ADDRESS")]
    assert K.parse_csv((FX / "values.csv").read_text()) == [("Testco Holdings", "ORG"), ("AX-99-1234", "ID")]


def test_init_merges_without_duplicates_0600_counts_only(tmp_path):
    kf = tmp_path / "known.yaml"
    kf.write_text("PERSON:\n  - jane q testperson\nPHONE:\n  - \"425-555-0101\"\n")
    r = _cli(["--vcf", str(FX / "contacts.vcf"), "--csv", str(FX / "google_contacts.csv"), "--out", str(kf)], tmp_path)
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout)
    for v in ("Testperson", "example", "555", "Elm", "Priya"):
        assert v not in r.stdout and v not in r.stderr                       # counts only
    assert res["existing"] == 2 and res["written"] is True
    assert res["added"] == {"EMAIL": 4, "PHONE": 3, "ADDRESS": 2, "PERSON": 4}
    assert res["duplicates_skipped"] == 4                 # Jane (twice), her phone (twice in other formats), her email
    assert stat.S_IMODE(os.stat(kf).st_mode) == 0o600
    raw = K.read_known_raw(str(kf))
    assert raw["PERSON"][0] == "jane q testperson" and len(raw["PERSON"]) == 5
    assert "Mom" not in raw["PERSON"] and "Jane Testperson" in raw["PERSON"]      # a relation is not a name
    pairs = s.load_known(str(kf))                         # the proxy reads it
    assert ("priya.fakename@example.com", "EMAIL") in pairs and ("(425) 555-0104", "PHONE") in pairs
    again = json.loads(_cli(["--vcf", str(FX / "contacts.vcf"), "--out", str(kf)], tmp_path).stdout)
    assert again["added"] == {} and again["written"] is False


def test_show_prints_values_and_dry_run_writes_nothing(tmp_path):
    kf = tmp_path / "k.yaml"
    r = _cli(["--csv", str(FX / "outlook_contacts.csv"), "--out", str(kf), "--dry-run", "--show"], tmp_path)
    res = json.loads(r.stdout)
    assert "Ravi Notreal" in res["added_values"]["PERSON"] and not kf.exists() and res["written"] is False


def test_scan_suggests_only_into_separate_file(tmp_path):
    kf = tmp_path / "known.yaml"
    r = _cli(["--vcf", str(FX / "contacts.vcf"), "--scan", str(FX / "prompts.jsonl"), "--out", str(kf)], tmp_path)
    res = json.loads(r.stdout)
    assert "Kofi" not in r.stdout and "kofi" not in r.stdout
    sf = pathlib.Path(res["suggestions_file"])
    assert sf.name == "known.suggestions.yaml" and stat.S_IMODE(os.stat(sf).st_mode) == 0o600
    sug = K.read_known_raw(str(sf))
    assert sug["PERSON"] == ["Kofi Madeup", "Quarterly Review"]     # seen twice or more; Lena (once) is not
    assert sug["EMAIL"] == ["kofi.madeup@example.com"] and len(sug["PHONE"]) == 1
    assert "Jane Q Testperson" not in json.dumps(sug)               # already known
    assert "NOT used by the proxy" in sf.read_text()
    raw = K.read_known_raw(str(kf))
    assert "Kofi Madeup" not in raw.get("PERSON", []) and "kofi.madeup@example.com" not in raw.get("EMAIL", [])
    r2 = _cli(["--yaml", str(sf), "--out", str(kf)], tmp_path)     # after review, merging is an explicit step
    assert json.loads(r2.stdout)["added"]["PERSON"] == 2


def test_init_needs_a_source(tmp_path):
    r = _cli([], tmp_path)
    assert r.returncode == 2 and "--vcf" in r.stderr
