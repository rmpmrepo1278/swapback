"""0.4.7: review #7 repros. A value in JSON nested 1 to 8 string levels deep is restored escaped once per level, so
every level parses and the value comes back byte for byte (plain and streamed); a string nested deeper than the 8
levels the matcher reads is blocked, not forwarded, and fast; a dictionary-word name is not joined across a sentence end,
a blank line or separate HTML block elements; a missing or broken config file is one readable line."""
import json, os, subprocess, sys, time
import pytest
from swapback import core as c

ADDR = "2219 Larkspur Hollow Road\nSuite 410\nBoise, ID 83702"
TABNAME = "Odile\tMarchetti"
LATER = ["Suite 410", "Boise", "83702", "Marchetti"]


def _scr(tmp_path):
    return c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=str(tmp_path)),
                      known=[(ADDR, "ADDRESS"), ("Odile Marchetti", "PERSON"), ("Grace Fields", "PERSON")])


def nest(x, k, ea=True):
    for _ in range(k):
        x = json.dumps({"v": x}, ensure_ascii=ea)
    return x


def unnest(x, k):
    for _ in range(k):
        x = json.loads(x)["v"]
    return x


def _tool(doc):
    return {"messages": [{"role": "user", "content": "look it up"}, {"role": "tool", "tool_call_id": "c1", "content": doc}]}


@pytest.mark.parametrize("k", range(1, 9))
@pytest.mark.parametrize("val", [ADDR, TABNAME])
@pytest.mark.parametrize("ea", [True, False])
@pytest.mark.parametrize("streamed", [False, True])
def test_review7_nested_restore_byte_exact(tmp_path, k, val, ea, streamed):
    s = _scr(tmp_path)
    doc = nest(val, k, ea)
    body, fo = _tool(doc), c.Forms()
    s.scrub_request(body, forms=fo)
    up = body["messages"][-1]["content"]
    assert not any(x in up for x in LATER)
    if streamed:
        sr = s.stream_restorer(forms=fo)
        back = "".join(sr.feed(up[i:i + 2]) for i in range(0, len(up), 2)) + sr.flush()
    else:
        back = s.restore_obj({"choices": [{"message": {"content": up}}]}, forms=fo)["choices"][0]["message"]["content"]
    assert unnest(back, k) == val
    assert back == doc


@pytest.mark.parametrize("k", [3, 5, 8])
def test_review7_nested_tool_args_escaped_placeholder(tmp_path, k):
    s = _scr(tmp_path)
    body, fo = _tool("addr: " + ADDR), c.Forms()
    s.scrub_request(body, forms=fo)
    ph = [w for w in body["messages"][-1]["content"].split() if "ADDRESS_" in w][0]
    out = s.restore_text(json.dumps({"to": nest(ph, k - 1)}), js=True, forms=fo)
    assert unnest(json.loads(out)["to"], k - 1) == ADDR


@pytest.mark.parametrize("k", [9, 10, 12, 16])
@pytest.mark.parametrize("val", [ADDR, TABNAME, "S\u00f8ren"])
def test_review7_deeper_than_cap_is_blocked(tmp_path, k, val):
    s = _scr(tmp_path)
    with pytest.raises(c.ScrubError, match="deeper than 8"):
        s.scrub_request(_tool(nest(val, k)))


def test_review7_big_16_level_body_answers_fast(tmp_path):
    s = _scr(tmp_path)
    doc = nest("Contact " + ADDR + " " + "row ok, nothing new. " * 40000, 16)
    assert len(doc) > 1_000_000
    t = time.time()
    with pytest.raises(c.ScrubError):
        s.scrub_request(_tool(doc))
    assert time.time() - t < 2


def test_review7_eight_levels_with_backslashes_not_blocked(tmp_path):
    s = _scr(tmp_path)
    doc = nest('a Windows path C:\\temp\\x and a "quote"', 7)
    body = _tool(doc)
    s.scrub_request(body)
    assert body["messages"][-1]["content"] == doc


@pytest.mark.parametrize("txt", ["Saving Grace. Fields of gold are lovely.", "<div>Grace</div>\n<div>Fields</div>",
                                 "<p>Grace</p><p>Fields</p>", "Amazing Grace!\n\nFields of barley.",
                                 json.dumps({"html": "<li>Grace</li>\n<li>Fields</li>"})])
def test_review7_dictionary_name_not_joined_across_breaks(tmp_path, txt):
    s = _scr(tmp_path)
    body = {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": txt}]}
    s.scrub_request(body)
    assert body["messages"][-1]["content"] == txt


@pytest.mark.parametrize("txt", ["Ask Grace Fields today.", "<b>Grace</b> Fields", "Grace<br>Fields", "Grace  Fields"])
def test_review7_dictionary_name_still_swapped(tmp_path, txt):
    s = _scr(tmp_path)
    body = {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": txt}]}
    s.scrub_request(body)
    out = body["messages"][-1]["content"]
    assert "PERSON_" in out and "Fields" not in out


def test_review7_non_dictionary_name_across_sentence_still_swapped(tmp_path):
    s = _scr(tmp_path)
    body = {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "Call Odile. Marchetti knows."}]}
    try:
        s.scrub_request(body)
    except c.ScrubError:
        return
    assert "Marchetti" not in body["messages"][-1]["content"]


@pytest.mark.parametrize("content,needle", [(None, "not found"), ("listen: [\n", "not valid YAML"),
                                            ("- a\n- b\n", "mapping")])
def test_review7_config_error_one_line(tmp_path, content, needle):
    p = tmp_path / "swapback.yaml"
    if content is not None:
        p.write_text(content)
    with pytest.raises(c.ConfigError, match=needle):
        c.load_cfg(str(p))
    env = dict(os.environ, SWAPBACK_CONFIG=str(p))
    for cmd in ([sys.executable, "-c", "from swapback.core import main; main(['swapback', 'check-config'])"],
                [sys.executable, "-c", "from swapback.proxy import main; main(['swapback-proxy'])"]):
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 2
        assert "Traceback" not in r.stderr and len(r.stderr.strip().splitlines()) == 1 and needle in r.stderr
