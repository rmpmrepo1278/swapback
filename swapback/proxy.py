"""swapback.proxy: a small HTTP proxy in front of any OpenAI-compatible endpoint. JSON request bodies are scrubbed
(swapback.core) before they leave and responses (plain JSON or SSE streams) are restored on the way back. Paths,
headers and models pass through unchanged. Fail closed: a scrub error answers 503 and nothing is forwarded. Logs
counts and types only. OpenAI chat-completion chunks, Responses API events and Anthropic /v1/messages events are all restored in streams.
GET /scrubber/health -> {"ok": ...}. Optional hooks (swapback.hooks, the `hooks:` config section) can reroute,
answer, account for usage and add GET endpoints; they only ever see scrubbed requests. Request header X-Scrubber-Raw: 1 returns the provider's
answer without the swap-back (used by `swapback canary` to prove what the provider saw)."""
import copy, http.client, json, os, socket, sys, threading, time, urllib.parse
_NOJSON = object()
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import core as S
from . import hooks as HK

CFG = None
SCR = None
UP = None
LOG = None
HOOKS = HK.Hooks()
HOP = {"host", "content-length", "accept-encoding", "connection", "keep-alive", "transfer-encoding", "proxy-connection",
       "te", "upgrade", "x-scrubber-raw"}
RHOP = {"content-length", "transfer-encoding", "connection", "keep-alive", "content-encoding"}
STATE = {"started": time.time(), "requests": 0, "blocked": 0, "errors": 0, "last_error": "", "cfg_mtime": 0}
_lock = threading.Lock()


def _cfg_refresh():
    global CFG
    p = S.config_path()
    try:
        st = os.stat(p) if p else None
        mt = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns) if st else 0
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
    if HOOKS.has("alert"):                               # the hooks module delivers alerts itself
        threading.Thread(target=HOOKS.soft, args=("alert", "swapback: " + text[:300]), daemon=True).start()
        return
    threading.Thread(target=S.alert, args=(CFG, text), daemon=True).start()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    server_version = "swapback/1"

    def log_message(self, *a):
        pass

    def _send_json(self, code, obj, extra=None):
        b = json.dumps(obj).encode()
        self.send_response(code)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        fn = HOOKS.route(self.path) if HOOKS.routes and not self.path.startswith("/scrubber/health") else None
        if fn is not None:                               # an extra GET endpoint from the hooks module
            try:
                res = fn()
                code, obj = res if isinstance(res, tuple) else (200, res)
            except Exception as e:
                HOOKS.errors += 1
                code, obj = 500, {"ok": False, "error": "hook route failed: " + type(e).__name__}
            return self._send_json(int(code), obj)
        if self.path.startswith("/scrubber/health"):
            _cfg_refresh()
            try:
                SCR.reload_known()
            except S.ScrubError as e:
                STATE["last_error"] = str(e)[:120]
            warn = SCR.warnings()
            # status: "ok"; "warning" when the known file shrank sharply (ok is false while the previous values are still
            # being protected, so a monitor that reads ok notices); "blocked" when every request is refused (an empty
            # list, or the file went missing or unreadable while running; back to ok once a good file is read)
            ok, status, why = SCR.status()
            if why and why not in warn:
                warn = [why] + list(warn)
            return self._send_json(200, {"ok": ok, "status": status,
                                         "mode": CFG["mode"],
                                         "known": SCR.known_count(), "known_builtin": len(S.CANARY),
                                         "vault": len(SCR.rev), "warnings": warn,
                                         "requests": STATE["requests"], "blocked": STATE["blocked"],
                                         "errors": STATE["errors"], "last_error": STATE["last_error"],
                                         "uptime_s": int(time.time() - STATE["started"]),
                                         "hooks": HOOKS.name, "hook_errors": HOOKS.errors})
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
                api = _api(rec["path"])
                if mode == "on":
                    SCR.scrub_request(j, counts, forms, api=api)
                    out = json.dumps(j, ensure_ascii=False).encode("utf-8")
                    swapped = True
                else:                                   # watch: count only, forward the original
                    SCR.scrub_request(copy.deepcopy(j), counts, api=api)
            except Exception as e:                      # FAIL CLOSED
                msg = str(e) if isinstance(e, S.ScrubError) else "scrub failed: " + type(e).__name__
                rec.update(status=503, blocked=True, error=msg[:120])
                _log(rec)
                if mode == "on":
                    STATE["blocked"] += 1
                    STATE["last_error"] = msg[:120]
                    _alert_async("blocked a model request (fail-closed): " + msg[:120] + ". See requests.jsonl in the "
                                 "state dir.")
                    return self._blocked(msg, "scrubber_error")
                out = body
            rec["swaps"] = counts
        rec["scrub_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        headers["Accept-Encoding"] = "identity"
        headers["Host"] = UP.netloc
        restore = swapped and not raw
        ctx = {"log": {}, "path": rec["path"], "mode": mode}
        route = None
        if HOOKS.has("before_forward"):
            try:
                hb = j if j is not _NOJSON else None
                if hb is None and out:
                    try:
                        hb = json.loads(out)
                    except ValueError:
                        hb = None
                req = HK.Request(self.command, self.path, headers, hb, _api(rec["path"]), mode)
                ctx["request"] = req
                route = HK.check_action(HOOKS.call("before_forward", req, ctx), "before_forward")
            except Exception as e:                      # FAIL CLOSED, like a scrub error
                return self._hook_fail(rec, "before_forward", e)
            if route and "respond" in route:
                return self._respond(route["respond"], rec, ctx, restore, forms, t0)
        c = r = None
        pre = None                                      # an error body already read for on_upstream_response
        err = None
        try:
            c, r = self._send(self._target(route, out, headers))
        except Exception as e:
            err = e
            STATE["errors"] += 1
            STATE["last_error"] = "upstream: " + type(e).__name__
        if HOOKS.has("on_upstream_response") and (r is None or r.status >= 400):
            if r is not None:
                pre = r.read()
            resp = HK.Response(r.status if r is not None else None, pre or b"", r.getheaders() if r is not None else {},
                               "" if r is not None else type(err).__name__)
            try:
                alt = HK.check_action(HOOKS.call("on_upstream_response", resp, ctx), "on_upstream_response")
            except Exception as e:
                if c is not None:
                    c.close()
                return self._hook_fail(rec, "on_upstream_response", e)
            if alt:
                try:
                    c2, r2 = self._send(self._target(alt, out, headers))
                    if c is not None:
                        c.close()
                    c, r, pre = c2, r2, None
                    rec["rerouted"] = ctx["rerouted"] = True
                except Exception as e2:
                    rec["reroute_error"] = type(e2).__name__
        if r is None:
            rec.update(status=502, error="upstream " + type(err).__name__)
            if ctx["log"]:
                rec["hook"] = ctx["log"]
            _log(rec)
            if c is not None:
                c.close()
            HOOKS.soft("on_complete", {"status": 502, "headers": {}, "rerouted": bool(ctx.get("rerouted")),
                                       "error": "upstream " + type(err).__name__}, ctx)
            return self._send_json(502, {"error": {"message": "swapback: upstream unreachable",
                                                   "type": "upstream_error"}})
        ctype = r.getheader("Content-Type") or ""
        stats = {}
        usage = HOOKS.has("on_usage")
        try:
            if "text/event-stream" in ctype and pre is None:
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
                    if usage and line.startswith(b"data:"):
                        try:                            # a separate parse: the hook never holds a restored object
                            HOOKS.soft("on_usage", json.loads(line[5:].strip() or b"null"), ctx)
                        except ValueError:
                            pass
                    if restore and line.startswith(b"data:"):
                        data = line[5:].strip()
                        if data == b"[DONE]":
                            fl = S.flush_all(SCR, st, tmpl)
                            if fl:
                                self._chunk(b"data: " + json.dumps(fl, ensure_ascii=False).encode() + b"\n\n")
                        elif data:
                            try:
                                ev = json.loads(data)
                                if S.is_responses_event(ev) or S.is_anthropic_event(ev):
                                    restore_ev = S.restore_responses_event if S.is_responses_event(ev) \
                                        else S.restore_anthropic_event
                                    for pre in restore_ev(SCR, ev, st):
                                        self._event(pre)
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
                    for pre in S.flush_anthropic(st) + S.flush_responses(st):
                        self._event(pre)
                    fl = S.flush_all(SCR, st, tmpl)
                    if fl:
                        self._chunk(b"data: " + json.dumps(fl, ensure_ascii=False).encode() + b"\n\n")
                    stats = st.get("_stats") or {}
                self._chunk(b"")
            else:
                data = pre if pre is not None else r.read()
                if usage and data and "json" in ctype:
                    try:
                        HOOKS.soft("on_usage", json.loads(data), ctx)
                    except ValueError:
                        pass
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
        HOOKS.soft("on_complete", {"status": r.status, "headers": {k.lower(): v for k, v in r.getheaders()},
                                   "rerouted": bool(ctx.get("rerouted")), "client_gone": bool(rec.get("client_gone"))},
                   ctx)
        if ctx["log"]:
            rec["hook"] = ctx["log"]
        rec.update(status=r.status, restored=stats.get("restored", 0), unknown=stats.get("unknown", 0),
                   tool_kept=stats.get("tool_kept", 0),
                   total_ms=round((time.perf_counter() - t0) * 1000, 1))
        _log(rec)

    def _target(self, route, out, headers):
        """(url, path, body, headers, method, timeout) for the configured upstream, or for a hook's route."""
        route = route or {}
        if route.get("url"):
            u = urllib.parse.urlparse(str(route["url"]))
            path = (u.path or "/") + ("?" + u.query if u.query else "")
        else:
            u, path = UP, UP.path.rstrip("/") + self.path
        body = out
        if "body" in route:
            b = route["body"]
            body = b if b is None or isinstance(b, (bytes, bytearray)) else json.dumps(b, ensure_ascii=False).encode("utf-8")
        hdrs = dict(route["headers"]) if route.get("headers") is not None else dict(headers)
        hdrs["Host"] = u.netloc
        hdrs["Accept-Encoding"] = "identity"
        timeout = float(route.get("timeout_s") or CFG.get("upstream_timeout_s") or 900)
        return u, path, body, hdrs, str(route.get("method") or self.command), timeout

    def _send(self, target):
        u, path, body, hdrs, method, timeout = target
        conn = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        c = conn(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=timeout)
        try:
            c.connect()
            c.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if body is not None:
                hdrs["Content-Length"] = str(len(body))
            c.request(method, path, body=body, headers=hdrs)
            return c, c.getresponse()
        except Exception:
            c.close()
            raise

    def _hook_fail(self, rec, where, e):
        msg = "%s hook failed: %s" % (where, str(e)[:80] if isinstance(e, HK.HookError) else type(e).__name__)
        STATE["blocked"] += 1
        STATE["last_error"] = msg[:120]
        rec.update(status=503, blocked=True, error=msg[:120])
        _log(rec)
        _alert_async("blocked a model request (fail-closed): " + msg[:120])
        return self._blocked(msg, "hook_error")

    def _blocked(self, reason, kind):
        """answer a blocked request with 503 in the error shape the client's API uses (OpenAI or Anthropic), with a
        message written for the person reading the agent's screen, and one log line on stderr. Never a value."""
        reason = reason[:120]
        text = ("swapback blocked this request (fail-closed), so nothing was sent to the model: %s. Run `swapback health` "
                "for details." % reason)
        print("swapback: blocked %s %s: %s" % (self.command, self.path.split("?")[0], reason), file=sys.stderr,
              flush=True)
        if _api(self.path.split("?")[0]) == "anthropic":
            obj = {"type": "error", "error": {"type": "api_error", "message": text}, "swapback": {"blocked": kind}}
        else:
            obj = {"error": {"message": text, "type": kind, "param": None, "code": 503}}
        return self._send_json(503, obj, {"X-Swapback-Blocked": kind})

    def _respond(self, rsp, rec, ctx, restore, forms, t0):
        """before_forward answered the client itself; its JSON is restored like an upstream reply."""
        obj, stats = rsp.get("json"), {}
        if restore and obj is not None:
            obj = SCR.restore_obj(obj, stats, forms=forms)
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(rsp["status"])
        for k, v in (rsp.get("headers") or {}).items():
            if k.lower() not in RHOP and k.lower() != "content-type":
                self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
        HOOKS.soft("on_complete", {"status": rsp["status"], "headers": {}, "rerouted": False, "responded": True}, ctx)
        if ctx["log"]:
            rec["hook"] = ctx["log"]
        rec.update(status=rsp["status"], hook_response=True, restored=stats.get("restored", 0),
                   total_ms=round((time.perf_counter() - t0) * 1000, 1))
        _log(rec)

    def _event(self, ev):
        """one extra SSE event (a held-back delta), named by its own type."""
        self._chunk(b"event: " + str(ev.get("type") or "message").encode() + b"\ndata: "
                    + json.dumps(ev, ensure_ascii=False).encode() + b"\n\n")

    def _chunk(self, b):
        self.wfile.write(b"%x\r\n%s\r\n" % (len(b), b))
        self.wfile.flush()


def _api(path):
    return "anthropic" if path.endswith("/messages") else "responses" if path.endswith("/responses") else "chat"


def make_server(cfg=None, hooks=None):
    """build (not start) the proxy server from cfg; returns the ThreadingHTTPServer. hooks: a loaded hooks.Hooks to use
    instead of the cfg `hooks` section (tests and embedders)."""
    global CFG, SCR, UP, LOG, HOOKS
    CFG = cfg or S.load_cfg()
    SCR = S.Scrubber(CFG)
    SCR.on_alert = _alert_async
    HOOKS = hooks if hooks is not None else HK.Hooks(CFG)
    HOOKS.setup(CFG)
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


def cli_parser():
    import argparse
    from . import __version__
    ap = argparse.ArgumentParser(
        prog="swapback-proxy", formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Start the swapback proxy: a local HTTP proxy in front of any OpenAI-compatible\n"
            "endpoint (and Anthropic /v1/messages). Personal data in each request is swapped\n"
            "for placeholders (PERSON_1, EMAIL_1, ...) before it leaves your machine; replies,\n"
            "streamed or not, tool calls included, get the real values back. Fails closed: if\n"
            "scrubbing fails, the request is answered 503 and nothing is forwarded.\n"
            "Point your client's base URL at http://127.0.0.1:8787/v1 (or your listen address).\n"
            "Runs in the foreground; prints one JSON line when it is listening."),
        epilog=S.CLI_ENV_HELP + "\n\nsee also:\n"
               "  swapback check-config   check the config before starting (counts, never values)\n"
               "  swapback health         status of the running proxy (or GET /scrubber/health)\n"
               "  swapback --help         all commands")
    ap.add_argument("--version", action="version", version="swapback-proxy " + __version__)
    return ap


def main(argv=None):
    argv = sys.argv if argv is None else argv
    try:
        cli_parser().parse_args(argv[1:])        # --help/--version/unknown flags exit here; no config load, no port
    except BrokenPipeError:                      # `swapback-proxy --help | head`
        S._quiet_pipe()
    try:
        srv = make_server()
    except S.ConfigError as e:                   # a missing/broken config file: one readable line, no traceback
        print("swapback: error: %s. Fix the config file, then start again." % e, file=sys.stderr, flush=True)
        sys.exit(2)
    except S.ScrubError as e:                    # a missing/broken known file at startup: one readable line, no traceback
        print("swapback: error: %s. Fix the path in your config (or run `swapback init`), then start again." % e,
              file=sys.stderr, flush=True)
        sys.exit(2)
    h, p = srv.server_address[:2]
    print(json.dumps({"listening": "%s:%s" % (h, p), "mode": CFG["mode"], "known": SCR.known_count(),
                      "private_rules": len(SCR.private), "upstream": "%s://%s" % (UP.scheme, UP.netloc),
                      "hooks": HOOKS.name or None}), flush=True)
    for w in SCR.warnings():
        print("swapback: warning: " + w, file=sys.stderr, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
