"""0.4.2: listed values in escaped text (JSON \\uXXXX, HTML entities, %XX) on every request path, the egress re-check on
the decoded text, separators inside listed addresses, Title Case matching for names that are ordinary phrases, and the
loud-failure gaps (shrink log line, health status, check-config exit code)."""
import json, threading, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from swapback import core as c

NAME = "S\u00f8ren Haukeland"            # all made up
NAME2 = "Jos\u00e9 \u00c1ngel Qui\u00f1ones"
ADDR = "17 Example Mews, Dublin D04 X2Y1"


def _scr(tmp_path, known=None, **cfg):
    return c.Scrubber(dict(c.DEFAULTS, state_dir=str(tmp_path / "state"), **cfg), known=known)


def _round(s, text):
    f = c.Forms()
    out = s.scrub_text(text, forms=f)
    return out, s.restore_text(out, forms=f)


def _strings(o):
    if isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)
    elif isinstance(o, str):
        yield o
        try:
            yield from _strings(json.loads(o))
        except ValueError:
            pass


def _leaks(body, values=(NAME, NAME2)):
    folded = [c._strip_accents(v).lower() for v in values]
    for s in _strings(body):
        for d in [s] + [v[0] for v in c.decoded_views(s)]:
            low = c._strip_accents(d).lower()
            if any(f in low or " ".join(reversed(f.split(" ", 1))) in low for f in folded):
                return True
    return False


# ---------------------------------------------------------------- unit: escaped spellings
@pytest.mark.parametrize("text", [
    json.dumps({"name": NAME}), json.dumps({"name": NAME.upper()}), json.dumps({"name": "Haukeland, S\u00f8ren"}),
    json.dumps(json.dumps({"name": NAME})),                                  # JSON inside JSON
    json.dumps({"name": NAME}).replace("\\u00f8", "\\u00F8"),               # upper-case hex
    "S&oslash;ren Haukeland", "S&#248;ren Haukeland", "S&#xF8;ren Haukeland", "S%C3%B8ren%20Haukeland",
    "S\\xf8ren Haukeland", json.dumps({"who": NAME2}), json.dumps({"who": "Jose Angel Quinones"}),
])
def test_escaped_spellings_are_swapped_and_restored_decoded(tmp_path, text):
    s = _scr(tmp_path, known=[(NAME, "PERSON"), (NAME2, "PERSON")])
    out, back = _round(s, text)
    assert "PERSON_" in out and not _leaks({"t": out})
    assert s.leftover_known(out) == 0
    assert c._strip_accents(back).lower().replace(",", "").split() != []      # comes back as readable text


def test_restore_gives_the_decoded_spelling(tmp_path):
    s = _scr(tmp_path, known=[(NAME, "PERSON")])
    out, back = _round(s, json.dumps({"name": NAME}))
    assert back == '{"name": "%s"}' % NAME


def test_egress_check_decodes_escapes_before_checking(tmp_path, monkeypatch):
    s = _scr(tmp_path, known=[(NAME, "PERSON")])
    assert s.leftover_known(json.dumps({"name": NAME})) == 1
    assert s.leftover_known("S&#248;ren Haukeland") == 1
    monkeypatch.setattr(s, "spans_keyed", lambda text: [])                 # simulate a swap that misses everything
    body = {"messages": [{"role": "tool", "tool_call_id": "t", "content": json.dumps({"name": NAME})}]}
    with pytest.raises(c.ScrubError, match="egress check"):
        s.scrub_request(body)


@pytest.mark.parametrize("text", ["C:\\new\\temp", "100% sure", "a &amp; b", "50%off", "\\u00f8 alone", "%%20",
                                  json.dumps({"k": "caf\u00e9 au lait"}), "&#xD800; &#99999999;", "\\ud800 broken"])
def test_escapes_without_a_listed_value_are_untouched(tmp_path, text):
    s = _scr(tmp_path, known=[(NAME, "PERSON")])
    assert _round(s, text) == (text, text)


# ---------------------------------------------------------------- unit: separators inside a listed address
@pytest.mark.parametrize("text", ["17 Example Mews\nDublin D04 X2Y1", "17 Example Mews Dublin D04 X2Y1",
                                  "17 Example Mews,\nDublin D04 X2Y1", "17 EXAMPLE MEWS, DUBLIN D04 X2Y1",
                                  json.dumps({"addr": "17 Example Mews\nDublin D04 X2Y1"})])
def test_two_line_or_comma_free_address(tmp_path, text):
    s = _scr(tmp_path, known=[(ADDR, "ADDRESS")])
    out, back = _round(s, text)
    assert "Mews" not in out and "MEWS" not in out and "D04" not in out and s.leftover_known(out) == 0
    if "\\n" not in text:
        assert back == text


# ---------------------------------------------------------------- unit: names that are ordinary phrases
@pytest.mark.parametrize("text,swapped", [
    ("I will hunt for the keys", False), ("Will hunt season open?", False), ("The rose marsh by the river", False),
    ("Will Hunt called", True), ("WILL HUNT called", True), ("Hunt, Will said", True), ("ask Rose Marsh", True),
])
def test_common_phrase_names_match_title_case_only(tmp_path, text, swapped):
    s = _scr(tmp_path, known=[("Will Hunt", "PERSON"), ("Rose Marsh", "PERSON")])
    out, back = _round(s, text)
    assert ("PERSON_" in out) is swapped and back == text and s.leftover_known(out) == 0


def test_common_phrase_opt_out_and_other_names_any_case(tmp_path):
    s = _scr(tmp_path, known=[("Will Hunt", "PERSON")], common_names_title_case=False)
    assert "PERSON_" in _round(s, "I will hunt for the keys")[0]
    s = _scr(tmp_path, known=[("Priya Raman", "PERSON"), ("Will Hunt", "PERSON")])
    assert "PERSON_" in _round(s, "priya raman")[0]                       # only ordinary-word names are strict


def test_lower_case_common_phrase_does_not_block(tmp_path):
    s = _scr(tmp_path, known=[("Will Hunt", "PERSON")])
    body = {"model": "i will hunt", "messages": [{"role": "user", "content": "I will hunt"}]}
    s.scrub_request(body)
    assert body["messages"][0]["content"].startswith("I will hunt")


def test_common_phrase_warning_names_the_setting(tmp_path):
    kf = tmp_path / "known.yaml"
    kf.write_text("PERSON:\n  - Priya Raman\n  - Will Hunt\n")
    w = c.known_warnings(str(kf))
    assert len(w) == 1 and "line 3" in w[0] and "common_names_title_case" in w[0] and "Will" not in w[0]


# ---------------------------------------------------------------- loud failures
def test_shrink_prints_a_line_and_health_is_not_plain_ok(tmp_path, capfd):
    from swapback import proxy
    kf = tmp_path / "known.yaml"
    kf.write_text("PERSON:\n" + "".join("  - Person%s Name\n" % x for x in "ABCDEFGH"))
    cfgp = tmp_path / "swapback.yaml"
    cfgp.write_text("upstream: http://127.0.0.1:9\nlisten: 127.0.0.1:0\nknown_file: known.yaml\nstate_dir: st\n")
    srv = proxy.make_server(c.load_cfg(str(cfgp)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = "http://127.0.0.1:%d/scrubber/health" % srv.server_address[1]
        h = json.loads(urllib.request.urlopen(url, timeout=10).read())
        assert h["ok"] is True and h["status"] == "ok"
        kf.write_text("PERSON:\n  - PersonA Name\n")
        h = json.loads(urllib.request.urlopen(url, timeout=10).read())
        assert h["ok"] is False and h["status"] == "warning" and any("still protecting" in w for w in h["warnings"])
        err = capfd.readouterr().err
        assert "swapback: warning: known_file went from" in err and "PersonB" not in err
    finally:
        srv.shutdown()
        srv.server_close()


def test_check_config_exits_non_zero_on_an_empty_known_file(tmp_path, monkeypatch, capsys):
    (tmp_path / "known.yaml").write_text("PERSON: []\n")
    (tmp_path / "swapback.yaml").write_text("known_file: known.yaml\nstate_dir: st\n")
    monkeypatch.setenv("SWAPBACK_CONFIG", str(tmp_path / "swapback.yaml"))
    with pytest.raises(SystemExit) as e:
        c.main(["swapback", "check-config"])
    assert e.value.code == 2 and "no values" in capsys.readouterr().err


# ---------------------------------------------------------------- every request path, through the proxy
class _Up(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.loads(raw)
        _Up.seen.append(body)
        last = (body.get("messages") or body.get("input"))[-1]            # the tool result (not the system note)
        txt = json.dumps(last, ensure_ascii=False)
        i = txt.find("\u00abPERSON_")
        ph = txt[i:txt.find("\u00bb", i) + 1] if i >= 0 else "none"
        reply = "hello " + ph
        stream = body.get("stream")
        if self.path.endswith("/chat/completions"):
            if not stream:
                return self._json({"id": "x", "object": "chat.completion", "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}]})
            evs = [{"id": "x", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": reply[k:k + 4]}}]}
                   for k in range(0, len(reply), 4)]
            evs.append({"id": "x", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            return self._sse([(None, e) for e in evs] + [(None, "[DONE]")])
        if self.path.endswith("/messages"):
            if not stream:
                return self._json({"id": "m", "type": "message", "role": "assistant", "content": [{"type": "text", "text": reply}],
                                   "stop_reason": "end_turn"})
            evs = [("message_start", {"type": "message_start", "message": {"id": "m", "role": "assistant", "content": []}}),
                   ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})]
            evs += [("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": reply[k:k + 4]}})
                    for k in range(0, len(reply), 4)]
            evs += [("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_stop", {"type": "message_stop"})]
            return self._sse(evs)
        if not stream:
            return self._json({"id": "r", "object": "response", "output": [
                {"type": "message", "id": "o", "role": "assistant", "content": [{"type": "output_text", "text": reply}]}]})
        evs = [("response.output_text.delta", {"type": "response.output_text.delta", "item_id": "o", "output_index": 0,
                                               "content_index": 0, "delta": reply[k:k + 4]}) for k in range(0, len(reply), 4)]
        evs += [("response.output_text.done", {"type": "response.output_text.done", "item_id": "o", "output_index": 0,
                                               "content_index": 0, "text": reply}),
                ("response.completed", {"type": "response.completed", "response": {"id": "r", "output": []}})]
        return self._sse(evs)

    def _json(self, o):
        b = json.dumps(o).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _sse(self, evs):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for name, e in evs:
            data = e if isinstance(e, str) else json.dumps(e)
            self.wfile.write((("event: %s\n" % name) if name else "").encode() + b"data: " + data.encode() + b"\n\n")
        self.wfile.flush()
        self.close_connection = True


@pytest.fixture(scope="module")
def paths_proxy(tmp_path_factory):
    from swapback import proxy
    d = tmp_path_factory.mktemp("p042")
    up = ThreadingHTTPServer(("127.0.0.1", 0), _Up)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    (d / "known.yaml").write_text("PERSON:\n  - %s\n  - %s\nADDRESS:\n  - %s\n" % (NAME, NAME2, ADDR), encoding="utf-8")
    cfgp = d / "swapback.yaml"
    cfgp.write_text("upstream: http://127.0.0.1:%d\nlisten: 127.0.0.1:0\nknown_file: known.yaml\nstate_dir: st\n"
                    % up.server_address[1])
    srv = proxy.make_server(c.load_cfg(str(cfgp)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    up.shutdown()


def _tool_json(v):
    return json.dumps({"name": v, "addr": "17 Example Mews\nDublin D04 X2Y1"})       # ensure_ascii: S\u00f8ren


def _bodies(v):
    args = json.dumps({"q": v})
    chat = {"model": "m", "messages": [
        {"role": "user", "content": "look them up"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t0", "type": "function", "function": {"name": "lookup_contact", "arguments": args}}]},
        {"role": "tool", "tool_call_id": "t0", "content": _tool_json(v)}]}
    anth = {"model": "m", "max_tokens": 20, "messages": [
        {"role": "user", "content": "look them up"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t0", "name": "lookup_contact", "input": {"q": args}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t0", "content": _tool_json(v)},
            {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": _tool_json(v)}]}]}]}
    resp = {"model": "m", "input": [
        {"role": "user", "content": "look them up"},
        {"type": "function_call", "call_id": "t0", "name": "lookup_contact", "arguments": args},
        {"type": "function_call_output", "call_id": "t0", "output": _tool_json(v)}]}
    return {"/v1/chat/completions": chat, "/v1/messages": anth, "/v1/responses": resp}


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/messages", "/v1/responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("v", [NAME, NAME2])
def test_escaped_listed_values_never_reach_the_upstream(paths_proxy, path, stream, v):
    body = dict(_bodies(v)[path], stream=stream)
    _Up.seen.clear()
    data = json.dumps(body).encode()                    # ensure_ascii: the whole request is \u-escaped too
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (paths_proxy, path), data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        assert r.status == 200
        out = r.read().decode("utf-8")
    assert len(_Up.seen) == 1
    seen = _Up.seen[0]
    assert not _leaks(seen, (v,)), "listed value reached the upstream"
    assert "Mews" not in json.dumps(seen, ensure_ascii=False)
    assert v in out or json.dumps(v)[1:-1] in out          # the reply came back with the real value


# ---------------------------------------------------------------- swapback selftest
def test_selftest_passes_and_prints_numbers(capsys):
    with pytest.raises(SystemExit) as e:
        c.main(["swapback", "selftest", "--json"])
    assert e.value.code == 0
    r = json.loads(capsys.readouterr().out)
    assert r["pass"] and r["leaked_forms"] == 0 and r["listed_caught"] == r["listed_values"] >= 30
    assert r["round_trip_exact"] == r["round_trips"] == r["requests"] and r["paths"] == 7
    assert r["negatives_untouched"] == r["negatives"] >= 40


def test_selftest_leak_check_sees_escaped_and_reordered_values():
    from swapback import selftest as st
    assert st.leaked({"m": json.dumps({"n": NAME})}, NAME, "PERSON")
    assert st.leaked({"m": "Haukeland, S&#248;ren"}, NAME, "PERSON")
    assert st.leaked({"m": "call 98450-12873"}, "+91 98450 12873", "PHONE")
    assert not st.leaked({"m": "\u00abPERSON_1\u00bb said hi"}, NAME, "PERSON")
