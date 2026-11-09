"""0.4.1: listed non-US phones, the known-file guard, readable blocks, `swapback demo`, cheap name/ID variants."""
import io, json, os, threading, time, urllib.error, urllib.request

import pytest

from swapback import core as c


def _scr(tmp_path, known=None, **cfg):
    conf = dict(c.DEFAULTS, state_dir=str(tmp_path / "state"), **cfg)
    return c.Scrubber(conf, known=known)


def _round(s, text):
    f = c.Forms()
    out = s.scrub_text(text, forms=f)
    return out, s.restore_text(out, forms=f)


# ---------------------------------------------------------------- 1. listed phones outside the US
@pytest.mark.parametrize("listed,written", [
    ("98765 43210", "98765 43210"), ("98765 43210", "9876543210"), ("98765 43210", "+91 98765 43210"),
    ("98765 43210", "+91-98765-43210"), ("98765 43210", "098765 43210"), ("+91 98765 43210", "98765 43210"),
    ("0412 345 678", "0412 345 678"), ("0412 345 678", "0412345678"), ("0412 345 678", "+61 412 345 678"),
    ("0412 345 678", "(04) 1234 5678"), ("020 7946 0958", "+44 20 7946 0958"),
])
def test_listed_non_us_phone_is_swapped_in_any_grouping(tmp_path, listed, written):
    s = _scr(tmp_path, known=[(listed, "PHONE")])
    text = "call me on %s tonight" % written
    out, back = _round(s, text)
    assert "PHONE_" in out and not any(ch.isdigit() for ch in out.replace("PHONE_1", "").replace("PHONE_2", ""))
    assert back == text
    assert s.leftover_known(out) == 0


def test_listed_non_us_phone_from_known_file_with_default_us_region(tmp_path):
    kf = tmp_path / "known.yaml"
    kf.write_text('PHONE:\n  - "98765 43210"\n  - "0412 345 678"\n')
    s = c.Scrubber(dict(c.DEFAULTS, state_dir=str(tmp_path / "st"), known_file=str(kf)))
    assert s.cfg["phone_regions"] == ["US"]
    body = {"messages": [{"role": "user", "content": "IN 98765 43210, AU 0412 345 678"},
                         {"role": "tool", "tool_call_id": "t", "content": "found 9876543210 and 0412-345-678"}]}
    s.scrub_request(body)
    txt = json.dumps(body)
    assert "98765" not in txt and "43210" not in txt and "0412" not in txt and "345" not in txt


def test_egress_check_catches_a_listed_non_us_phone(tmp_path):
    s = _scr(tmp_path, known=[("98765 43210", "PHONE")])
    assert s.leftover_known("ring 98765-43210") == 1
    with pytest.raises(c.ScrubError):                    # a field that is never swapped (model) is still checked
        s.scrub_request({"model": "x 98765 43210", "messages": [{"role": "user", "content": "hi"}]})


def test_us_formats_only_for_us_numbers():
    assert "987-654-3210" not in c._phone_variants("98765 43210")
    assert "(041) 234-5678" not in c._phone_variants("0412 345 678")
    assert c._phone_variants("98765 43210")[0] == "98765 43210"          # the form as written comes first
    us = c._phone_variants("425-555-0182")
    assert "(425) 555-0182" in us and "+14255550182" in us and us[0] == "425-555-0182"
    assert c._is_us_phone("+1 425 555 0182") and not c._is_us_phone("+91 9876543210")


@pytest.mark.parametrize("text", ["build 20261109 98765", "order 2026-11-09 1234", "v1.2.3 4321", "x98765 4321"])
def test_phone_digit_matcher_leaves_other_numbers_alone(tmp_path, text):
    s = _scr(tmp_path, known=[("98765 43210", "PHONE")])
    assert _round(s, text)[0] == text


def test_date_before_a_listed_phone_is_kept(tmp_path):
    s = _scr(tmp_path, known=[("98765 43210", "PHONE")])
    out, back = _round(s, "on 2026-11-09 98765 43210")
    assert out.startswith("on 2026-11-09 \u00abPHONE_") and back == "on 2026-11-09 98765 43210"


# ---------------------------------------------------------------- 5. cheap variants
def test_surname_first_accent_free_and_compact_id_share_the_placeholder(tmp_path):
    s = _scr(tmp_path, known=[("Marisol Quintero", "PERSON"), ("Ana\u00efs Lef\u00e8vre", "PERSON"),
                              ("QQ 12 34 56 C", "ID")])
    a, _ = _round(s, "Marisol Quintero")
    for text in ("Quintero, Marisol said hi", "QUINTERO MARISOL"):
        out, back = _round(s, text)
        assert a in out and back == text
    b, _ = _round(s, "Ana\u00efs Lef\u00e8vre")
    out, back = _round(s, "Anais Lefevre and Lefevre, Anais")
    assert out == "%s and %s" % (b, b) and back == "Anais Lefevre and Lefevre, Anais"
    i, _ = _round(s, "QQ 12 34 56 C")
    out, back = _round(s, "NI QQ123456C")
    assert out == "NI " + i and back == "NI QQ123456C"


def test_common_phrase_name_warns_with_line_number(tmp_path):
    kf = tmp_path / "known.yaml"
    kf.write_text("PERSON:\n  - Priya Raman\n  - Will Hunt\n")
    w = c.known_warnings(str(kf))
    assert len(w) == 1 and "line 3" in w[0] and "Will" not in w[0]


# ---------------------------------------------------------------- 3. the known-file guard
def test_empty_known_file_blocks_unless_allowed(tmp_path):
    kf = tmp_path / "known.yaml"
    kf.write_text("PERSON: []\n")
    s = c.Scrubber(dict(c.DEFAULTS, state_dir=str(tmp_path / "a"), known_file=str(kf)))
    with pytest.raises(c.ScrubError, match="no values"):
        s.scrub_request({"messages": [{"role": "user", "content": "hi"}]})
    assert s.warnings() and "no values" in s.warnings()[0]
    s2 = c.Scrubber(dict(c.DEFAULTS, state_dir=str(tmp_path / "b"), known_file=str(kf), allow_empty_known=True))
    s2.scrub_request({"messages": [{"role": "user", "content": "hi"}]})
    kf.write_text("PERSON:\n  - Priya Raman\n")         # adding values lifts the block
    body = {"messages": [{"role": "user", "content": "hi Priya Raman"}]}
    s.scrub_request(body)
    assert "Priya" not in json.dumps(body)


def _replace_keep_mtime(path, text):
    st = os.stat(path)
    tmp = str(path) + ".new"
    with open(tmp, "w") as f:
        f.write(text)
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(tmp, path)


def test_shrunk_or_emptied_known_file_keeps_protecting_and_warns(tmp_path):
    kf = tmp_path / "known.yaml"
    names = ["Priya Raman", "Tomasz Wilk", "Ines Duarte", "Kofi Mensah", "Lena Vogel", "Rafael Ortiz"]
    kf.write_text("PERSON:\n" + "".join("  - %s\n" % n for n in names))
    alerts = []
    s = c.Scrubber(dict(c.DEFAULTS, state_dir=str(tmp_path / "st"), known_file=str(kf)))
    s.on_alert = alerts.append
    s.scrub_request({"messages": [{"role": "user", "content": "x"}]})
    for text in ("", "PERSON:\n  - Priya Raman\n"):
        _replace_keep_mtime(kf, text)                     # a save by rename that keeps the old mtime
        body = {"messages": [{"role": "user", "content": "Tomasz Wilk and Rafael Ortiz"}]}
        s.scrub_request(body)
        assert "Tomasz" not in json.dumps(body) and "Rafael" not in json.dumps(body)
        assert any("still protecting" in w for w in s.warnings())
    assert alerts and "Tomasz" not in " ".join(alerts)


def test_reload_on_content_change_with_the_same_mtime(tmp_path):
    kf = tmp_path / "known.yaml"
    kf.write_text("PERSON:\n  - Priya Raman\n")
    s = c.Scrubber(dict(c.DEFAULTS, state_dir=str(tmp_path / "st"), known_file=str(kf)))
    _replace_keep_mtime(kf, "PERSON:\n  - Priya Raman\n  - Kofi Mensah\n")
    body = {"messages": [{"role": "user", "content": "Kofi Mensah"}]}
    s.scrub_request(body)
    assert "Kofi" not in json.dumps(body)


def test_shrink_since_last_start_warns(tmp_path):
    kf = tmp_path / "known.yaml"
    kf.write_text("PERSON:\n" + "".join("  - Person%s Name\n" % x for x in "ABCDEFGH"))
    st = str(tmp_path / "st")
    c.Scrubber(dict(c.DEFAULTS, state_dir=st, known_file=str(kf)))
    kf.write_text("PERSON:\n  - PersonA Name\n")
    s = c.Scrubber(dict(c.DEFAULTS, state_dir=st, known_file=str(kf)))
    assert any("down from 8" in w for w in s.warnings())


def test_check_config_warns_about_example_values(tmp_path, monkeypatch, capsys):
    c.init_files(str(tmp_path))
    capsys.readouterr()
    monkeypatch.setenv("SWAPBACK_CONFIG", str(tmp_path / "swapback.yaml"))
    monkeypatch.setenv("SWAPBACK_STATE_DIR", str(tmp_path / "st"))
    c.main(["swapback", "check-config"])
    out = json.loads(capsys.readouterr().out)
    assert any("example value" in w for w in out.get("warnings", []))


# ---------------------------------------------------------------- 4. readable blocks
@pytest.fixture
def blocking_proxy(tmp_path, monkeypatch):
    from swapback import proxy
    kf = tmp_path / "known.yaml"
    kf.write_text("PERSON: []\n")
    cfgp = tmp_path / "swapback.yaml"
    cfgp.write_text("upstream: http://127.0.0.1:9\nlisten: 127.0.0.1:0\nknown_file: known.yaml\nstate_dir: st\n")
    monkeypatch.setenv("SWAPBACK_CONFIG", str(cfgp))
    srv = proxy.make_server(c.load_cfg(str(cfgp)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _post(port, path, body):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read())


def test_block_is_readable_in_openai_and_anthropic_shapes(blocking_proxy, capfd):
    code, h, j = _post(blocking_proxy, "/v1/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert code == 503 and h.get("X-Swapback-Blocked") == "scrubber_error"
    assert j["error"]["message"].startswith("swapback blocked this request") and "nothing was sent" in j["error"]["message"]
    code, h, j = _post(blocking_proxy, "/v1/messages", {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})
    assert code == 503 and j["type"] == "error" and j["error"]["type"] == "api_error"
    assert "swapback blocked" in j["error"]["message"]
    err = capfd.readouterr().err
    assert "swapback: blocked POST /v1/chat/completions: known_file has no values" in err
    with urllib.request.urlopen("http://127.0.0.1:%d/scrubber/health" % blocking_proxy, timeout=10) as r:
        hl = json.loads(r.read())
    assert hl["ok"] is False and hl["warnings"]


# ---------------------------------------------------------------- 2. the pip-only demo
def test_demo_runs_from_the_package(monkeypatch, tmp_path):
    from swapback import demo
    monkeypatch.chdir(tmp_path)
    buf = io.StringIO()
    assert demo.run(buf) == 0
    out = buf.getvalue()
    assert "PASS" in out and "\u00abPERSON_1\u00bb" in out and "You said: Email Jane Q Doe" in out
    assert not os.listdir(tmp_path)
