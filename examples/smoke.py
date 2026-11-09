"""End-to-end smoke: start the mock upstream and the proxy, send a made-up name and phone number (plain and streamed),
and show that the upstream saw placeholders while the client got the real values back.
usage: python examples/smoke.py   (from the repo root, with the package installed)"""
import json, os, socket, subprocess, sys, tempfile, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
NAME, PHONE = "Jane Q Doe", "(425) 555-0182"


def port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait(url):
    for _ in range(200):
        try:
            return urllib.request.urlopen(url, timeout=1)
        except Exception:
            time.sleep(0.05)
    raise SystemExit("not up: " + url)


def post(px, body):
    rq = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % px, data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(rq, timeout=10) as r:
        return r.status, r.read().decode()


def main():
    d = tempfile.mkdtemp(prefix="pii-smoke-")
    up, px = port(), port()
    with open(os.path.join(d, "known.yaml"), "w") as f:
        f.write("PERSON: [%s]\n" % NAME)
    with open(os.path.join(d, "swapback.yaml"), "w") as f:
        f.write("mode: \"on\"\nlisten: 127.0.0.1:%d\nupstream: http://127.0.0.1:%d\nknown_file: known.yaml\n"
                "state_dir: state\n" % (px, up))
    seen = os.path.join(d, "seen.jsonl")
    m = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_upstream.py"), str(up)], env=dict(os.environ, MOCK_SEEN=seen))
    p = subprocess.Popen([sys.executable, "-m", "swapback.proxy"],
                         env=dict(os.environ, SWAPBACK_CONFIG=os.path.join(d, "swapback.yaml")))
    try:
        wait("http://127.0.0.1:%d/scrubber/health" % px)
        msg = "Please call %s at %s tomorrow." % (NAME, PHONE)
        code, out = post(px, {"model": "mock", "messages": [{"role": "user", "content": msg}]})
        plain = json.loads(out)["choices"][0]["message"]["content"]
        code2, out2 = post(px, {"model": "mock", "stream": True, "messages": [{"role": "user", "content": msg}]})
        stream = "".join(json.loads(l[6:])["choices"][0]["delta"].get("content") or ""
                         for l in out2.splitlines() if l.startswith("data: {"))
        got = [json.loads(x)["messages"][-1]["content"] for x in open(seen, encoding="utf-8").read().splitlines()]
        res = {"status": [code, code2], "upstream_saw": got[0],
               "upstream_saw_name_or_phone": any(NAME in g or PHONE in g for g in got),
               "client_got_plain": plain, "client_got_stream": stream,
               "restored": plain == stream == "You said: " + msg}
        print(json.dumps(res, indent=1, ensure_ascii=False))
        ok = res["restored"] and not res["upstream_saw_name_or_phone"]
        print("SMOKE", "PASS" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    finally:
        p.kill()
        m.kill()


if __name__ == "__main__":
    main()
