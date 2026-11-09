"""swapback demo: see the swap and the restore with no API key, no config and no network.

`swapback demo` starts a built-in fake model on a free loopback port (it echoes what it receives and, when the request
offers tools, asks for two tool calls), puts the proxy in front of it with a few made-up known values, sends three
requests and prints what your app sent, what the model received and what your app got back. Everything is fake and
lives in a temporary directory that is removed at the end. Exit code 0 when the model saw only placeholders and
every reply came back restored, 1 otherwise."""
import json, os, re, shutil, sys, tempfile, threading, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KNOWN = """PERSON:
  - Jane Q Doe
EMAIL:
  - jane.doe@example.com
PHONE:
  - "(425) 555-0182"
"""
MSG = "Email Jane Q Doe at jane.doe@example.com or call (425) 555-0182 about Friday."
FAKE = ("Jane Q Doe", "jane.doe@example.com", "555-0182")
PH = re.compile("\u00ab[A-Z_]+_\\d+\u00bb")


class MockModel(BaseHTTPRequestHandler):
    """a fake OpenAI-compatible model: echoes the last user message, plain or streamed; with tools, it calls
    web_fetch (a URL with the email in it) and send_email (to the email)."""
    protocol_version = "HTTP/1.1"
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        b = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        j = json.loads(b or b"{}")
        last = next((m.get("content") for m in reversed(j.get("messages") or []) if m.get("role") == "user"), "")
        MockModel.seen.append(last)
        if j.get("tools"):
            email = next(iter(re.findall("\u00abEMAIL_\\d+\u00bb", last)), "nobody@example.com")
            calls = [{"id": "c1", "type": "function", "function": {"name": "web_fetch", "arguments": json.dumps(
                         {"url": "https://lookup.example/?q=" + email}, ensure_ascii=False)}},
                     {"id": "c2", "type": "function", "function": {"name": "send_email", "arguments": json.dumps(
                         {"to": email, "body": "Hi, about Friday"}, ensure_ascii=False)}}]
            return self._json({"id": "m", "object": "chat.completion", "model": j.get("model"), "choices": [
                {"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": calls},
                 "finish_reason": "tool_calls"}]})
        reply = "You said: %s" % last
        if not j.get("stream"):
            return self._json({"id": "m", "object": "chat.completion", "model": j.get("model"), "choices": [
                {"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}]})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for i in range(0, len(reply), 7):                 # small chunks, so placeholders arrive split
            d = {"id": "m", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": reply[i:i + 7]}}]}
            self._chunk(("data: " + json.dumps(d, ensure_ascii=False) + "\n\n").encode())
        self._chunk(b"data: [DONE]\n\n")
        self._chunk(b"")

    def _json(self, o):
        b = json.dumps(o, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _chunk(self, b):
        self.wfile.write(b"%x\r\n%s\r\n" % (len(b), b))
        self.wfile.flush()


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer demo"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8")


def _stream_text(raw):
    out = []
    for ln in raw.splitlines():
        if ln.startswith("data: ") and ln[6:].strip() != "[DONE]":
            for ch in json.loads(ln[6:]).get("choices") or []:
                out.append((ch.get("delta") or {}).get("content") or "")
    return "".join(out)


def run(out=None):
    out = out or sys.stdout
    say = lambda s="": print(s, file=out, flush=True)
    tmp = tempfile.mkdtemp(prefix="swapback-demo-")
    old_env = os.environ.get("SWAPBACK_CONFIG")
    mock = srv = None
    ok = True
    try:
        MockModel.seen = []
        mock = ThreadingHTTPServer(("127.0.0.1", 0), MockModel)
        mock.daemon_threads = True
        threading.Thread(target=mock.serve_forever, daemon=True).start()
        with open(os.path.join(tmp, "known.yaml"), "w", encoding="utf-8") as f:
            f.write(KNOWN)
        cfgp = os.path.join(tmp, "swapback.yaml")
        with open(cfgp, "w", encoding="utf-8") as f:
            f.write("upstream: http://127.0.0.1:%d\nlisten: 127.0.0.1:0\nknown_file: known.yaml\nstate_dir: state\n"
                    "log_requests: false\nsystem_note: ''\ntool_restore:\n  allow: [send_email]\n" % mock.server_address[1])
        os.environ["SWAPBACK_CONFIG"] = cfgp
        from . import core, proxy
        srv = proxy.make_server(core.load_cfg(cfgp))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d/v1/chat/completions" % srv.server_address[1]
        say("swapback demo: a fake model behind the proxy, made-up data, nothing leaves this machine.")
        say()
        say("1) your app sends")
        say("   " + MSG)
        r = json.loads(_post(base, {"model": "demo", "messages": [{"role": "user", "content": MSG}]}))
        got = r["choices"][0]["message"]["content"]
        saw = MockModel.seen[-1]
        say("2) the model receives")
        say("   " + saw)
        say("3) your app gets back")
        say("   " + got)
        ok &= not any(v in saw for v in FAKE) and got == "You said: " + MSG
        say()
        say("4) the same, streamed in 7-character chunks (placeholders arrive split and are put back whole)")
        st = _stream_text(_post(base, {"model": "demo", "stream": True, "messages": [{"role": "user", "content": MSG}]}))
        say("   " + st)
        ok &= st == "You said: " + MSG and not any(v in MockModel.seen[-1] for v in FAKE)
        say()
        say("5) tool calls: the model asks to fetch a URL and to send an email")
        tools = [{"type": "function", "function": {"name": n, "parameters": {"type": "object"}}}
                 for n in ("web_fetch", "send_email")]
        r = json.loads(_post(base, {"model": "demo", "tools": tools, "messages": [{"role": "user", "content": MSG}]}))
        for c in r["choices"][0]["message"]["tool_calls"]:
            fn = c["function"]
            say("   %-10s %s" % (fn["name"], fn["arguments"]))
        args = {c["function"]["name"]: c["function"]["arguments"] for c in r["choices"][0]["message"]["tool_calls"]}
        ok &= "jane.doe@example.com" not in args.get("web_fetch", "") and bool(PH.search(args.get("web_fetch", "")))
        say("   web_fetch keeps the placeholder: a URL could carry the address off your machine.")
        if "jane.doe@example.com" in args.get("send_email", ""):
            say("   send_email is on this demo's allow list, so it gets the real address and the email can be sent.")
        else:
            say("   send_email keeps the placeholder too: send-style tools keep it unless you allow them")
            say("   (tool_restore: allow: [send_email] in swapback.yaml).")
        say()
        if ok:
            say("PASS: the model never saw the made-up name, email or phone, and every reply came back exact.")
            say("Next: `swapback init` writes a starter config; see the README's Set it up for a real provider.")
        else:
            say("FAIL: something above did not swap or restore as expected. Please open an issue with this output.")
        return 0 if ok else 1
    except Exception as e:                                # a demo that cannot run says why, never a traceback wall
        say("demo could not run: %s: %s" % (type(e).__name__, e))
        return 1
    finally:
        for s in (srv, mock):
            if s is not None:
                s.shutdown()
                s.server_close()
        if old_env is None:
            os.environ.pop("SWAPBACK_CONFIG", None)
        else:
            os.environ["SWAPBACK_CONFIG"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
