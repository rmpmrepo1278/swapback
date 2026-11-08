"""tests for pii_scrubber.core and pii_scrubber.proxy (swap and restore). Made-up values only."""
import http.server, json, os, socket, subprocess, sys, threading, time, urllib.error, urllib.request
import pytest

from pii_scrubber import core as s

KNOWN = [("Jane Q Doe", "PERSON"), ("Jane", "PERSON"), ("jdoe@corp.example", "EMAIL"), ("Ozzie Pellam", "PERSON")] + s.CANARY


@pytest.fixture
def sc(tmp_path):
    cfg = dict(s.DEFAULTS, mode="on", state_dir=str(tmp_path / "st"))
    return s.Scrubber(cfg, known=KNOWN)


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
    b = sc.scrub_text("reply to JANE Q DOE now")
    assert a.split()[0] == "\u00abPERSON_1\u00bb"
    assert sc.scrub_text("Jane Q Doe again").split()[0] == "\u00abPERSON_1\u00bb"     # stable for the same form
    assert "JANE" not in b
    assert sc.restore_text(b) == "reply to JANE Q DOE now"      # the original case comes back
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
    out = sc.restore_obj({"tool_calls": [{"function": {"name": "send", "arguments": args}}]})
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
    monkeypatch.setattr(sc, "spans", boom)
    with pytest.raises(s.ScrubError):
        sc.scrub_request({"messages": [{"role": "user", "content": "Jane Q Doe"}]})


def test_fail_closed_egress_leftover(sc, monkeypatch):
    monkeypatch.setattr(sc, "scrub_text", lambda t, c=None: t)          # a broken swap
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


def test_config_file_env_and_missing_known_fails_closed(tmp_path, monkeypatch):
    c = tmp_path / "cfg.yaml"
    c.write_text("listen: 127.0.0.1:9999\nknown_file: known.yaml\nstate_dir: st\n")
    monkeypatch.setenv("PII_SCRUBBER_UPSTREAM", "http://127.0.0.1:1234")
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
def _free_port():
    x = socket.socket()
    x.bind(("127.0.0.1", 0))
    p = x.getsockname()[1]
    x.close()
    return p


class _Up(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        b = self.rfile.read(int(self.headers["Content-Length"]))
        _Up.seen.append(b.decode("utf-8", "replace"))
        try:
            j = json.loads(b)
        except ValueError:
            j = None
        if not isinstance(j, dict) or "messages" not in j:          # embeddings / completions / non-JSON: echo back
            echo = (j.get("input") if "input" in j else j.get("prompt")) if isinstance(j, dict) else None
            o = json.dumps({"object": "list", "data": [{"embedding": [0.25, -0.5], "index": 0}], "echo": echo}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(o)))
            self.end_headers()
            self.wfile.write(o)
            return
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


@pytest.fixture
def proxy(tmp_path):
    up = _free_port()
    px = _free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", up), _Up)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfgp = tmp_path / "pii-scrubber.yaml"
    st = tmp_path / "st"
    st.mkdir()
    (tmp_path / "known.json").write_text(json.dumps({"known": [list(x) for x in KNOWN]}))
    cfgp.write_text("mode: \"on\"\nlisten: 127.0.0.1:%d\nupstream: http://127.0.0.1:%d\nstate_dir: %s\n"
                    "known_file: known.json\n" % (px, up, st))
    env = dict(os.environ, PII_SCRUBBER_CONFIG=str(cfgp))
    p = subprocess.Popen([sys.executable, "-B", "-m", "pii_scrubber.proxy"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(200):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/scrubber/health" % px, timeout=1)
            break
        except Exception:
            time.sleep(0.05)
    yield px, cfgp, tmp_path
    p.kill()
    srv.shutdown()


def _post(px, body):
    r = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % px,
                                                      data=json.dumps(body).encode(), method="POST",
                                                      headers={"Content-Type": "application/json"}), timeout=10)
    return r.status, r.read().decode()


def test_proxy_non_stream_and_stream(proxy):
    px, _, _ = proxy
    _Up.seen.clear()
    code, out = _post(px, {"model": "m", "messages": [{"role": "user", "content": "say Jane Q Doe"}]})
    assert code == 200 and json.loads(out)["choices"][0]["message"]["content"] == "ok Jane Q Doe"
    code, out = _post(px, {"model": "m", "stream": True, "messages": [{"role": "user", "content": "say Ozzie Pellam"}]})
    text = "".join(json.loads(l[6:])["choices"][0]["delta"].get("content", "") for l in out.splitlines()
                   if l.startswith("data: {"))
    assert text == "ok Ozzie Pellam done"
    assert all("Jane" not in b and "Ozzie" not in b for b in _Up.seen)


def test_proxy_fail_closed_blocks(proxy):
    px, cfgp, st = proxy
    _Up.seen.clear()
    (st / "known.json").write_text("{not json")                   # a broken known file must not let data out
    os.utime(st / "known.json", None)
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(px, {"model": "m", "messages": [{"role": "user", "content": "say Jane Q Doe"}]})
    assert e.value.code == 503 and _Up.seen == []


def test_system_note_appended_once(sc):
    b = {"messages": [{"role": "system", "content": "You are a helpful assistant."}, {"role": "user", "content": "hi Jane Q Doe"}]}
    sc.scrub_request(b)
    sc.scrub_request(b)
    assert b["messages"][0]["content"].count("privacy stand-ins") == 1


def test_egress_checks_skipped_keys(sc):
    with pytest.raises(s.ScrubError, match="egress"):
        sc.scrub_request({"user": "Jane Q Doe", "messages": [{"role": "user", "content": "x"}]})



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
    assert not {t for t, p, g in s.PATTERNS} & {"EMAIL", "PHONE", "CARD", "IBAN", "SSN", "IP", "PROFILE"}
    assert isinstance(sc.known_m.a, ahocorasick.Automaton)


def test_phone_formatted_only(sc):
    for t in ("job 4412360947", "id 14257860000", "at 2026-11-07 16:44", "host (100.64.58.40)"):
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
    monkeypatch.setattr(scp, "spans", lambda text: [])
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
    o = sc.scrub_text(text)
    assert gone not in o
    assert sc.restore_text(o) == text


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
    o = sc.scrub_text("jane said hi")
    assert "jane" not in o
    assert sc.restore_text(o) == "jane said hi"
    r = sc.stream_restorer()
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
    second = sc.scrub_text(variant)
    assert "7946" not in second and "JDoe" not in second
    assert sc.restore_text(first) == TEXT and sc.restore_text(second) == variant
    for k in ("arguments",):                            # tool-call arguments come back exact too (JSON-escaped path)
        args = json.dumps({"to": FAKE_PHONES[0], "who": "Jane Q Doe"})
        body = {"messages": [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                "function": {"name": "send", k: args}}]}]}
        sc.scrub_request(body)
        sent = body["messages"][0]["tool_calls"][0]["function"][k]
        assert "555" not in sent and "Jane" not in sent
        assert sc.restore_obj(body)["messages"][0]["tool_calls"][0]["function"][k] == args


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
    _Up.seen.clear()
    h = {"Content-Length": str(len(CHAT))}
    if ctype:
        h["Content-Type"] = ctype
    code, out = _raw(px, "/v1/chat/completions", CHAT, h)
    assert code == 200 and json.loads(out)["choices"][0]["message"]["content"] == "ok Jane Q Doe"
    assert len(_Up.seen) == 1 and "Jane" not in _Up.seen[0] and "PERSON_" in _Up.seen[0]


@pytest.mark.parametrize("phone", FAKE_PHONES)
def test_proxy_phone_round_trip_text_plain(proxy, phone):
    px, _, _ = proxy
    _Up.seen.clear()
    b = json.dumps({"model": "m", "messages": [{"role": "user", "content": "call " + phone}]}).encode()
    code, out = _raw(px, "/v1/chat/completions", b, {"Content-Type": "text/plain", "Content-Length": str(len(b))})
    assert code == 200 and json.loads(out)["choices"][0]["message"]["content"] == "ok " + phone   # as written
    assert "555" not in _Up.seen[0]


@pytest.mark.parametrize("path,key", [("/v1/embeddings", "input"), ("/v1/completions", "prompt")])
@pytest.mark.parametrize("ctype", ["application/json", "text/plain", None])
def test_proxy_embeddings_and_completions_scrubbed(proxy, path, key, ctype):
    px, _, _ = proxy
    _Up.seen.clear()
    b = json.dumps({"model": "m", key: "notes on Jane Q Doe, jdoe@corp.example, " + FAKE_PHONES[0]}).encode()
    h = {"Content-Length": str(len(b))}
    if ctype:
        h["Content-Type"] = ctype
    code, out = _raw(px, path, b, h)
    assert code == 200
    assert not any(v in _Up.seen[0] for v in ("Jane", "jdoe@", "555"))
    assert json.loads(out)["echo"] == json.loads(b)[key]          # restored exactly
    b2 = json.dumps({"model": "m", key: [[101, 2023, 102]]}).encode()
    _Up.seen.clear()
    code, _ = _raw(px, path, b2, {"Content-Type": "application/json", "Content-Length": str(len(b2))})
    assert code == 503 and _Up.seen == []


@pytest.mark.parametrize("data,extra", [(b"hello Jane Q Doe", {"Content-Type": "text/plain"}),
                                        (b"hello Jane Q Doe", {}),
                                        (b'{"model": "m", "messages": [', {"Content-Type": "text/plain"}),
                                        (b"\xff\xfe junk", {}),
                                        (None, {"Content-Type": "text/plain", "Content-Encoding": "gzip"})])
def test_proxy_unreadable_body_on_model_endpoint_fails_closed(proxy, data, extra):
    import gzip
    px, _, _ = proxy
    _Up.seen.clear()
    data = gzip.compress(CHAT) if data is None else data
    code, _ = _raw(px, "/v1/chat/completions", data, dict(extra, **{"Content-Length": str(len(data))}))
    assert code == 503 and _Up.seen == []


def test_proxy_non_json_off_model_path_passes(proxy):
    px, _, _ = proxy
    _Up.seen.clear()
    code, _ = _raw(px, "/other/upload", b"plain words", {"Content-Type": "text/plain", "Content-Length": "11"})
    assert code == 200 and _Up.seen == ["plain words"]
    b = json.dumps({"note": "Jane Q Doe"}).encode()              # JSON off the model path is still scrubbed
    code, _ = _raw(px, "/other/upload", b, {"Content-Type": "text/plain", "Content-Length": str(len(b))})
    assert code == 200 and "Jane" not in _Up.seen[-1]
