"""0.4.3: '+' and '/' as separators (URL query strings, form bodies), NFD accents, zero-width characters and fullwidth
digits, on every request path and in the egress re-check; Title Case only for names made of dictionary words; commas
between ordinary lower-case words; JSON-escaped restore inside a JSON reply; one-line startup error; health exit code."""
import json, threading, unicodedata, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer

import pytest

from swapback import core as c
from test_v042 import _Up, _strings

# all made up
NAME = "Tobias Wrenfield"
ACC = "Th\u00e9r\u00e8se Gauthier-\u00c9brard"
ADDR = "14 Kittiwake Lane, Portree IV51 9EH"
PHONE = "+44 7700 914 382"
EMAIL = "t.wrenfield@example.org"
FW = {ch: chr(ord(ch) + 0xFEE0) for ch in "0123456789+"}
KNOWN = [(NAME, "PERSON"), (ACC, "PERSON"), (ADDR, "ADDRESS"), (PHONE, "PHONE"), (EMAIL, "EMAIL"),
         ("Drew Wells", "PERSON"), ("Jack Frost", "PERSON"), ("Gene Pool", "PERSON"), ("Grace Lee", "PERSON"),
         ("Ngozi Okafor", "PERSON")]


def _scr(tmp_path, known=KNOWN, **cfg):
    return c.Scrubber(dict(c.DEFAULTS, state_dir=str(tmp_path / "state"), **cfg), known=known)


def _zw(v):
    return v[:2] + "\u200b" + v[2:]


ENCODED = [
    ("plus", urllib.parse.quote_plus(NAME)), ("plus_url", "https://example.com/search?q=" + urllib.parse.quote_plus(NAME)),
    ("plus_acc", urllib.parse.quote_plus(ACC)), ("plus_addr", urllib.parse.quote_plus(ADDR)),
    ("plus_phone", urllib.parse.quote_plus(PHONE)), ("form_body", "name=%s&city=x" % urllib.parse.quote_plus(NAME)),
    ("slash", "Tobias/Wrenfield"), ("raw_plus", "Tobias+Wrenfield"), ("nfd", unicodedata.normalize("NFD", ACC)),
    ("zw_name", _zw(NAME)), ("zw_acc", _zw(ACC)), ("zw_email", _zw(EMAIL)), ("zw_join", "Tobias\u200b Wrenfield"),
    ("fw_phone", "".join(FW.get(ch, ch) for ch in PHONE)), ("fw_phone_spaced", "\uff10\uff17\uff17\uff10\uff10 \uff19\uff11\uff14 \uff13\uff18\uff12"),
    ("fw_letters", "\uff34\uff4f\uff42\uff49\uff41\uff53 Wrenfield"),
]


# ---------------------------------------------------------------- unit
@pytest.mark.parametrize("kind,text", ENCODED)
def test_encoded_forms_are_swapped_in_text_and_in_json(tmp_path, kind, text):
    s = _scr(tmp_path)
    for t in (text, json.dumps({"v": text}), json.dumps({"v": text}, ensure_ascii=False), json.dumps(json.dumps({"q": text}))):
        out = s.scrub_text(t)
        assert out != t and "\u00ab" in out, (kind, t)
        assert s.leftover_known(out) == 0


@pytest.mark.parametrize("kind,text", ENCODED)
def test_egress_check_alone_blocks_every_encoded_form(tmp_path, monkeypatch, kind, text):
    s = _scr(tmp_path)
    monkeypatch.setattr(s, "spans_keyed", lambda t: [])                     # a swap that misses everything
    for content in (text, json.dumps({"contact": text})):
        body = {"messages": [{"role": "tool", "tool_call_id": "t", "content": content}]}
        with pytest.raises(c.ScrubError, match="egress check"):
            s.scrub_request(body)


@pytest.mark.parametrize("text", [unicodedata.normalize("NFD", ACC), _zw(NAME), "Tobias+Wrenfield", "Tobias/Wrenfield",
                                  "".join(FW.get(ch, ch) for ch in PHONE)])
def test_plain_normalised_forms_round_trip_byte_exact(tmp_path, text):
    s = _scr(tmp_path)
    f = c.Forms()
    out = s.scrub_text("hi " + text + " bye", forms=f)
    assert "\u00ab" in out and s.restore_text(out, forms=f) == "hi " + text + " bye"


def test_paths_with_a_listed_name_segment_are_still_kept(tmp_path):
    s = _scr(tmp_path)
    for p in ("/home/tobias/wrenfield/notes.txt", "~/tobias/wrenfield/x"):
        assert s.scrub_text("open " + p) == "open " + p


@pytest.mark.parametrize("text", [
    "she drew wells for three villages", "Drew wells before the dry season?", "jack frost nipped the roses",
    "Jack frost warnings tonight.", "the gene pool is small", "Gene pool size matters.", "say grace, lee side",
    "Wells, drew a crowd.", "Frost, jack up the car.", "the drew/wells column", "Pool, gene therapy news."])
def test_dictionary_word_names_are_left_in_ordinary_text(tmp_path, text):
    assert _scr(tmp_path).scrub_text(text) == text


@pytest.mark.parametrize("text", ["Drew Wells called.", "DREW WELLS", "Wells, Drew", "Jack Frost said hi",
                                  "Gene Pool", "Lee, Grace said", "ngozi okafor called", "okafor, ngozi"])
def test_names_as_written_still_swap(tmp_path, text):
    assert "\u00abPERSON_" in _scr(tmp_path).scrub_text(text)


def test_dictionary_word_names_opt_out(tmp_path):
    s = _scr(tmp_path, common_names_title_case=False)
    assert "\u00abPERSON_" in s.scrub_text("she drew wells")


def test_wordlist_ships_and_marks_dictionary_names():
    w = c.dictionary_words()
    assert len(w) > 50000 and {"drew", "wells", "frost", "gates", "pool"} <= w
    assert c._is_common_phrase("Bill Gates") and c._is_common_phrase("Jack Frost")
    assert not c._is_common_phrase("Ngozi Okafor") and not c._is_common_phrase(ACC)


# ---------------------------------------------------------------- restore into a JSON reply
def _addr_req(s):
    f = c.Forms()
    out = s.scrub_text(json.dumps({"a": "14 Kittiwake Lane,\nPortree IV51 9EH", "q": 'say "hi" \\ there'}), forms=f)
    ph = c.PH_RE.search(out).group(0)
    return f, ph


def test_restore_inside_a_json_reply_keeps_it_valid(tmp_path):
    s = _scr(tmp_path)
    f, ph = _addr_req(s)
    reply = '{"address": "%s", "n": 1}' % ph
    back = s.restore_text(reply, forms=f)
    assert json.loads(back)["address"] == "14 Kittiwake Lane,\nPortree IV51 9EH"


def test_restore_inside_a_streamed_json_reply_keeps_it_valid(tmp_path):
    s = _scr(tmp_path)
    f, ph = _addr_req(s)
    reply = '  [{"address": "%s"}, "%s"]' % (ph, ph)
    for size in (1, 3, 7):
        f2, _ = _addr_req(s)
        r = s.stream_restorer(forms=f2)
        out = "".join(r.feed(reply[i:i + size]) for i in range(0, len(reply), size)) + r.flush()
        assert json.loads(out)[0]["address"] == "14 Kittiwake Lane,\nPortree IV51 9EH"


def test_restore_in_prose_or_outside_strings_stays_raw(tmp_path):
    s = _scr(tmp_path)
    f, ph = _addr_req(s)
    assert s.restore_text("Address: " + ph, forms=f) == "Address: 14 Kittiwake Lane,\nPortree IV51 9EH"


# ---------------------------------------------------------------- every request path, through the proxy
@pytest.fixture(scope="module")
def proxy43(tmp_path_factory):
    from swapback import proxy
    d = tmp_path_factory.mktemp("p043")
    up = ThreadingHTTPServer(("127.0.0.1", 0), _Up)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    by = {}
    for v, t in KNOWN:
        by.setdefault(t, []).append(v)
    (d / "known.yaml").write_text("".join("%s:\n%s" % (t, "".join("  - %s\n" % json.dumps(v, ensure_ascii=False) for v in vs))
                                          for t, vs in by.items()), encoding="utf-8")
    cfgp = d / "swapback.yaml"
    cfgp.write_text("upstream: http://127.0.0.1:%d\nlisten: 127.0.0.1:0\nknown_file: known.yaml\nstate_dir: st\n"
                    % up.server_address[1])
    srv = proxy.make_server(c.load_cfg(str(cfgp)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    up.shutdown()


def _fold(s):
    s = unicodedata.normalize("NFKD", s.translate(dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return " ".join(urllib.parse.unquote_plus(s).replace("/", " ").replace("-", " ").split())


def _leak(seen):
    needles = ["tobias wrenfield", "therese gauthier ebrard", "kittiwake lane", "7700914382", "t.wrenfield@example.org"]
    for s in _strings(seen):
        f = _fold(s)
        digits = "".join(ch for ch in f if ch.isdigit())
        if any(n in f for n in needles[:3] + needles[4:]) or needles[3] in digits:
            return True
    return False


def _bodies(text):
    tool = json.dumps({"contact": text})
    args = json.dumps({"q": text})
    chat = {"model": "m", "messages": [
        {"role": "user", "content": "look it up: " + text},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t0", "type": "function", "function": {"name": "lookup_contact", "arguments": args}}]},
        {"role": "tool", "tool_call_id": "t0", "content": tool}]}
    anth = {"model": "m", "max_tokens": 20, "messages": [
        {"role": "user", "content": "look it up: " + text},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t0", "name": "lookup_contact", "input": {"q": text}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t0", "content": tool}]}]}
    resp = {"model": "m", "input": [
        {"role": "user", "content": "look it up: " + text},
        {"type": "function_call", "call_id": "t0", "name": "lookup_contact", "arguments": args},
        {"type": "function_call_output", "call_id": "t0", "output": tool}]}
    return {"/v1/chat/completions": chat, "/v1/messages": anth, "/v1/responses": resp}


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/messages", "/v1/responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("kind,text", ENCODED)
def test_encoded_forms_never_reach_the_upstream(proxy43, path, stream, kind, text):
    _Up.seen.clear()
    for ascii_only in (True, False):
        body = dict(_bodies(text)[path], stream=stream)
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (proxy43, path),
                                     data=json.dumps(body, ensure_ascii=ascii_only).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            assert r.status == 200
            r.read()
    assert len(_Up.seen) == 2 and not any(_leak(x) for x in _Up.seen), kind


# ---------------------------------------------------------------- CLI polish
def test_missing_known_file_at_startup_is_one_line(tmp_path):
    import subprocess, sys
    (tmp_path / "swapback.yaml").write_text("known_file: nothere.yaml\nstate_dir: st\nlisten: 127.0.0.1:0\n")
    env = dict(__import__("os").environ, SWAPBACK_CONFIG=str(tmp_path / "swapback.yaml"))
    for argv in (["-c", "from swapback.proxy import main; main()"], ["-c", "from swapback.core import main; main()", "stats"]):
        p = subprocess.run([sys.executable] + argv, env=env, capture_output=True, text=True, timeout=60)
        assert p.returncode == 2, p.stderr
        assert "Traceback" not in p.stderr and p.stderr.strip().count("\n") == 0
        assert "missing" in p.stderr and "nothere.yaml" in p.stderr


def test_health_exits_non_zero_when_not_ok(tmp_path, monkeypatch, capsys):
    (tmp_path / "swapback.yaml").write_text("state_dir: st\nlisten: 127.0.0.1:1\n")
    monkeypatch.setenv("SWAPBACK_CONFIG", str(tmp_path / "swapback.yaml"))
    with pytest.raises(SystemExit) as e:
        c.main(["swapback", "health"])
    assert e.value.code == 1 and json.loads(capsys.readouterr().out)["ok"] is False
    monkeypatch.setattr(c, "health", lambda cfg=None: {"ok": True, "status": "ok"})
    with pytest.raises(SystemExit) as e:
        c.main(["swapback", "health"])
    assert e.value.code == 0


def test_selftest_covers_the_new_forms(capsys):
    from swapback import selftest as st
    fs = st.forms("Mireille Ch\u00e2teau-Dufresne", "PERSON")
    assert any("+" in x for x in fs) and any("\u200b" in x for x in fs) and any(unicodedata.is_normalized("NFC", x) is False for x in fs)
    assert any("\uff10" <= ch <= "\uff19" for x in st.forms("+1 503 555 0147", "PHONE") for ch in x)
    r = st.run(as_json=True)
    capsys.readouterr()
    assert r["pass"] and r["egress_caught"] == r["egress_forms"] > 0 and r["leaked_forms"] == 0
