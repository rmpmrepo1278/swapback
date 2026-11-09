"""tests for swapback.hooks: the proxy's optional extension points. Made-up values only."""
import http.server, json, os, socket, subprocess, sys, threading, time, urllib.error, urllib.request
import pytest

from swapback import core as s
from swapback import hooks as hk

KNOWN = [("Jane Q Doe", "PERSON"), ("jdoe@corp.example", "EMAIL")]

HOOKS_PY = r"""
import json, os
OPT = {}

def _w(kind, obj):
    with open(OPT["out"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": kind, "obj": obj}) + "\n")

def setup(options, cfg):
    OPT.update(options)
    _w("setup", {"mode": cfg["mode"]})

def before_forward(req, ctx):
    _w("before", {"body": req.body, "headers": sorted(k.lower() for k in req.headers), "api": req.api})
    what = req.headers.pop("X-Hook", None) or req.headers.pop("x-hook", None)
    ctx["log"]["what"] = what or "none"
    if what == "boom":
        raise RuntimeError("hook exploded")
    if what == "respond":
        user = req.body["messages"][-1]["content"]
        return {"respond": {"status": 200, "json": {"choices": [{"message": {"role": "assistant",
                                                                          "content": "local: " + user}}]}}}
    if what == "reroute":
        return {"url": OPT["alt"] + "/v1/chat/completions", "body": dict(req.body, model="alt-model")}
    return None

def on_upstream_response(resp, ctx):
    _w("upstream", {"status": resp.status, "error": resp.error, "body": resp.body.decode("utf-8", "replace")})
    if resp.status is None or resp.status == 429:
        body = dict(ctx["request"].body, model="fallback-model")
        return {"url": OPT["alt"] + "/v1/chat/completions", "body": body}
    return None

def on_usage(obj, ctx):
    if isinstance(obj, dict) and obj.get("usage"):
        _w("usage", obj)

def on_complete(result, ctx):
    _w("complete", {"status": result["status"], "cost": result["headers"].get("x-cost"),
                    "rerouted": result["rerouted"]})

def extra_routes():
    return {"/hooktest/health": lambda: {"ok": True, "hook": "yes"},
            "/hooktest/teapot": lambda: (418, {"ok": False})}

def alert(text):
    _w("alert", {"text": text})
"""


class _Up(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = None

    def log_message(self, *a):
        pass

    def _json(self, code, obj, extra=None):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        j = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.seen.append({"body": j, "headers": {k.lower(): v for k, v in self.headers.items()}})
        if j.get("model") == "capped":
            return self._json(429, {"error": {"message": "usage limit reached"}})
        text = "said " + j["messages"][-1]["content"]
        usage = {"prompt_tokens": 7, "completion_tokens": 3}
        if j.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            evs = [{"id": "x", "model": j["model"], "choices": [{"index": 0, "delta": {"content": text[:7]}}]},
                   {"id": "x", "model": j["model"], "choices": [{"index": 0, "delta": {"content": text[7:]}}]},
                   {"id": "x", "model": j["model"], "choices": [], "usage": usage}]
            for d in ["data: " + json.dumps(e) + "\n\n" for e in evs] + ["data: [DONE]\n\n", ""]:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(d.encode()), d.encode()))
            return
        self._json(200, {"id": "x", "model": j["model"], "usage": usage,
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}]},
                   {"x-cost": "0.0012"})


def _upstream():
    seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), type("_UpH", (_Up,), {"seen": seen}))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def _free_port():
    so = socket.socket()
    so.bind(("127.0.0.1", 0))
    p = so.getsockname()[1]
    so.close()
    return p


def _start(tmp_path, upstream, alt, hooks_src=HOOKS_PY, extra_cfg=""):
    (tmp_path / "my_hooks.py").write_text(hooks_src)
    (tmp_path / "known.yaml").write_text("PERSON:\n  - Jane Q Doe\nEMAIL:\n  - jdoe@corp.example\n")
    out = tmp_path / "hooks.out.jsonl"
    cfgp = tmp_path / "swapback.yaml"
    cfgp.write_text('mode: "on"\nlisten: 127.0.0.1:0\nupstream: %s\nstate_dir: %s\nknown_file: known.yaml\n'
                    "hooks:\n  module: my_hooks.py\n  options: {out: %s, alt: '%s'}\n%s"
                    % (upstream, tmp_path / "st", out, alt, extra_cfg))
    errp = tmp_path / "proxy.stderr"
    with open(errp, "wb") as ef:
        p = subprocess.Popen([sys.executable, "-B", "-m", "swapback.proxy"], env=dict(os.environ, SWAPBACK_CONFIG=str(cfgp)),
                             stdout=subprocess.PIPE, stderr=ef)
    deadline = time.time() + 60
    port = None
    while time.time() < deadline and p.poll() is None:
        line = p.stdout.readline()
        if not line:
            break
        try:
            info = json.loads(line)
        except ValueError:
            continue
        if "listening" in info:
            port = int(info["listening"].rsplit(":", 1)[1])
            assert info["hooks"]
            break
    if port is None:
        p.kill()
        return p, None, out, errp
    threading.Thread(target=p.stdout.read, daemon=True).start()
    return p, port, out, errp


@pytest.fixture
def hp(tmp_path):
    up, seen = _upstream()
    alt, alt_seen = _upstream()
    p, port, out, errp = _start(tmp_path, "http://127.0.0.1:%d" % up.server_address[1],
                                "http://127.0.0.1:%d" % alt.server_address[1])
    if port is None:
        pytest.fail("proxy did not start: " + errp.read_text(errors="replace")[-800:])
    try:
        yield {"port": port, "seen": seen, "alt_seen": alt_seen, "out": out, "tmp": tmp_path}
    finally:
        p.kill()
        p.wait(timeout=10)
        for srv in (up, alt):
            srv.shutdown()
            srv.server_close()


def _rec(out, kind):
    if not out.exists():
        return []
    return [r["obj"] for r in map(json.loads, out.read_text().splitlines()) if r["kind"] == kind]


def _post(port, body, headers=None):
    req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % port, data=json.dumps(body).encode(),
                                 method="POST", headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _msg(text, model="m", stream=False):
    return {"model": model, "stream": stream, "messages": [{"role": "user", "content": text}]}


def _stream_text(out):
    return "".join(c["delta"].get("content", "") for l in out.splitlines() if l.startswith("data: {")
                   for c in json.loads(l[6:]).get("choices") or [])


def test_hooks_see_placeholders_only_and_reply_is_restored(hp):
    code, out = _post(hp["port"], _msg("mail jdoe@corp.example for Jane Q Doe"))
    assert code == 200 and json.loads(out)["choices"][0]["message"]["content"] == "said mail jdoe@corp.example for Jane Q Doe"
    raw = hp["out"].read_text()
    assert "jdoe@" not in raw and "Jane" not in raw                  # no hook ever saw a real value
    b = _rec(hp["out"], "before")[-1]
    assert "\u00abEMAIL_1\u00bb" in b["body"]["messages"][-1]["content"] and b["api"] == "chat"
    assert _rec(hp["out"], "setup") == [{"mode": "on"}]


def test_hook_can_drop_a_header(hp):
    _post(hp["port"], _msg("hi"), {"X-Hook": "none", "X-Keep": "1"})
    h = hp["seen"][-1]["headers"]
    assert "x-hook" not in h and h.get("x-keep") == "1"


def test_usage_and_complete_non_stream_and_stream(hp):
    _post(hp["port"], _msg("hello Jane Q Doe"))
    code, out = _post(hp["port"], _msg("hello Jane Q Doe", stream=True))
    assert code == 200 and _stream_text(out) == "said hello Jane Q Doe"
    u = _rec(hp["out"], "usage")
    assert len(u) == 2 and all(x["usage"] == {"prompt_tokens": 7, "completion_tokens": 3} for x in u)
    c = _rec(hp["out"], "complete")
    assert c[0] == {"status": 200, "cost": "0.0012", "rerouted": False} and c[1]["status"] == 200
    assert "Jane" not in hp["out"].read_text()


def test_before_forward_answers_locally_and_is_restored(hp):
    code, out = _post(hp["port"], _msg("ping Jane Q Doe"), {"X-Hook": "respond"})
    assert code == 200 and json.loads(out)["choices"][0]["message"]["content"] == "local: ping Jane Q Doe"
    assert hp["seen"] == [] and hp["alt_seen"] == []


def test_before_forward_reroutes(hp):
    code, out = _post(hp["port"], _msg("to Jane Q Doe", stream=True), {"X-Hook": "reroute"})
    assert code == 200 and _stream_text(out) == "said to Jane Q Doe"
    assert hp["seen"] == [] and hp["alt_seen"][-1]["body"]["model"] == "alt-model"
    assert "Jane" not in json.dumps(hp["alt_seen"])


def test_upstream_error_retried_on_alternate(hp):
    code, out = _post(hp["port"], _msg("capped Jane Q Doe", model="capped"))
    assert code == 200 and json.loads(out)["choices"][0]["message"]["content"] == "said capped Jane Q Doe"
    assert hp["alt_seen"][-1]["body"]["model"] == "fallback-model"
    up = _rec(hp["out"], "upstream")[-1]
    assert up["status"] == 429 and "usage limit" in up["body"]
    assert _rec(hp["out"], "complete")[-1]["rerouted"] is True
    log = [json.loads(l) for l in (hp["tmp"] / "st" / "requests.jsonl").read_text().splitlines()]
    assert log[-1]["rerouted"] is True and log[-1]["hook"] == {"what": "none"}


def test_unreachable_upstream_goes_to_alternate(tmp_path):
    alt, alt_seen = _upstream()
    p, port, out, errp = _start(tmp_path, "http://127.0.0.1:%d" % _free_port(),
                                "http://127.0.0.1:%d" % alt.server_address[1])
    try:
        assert port, errp.read_text()
        code, body = _post(port, _msg("down Jane Q Doe", stream=True))
        assert code == 200 and _stream_text(body) == "said down Jane Q Doe"
        up = _rec(out, "upstream")[-1]
        assert up["status"] is None and up["error"]
    finally:
        p.kill()
        p.wait(timeout=10)
        alt.shutdown()
        alt.server_close()


def test_hook_exception_fails_closed(hp):
    code, out = _post(hp["port"], _msg("Jane Q Doe"), {"X-Hook": "boom"})
    assert code == 503 and "fail-closed" in out and "hook" in out
    assert hp["seen"] == [] and hp["alt_seen"] == []
    for _ in range(100):                                       # alerts go out on a thread
        if _rec(hp["out"], "alert"):
            break
        time.sleep(0.05)
    a = _rec(hp["out"], "alert")
    assert a and "before_forward" in a[-1]["text"] and "Jane" not in a[-1]["text"]


def test_extra_routes_and_health(hp):
    get = lambda p: urllib.request.urlopen("http://127.0.0.1:%d%s" % (hp["port"], p), timeout=5)
    assert json.loads(get("/hooktest/health").read()) == {"ok": True, "hook": "yes"}
    with pytest.raises(urllib.error.HTTPError) as e:
        get("/hooktest/teapot")
    assert e.value.code == 418
    h = json.loads(get("/scrubber/health").read())
    assert h["hooks"] and h["hook_errors"] == 0


def test_soft_hook_errors_are_counted_not_raised(tmp_path):
    up, seen = _upstream()
    src = "def on_usage(obj, ctx):\n    raise ValueError('x')\n"
    p, port, out, errp = _start(tmp_path, "http://127.0.0.1:%d" % up.server_address[1], "http://x", hooks_src=src)
    try:
        code, body = _post(port, _msg("Jane Q Doe"))
        assert code == 200 and "Jane Q Doe" in body
        h = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/scrubber/health" % port, timeout=5).read())
        assert h["hook_errors"] == 1
    finally:
        p.kill()
        p.wait(timeout=10)
        up.shutdown()
        up.server_close()


def test_missing_hooks_module_stops_start(tmp_path):
    cfgp = tmp_path / "swapback.yaml"
    cfgp.write_text("hooks: {module: nope_not_here.py}\nstate_dir: %s\n" % (tmp_path / "st"))
    cfg = s.load_cfg(str(cfgp))
    with pytest.raises(hk.HookError):
        hk.Hooks(cfg)


@pytest.mark.parametrize("bad", ["hooks: {options: {a: 1}}\n", "hooks: {module: x, options: [1]}\n"])
def test_bad_hooks_section_fails_at_load(tmp_path, bad):
    cfgp = tmp_path / "swapback.yaml"
    cfgp.write_text(bad)
    with pytest.raises(ValueError):
        s.load_cfg(str(cfgp))


def test_check_action_rules():
    assert hk.check_action(None, "before_forward") is None
    assert hk.check_action({"url": "http://127.0.0.1:1/v1/x"}, "before_forward")["url"]
    r = hk.check_action({"respond": {"status": 429, "json": {"e": 1}}}, "before_forward")
    assert r["respond"]["status"] == 429
    for bad, where in (({"respond": {"status": 200}}, "on_upstream_response"), ({"url": "file:///etc/x"}, "before_forward"),
                       ({"nope": 1}, "before_forward"), ("x", "before_forward")):
        with pytest.raises(hk.HookError):
            hk.check_action(bad, where)


def test_hooks_object_without_module_is_inert():
    h = hk.Hooks()
    assert not h and h.call("before_forward", 1, 2) is None and h.soft("on_usage", 1, 2) is None and h.route("/x") is None


def test_check_config_lists_hooks(tmp_path):
    (tmp_path / "my_hooks.py").write_text(HOOKS_PY)
    cfgp = tmp_path / "swapback.yaml"
    cfgp.write_text("hooks: {module: my_hooks.py}\nstate_dir: %s\n" % (tmp_path / "st"))
    r = subprocess.run([sys.executable, "-m", "swapback.core", "check-config"], env=dict(os.environ, SWAPBACK_CONFIG=str(cfgp)),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    h = json.loads(r.stdout)["hooks"]
    assert set(h["functions"]) >= {"before_forward", "on_upstream_response", "on_usage", "extra_routes", "setup"}

