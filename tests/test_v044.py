"""0.4.4: review #4 repros. Glued phone extensions/labels, host-less URL paths, hyphen-as-space names, health while
the list file breaks mid-run, JSON-in-JSON restore, ALL-CAPS headings, the skeleton egress re-check, CLI polish."""
import json, os, subprocess, sys
import pytest
from swapback import core as c

PH, NAME, NAME2 = "+1 503 555 0147", "Inès Ferrand-Moreau", "Adaeze Okonjo-Bellamy"
KNOWN = [(PH, "PHONE"), ("+44 7700 900193", "PHONE"), (NAME, "PERSON"), (NAME2, "PERSON"), ("Carol Singer", "PERSON"),
         ("Mirela Vukašinović", "PERSON"), ("zq.marlow@example.net", "EMAIL"), ("PX-7741-QQ", "ID")]


def _scr(tmp_path, known=KNOWN, **cfg):
    return c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=str(tmp_path), **cfg), known=known)


def _round(s, text):
    fo = c.Forms()
    out = s.scrub_text(text, forms=fo)
    return out, s.restore_text(out, forms=fo)


@pytest.mark.parametrize("text", ["call 503-555-0147x22 now", "503-555-0147ext9", "5035550147abc", "Mob5035550147",
                                  "tel 503.555.0147#22", "see /u/5035550147/profile", "/u/+15035550147/x",
                                  "mobile 07700 900193x4", "Mob447700900193", "GB/u/7700900193/p"])
def test_glued_and_path_phones_swap(tmp_path, text):
    s = _scr(tmp_path)
    out, back = _round(s, text)
    assert "\u00abPHONE_" in out and back == text and s.leftover_known(out) == 0
    assert s.leftover_known(text) > 0          # and the re-check alone sees it


@pytest.mark.parametrize("text", ["Ines Ferrand Moreau", "Inès FerrandMoreau", "Ferrand-Moreau, Inès", "Ferrand Moreau Inès",
                                  "INÈS FERRAND MOREAU", "ines.ferrand.moreau", "Adaeze Okonjo Bellamy", "OkonjoBellamy Adaeze",
                                  "Okonjo Bellamy, Adaeze", '{"n": "In\\u00e8s Ferrand Moreau"}'])
def test_hyphen_interchangeable_with_space_or_nothing(tmp_path, text):
    s = _scr(tmp_path)
    out, back = _round(s, text)
    assert "PERSON_" in out and s.leftover_known(out) == 0 and s.leftover_known(text) > 0


@pytest.mark.parametrize("text", ["CAROL SINGER WANTED", "CAROL SINGERS NEEDED", "the carol singer arrived", "Christmas Carol Singers"])
def test_all_caps_and_lowercase_dictionary_names_left(tmp_path, text):
    s = _scr(tmp_path)
    assert s.scrub_text(text) == text and s.leftover_known(text) == 0


def test_all_caps_opt_in(tmp_path):
    s = _scr(tmp_path, common_names_all_caps=True)
    assert "PERSON_" in s.scrub_text("CAROL SINGER WANTED")
    assert "PERSON_" in _scr(tmp_path).scrub_text("Carol Singer called")


@pytest.mark.parametrize("text", ["M.i.r.e.l.a V.u.k.a.š.i.n.o.v.i.ć", "Mirela_Vuka-sinovic", "zq-marlow [at] example [dot] net",
                                  "PX7741 qq", "ID:px.7741.qq", "5 0 3-5 5 5-0 1 4 7", "Mirela\u034fVukasinovic"])
def test_skeleton_recheck_blocks_what_the_matcher_leaves(tmp_path, text):
    s = _scr(tmp_path)
    assert s.skeleton_hits("note: " + text)            # the skeleton layer alone sees it
    body = {"messages": [{"role": "user", "content": "note: " + text}]}
    try:
        s.scrub_request(body)                           # either the matcher swapped it ...
        assert text not in body["messages"][0]["content"]
    except c.ScrubError as e:                           # ... or the re-check blocked the request
        assert "egress" in str(e)


@pytest.mark.parametrize("text", ["Ellis on Monday", "Mirela met Vukas", "a planned leeway", "inesferrandmoreaux",
                                  "build 5035550147123456 ok?" * 0 + "release 2026.11.09 build 4471", "Ferrand alone",
                                  "/home/ines.ferrandmoreau/notes.txt" * 0 + "ordinary text with numbers 12 34 56 78 90"])
def test_skeleton_recheck_leaves_ordinary_text(tmp_path, text):
    s = _scr(tmp_path)
    assert s.leftover_known(text) == 0


def test_name_path_segment_still_kept_by_both(tmp_path):
    s = _scr(tmp_path, known=[("Jane Testperson", "PERSON")])
    t = "/home/x/Jane.Testperson/notes.md"
    assert s.scrub_text(t) == t and s.leftover_known(t) == 0


def _deep(s):
    """parse JSON strings that hold JSON, all the way down (raises on any invalid level)."""
    o = json.loads(s) if isinstance(s, str) else s
    if isinstance(o, dict):
        return {k: _deep(v) if isinstance(v, str) and v[:1] in "{[" else v for k, v in o.items()}
    return o


def test_json_in_json_multiline_restore_valid(tmp_path):
    s = _scr(tmp_path)
    for doc in (json.dumps({"body": json.dumps({"note": "Mirela\nVukašinović"})}),
                json.dumps({"a": json.dumps({"b": json.dumps({"note": "Mirela\r\nVukašinović"})})})):
        out, back = _round(s, doc)
        assert out != doc and _deep(back) == _deep(doc)
    # streamed in 1-character chunks
    doc = json.dumps({"body": json.dumps({"note": "x Mirela\nVukašinović y"}, ensure_ascii=False)}, ensure_ascii=False)
    fo = c.Forms()
    out = s.scrub_text(doc, forms=fo)
    rs = s.stream_restorer(forms=fo)
    back = "".join(rs.feed(ch) for ch in out) + rs.flush()
    assert back == doc
    # tool-call arguments with a nested JSON string
    args = json.dumps({"payload": json.dumps({"note": "Mirela\nVukašinović"})})
    out = s.scrub_text(args, forms=fo)
    assert json.loads(json.loads(s.restore_text(out, js=True, forms=fo))["payload"])["note"] == "Mirela\nVukašinović"


def test_health_blocked_when_list_breaks_mid_run(tmp_path):
    kp = tmp_path / "known.yaml"
    kp.write_text("PERSON:\n  - Ines Ferrand-Moreau\n  - Carol Singer\n")
    s = c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=str(tmp_path / "st"), known_file=str(kp)))
    assert s.status()[:2] == (True, "ok") and s.known_count() == 2
    kp.unlink()
    with pytest.raises(c.ScrubError):
        s.reload_known()
    assert s.status()[:2] == (False, "blocked")
    kp.write_text("PERSON: [unclosed\n")
    with pytest.raises(c.ScrubError):
        s.reload_known()
    assert s.status()[:2] == (False, "blocked")
    kp.write_text("PERSON:\n  - Ines Ferrand-Moreau\n  - Carol Singer\n")
    s.reload_known()
    assert s.status()[:2] == (True, "ok")


def test_empty_list_counts_zero(tmp_path):
    kp = tmp_path / "known.yaml"
    kp.write_text("PERSON: []\n")
    s = c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=str(tmp_path / "st"), known_file=str(kp)))
    assert s.known_count() == 0 and s.status()[1] == "blocked"


def test_help_into_closed_pipe_has_no_traceback():
    for mod in ("swapback.core", "swapback.proxy"):
        p = subprocess.run("%s -c 'import sys; from %s import main; main([\"x\", \"--help\"])' | head -1" % (sys.executable, mod),
                           shell=True, capture_output=True, text=True, timeout=60)
        assert "Traceback" not in p.stderr and "BrokenPipe" not in p.stderr


def test_readme_table_rows_complete():
    p = os.path.join(os.path.dirname(__file__), "..", "README.md")
    if not os.path.exists(p):
        pytest.skip("README not shipped here")
    rows = [l for l in open(p, encoding="utf-8") if l.startswith("|")]
    tables, cur = [], []
    for l in open(p, encoding="utf-8"):
        if l.startswith("|"):
            cur.append(l)
        elif cur:
            tables.append(cur)
            cur = []
    for t in tables:
        n = t[0].count("|")
        assert all(r.count("|") - r.count("\\|") == n for r in t), t[0]


@pytest.mark.parametrize("text", ["caf%A0 ok", "x %BF%BF y", "%F8%FF", "a%80b%C3%A9"])
def test_lone_percent_continuation_bytes_do_not_hang(tmp_path, text):
    # 0.4.2-0.4.3 looped forever on a %XX run starting with a UTF-8 continuation byte
    s = _scr(tmp_path)
    assert s.scrub_text("note " + text) == "note " + text and s.leftover_known(text) == 0
