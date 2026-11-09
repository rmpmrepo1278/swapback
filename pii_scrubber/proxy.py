"""pii_scrubber.proxy: a small HTTP proxy in front of any OpenAI-compatible endpoint. JSON request bodies are scrubbed
(pii_scrubber.core) before they leave and responses (plain JSON or SSE streams) are restored on the way back. Paths,
headers and models pass through unchanged. Fail closed: a scrub error answers 503 and nothing is forwarded. Logs
counts and types only. OpenAI chat-completion chunks and Anthropic /v1/messages events are both restored in streams.
GET /scrubber/health -> {"ok": ...}. Request header X-Scrubber-Raw: 1 returns the provider's
answer without the swap-back (used by `pii-scrubber canary` to prove what the provider saw)."""
import copy, http.client, json, os, socket, sys, threading, time, urllib.parse
_NOJSON = object()
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import core as S

CFG = None
SCR = None
UP = None
LOG = None
HOP = {"host", "content-length", "accept-encoding", "connection", "keep-alive", "transfer-encoding", "proxy-connection",
       "te", "upgrade", "x-scrubber-raw"}
RHOP = {"content-length", "transfer-encoding", "connection", "keep-alive", "content-encoding"}
STATE = {"started": time.time(), "requests": 0, "blocked": 0, "errors": 0, "last_error": "", "cfg_mtime": 0}
_lock = threading.Lock()


def _cfg_refresh():
    global CFG
    p = S.config_path()
    try:
        mt = os.stat(p).st_mtime if p else 0
    except OSError:
        return
    if mt != STATE["cfg_mtime"]:
        try:
            new = S.load_cfg()
        except Exception:                                # a broken edit keeps the last good config
            return
        new["upstream"], new["listen"] = CFG["upstream"], CFG["listen"]   # these need a restart
        CFG = new
        SCR.cfg = CFG
        STATE["cfg_mtime"] = mt


def _log(rec):
    if not CFG.get("log_requests", True):
        return
    try:
        with _lock:
            if os.path.exists(LOG) and os.path.getsize(LOG) > 5_000_000:
                os.replace(LOG, LOG + ".1")
            fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.write(fd, (json.dumps(rec) + "\n").encode())
            os.close(fd)
    except Exception:
        pass


def _alert_async(text):
    threading.Thread(target=S.alert, args=(CFG, text), daemon=True).start()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    server_version = "scrub-proxy/1"

    def log_message(self, *a):
        pass

    def _send_json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/scrubber/health"):
            _cfg_refresh()
            return self._send_json(200, {"ok": True, "mode": CFG["mode"], "known": len(SCR.known), "vault": len(SCR.rev),
                                         "requests": STATE["requests"], "blocked": STATE["blocked"],
                                         "errors": STATE["errors"], "last_error": STATE["last_error"],
                                         "uptime_s": int(time.time() - STATE["started"])})
        self._proxy(None)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._proxy(self.rfile.read(n) if n else b"")

    do_PUT = do_PATCH = do_POST

    def do_DELETE(self):
        self._proxy(None)

    def _proxy(self, body):
        _cfg_refresh()
        t0 = time.perf_counter()
        mode = CFG["mode"]
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "path": self.path.split("?")[0], "mode": mode}
        STATE["requests"] += 1
        raw = self.headers.get("X-Scrubber-Raw") == "1"
        out = body
        swapped = False
        # fix2 (Nov 8): scrub ANY body that parses as JSON, whatever its Content-Type (text/plain, none, ...). On a model
        # endpoint (/v1/*), a body that is not readable JSON (encoded, chunked, not JSON) is blocked, never forwarded raw.
        ctype = (self.headers.get("Content-Type") or "").lower()
        cenc = (self.headers.get("Content-Encoding") or "identity").strip().lower()
        chunked = "chunked" in (self.headers.get("Transfer-Encoding") or "").lower() and not self.headers.get("Content-Length")
        model_ep = rec["path"].startswith(("/v1/", "/api/v1/"))
        strict = model_ep or "json" in ctype
        j = _NOJSON
        if mode != "off" and body and cenc not in ("", "identity") and not strict:
            rec["unscrubbed"] = "encoded non-model body"
        elif mode != "off" and body and not strict:
            try:
                j = json.loads(body)
            except ValueError:
                rec["unscrubbed"] = "non-JSON non-model body"
        forms = S.Forms()                               # surface forms seen in this request, for exact restore
        if mode != "off" and ((body and (strict or j is not _NOJSON)) or (chunked and model_ep)):
            counts = {}
            try:
                if chunked and not body:
                    raise S.ScrubError("chunked request body cannot be checked")
                if cenc not in ("", "identity"):
                    raise S.ScrubError("encoded request body (%s) cannot be checked" % cenc[:20])
                if j is _NOJSON:
                    try:
                        j = json.loads(body)
                    except ValueError:
                        raise S.ScrubError("request body is not JSON (%s)" % (ctype.split(";")[0] or "no content type")) from None
                rec["model"] = str(j.get("model") or "")[:60] if isinstance(j, dict) else ""
                rec["stream"] = bool(isinstance(j, dict) and j.get("stream"))
                if mode == "on":
                    SCR.scrub_request(j, counts, forms)
                    out = json.dumps(j, ensure_ascii=False).encode("utf-8")
                    swapped = True
                else:                                   # watch: count only, forward the original
                    SCR.scrub_request(copy.deepcopy(j), counts)
            except Exception as e:                      # FAIL CLOSED
                msg = str(e) if isinstance(e, S.ScrubError) else "scrub failed: " + type(e).__name__
                rec.update(status=503, blocked=True, error=msg[:120])
                _log(rec)
                if mode == "on":
                    STATE["blocked"] += 1
                    STATE["last_error"] = msg[:120]
                    _alert_async("blocked a model request (fail-closed): " + msg[:120] + ". See requests.jsonl in the "
                                 "state dir.")
                    return self._send_json(503, {"error": {"message": "pii scrubber blocked this request (fail-closed): "
                                                           + msg[:120], "type": "scrubber_error", "code": 503}})
                out = body
            rec["swaps"] = counts
        rec["scrub_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        headers["Accept-Encoding"] = "identity"
        headers["Host"] = UP.netloc
        c = r = None
        try:
            conn = http.client.HTTPSConnection if UP.scheme == "https" else http.client.HTTPConnection
            c = conn(UP.hostname, UP.port or (443 if UP.scheme == "https" else 80),
                     timeout=float(CFG.get("upstream_timeout_s") or 900))
            c.connect()
            c.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if out is not None:
                headers["Content-Length"] = str(len(out))
            c.request(self.command, UP.path.rstrip("/") + self.path, body=out, headers=headers)
            r = c.getresponse()
        except Exception as e:
            STATE["errors"] += 1
            STATE["last_error"] = "upstream: " + type(e).__name__
            rec.update(status=502, error="upstream " + type(e).__name__)
            _log(rec)
            if c is not None:
                c.close()
            return self._send_json(502, {"error": {"message": "pii scrubber: upstream unreachable",
                                                   "type": "upstream_error"}})
        ctype = r.getheader("Content-Type") or ""
        restore = swapped and not raw
        stats = {}
        try:
            if "text/event-stream" in ctype:
                self.send_response(r.status)
                for k, v in r.getheaders():
                    if k.lower() not in RHOP:
                        self.send_header(k, v)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                st = {"_forms": forms}
                tmpl = None
                held = []                               # an Anthropic `event:` line waits for its `data:` line, so a
                while True:                             # flushed delta can be sent as a whole event before it
                    line = r.readline()
                    if not line:
                        break
                    if restore and line.startswith(b"event:") and not held:
                        held.append(line)
                        continue
                    if restore and line.startswith(b"data:"):
                        data = line[5:].strip()
                        if data == b"[DONE]":
                            fl = S.flush_all(SCR, st, tmpl)
                            if fl:
                                self._chunk(b"data: " + json.dumps(fl, ensure_ascii=False).encode() + b"\n\n")
                        elif data:
                            try:
                                ev = json.loads(data)
                                if S.is_anthropic_event(ev):
                                    for pre in S.restore_anthropic_event(SCR, ev, st):
                                        self._chunk(b"event: content_block_delta\ndata: "
                                                    + json.dumps(pre, ensure_ascii=False).encode() + b"\n\n")
                                else:
                                    if tmpl is None and isinstance(ev, dict):
                                        tmpl = {k: ev[k] for k in ("id", "object", "created", "model") if k in ev}
                                    S.restore_sse_event(SCR, ev, st)
                                line = b"data: " + json.dumps(ev, ensure_ascii=False).encode() + b"\n"
                            except ValueError:
                                pass
                    if held:
                        self._chunk(b"".join(held))
                        held = []
                    self._chunk(line)
                if held:
                    self._chunk(b"".join(held))
                if restore:
                    for pre in S.flush_anthropic(st):
                        self._chunk(b"event: content_block_delta\ndata: " + json.dumps(pre, ensure_ascii=False).encode()
                                    + b"\n\n")
                    fl = S.flush_all(SCR, st, tmpl)
                    if fl:
                        self._chunk(b"data: " + json.dumps(fl, ensure_ascii=False).encode() + b"\n\n")
                    stats = st.get("_stats") or {}
                self._chunk(b"")
            else:
                data = r.read()
                if restore and data and "json" in ctype:
                    try:
                        data = json.dumps(SCR.restore_obj(json.loads(data), stats, forms=forms),
                                          ensure_ascii=False).encode("utf-8")
                    except ValueError:
                        data = SCR.restore_text(data.decode("utf-8", "replace"), stats=stats, forms=forms).encode("utf-8")
                self.send_response(r.status)
                for k, v in r.getheaders():
                    if k.lower() not in RHOP:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            rec["client_gone"] = True
        except Exception as e:
            STATE["errors"] += 1
            STATE["last_error"] = "restore: " + type(e).__name__
            rec["error"] = "restore " + type(e).__name__
            _alert_async("error while restoring a model response (%s); the answer kept its placeholders." % type(e).__name__)
        finally:
            if c is not None:
                c.close()
        rec.update(status=r.status, restored=stats.get("restored", 0), unknown=stats.get("unknown", 0),
                   total_ms=round((time.perf_counter() - t0) * 1000, 1))
        _log(rec)

    def _chunk(self, b):
        self.wfile.write(b"%x\r\n%s\r\n" % (len(b), b))
        self.wfile.flush()


def make_server(cfg=None):
    """build (not start) the proxy server from cfg; returns the ThreadingHTTPServer."""
    global CFG, SCR, UP, LOG
    CFG = cfg or S.load_cfg()
    SCR = S.Scrubber(CFG)
    UP = urllib.parse.urlparse(CFG.get("upstream") or S.DEFAULTS["upstream"])
    if UP.scheme not in ("http", "https") or not UP.hostname:
        raise SystemExit("upstream must be an http(s) URL")
    LOG = os.path.join(S.state_dir(CFG), "requests.jsonl")
    host, port = str(CFG.get("listen") or S.DEFAULTS["listen"]).rsplit(":", 1)
    if host not in ("127.0.0.1", "localhost", "::1") and not CFG.get("allow_remote"):
        raise SystemExit("refusing to listen off loopback (set allow_remote: true if you really mean it)")
    srv = ThreadingHTTPServer((host, int(port)), H)
    srv.daemon_threads = True
    return srv


def main():
    srv = make_server()
    h, p = srv.server_address[:2]
    print(json.dumps({"listening": "%s:%s" % (h, p), "mode": CFG["mode"], "known": len(SCR.known),
                      "private_rules": len(SCR.private), "upstream": "%s://%s" % (UP.scheme, UP.netloc)}), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
