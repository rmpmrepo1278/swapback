r"""0.4.10: review #10. A value whose span holds escaped quotes or escaped markup comes back byte-exact (escaped once
for the JSON level it lands in, not twice); the cell rule also reads HTML escaped inside JSON (\u003c, <\/td>, &lt;);
a name swapped out of table cells and reused elsewhere comes back as the listed name; a capital dotted I, a quoted
("> ") address and many copies of a value in one long string."""
import json
import time

import pytest

from swapback import core as c
from swapback import selftest as st

ADDR = "27 Gallowgate Mews\nUnit 9\nBurlington, VT 05401"
TURK = "\u0130pek Y\u0131ld\u0131r\u0131mc\u0131"
EMAIL = "amber.lane@northgable.example"


def _scr(tmp_path):
    known = [(ADDR, "ADDRESS"), ("Amber Lane", "PERSON"), ("Ottoline Frobisher", "PERSON"), (TURK, "PERSON"),
             (EMAIL, "EMAIL")]
    return c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=str(tmp_path / "st")), known=known)


def _round(s, content, role="tool"):
    fo = c.Forms()
    body = {"messages": [{"role": "user", "content": "read"}, {"role": role, "tool_call_id": "c", "content": content}]}
    s.scrub_request(body, forms=fo)
    sent = body["messages"][-1]["content"]
    return sent, s.restore_text(sent, forms=fo), fo


def _leak(sent):
    return any(w in sent for w in ("Gallowgate", "Ottoline", "Frobisher", "northgable")) or ("Amber" in sent and "Lane" in sent)


def test_review10_repro_div_dir_address(tmp_path):
    s = _scr(tmp_path)
    content = json.dumps({"html": '<div dir="ltr">27 Gallowgate Mews</div><div dir="ltr">Unit 9</div>'
                                  '<div dir="ltr">Burlington, VT 05401</div>'})
    sent, back, _ = _round(s, content)
    assert "Gallowgate" not in sent and back == content


@pytest.mark.parametrize("html", st.REVIEW10_ATTR)
@pytest.mark.parametrize("lv,ea", [(1, True), (1, False), (2, True), (3, False)])
def test_quoted_attributes_round_trip_exact(tmp_path, html, lv, ea):
    s = _scr(tmp_path)
    content = html
    for _ in range(lv):
        content = json.dumps({"html": content}, ensure_ascii=ea)
    sent, back, _ = _round(s, content)
    assert not _leak(sent) and back == content


def _encodings(row):
    plain = json.dumps({"html": row})
    return {"go": plain.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"),
            "php": plain.replace("/", "\\/"),
            "entities": json.dumps({"html": row.replace("<", "&lt;").replace(">", "&gt;")})}


@pytest.mark.parametrize("layout", st.REVIEW9_LAYOUTS)
@pytest.mark.parametrize("enc", ["go", "php", "entities"])
def test_cell_rule_reads_escaped_html(tmp_path, layout, enc):
    s = _scr(tmp_path)
    content = _encodings(layout.format(F="Amber", L="Lane", f="amber", l="lane", LU="LANE"))[enc]
    sent, back, _ = _round(s, content)
    assert not _leak(sent), sent
    assert back == content


def test_review10_repro_go_escaped_row(tmp_path):
    s = _scr(tmp_path)
    content = json.dumps({"html": "<tr><td>Amber</td><td>Lane</td><td>Tucson</td></tr>"}).replace(
        "<", "\\u003c").replace(">", "\\u003e")
    sent, back, _ = _round(s, content)
    assert "Amber" not in sent and "Lane" not in sent and "Tucson" in sent and back == content


@pytest.mark.parametrize("enc", ["raw", "json", "go"])
def test_cell_name_reused_elsewhere_is_the_listed_name(tmp_path, enc):
    s = _scr(tmp_path)
    t = "<table><tr><td>Amber</td><td>Lane</td><td>Tucson</td></tr></table>"
    content = t if enc == "raw" else json.dumps({"html": t}) if enc == "json" else \
        json.dumps({"html": t}).replace("<", "\\u003c").replace(">", "\\u003e")
    sent, back, fo = _round(s, content)
    ph = c.PH_RE.search(sent).group(0)
    assert back == content
    assert s.restore_text(sent + "\nDear " + ph + ",", forms=fo).endswith("Dear Amber Lane,")
    assert s.restore_text(json.dumps({"to_name": ph}, ensure_ascii=False), js=True, forms=fo) == '{"to_name": "Amber Lane"}'
    for cut in range(1, len(sent)):              # streamed: the echo stays exact at every split
        fo2 = c.Forms()
        s.scrub_request({"messages": [{"role": "user", "content": content}]}, forms=fo2)
        r = s.stream_restorer(False, None, fo2)
        assert r.feed(sent[:cut]) + r.feed(sent[cut:]) + r.flush() == content


def test_address_not_from_cells_keeps_its_tags_anywhere(tmp_path):
    s = _scr(tmp_path)
    txt = "27 Gallowgate Mews</p>\n<p>Unit 9</p>\n<p>Burlington, VT 05401"
    sent, back, fo = _round(s, "Please note: %s." % txt, role="user")
    assert back == "Please note: %s." % txt


@pytest.mark.parametrize("text", [TURK + " signed.", TURK.upper() + " signed.", json.dumps({"n": TURK}),
                                  json.dumps({"n": TURK}, ensure_ascii=False), "ipek y\u0131ld\u0131r\u0131mc\u0131 signed."])
def test_capital_dotted_i_is_swapped_not_blocked(tmp_path, text):
    s = _scr(tmp_path)
    sent, back, _ = _round(s, text, role="user")
    assert c.PH_RE.search(sent) and back == text


@pytest.mark.parametrize("text", ["> " + ADDR.replace("\n", "\n> "), "She wrote:\n> " + ADDR.replace("\n", "\n> ") + "\n\nok",
                                  ">" + ADDR.replace("\n", "\n>"), "> > " + ADDR.replace("\n", "\n> > "),
                                  json.dumps({"body": "She wrote:\n> " + ADDR.replace("\n", "\n> ")})])
def test_quoted_address_is_swapped_not_blocked(tmp_path, text):
    s = _scr(tmp_path)
    sent, back, _ = _round(s, text, role="user")
    assert "Gallowgate" not in sent and back == text


def test_many_copies_in_one_long_string_stay_fast(tmp_path):
    s = _scr(tmp_path)
    content = json.dumps({"v": ("lorem ipsum %s " % EMAIL) * 15000})
    t0 = time.time()
    sent, back, _ = _round(s, content)
    assert time.time() - t0 < 10 and "northgable" not in sent and back == content


def test_json_escaped_value_reused_in_prose_is_decoded(tmp_path):
    s = _scr(tmp_path)
    fo = c.Forms()
    body = {"messages": [{"role": "tool", "tool_call_id": "t", "content": json.dumps({"addr": ADDR})}]}
    s.scrub_request(body, forms=fo)
    ph = c.PH_RE.search(body["messages"][-1]["content"]).group(0)
    assert s.restore_text("Ship to " + ph, forms=fo) == "Ship to " + ADDR


def test_selftest_has_the_review10_checks():
    r = st.run(as_json=True, out=open("/dev/null", "w"))
    assert r["pass"] and r["r10_exact_ok"] == r["r10_exact"] > 0 and r["r10_escaped_ok"] == r["r10_escaped"] > 0
    assert r["r10_more_ok"] == r["r10_more"] > 0
