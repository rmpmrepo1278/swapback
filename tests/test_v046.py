"""0.4.6: review #6 repros. A listed multi-line address whose lines are joined by an HTML tag plus a line break
(<br>\\n, <br />\\n, <br/>\\r\\n, </p>\\n<p>) inside JSON text is swapped whole; the listed matcher claims its span before
the pattern detectors; a listed value a pattern swap cut into is still blocked by the egress re-check; JSON up to 5
string levels deep is matched; doubly escaped placeholders in tool-call arguments are restored."""
import json, os, re
import pytest
from swapback import core as c

ADDR = "1407 Quillback Lane\nUnit 12C\nEugene, OR 97403"
ADDR2 = "3302 Saguaro Wash Drive\nTucson, AZ 85719"
JOINS = ["<br>\n", "<br />\n", "<br/>\r\n", "</p>\n<p>"]
LATER = ["Unit 12C", "Eugene", "97403", "Tucson", "85719"]


def _scr(tmp_path, known=((ADDR, "ADDRESS"), (ADDR2, "ADDRESS"), ("Hazel Wood", "PERSON"))):
    return c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=str(tmp_path)), known=list(known))


def _flat(o):
    return json.dumps(o, ensure_ascii=False)


def _tool_bodies(doc):
    tc = [{"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": json.dumps({"q": "x"})}}]
    tc2 = [{"id": "c2", "type": "function", "function": {"name": "send_note", "arguments": doc}}]
    return [
        {"messages": [{"role": "user", "content": "look it up"}, {"role": "assistant", "content": None, "tool_calls": tc},
                      {"role": "tool", "tool_call_id": "c1", "content": doc}]},
        {"messages": [{"role": "user", "content": "note it"}, {"role": "assistant", "content": None, "tool_calls": tc2},
                      {"role": "tool", "tool_call_id": "c2", "content": "ok"}]},
        {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": doc}]}]},
    ]


def test_review6_repro_exact(tmp_path):
    s = _scr(tmp_path, known=[(ADDR, "ADDRESS")])
    doc = json.dumps({"result": "1407 Quillback Lane<br />\nUnit 12C<br />\nEugene, OR 97403"})
    out = s.scrub_text(doc)
    assert out.count("ADDRESS_") == 1 and not any(x in out for x in LATER), out


@pytest.mark.parametrize("join", JOINS)
@pytest.mark.parametrize("addr", [ADDR, ADDR2])
@pytest.mark.parametrize("ensure_ascii", [True, False])
@pytest.mark.parametrize("wrap", ["%s", "Please forward this to %s before the review.", "notes:\n- owner: %s\n- status: open"])
def test_tag_joined_address_in_json_is_swapped_not_blocked(tmp_path, join, addr, ensure_ascii, wrap):
    s = _scr(tmp_path)
    doc = json.dumps({"result": wrap % join.join(addr.split("\n"))}, ensure_ascii=ensure_ascii)
    for body in _tool_bodies(doc):
        s.scrub_request(body)                       # raises ScrubError when blocked: the test fails then
        flat = _flat(body)
        assert "ADDRESS_" in flat and not any(x in flat for x in LATER), flat[-300:]


@pytest.mark.parametrize("join", JOINS + ["\\n"])
def test_nested_four_levels_deep_is_swapped(tmp_path, join):
    s = _scr(tmp_path)
    txt = "The record says %s and nothing else." % join.join(ADDR.split("\n"))
    doc = json.dumps({"data": json.dumps({"rows": [json.dumps({"v": txt})]})})
    body = _tool_bodies(doc)[0]
    s.scrub_request(body)
    flat = _flat(body)
    assert "ADDRESS_" in flat and not any(x in flat for x in LATER)


def test_deeper_than_matcher_is_never_sent(tmp_path):
    """past the matcher's 5 levels: swapped or blocked, never sent as written"""
    s = _scr(tmp_path)
    doc = "The record says " + "\\n".join(ADDR.split("\n"))
    for _ in range(6):
        doc = json.dumps({"v": doc})
    body = _tool_bodies(doc)[0]
    try:
        s.scrub_request(body)
    except c.ScrubError:
        return
    assert not any(x in _flat(body) for x in LATER)


def test_listed_value_claims_its_span_before_the_detectors():
    listed = (10, 60, "ADDRESS", 0, "v", ("k", "v"))
    inside = (10, 29, "ADDRESS", 1, "street", None)      # the street line only: cuts into the listed value
    around = (5, 70, "ADDRESS", 1, "wider", None)        # covers it whole: kept
    apart = (80, 90, "PHONE", 1, "p", None)
    assert c._listed_first([listed, inside, around, apart]) == [listed, around, apart]


def test_pattern_swap_cutting_a_listed_value_still_blocks(tmp_path, monkeypatch):
    """the egress re-check sees the text with only listed values swapped, so a listed address that a pattern swap
    split (a matcher miss) is blocked, not sent with its later lines"""
    s = _scr(tmp_path, known=[(ADDR, "ADDRESS")])
    monkeypatch.setattr(s, "_known_hits_full", lambda text, low=None: [])     # a matcher miss
    doc = json.dumps({"result": "1407 Quillback Lane<br />\nUnit 12C<br />\nEugene, OR 97403"})
    body = _tool_bodies(doc)[0]
    with pytest.raises(c.ScrubError, match="egress"):
        s.scrub_request(body)


def test_plain_and_bare_br_still_swapped(tmp_path):
    s = _scr(tmp_path)
    for t in ("<br>".join(ADDR.split("\n")), "<br />\n".join(ADDR.split("\n")), ADDR):
        out = s.scrub_text(t)
        assert out.count("ADDRESS_") == 1 and not any(x in out for x in LATER)


@pytest.mark.parametrize("ensure_ascii", [True, False])
def test_doubly_escaped_placeholder_in_tool_args_restored(tmp_path, ensure_ascii):
    s = _scr(tmp_path)
    fo = c.Forms()
    ph = re.findall("\u00ab[A-Z_]+_\\d+\u00bb", s.scrub_text("send it to " + ADDR, forms=fo))[0]
    args = json.dumps({"to": ph, "note": "line1\nline2", "nested": json.dumps({"a": ph}, ensure_ascii=ensure_ascii)},
                      ensure_ascii=ensure_ascii)
    o = json.loads(s.restore_text(args, js=True, forms=fo))
    assert o["to"] == ADDR and o["note"] == "line1\nline2" and json.loads(o["nested"])["a"] == ADDR


def test_doubly_escaped_placeholder_streamed_split_anywhere(tmp_path):
    s = _scr(tmp_path)
    fo = c.Forms()
    ph = re.findall("\u00ab[A-Z_]+_\\d+\u00bb", s.scrub_text("send it to Hazel Wood", forms=fo))[0]
    args = json.dumps({"to": ph, "nested": json.dumps({"a": ph})})
    want = s.restore_text(args, js=True, forms=fo)
    assert json.loads(json.loads(want)["nested"])["a"] == "Hazel Wood"
    for k in range(1, 9):
        sr = s.stream_restorer(js=True, forms=fo)
        got = "".join(sr.feed(args[i:i + k]) for i in range(0, len(args), k)) + sr.flush()
        assert got == want, k


def test_triple_backslash_is_an_escaped_backslash_then_one_level(tmp_path):
    s = _scr(tmp_path)
    fo = c.Forms()
    ph = re.findall("\u00ab[A-Z_]+_\\d+\u00bb", s.scrub_text("send it to Hazel Wood", forms=fo))[0]
    args = json.dumps({"p": "\\" + ph})                  # a literal backslash before the placeholder
    assert json.loads(s.restore_text(args.replace(ph, json.dumps(ph)[1:-1]), js=True, forms=fo))["p"] == "\\Hazel Wood"


def test_readme_claims():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    readme = open(os.path.join(root, "README.md"), encoding="utf-8").read()
    assert "or capitals" not in readme
    pkg = os.path.join(root, "swapback")
    lines = size = 0
    for d, _dirs, files in os.walk(pkg):
        for f in files:
            if f.endswith(".py"):
                b = open(os.path.join(d, f), "rb").read()
                lines += b.count(b"\n")
                size += len(b)
    m = re.search(r"about ([\d,]+) lines of Python", readme)
    assert m and abs(int(m.group(1).replace(",", "")) - lines) <= 0.05 * lines, (m and m.group(1), lines)
    m = re.search(r"about ([\d,]+) lines \((\d+) KB\)", readme)
    assert m and abs(int(m.group(1).replace(",", "")) - lines) <= 0.05 * lines and abs(int(m.group(2)) - size / 1024) <= 0.05 * size / 1024


def test_escaped_placeholder_in_anthropic_tool_use_input_restored(tmp_path):
    s = _scr(tmp_path)
    fo = c.Forms()
    ph = re.findall("\u00ab[A-Z_]+_\\d+\u00bb", s.scrub_text("send it to " + ADDR, forms=fo))[0]
    block = {"type": "tool_use", "id": "t9", "name": "save_contact",
             "input": {"to": ph, "nested": json.dumps({"a": ph}), "plain": "keep \\u00e9 as written"}}
    out = s.restore_obj(block, forms=fo)
    assert out["input"]["to"] == ADDR and json.loads(out["input"]["nested"])["a"] == ADDR
    assert out["input"]["plain"] == "keep \\u00e9 as written"


@pytest.mark.parametrize("body", ["%25" * 60000 + "41", "\\\\" * 50000 + "n", "%2525252541" * 20000])
def test_deep_decode_stays_fast(tmp_path, body):
    import time
    s = _scr(tmp_path)
    t0 = time.time()
    try:
        s.scrub_request({"messages": [{"role": "user", "content": body}]})
    except c.ScrubError:
        pass
    assert time.time() - t0 < 3


def test_pattern_hit_holding_a_whole_listed_value_is_not_blocked(tmp_path):
    s = _scr(tmp_path, known=[("w.strath@moorhaven.example", "EMAIL")])
    body = {"messages": [{"role": "user", "content": "Forward to w.strath [at] moorhaven [dot] example today."}]}
    s.scrub_request(body)
    assert "EMAIL_" in _flat(body) and "moorhaven" not in _flat(body)
