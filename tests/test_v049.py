"""0.4.9: review #9. A listed dictionary-word name split across one-word table cells or blocks is swapped wherever the
pair sits in the row or list; the system note's example placeholders are never-assigned slot 0; a broken known file is
reported as broken YAML."""
import json
import re
import subprocess
import sys

import pytest

from swapback import core as c
from swapback import selftest as st

NAMES = [("Holly", "Marsh"), ("Jasper", "Stone"), ("Faith", "Bishop"), ("Ivy", "Glen"), ("Hunter", "Moss"),
         ("Iris", "Bell"), ("Thomasina", "Wexley")]


def _scr(tmp_path, extra=()):
    known = [("%s %s" % n, "PERSON") for n in NAMES] + [("l.varga@quillfeather.example", "EMAIL"),
                                                        ("Grace Fields", "PERSON")] + list(extra)
    return c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=str(tmp_path / "st")), known=known)


def _shapes(txt):
    yield txt
    yield json.dumps({"body_html": txt})
    yield json.dumps({"messages": [{"id": "m1", "body_html": "<html><body><p>Hi,</p>" + txt + "<p>Thanks</p></body></html>"}]})


def _leaks(sent, f, l):
    return bool(re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % f, sent, re.I)
                and re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % l, sent, re.I))


@pytest.mark.parametrize("layout", st.REVIEW9_LAYOUTS)
def test_review9_layouts_swapped(tmp_path, layout):
    s = _scr(tmp_path)
    for f, l in NAMES:
        txt = layout.format(F=f, L=l, f=f.lower(), l=l.lower(), LU=l.upper())
        for content in _shapes(txt):
            for role in ("user", "tool"):
                body = {"messages": [{"role": "user", "content": "read"},
                                     {"role": role, "tool_call_id": "c", "content": content}]}
                s.scrub_request(body)
                sent = body["messages"][-1]["content"]
                assert not _leaks(sent, f, l), (layout, f)
                assert c.PH_RE.search(sent)


def test_review9_repro_and_round_trip(tmp_path):
    s = _scr(tmp_path)
    txt = "<tr><td>Holly</td><td>Marsh</td><td>Denver</td></tr>"
    fo = c.Forms()
    body = {"messages": [{"role": "user", "content": txt}]}
    s.scrub_request(body, forms=fo)
    sent = body["messages"][-1]["content"]
    assert "Holly" not in sent and "Marsh" not in sent and "Denver" in sent
    assert s.restore_text(sent, forms=fo) == txt


@pytest.mark.parametrize("txt", st.REVIEW9_ORDINARY + [
    "<ul><li>Home</li><li>About</li><li>Pricing</li><li>Blog</li></ul>",
    "<table><tr><td>Run</td><td>Rest</td><td>Swim</td></tr></table>",
    "<tr><td>Marsh</td><td>Holly</td></tr>",
    "<ul><li>Oak</li><li>Holly</li></ul><ul><li>Marsh</li></ul>",
    "<td>Hazel</td><td>Woods and forests</td>",
])
def test_review9_ordinary_one_word_blocks_untouched(tmp_path, txt):
    s = _scr(tmp_path)
    for content in _shapes(txt):
        body = {"messages": [{"role": "user", "content": content}]}
        s.scrub_request(body)
        assert body["messages"][-1]["content"] == content


@pytest.mark.parametrize("txt", ["<div>Grace</div>\n<div>Fields</div>\n<div>Pricing</div>",
                                 json.dumps({"html": "<li>Grace</li>\n<li>Fields</li>\n<li>Pricing</li>"})])
def test_review9_listed_pair_in_a_menu_is_swapped(tmp_path, txt):
    # 0.4.7/0.4.8 left these as written; a menu and a name list cannot be told apart, and a leak is worse than a swap
    s = _scr(tmp_path)
    fo = c.Forms()
    body = {"messages": [{"role": "user", "content": txt}]}
    s.scrub_request(body, forms=fo)
    sent = body["messages"][-1]["content"]
    assert "Fields" not in sent and "Pricing" in sent
    assert s.restore_text(sent, forms=fo) == txt


def test_review9_note_examples_never_assigned(tmp_path):
    ex = re.findall(c.L + r"[A-Z]+_\d+" + c.R, c.DEFAULTS["system_note"])
    assert ex and all(x.endswith("_0" + c.R) for x in ex)
    s = _scr(tmp_path)
    fo = c.Forms()
    body = {"messages": [{"role": "user", "content": "Holly Marsh, Jasper Stone, Faith Bishop and Thomasina Wexley "
                                                     "wrote to l.varga@quillfeather.example"}]}
    s.scrub_request(body, forms=fo)
    assert "\u00abPERSON_4\u00bb" in body["messages"][-1]["content"]
    for reply in ("You wrote %s per the note." % " and ".join(ex), json.dumps({"to": ex[-1]})):
        assert s.restore_text(reply, forms=fo) == reply
        assert s.restore_text(reply) == reply


def test_review9_broken_known_yaml_says_yaml(tmp_path):
    k = tmp_path / "known.yaml"
    k.write_text("PERSON:\n  - [unclosed\n")
    with pytest.raises(c.ScrubError) as e:
        c.load_known(str(k))
    assert "not valid YAML" in str(e.value) and "line" in str(e.value)
    cfg = tmp_path / "swapback.yaml"
    cfg.write_text("mode: on\nknown_file: %s\nstate_dir: %s\nlisten: 127.0.0.1:0\n" % (k, tmp_path / "s"))
    pr = subprocess.run([sys.executable, "-m", "swapback.proxy"], capture_output=True, text=True, timeout=60,
                        env=dict(__import__("os").environ, SWAPBACK_CONFIG=str(cfg)))
    err = pr.stderr.strip()
    assert pr.returncode == 2 and len(err.splitlines()) == 1 and "Traceback" not in err
    assert "not valid YAML" in err and "Fix the path" not in err


def test_review9_missing_known_refuses_to_start(tmp_path):
    cfg = tmp_path / "swapback.yaml"
    cfg.write_text("mode: on\nknown_file: %s\nstate_dir: %s\nlisten: 127.0.0.1:0\n"
                   % (tmp_path / "nope.yaml", tmp_path / "s"))
    pr = subprocess.run([sys.executable, "-m", "swapback.proxy"], capture_output=True, text=True, timeout=60,
                        env=dict(__import__("os").environ, SWAPBACK_CONFIG=str(cfg)))
    err = pr.stderr.strip()
    assert pr.returncode == 2 and len(err.splitlines()) == 1 and "missing" in err and "Fix the path" in err
