"""0.4.8: review #8 repros. Tool-call arguments with mixed escaping (an ASCII-escaped outer level around raw inner
JSON) get the restored value escaped for the JSON level it really sits at; \u00ab-escaped placeholders in a text reply
are restored inside JSON; a dictionary-word name whose words each fill one HTML block element is swapped; a bad
`listen` value or a port in use is one readable line."""
import json, os, socket, subprocess, sys
import pytest
from swapback import core as c

ADDR = "3381 Wintergreen Spur Road\nSuite 220\nBend, OR 97701"
TABNAME = "Odile\tMarchetti"


def _scr(tmp_path):
    return c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=str(tmp_path)),
                      known=[(ADDR, "ADDRESS"), ("Odile Marchetti", "PERSON"), ("Hazel Woods", "PERSON"),
                             ("Summer Lake", "PERSON"), ("Grace Fields", "PERSON")])


def _ph(s, val):
    body, fo = {"messages": [{"role": "user", "content": "x"},
                             {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"v": val})}]}, c.Forms()
    s.scrub_request(body, forms=fo)
    ph = json.loads(body["messages"][-1]["content"])["v"]
    assert c.PH_RE.fullmatch(ph), ph
    return ph, fo


def mixed_args(ph, inner, mix):
    """arguments = json.dumps({"path", "content": <ph nested `inner` levels>}); mix says which levels ASCII-escape"""
    x = ph
    for i in range(inner):
        x = json.dumps({"v": x}, ensure_ascii=(mix == "inner_ascii" or (mix == "alternate" and i % 2 == 0)))
    return json.dumps({"path": "a.json", "content": x}, ensure_ascii=(mix != "inner_ascii"))


def unwrap(args, inner):
    x = json.loads(args)["content"]
    for _ in range(inner):
        x = json.loads(x)["v"]
    return x


def _restore_args(s, fo, args, streamed, step=3):
    if not streamed:
        msg = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "write_file", "arguments": args}}]}
        out = s.restore_obj({"choices": [{"message": msg}]}, forms=fo)
        return out["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    r = c.ToolArgsStream(s, True, None, fo, "write_file")
    return "".join(r.feed(args[i:i + step]) for i in range(0, len(args), step)) + r.flush()


@pytest.mark.parametrize("inner", range(1, 9))
@pytest.mark.parametrize("val", [ADDR, TABNAME])
@pytest.mark.parametrize("mix", ["outer_ascii", "inner_ascii", "alternate"])
@pytest.mark.parametrize("streamed", [False, True])
def test_review8_mixed_escaping_tool_args(tmp_path, inner, val, mix, streamed):
    s = _scr(tmp_path)
    ph, fo = _ph(s, val)
    out = _restore_args(s, fo, mixed_args(ph, inner, mix), streamed)
    assert unwrap(out, inner) == val


def test_review8_repro_write_file(tmp_path):
    s = _scr(tmp_path)
    ph, fo = _ph(s, ADDR)
    args = json.dumps({"path": "a.json", "content": json.dumps({"address": ph}, ensure_ascii=False)})
    for streamed in (False, True):
        out = _restore_args(s, fo, args, streamed)
        assert json.loads(json.loads(out)["content"])["address"] == ADDR


@pytest.mark.parametrize("streamed", [False, True])
def test_review8_consistent_escaping_unchanged(tmp_path, streamed):
    s = _scr(tmp_path)
    ph, fo = _ph(s, ADDR)
    for ea in (True, False):
        for k in range(1, 8):
            x = ph
            for _ in range(k):
                x = json.dumps({"v": x}, ensure_ascii=ea)
            args = json.dumps({"path": "a.json", "content": x}, ensure_ascii=ea)
            assert unwrap(_restore_args(s, fo, args, streamed, 5), k) == ADDR


def _text(s, fo, t, streamed):
    if streamed:
        sr = s.stream_restorer(forms=fo)
        return "".join(sr.feed(t[i:i + 4]) for i in range(0, len(t), 4)) + sr.flush()
    return s.restore_obj({"choices": [{"message": {"content": t}}]}, forms=fo)["choices"][0]["message"]["content"]


@pytest.mark.parametrize("streamed", [False, True])
def test_review8_u00ab_placeholders_in_text(tmp_path, streamed):
    s = _scr(tmp_path)
    ph, fo = _ph(s, ADDR)
    e1 = json.dumps(ph)[1:-1]                        # \u00abADDRESS_1\u00bb
    fenced = "Here it is:\n```json\n{\"address\": \"%s\"}\n```\nDone." % e1
    out = _text(s, fo, fenced, streamed)
    assert json.loads(out.split("```json\n")[1].split("\n```")[0])["address"] == ADDR
    inline = 'Saved: {"address": "%s"} to the file.' % e1
    out = _text(s, fo, inline, streamed)
    assert json.loads(out[out.index("{"):out.rindex("}") + 1])["address"] == ADDR
    two = "```json\n%s\n```" % json.dumps({"doc": json.dumps({"address": ph})})
    out = _text(s, fo, two, streamed)
    assert json.loads(json.loads(out.split("```json\n")[1].split("\n```")[0])["doc"])["address"] == ADDR
    prose = "The escape \\u00ab is a guillemet; %s stays." % e1    # outside JSON: left as written
    assert _text(s, fo, prose, streamed) == prose


def _sent(s, text):
    body = {"messages": [{"role": "user", "content": text}]}
    s.scrub_request(body)
    return [m for m in body["messages"] if m["role"] == "user"][-1]["content"]


@pytest.mark.parametrize("text,gone", [
    ("<table><tr><td>Hazel</td><td>Woods</td></tr></table>", "Woods"),
    ("<ul><li>Hazel</li><li>Woods</li></ul>", "Woods"),
    ("<div>Summer</div>\n<div>Lake</div>", "Lake"),
    ("<tr>\n  <td><b>Hazel</b></td>\n  <td>Woods</td>\n</tr>", "Woods"),
    ("<tr><td>Hazel</td><td>Woods</td><td>hazel.w@example.net</td><td>Portland</td></tr>", "Woods"),
])
def test_review8_one_word_cells_swapped(tmp_path, text, gone):
    s = _scr(tmp_path)
    out = _sent(s, text)
    assert gone not in out and c.PH_RE.search(out), out
    tool = {"messages": [{"role": "user", "content": "x"},
                         {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"html": text})}]}
    s.scrub_request(tool)
    assert gone not in tool["messages"][-1]["content"]


@pytest.mark.parametrize("text", [
    "<h2>Summer</h2><p>Lake trips are the best part of the year.</p>",
    "<td>Hazel</td><td>Woods and forests</td>",
    "<li>Summer sale</li><li>Lake view rooms</li>",
    "Saving Grace. Fields of gold.",
])
def test_review8_ordinary_blocks_untouched(tmp_path, text):
    s = _scr(tmp_path)
    try:
        out = _sent(s, text)
    except c.ScrubError:
        pytest.fail("blocked")
    assert out == text


def _run_proxy(tmp_path, listen):
    cfg = tmp_path / "swapback.yaml"
    kn = tmp_path / "known.yaml"
    kn.write_text("PERSON:\n  - Odile Marchetti\n")
    cfg.write_text("mode: on\nknown_file: %s\nstate_dir: %s\nlisten: %s\n" % (kn, tmp_path / "st", listen))
    env = dict(os.environ, SWAPBACK_CONFIG=str(cfg))
    return subprocess.run([sys.executable, "-m", "swapback.proxy"], env=env, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("listen", ["'127.0.0.1'", "'127.0.0.1:notaport'", "'127.0.0.1:99999'"])
def test_review8_bad_listen_one_line(tmp_path, listen):
    p = _run_proxy(tmp_path, listen)
    assert p.returncode == 2 and "Traceback" not in p.stderr and len(p.stderr.strip().splitlines()) == 1, p.stderr
    assert "listen" in p.stderr


def test_review8_port_in_use_one_line(tmp_path):
    sk = socket.socket()
    sk.bind(("127.0.0.1", 0))
    sk.listen(1)
    try:
        p = _run_proxy(tmp_path, "'127.0.0.1:%d'" % sk.getsockname()[1])
    finally:
        sk.close()
    assert p.returncode == 2 and "Traceback" not in p.stderr and len(p.stderr.strip().splitlines()) == 1, p.stderr
    assert "cannot listen" in p.stderr


def test_review8_backslash_block_message(tmp_path):
    s = _scr(tmp_path)
    with pytest.raises(c.ScrubError, match="run of 256 or more backslashes"):
        s.scrub_request({"messages": [{"role": "user", "content": "\\" * 256}]})
