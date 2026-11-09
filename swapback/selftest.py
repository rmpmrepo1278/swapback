"""swapback selftest: a reproducible leak test you can run on your own machine, with made-up data shipped in the package.

It starts a recording fake model on a loopback port, puts the proxy in front of it with a fixed list of invented
values (accented names, names that are also ordinary phrases, emails, phones in several countries, IDs, addresses),
sends each value in several written forms through 7 request paths (OpenAI chat plain and streamed, chat with tool-call
arguments and a json.dumps tool result in the history, Anthropic messages streamed, Anthropic tool_use/tool_result,
OpenAI Responses streamed, Responses function_call/function_call_output) and then reads what the fake model received.

It prints: listed values caught (no form of them reached the model), forms leaked, ordinary sentences left untouched,
exact round trips, and how many unlisted values the pattern detectors caught (reported, not a pass condition).
Exit code 0 when nothing leaked, every round trip was exact and at least 95% of ordinary sentences were untouched.
The leak check is independent of swapback's own matching: it decodes JSON (nested), \\uXXXX escapes, HTML entities and
%XX on the model side, folds case and accents, and looks for each value's name words, email, phone digits, ID
characters and street line."""
import html, json, os, re, shutil, sys, tempfile, threading, time, unicodedata, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# All invented. Names and places are made up; phone numbers use fictional or reserved ranges where a country has one.
LISTED = {
    "PERSON": ["Mireille Ch\u00e2teau-Dufresne", "Bj\u00f6rn \u00c5kesson", "Zo\u00eb Papadakis", "Tom\u00e1s \u00d3 Briain",
               "Luc\u00eda Fern\u00e1ndez Ortega", "Adaeze Okonkwo", "Haruto Kiyomizu", "Priyanka Venkataraman",
               "Dmitri Volkonsky", "Will Hunt", "Grace Park", "Rose Marsh", "Mark Bell"],
    "EMAIL": ["mireille.cd@example.net", "bjorn.akesson@example.org", "z.papadakis@mail.example", "hkiyomizu@example.jp"],
    "PHONE": ["+1 503 555 0147", "(617) 555-0188", "+91 98450 12873", "07700 900417", "+61 491 570 006",
              "+49 30 90182047", "+33 6 12 34 56 78"],
    "ID": ["QQ 98 76 54 C", "PA-4471-9902", "GB82 WEST 1234 5698 7654 32", "4821 7735 9016"],
    "ADDRESS": ["17 Example Mews, Dublin D04 X2Y1", "Rua das Flores 212, 4050-262 Porto", "742 Alder Court, Springfield, OR 97477"],
}
NEGATIVES = [
    "I will hunt for the keys tonight.", "Will hunting season open early this year?", "Mark the bell schedule for Monday.",
    "We say grace in the park before lunch.", "The rose marsh by the river is flooded.", "Grace, park the car by the gate.",
    "Mark, bell peppers are on sale.", "Grace Hopper wrote the first compiler.", "Mark Twain wrote that line.",
    "Park Avenue is busy at noon.", "Hunt down the root cause first.", "Rose and marsh are both nouns.",
    "The will was read on Friday.", "A park ranger rang the bell.", "Marsh grass grows near the bay.",
    "Let's hunt for a better name.", "Release 2.14.3 fixes the cache bug.", "The laptop costs $1,299.00 with tax.",
    "Meeting moved to 2026-03-14 at 15:30.", "ISBN 978-0-306-40615-7 is on the shelf.", "Commit 3f9a2c1 broke the build.",
    "Open localhost:8080 in the browser.", "Room 4021 is on the fourth floor.", "Order 44721-AB shipped yesterday.",
    "Use 0.25 mg twice a day.", "Set the timeout to 900 seconds.", "Temperature hit 31.5 C today.",
    "The ratio is 16:9 on that screen.", "Flight leaves at 7:40 from gate B12.", "Ship it in Q3, not Q2.",
    '{"drink": "caf\\u00e9", "size": "large"}', "We are 100% done with the draft.", "Copy it to C:\\new\\temp now.",
    "Salt &amp; pepper on the side.", "See https://example.com/a%20b for details.", "Version v3 beats v2 by 12%.",
    "Lunch for 12 people at noon.", "The bus line 42 runs late.", "Chapter 7, page 213, line 4.",
    "Grace periods last 30 days.", "Will you review the PR?", "Mark it as done.", "The Rose Bowl is in January.",
    "Bell curves are common.", "Park the idea for now.",
]
UNLISTED = {"EMAIL": ["oskar.lindqvist@example.com"], "PHONE": ["(206) 555-0161", "+44 20 7946 0321"],
            "CARD": ["4111 1111 1111 1111"], "IBAN": ["DE89 3704 0044 0532 0130 00"], "SSN": ["536-22-1874"],
            "PERSON": ["Ingrid Solberg", "Kwame Asante"]}
PATHS = ["chat", "chat_stream", "chat_tools", "anthropic_stream", "anthropic_tools", "responses_stream", "responses_tools"]


def _fold(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.translate({0xf8: "o", 0xd8: "O", 0x131: "i"}).lower()


def forms(v, t):
    """the written forms a value is sent in."""
    out = [v, v.upper()] if t in ("PERSON", "EMAIL", "ADDRESS") else [v]
    if t == "PERSON":
        w = v.split()
        out.append(w[-1] + ", " + " ".join(w[:-1]))
        if _fold(v) != v.lower():
            out.append(unicodedata.normalize("NFKD", v).encode("ascii", "ignore").decode())
    if t == "PHONE":
        d = re.sub(r"\D", "", v)
        out += [("+" + d) if v.startswith("+") else d, re.sub(r"[ ]", "-", v)]
    if t == "ID":
        out.append(re.sub(r"[\s.\-]", "", v))
    if t == "ADDRESS":
        out.append(v.replace(", ", "\n", 1))
    seen, res = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res


def _needles(v, t):
    """(kind, needle) the model side must never contain."""
    if t == "PERSON":
        w = [x for x in re.split(r"[\s\-]+", _fold(v)) if len(x) > 1]
        return [("words", tuple(w))]
    if t == "EMAIL":
        return [("text", v.lower())]
    if t == "PHONE":
        d = re.sub(r"\D", "", v)
        if v.startswith("+"):
            d = d[1:] if d.startswith("1") else d[2:]
        return [("digits", d.lstrip("0"))]
    if t in ("ID", "IBAN", "CARD", "SSN"):
        return [("alnum", re.sub(r"[^0-9a-z]", "", v.lower()))]
    return [("text", " ".join(_fold(v).split(",")[0].split()))]


def _decodings(s, depth=3):
    out, cur = [s], [s]
    for _ in range(depth):
        nxt = []
        for x in cur:
            for f in (html.unescape, urllib.parse.unquote,
                      lambda y: re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), y),
                      lambda y: re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), y),
                      lambda y: y.replace("\\n", "\n").replace('\\"', '"')):
                try:
                    z = f(x)
                except Exception:
                    continue
                if z != x and z not in out:
                    out.append(z)
                    nxt.append(z)
            try:
                j = json.loads(x)
                for y in _strs(j):
                    if y not in out:
                        out.append(y)
                        nxt.append(y)
            except (ValueError, TypeError):
                pass
        cur = nxt
    return out


def _strs(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield str(k)
            yield from _strs(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strs(v)
    elif isinstance(o, str):
        yield o


def leaked(body, v, t):
    """True when any decoding of any string the model received contains the value."""
    for s0 in _strs(body):
        for s in _decodings(s0):
            f = _fold(s)
            for kind, n in _needles(v, t):
                if kind == "words":
                    sep = re.sub(r"[\s.\-_,]+", " ", f)
                    toks = sep.split()
                    for i in range(len(toks)):
                        if tuple(toks[i:i + len(n)]) == n or tuple(toks[i:i + len(n)]) == n[-1:] + n[:-1]:
                            return True
                elif kind == "text":
                    if n in re.sub(r"\s+", " ", f.replace(",", " ")).replace("  ", " ") or n in f:
                        return True
                elif kind == "digits":
                    if n and n in re.sub(r"(?<=\d)[\s().\-/]+(?=\d)", "", s):
                        return True
                elif kind == "alnum":
                    if n and n in re.sub(r"[^0-9a-z]", "", f):
                        return True
    return False


class _Model(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        _Model.seen.append(body)
        last = (body.get("messages") or body.get("input"))[-1]["content"]
        if isinstance(last, list):
            last = [b.get("text", "") for b in last if b.get("type") == "text"][-1]
        reply, stream = last, body.get("stream")
        ch = [reply[k:k + 5] for k in range(0, len(reply), 5)] or [""]
        if self.path.endswith("/chat/completions"):
            if not stream:
                return self._json({"id": "x", "object": "chat.completion", "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}]})
            evs = [(None, {"id": "x", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": x}}]})
                   for x in ch]
            evs += [(None, {"id": "x", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {},
                                                                                      "finish_reason": "stop"}]}), (None, "[DONE]")]
            return self._sse(evs)
        if self.path.endswith("/messages"):
            if not stream:
                return self._json({"id": "m", "type": "message", "role": "assistant",
                                   "content": [{"type": "text", "text": reply}], "stop_reason": "end_turn"})
            evs = [("message_start", {"type": "message_start", "message": {"id": "m", "role": "assistant", "content": []}}),
                   ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})]
            evs += [("content_block_delta", {"type": "content_block_delta", "index": 0,
                                             "delta": {"type": "text_delta", "text": x}}) for x in ch]
            evs += [("content_block_stop", {"type": "content_block_stop", "index": 0}), ("message_stop", {"type": "message_stop"})]
            return self._sse(evs)
        if not stream:
            return self._json({"id": "r", "object": "response", "output": [
                {"type": "message", "id": "o", "role": "assistant", "content": [{"type": "output_text", "text": reply}]}]})
        evs = [("response.output_text.delta", {"type": "response.output_text.delta", "item_id": "o", "output_index": 0,
                                               "content_index": 0, "delta": x}) for x in ch]
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
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.end_headers()
        for name, e in evs:
            data = e if isinstance(e, str) else json.dumps(e)
            self.wfile.write((("event: %s\n" % name) if name else "").encode() + b"data: " + data.encode() + b"\n\n")
        self.wfile.flush()
        self.close_connection = True


def _body(path, text, tool_text):
    """(url path, request body) for one probe; the user text is always last (the fake model echoes it)."""
    tool_json = json.dumps({"contact": tool_text, "found": True})            # ensure_ascii, as Python tools write it
    args = json.dumps({"q": tool_text})
    if path.startswith("chat"):
        m = [{"role": "system", "content": "You help with contacts."}]
        if path == "chat_tools":
            m += [{"role": "user", "content": "look it up"},
                  {"role": "assistant", "content": None, "tool_calls": [
                      {"id": "t0", "type": "function", "function": {"name": "lookup_contact", "arguments": args}}]},
                  {"role": "tool", "tool_call_id": "t0", "content": tool_json}]
        m.append({"role": "user", "content": text})
        return "/v1/chat/completions", {"model": "fake", "messages": m, "stream": path == "chat_stream"}
    if path.startswith("anthropic"):
        m = []
        if path == "anthropic_tools":
            m += [{"role": "user", "content": "look it up"},
                  {"role": "assistant", "content": [{"type": "tool_use", "id": "t0", "name": "lookup_contact",
                                                     "input": {"q": tool_text}}]},
                  {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t0", "content": tool_json},
                                               {"type": "text", "text": text}]}]
        else:
            m.append({"role": "user", "content": text})
        return "/v1/messages", {"model": "fake", "max_tokens": 50, "system": "You help.", "messages": m, "stream": True}
    inp = []
    if path == "responses_tools":
        inp += [{"type": "function_call", "call_id": "t0", "name": "lookup_contact", "arguments": args},
                {"type": "function_call_output", "call_id": "t0", "output": tool_json}]
    inp.append({"role": "user", "content": text})
    return "/v1/responses", {"model": "fake", "instructions": "You help.", "input": inp, "stream": path == "responses_stream"}


def _reply_text(raw, path):
    if not raw.startswith("data:") and not raw.startswith("event:"):
        j = json.loads(raw)
        if "choices" in j:
            return j["choices"][0]["message"]["content"]
        if "content" in j:
            return "".join(b.get("text", "") for b in j["content"])
        return "".join(c.get("text", "") for o in j["output"] for c in o.get("content", []))
    out = []
    for line in raw.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        e = json.loads(line[6:])
        if "choices" in e:
            out.append(e["choices"][0].get("delta", {}).get("content") or "")
        elif e.get("type") == "content_block_delta":
            out.append(e["delta"].get("text", ""))
        elif e.get("type") == "response.output_text.delta":
            out.append(e.get("delta", ""))
    return "".join(out)


def _yaml_list(d):
    return "".join("%s:\n%s" % (t, "".join("  - %s\n" % json.dumps(v, ensure_ascii=False) for v in vals))
                   for t, vals in d.items())


def run(listed=None, negatives=None, unlisted=None, out=None, as_json=False):
    from . import core as c, proxy
    out = out or sys.stdout
    listed, negatives, unlisted = listed or LISTED, negatives or NEGATIVES, unlisted or UNLISTED
    d = tempfile.mkdtemp(prefix="swapback-selftest-")
    model = ThreadingHTTPServer(("127.0.0.1", 0), _Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    srv = None
    t0 = time.time()
    try:
        with open(os.path.join(d, "known.yaml"), "w", encoding="utf-8") as f:
            f.write(_yaml_list(listed))
        cfgp = os.path.join(d, "swapback.yaml")
        with open(cfgp, "w") as f:
            f.write("upstream: http://127.0.0.1:%d\nlisten: 127.0.0.1:0\nknown_file: known.yaml\nstate_dir: st\n"
                    "log_requests: false\n" % model.server_address[1])
        import io, contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            srv = proxy.make_server(c.load_cfg(cfgp))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]

        def send(path, text, tool_text):
            p, body = _body(path, text, tool_text)
            _Model.seen.clear()
            req = urllib.request.Request(base + p, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer fake"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read().decode("utf-8")
            except urllib.error.HTTPError as e:
                return e.code, None, None
            return 200, (_Model.seen[0] if _Model.seen else None), _reply_text(raw, path)

        res = {"listed_values": 0, "listed_caught": 0, "forms": 0, "requests": 0, "leaked_forms": 0, "blocked": 0,
               "round_trip_exact": 0, "round_trips": 0, "negatives": len(negatives), "negatives_untouched": 0,
               "unlisted": 0, "unlisted_caught": 0, "paths": len(PATHS), "leaks": []}
        for t, vals in listed.items():
            for v in vals:
                res["listed_values"] += 1
                clean = True
                for fm in forms(v, t):
                    res["forms"] += 1
                    for path in PATHS:
                        text = "Please note: %s." % fm
                        code, seen, back = send(path, text, fm)
                        res["requests"] += 1
                        if code != 200:
                            res["blocked"] += 1              # fail-closed: nothing left (not a leak)
                            continue
                        if leaked(seen, v, t):
                            res["leaked_forms"] += 1
                            clean = False
                            res["leaks"].append({"type": t, "path": path, "form": "#%d" % forms(v, t).index(fm)})
                        res["round_trips"] += 1
                        res["round_trip_exact"] += back == text
                res["listed_caught"] += clean
        for n in negatives:
            code, seen, back = send("chat", n, "nothing here")
            if code == 200 and seen is not None and seen["messages"][-1]["content"] == n and back == n:
                res["negatives_untouched"] += 1
        for t, vals in unlisted.items():
            for v in vals:
                res["unlisted"] += 1
                code, seen, back = send("chat", "Please note: %s." % v, "nothing here")
                if code != 200 or not leaked({"m": seen["messages"][-1]["content"]}, v, "EMAIL" if t == "EMAIL" else
                                             "PHONE" if t == "PHONE" else "PERSON" if t == "PERSON" else "ID"):
                    res["unlisted_caught"] += 1
        res["seconds"] = round(time.time() - t0, 1)
        res["pass"] = (res["leaked_forms"] == 0 and res["round_trip_exact"] == res["round_trips"]
                       and res["negatives_untouched"] >= 0.95 * res["negatives"])
        if as_json:
            print(json.dumps(res, ensure_ascii=False), file=out)
        else:
            pct = lambda a, b: "%d/%d (%.1f%%)" % (a, b, 100.0 * a / b if b else 0)
            print("swapback selftest: %d made-up listed values, %d written forms, %d paths, %d requests (%.1f s)"
                  % (res["listed_values"], res["forms"], res["paths"], res["requests"], res["seconds"]), file=out)
            print("  listed values caught (no form reached the model): " + pct(res["listed_caught"], res["listed_values"]), file=out)
            print("  forms leaked to the model:                        %d of %d sent" % (res["leaked_forms"], res["requests"]), file=out)
            if res["blocked"]:
                print("  requests blocked (fail-closed, nothing sent):     %d" % res["blocked"], file=out)
            print("  ordinary sentences left untouched:                " + pct(res["negatives_untouched"], res["negatives"]), file=out)
            print("  exact round trips:                                " + pct(res["round_trip_exact"], res["round_trips"]), file=out)
            print("  unlisted values caught by the pattern detectors:  " + pct(res["unlisted_caught"], res["unlisted"])
                  + "  (reported, not a pass condition; names are never caught without a list)", file=out)
            for lk in res["leaks"][:10]:
                print("  LEAK: %(type)s form %(form)s on %(path)s" % lk, file=out)
            print("PASS" if res["pass"] else "FAIL", file=out)
        return res
    finally:
        if srv is not None:
            srv.shutdown()
            srv.server_close()
        model.shutdown()
        model.server_close()
        shutil.rmtree(d, ignore_errors=True)


def main(as_json=False):
    return 0 if run(as_json=as_json)["pass"] else 1
