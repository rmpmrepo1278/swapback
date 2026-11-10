r"""0.4.11: review #11. The cell rule 7-8 JSON levels down (a quoted attribute there is hundreds of backslashes
long); a name taken from cells and reused after another tag comes back as the listed name; a name split by one comma
or across CSV columns is swapped; "i" + a combining dot; "| "-quoted lines; many copies of a value 8 levels down stay
linear; a request whose check runs past scrub_timeout_s is answered 503."""
import json
import time

import pytest

from swapback import core as c
from swapback import selftest as st

ADDR = "27 Gallowgate Mews\nUnit 9\nBurlington, VT 05401"


def _scr(tmp_path, **kw):
    known = [(ADDR, "ADDRESS"), ("Heath Cliff", "PERSON"), ("Faith Ridge", "PERSON"), ("\u0130rem Kaya", "PERSON")]
    return c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=str(tmp_path / "st"), **kw), known=known)


def _round(s, content):
    fo = c.Forms()
    body = {"messages": [{"role": "user", "content": "read"}, {"role": "tool", "tool_call_id": "c", "content": content}]}
    s.scrub_request(body, forms=fo)
    sent = body["messages"][-1]["content"]
    return sent, s.restore_text(sent, forms=fo), fo


def _leak(sent):
    return any(w in sent for w in ("Heath", "Gallowgate", "Kaya")) or ("Faith" in sent and "Ridge" in sent)


def _nest(html, enc, lv):
    x = html.replace("<", "&lt;").replace(">", "&gt;") if enc == "entities" else html
    for i in range(lv):
        x = json.dumps({"v": x})
        if i == 0 and enc == "go":
            x = x.replace("<", "\\u003c").replace(">", "\\u003e")
        if i == 0 and enc == "php":
            x = x.replace("/", "\\/")
    return x


def test_review11_repro_heath_cliff_level8(tmp_path):
    s = _scr(tmp_path)
    content = _nest('<div dir="ltr">Heath</div><div dir="ltr">Cliff</div>', "raw", 8)
    sent, back, _ = _round(s, content)
    assert not _leak(sent) and back == content


@pytest.mark.parametrize("html", st.REVIEW11_DEEP)
@pytest.mark.parametrize("enc", ["raw", "go", "php", "entities"])
@pytest.mark.parametrize("lv", [1, 6, 7, 8])
def test_cells_every_level(tmp_path, html, enc, lv):
    s = _scr(tmp_path)
    content = _nest(html, enc, lv)
    sent, back, fo = _round(s, content)
    assert not _leak(sent) and back == content
    for n in (1, 37, 500):                           # streamed: the text before a placeholder spans chunks
        rs = c.StreamRestorer(s, forms=fo)
        assert "".join(rs.feed(sent[i:i + n]) for i in range(0, len(sent), n)) + rs.flush() == content


def test_cells_reused_after_other_tag_give_listed_name(tmp_path):
    s = _scr(tmp_path)
    row = '<tr><td class="id">3</td><td class="first">Heath</td><td class="last">Cliff</td></tr>'
    sent, back, fo = _round(s, row)
    assert back == row
    ph = c.PH_RE.search(sent).group(0)
    assert s.restore_text("<li>%s</li>" % ph, forms=fo) == "<li>Heath Cliff</li>"
    assert s.restore_text("<p>%s</p>" % ph, forms=fo) == "<p>Heath Cliff</p>"
    assert s.restore_text("Dear %s," % ph, forms=fo) == "Dear Heath Cliff,"
    assert s.restore_text('<td class="first">%s</td>' % ph, forms=fo) == '<td class="first">Heath</td><td class="last">Cliff</td>'


@pytest.mark.parametrize("text", st.REVIEW11_TEXTS + ["Marisol, Quintanar", "3\tFaith\tRidge\tReno"])
@pytest.mark.parametrize("wrap", [False, True])
def test_comma_csv_and_dotted_i_swapped(tmp_path, text, wrap):
    s = _scr(tmp_path)
    content = json.dumps({"v": text}) if wrap else text
    sent, back, _ = _round(s, content)
    assert not _leak(sent) and back == content


def test_comma_lower_case_ordinary_words_left(tmp_path):
    s = _scr(tmp_path)
    sent, back, _ = _round(s, "keep the faith, ridge walkers")
    assert sent == "keep the faith, ridge walkers"


@pytest.mark.parametrize("text", ["Faith,\nRidge", '"Faith","Ridge"'])
def test_comma_newline_and_quoted_csv_still_blocked(tmp_path, text):
    s = _scr(tmp_path)
    with pytest.raises(c.ScrubError):
        _round(s, text)


@pytest.mark.parametrize("mark", ["| ", "|", "  | "])
def test_pipe_quoted_address(tmp_path, mark):
    s = _scr(tmp_path)
    content = "she wrote:\n" + mark + ADDR.replace("\n", "\n" + mark) + "\n" + mark + "thanks"
    for x in (content, json.dumps({"body": content})):
        sent, back, _ = _round(s, x)
        assert not _leak(sent) and back == x


def test_markdown_table_untouched(tmp_path):
    s = _scr(tmp_path)
    t = "| item | qty |\n| --- | --- |\n| rope | 2 |\n| bread | 1 |"
    assert _round(s, t)[0] == t


def test_many_copies_eight_levels_linear(tmp_path):
    s = _scr(tmp_path)
    deep = "\n---\n".join([ADDR] * 1000)
    for _ in range(8):
        deep = json.dumps({"v": deep})
    t0 = time.time()
    sent, back, _ = _round(s, deep)
    assert time.time() - t0 < 8 and not _leak(sent) and back == deep


def test_scrub_time_limit_503(tmp_path):
    s = _scr(tmp_path, scrub_timeout_s=0.05)
    deep = "\n---\n".join([ADDR] * 3000)
    for _ in range(8):
        deep = json.dumps({"v": deep})
    t0 = time.time()
    with pytest.raises(c.ScrubError, match="scrub_timeout_s"):
        _round(s, deep)
    assert time.time() - t0 < 3
    assert _round(s, "Heath Cliff called")[0] != "Heath Cliff called"      # the next request is checked as usual


def test_scrub_time_limit_off(tmp_path):
    s = _scr(tmp_path, scrub_timeout_s=0)
    sent, back, _ = _round(s, json.dumps({"v": "\n".join([ADDR] * 50)}))
    assert not _leak(sent)


def test_inner_depth_incremental_matches(tmp_path):
    import random
    rnd = random.Random(11)
    for _ in range(200):
        x = rnd.choice(["Heath Cliff", "a\nb", "[1] x", "true", 'q"t', "\u00e9"])
        for _ in range(rnd.randint(1, 9)):
            x = rnd.choice([json.dumps({"v": x}), json.dumps([1, x], ensure_ascii=False), "[1] Contact: " + json.dumps(x)])
        raw = json.dumps(x)[1:-1]
        inc, buf = c._InnerJson(c._RESTORE_DEPTH - 1), []
        for p in sorted(rnd.sample(range(len(raw) + 1), min(len(raw) + 1, 12))):
            buf.extend(raw[len(buf):p])
            assert inc.depth(buf) == c._inner_depth(raw[:p], c._RESTORE_DEPTH - 1)
