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
               "Dmitri Volkonsky", "Will Hunt", "Grace Park", "Rose Marsh", "Mark Bell", "Drew Wells", "Gene Pool"],
    "EMAIL": ["mireille.cd@example.net", "bjorn.akesson@example.org", "z.papadakis@mail.example", "hkiyomizu@example.jp"],
    "PHONE": ["+1 503 555 0147", "(617) 555-0188", "+91 98450 12873", "07700 900417", "+61 491 570 006",
              "+49 30 90182047", "+33 6 12 34 56 78"],
    "ID": ["QQ 98 76 54 C", "PA-4471-9902", "GB82 WEST 1234 5698 7654 32", "4821 7735 9016"],
    "ADDRESS": ["17 Example Mews, Dublin D04 X2Y1", "Rua das Flores 212, 4050-262 Porto", "742 Alder Court, Springfield, OR 97477",
                "58 Wrenfield Lane\nUnit 4D\nAshby, OR 97009"],
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
    "Bell curves are common.", "Park the idea for now.", "She drew wells for three villages.",
    "Drew wells before the dry season?", "The gene pool is small here.", "Gene pool size matters.",
    "We say grace, park the bikes, then eat.", "Wells, drew a crowd at noon.",
    "WILL HUNT FOR CLUES TONIGHT", "DREW WELLS ACROSS THE VALLEY", "GENE POOL PARTY AT NOON", "ROSE MARSH TRAIL CLOSED",
    "MARK BELL SCHEDULE: MONDAY", "GRACE PARK OPENS AT 9",
]
UNLISTED = {"EMAIL": ["oskar.lindqvist@example.com"], "PHONE": ["(206) 555-0161", "+44 20 7946 0321"],
            "CARD": ["4111 1111 1111 1111"], "IBAN": ["DE89 3704 0044 0532 0130 00"], "SSN": ["536-22-1874"],
            "PERSON": ["Ingrid Solberg", "Kwame Asante"]}
PATHS = ["chat", "chat_stream", "chat_tools", "anthropic_stream", "anthropic_tools", "responses_stream", "responses_tools"]


HTML_JOINS = ["<br>\n", "<br />\n", "<br/>\r\n", "</p>\n<p>"]
_ZW = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))


# review #7 (0.4.7): values nested 1 to 8 JSON levels deep, a dictionary-word name next to sentence ends and HTML blocks
REVIEW7_DEEP = ["2219 Larkspur Hollow Road\nSuite 410\nBoise, ID 83702", "Odile\tMarchetti", "S\u00f8ren \u00c5lund"]
REVIEW7_KNOWN = [(REVIEW7_DEEP[0], "ADDRESS"), ("Odile Marchetti", "PERSON"), ("S\u00f8ren \u00c5lund", "PERSON"),
                 ("Grace Fields", "PERSON")]
REVIEW7_NAME_TEXTS = [("Saving Grace. Fields of gold are lovely.", "untouched"),
                      ("<div>Grace</div>\n<div>Fields</div>\n<div>Pricing</div>", "swapped"),   # 0.4.9: joined
                      ("<p>Grace</p><p>Fields of gold</p>", "untouched"),
                      ("Amazing Grace!\n\nFields of barley.", "untouched"), ("Ask Grace Fields today.", "swapped"),
                      ("<b>Grace</b> Fields", "swapped"), ("Grace<br>Fields", "swapped")]
# review #8 (0.4.8): tool-call arguments with mixed escaping, \u00ab-escaped placeholders in a text reply, a
# dictionary-word name whose words each fill one HTML block element, a bad listen value or a port in use
REVIEW8_ADDR = "3381 Wintergreen Spur Road\nSuite 220\nBend, OR 97701"
REVIEW8_KNOWN = [(REVIEW8_ADDR, "ADDRESS"), ("Odile Marchetti", "PERSON"), ("Hazel Woods", "PERSON"), ("Summer Lake", "PERSON")]
REVIEW8_CELLS = [("<table><tr><td>Hazel</td><td>Woods</td></tr></table>", "swapped"),
                 ("<ul><li>Hazel</li><li>Woods</li></ul>", "swapped"), ("<div>Summer</div>\n<div>Lake</div>", "swapped"),
                 ("<tr><td>Hazel</td><td>Woods</td><td>hw@example.net</td></tr>", "swapped"),
                 ("<h2>Summer</h2><p>Lake trips are the best part of the year.</p>", "untouched"),
                 ("<li>Summer sale</li><li>Lake view rooms</li>", "untouched"),
                 ("Customer: Hazel. Woods is her surname.", "not swapped")]
# review #9 (0.4.9): a listed dictionary-word name split across one-word cells/blocks anywhere in a row or list (third
# cell after it, header row, leading id or word cell, pretty-printed, attributes, two rows, three items), the note's
# example placeholders never assigned, a broken known file reported as broken YAML
REVIEW9_KNOWN = [("Holly Marsh", "PERSON"), ("Jasper Stone", "PERSON"), ("Thomasina Wexley", "PERSON"),
                 ("l.varga@quillfeather.example", "EMAIL")]
REVIEW10_ATTR = ['<div dir="ltr">27 Gallowgate Mews</div><div dir="ltr">Unit 9</div><div dir="ltr">Burlington, VT 05401</div>',
                 '<p class="a">27 Gallowgate Mews</p><p class="a">Unit 9</p><p class="a">Burlington, VT 05401</p>',
                 '27 Gallowgate Mews<br class="l">Unit 9<br class="l">Burlington, VT 05401',
                 '<td class="a">Amber</td><td class="b">Lane</td>',
                 '<tr><td class="a">Amber</td><td class="b">Lane</td><td>Tucson</td></tr>',
                 '<ul><li class="m">Home</li><li class="m">Amber</li><li class="m">Lane</li><li class="m">About</li></ul>',
                 '<li data-x="1">Amber</li><li data-x="2">Lane</li>',
                 '<td class="a">Ottoline</td><td class="b">Frobisher</td>',
                 '<span class="n">Ottoline</span> <span class="n">Frobisher</span>']


REVIEW11_DEEP = ['<div dir="ltr">Heath</div><div dir="ltr">Cliff</div>',
                 '<tr><td class="first">Heath</td><td style="color:#222">Cliff</td><td>Reno</td></tr>',
                 '<tr><td>Heath</td><td>Cliff</td><td>Reno</td></tr>',
                 '<ul><li>Heath</li><li>Cliff</li><li>Ops</li></ul>']
REVIEW11_TEXTS = ["Thanks Faith, Ridge trail opens at 9.", "id,first,last,city\n3,Faith,Ridge,Reno\n4,Ann,Lee,Ames",
                  "i\u0307rem kaya signed it.", "I\u0307rem Kaya signed it."]


REVIEW9_LAYOUTS = ["<tr><td>{F}</td><td>{L}</td></tr>", "<tr><td>{F}</td><td>{L}</td><td>Denver</td></tr>",
                   "<tr><td>{F}</td><td>{L}</td><td>CO</td></tr>", "<tr><td>{F}</td><td>{L}</td><td>Manager</td></tr>",
                   "<tr><td>{F}</td><td>{L}</td><td>Engineering</td></tr>",
                   "<tr><td>{F}</td><td>{L}</td><td>{f}.{l}@acme.example</td></tr>",
                   "<tr><td>{F}</td><td>{L}</td><td>42</td></tr>", "<tr><td>{F}</td><td>{L}</td><td>New York</td></tr>",
                   "<table><tr><th>First</th><th>Last</th><th>City</th></tr><tr><td>{F}</td><td>{L}</td><td>Denver</td></tr></table>",
                   "<table><thead><tr><th>First</th><th>Last</th></tr></thead><tbody><tr><td>{F}</td><td>{L}</td></tr></tbody></table>",
                   "<tr><td>17</td><td>{F}</td><td>{L}</td><td>Denver</td></tr>", "<tr><td>Active</td><td>{F}</td><td>{L}</td></tr>",
                   "<tr>\n  <td>{F}</td>\n  <td>{L}</td>\n  <td>Denver</td>\n</tr>",
                   "<tr><td class=\"first\">{F}</td><td class=\"last\">{L}</td><td class=\"city\">Denver</td></tr>",
                   "<table><tr><td>{F}</td><td>{L}</td><td>Denver</td></tr><tr><td>Rob</td><td>Lee</td><td>Austin</td></tr></table>",
                   "<ul><li>{F}</li><li>{L}</li><li>Engineering</li></ul>", "<div>{F}</div><div>{L}</div><div>Boston</div>",
                   "<div>Owner</div><div>{F}</div><div>{L}</div>", "<p>{F}</p><p>{L}</p>", "<span>{F}</span> <span>{L}</span>",
                   "<tr><td>{F}</td><td>{LU}</td></tr>"]
REVIEW9_ORDINARY = ["<ul><li>Home</li><li>Holly</li><li>Stone</li><li>Pricing</li></ul>",
                    "<table><tr><td>Rob</td><td>Holly</td></tr><tr><td>Marsh</td><td>Lee</td></tr></table>",
                    "<tr><td>Jasper</td><td>Marsh</td><td>Denver</td></tr>", "<nav><div>Stone</div><div>Jasper</div></nav>",
                    "<p>Holly</p><p>Marsh walks are lovely in May.</p>"]


def _fold(s):
    s = unicodedata.normalize("NFKD", s.translate(_ZW))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.translate({0xf8: "o", 0xd8: "O", 0x131: "i"}).lower()


def _ascii(v):
    from .core import _strip_accents
    return _strip_accents(v).encode("ascii", "ignore").decode()


def forms(v, t):
    """the written forms a value is sent in."""
    from .core import _is_common_phrase, _phone_core
    # ALL-CAPS of a name made of dictionary words ("WILL HUNT") is a heading, not a name: not swapped by default
    upper = t in ("EMAIL", "ADDRESS") or (t == "PERSON" and not _is_common_phrase(v))
    out = [v, v.upper()] if upper else [v]
    if t == "PERSON":
        w = v.split()
        out.append(w[-1] + ", " + " ".join(w[:-1]))
        if _fold(v) != v.lower():
            out.append(_ascii(v))
        if len(w) >= 2:                              # words spread over path segments: a path, not a folder name
            out += ["/".join(w), "/Users/%s/notes.txt" % "/".join(w)]
        if "-" in v:                                 # a hyphen written as a space or as nothing, also surname first
            sp = v.replace("-", " ")
            out += [sp, v.replace("-", ""), " ".join(sp.split()[1:]) + " " + sp.split()[0]]
    if t == "PHONE":
        d = re.sub(r"\D", "", v)
        core = _phone_core(v) or d
        out += [("+" + d) if v.startswith("+") else d, re.sub(r"[ ]", "-", v)]
        # letters glued on (an extension, a label) and a phone in a URL path with no host
        out += [re.sub(r"[ ]", "-", v) + "x22", v + "ext9", core + "abc", "Mob" + core, "/u/%s/profile" % core]
    if t == "ID":
        out.append(re.sub(r"[\s.\-]", "", v))
    if t == "ADDRESS":
        lines = v.split("\n") if "\n" in v else v.split(", ")
        out.append(v.replace(", ", "\n", 1) if "\n" not in v else ", ".join(lines))
        out.append("<br>".join(lines))                 # lines joined by HTML line breaks
        for j in HTML_JOINS:                           # ... and by a tag plus a line break (an HTML mail body)
            out.append(j.join(lines))
    out += encoded_forms(v, t)
    seen, res = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res


_FW = {c: chr(ord(c) + 0xFEE0) for c in "0123456789+()-"}


def encoded_forms(v, t):
    """the same value as a URL / form body writes it (quote_plus: spaces as '+'), with decomposed accents (NFD), with a
    zero-width space inside a word, and (phones, IDs) in fullwidth digits."""
    out = []
    if " " in v:
        out.append(urllib.parse.quote_plus(v))
        out.append(v.replace(" ", "/", 1) if t == "PERSON" else v.replace(" ", "+"))
    nfd = unicodedata.normalize("NFD", v)
    if nfd != v:
        out.append(nfd)
    w = v.split(" ")[0] if t != "EMAIL" else v.split("@")[0]
    if len(w) >= 4 and not any(ch.isdigit() for ch in w[:3]):
        i = v.index(w) + 2
        out.append(v[:i] + "\u200b" + v[i:])
    if t in ("PHONE", "ID") and any(ch.isdigit() for ch in v):
        out.append("".join(_FW.get(ch, ch) for ch in v))
    return out


def is_escaped_form(fm):
    """a form whose restore comes back decoded (%XX): the same value, in its plain spelling."""
    return bool(re.search(r"%[0-9A-Fa-f]{2}", fm))


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
    lines = [x for x in re.split(r"\n|, ", _fold(v)) if x.strip()]
    return [("text", " ".join(x.replace(",", " ").split())) for x in (lines if "\n" in v else lines[:1])]   # every line of a multi-line one


def _decodings(s, depth=6):
    out, cur = [s], [s]
    for _ in range(depth):
        nxt = []
        for x in cur:
            for f in (html.unescape, urllib.parse.unquote, urllib.parse.unquote_plus,
                      lambda y: re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), y),
                      lambda y: re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), y),
                      lambda y: y.replace("\\n", "\n").replace('\\"', '"'),
                      lambda y: re.sub(r"<[^<>]{0,40}>", " ", y)):
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
                    sep = re.sub(r"[\s.\-_,+/]+", " ", f)
                    toks = sep.split()
                    for i in range(len(toks)):
                        if tuple(toks[i:i + len(n)]) == n or tuple(toks[i:i + len(n)]) == n[-1:] + n[:-1]:
                            return True
                elif kind == "text":
                    if n in re.sub(r"\s+", " ", f.replace(",", " ")).replace("  ", " ") or n in f:
                        return True
                elif kind == "digits":
                    if n and n in re.sub(r"(?<=\d)[\s().\-/+]+(?=\d)", "", _fold(s)):
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
               "round_trip_exact": 0, "round_trips": 0, "decoded_round_trips": 0, "decoded_round_trip_same": 0, "negatives": len(negatives), "negatives_untouched": 0,
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
                        if is_escaped_form(fm):      # %XX comes back as the plain spelling: same value, not same bytes
                            res["decoded_round_trips"] += 1
                            res["decoded_round_trip_same"] += back == "Please note: %s." % urllib.parse.unquote(fm)
                            continue
                        res["round_trips"] += 1
                        res["round_trip_exact"] += back == text
                        if back != text and len(res.setdefault("round_trip_misses", [])) < 10:
                            res["round_trip_misses"].append({"type": t, "path": path, "form": "#%d" % forms(v, t).index(fm)})
                res["listed_caught"] += clean
        # the egress re-check on its own (the swap switched off): every form, as text and as a json.dumps tool result,
        # must be seen by it, so a form the swap missed would be blocked rather than sent
        chk = c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=os.path.join(d, "chk")),
                         known=[(v, t) for t, vals in listed.items() for v in vals])
        res["egress_forms"] = res["egress_caught"] = 0
        for t, vals in listed.items():
            for v in vals:
                for fm in forms(v, t):
                    for s in ("Please note: %s." % fm, json.dumps({"contact": fm}), json.dumps(json.dumps({"q": fm}))):
                        res["egress_forms"] += 1
                        res["egress_caught"] += chk.leftover_known(s) > 0
        # the skeleton layer of the re-check on its own (no matcher rules at all): letters and digits only
        res["skeleton_forms"] = res["skeleton_caught"] = 0
        for t, vals in listed.items():
            for v in vals:
                for fm in forms(v, t):
                    for s in ("Please note: %s." % fm, json.dumps({"contact": fm})):
                        res["skeleton_forms"] += 1
                        res["skeleton_caught"] += bool(chk.skeleton_hits(s))
        res["skeleton_false"] = sum(bool(chk.skeleton_hits(n)) for n in negatives)
        # a multi-line value restored inside JSON inside JSON (json.dumps of json.dumps) keeps both levels valid
        res["nested_json"] = res["nested_json_ok"] = 0
        for t, vals in listed.items():
            for v in vals:
                if " " not in v:
                    continue
                ml = v.replace(" ", "\n", 1)
                for doc in (json.dumps({"body": json.dumps({"note": "re: " + ml})}),
                            json.dumps({"a": json.dumps({"b": json.dumps({"note": ml})})})):
                    res["nested_json"] += 1
                    fo = c.Forms()
                    sc = chk.scrub_text(doc, forms=fo)
                    back = chk.restore_text(sc, forms=fo)
                    try:
                        o = json.loads(back)
                        while isinstance(o, dict) and "note" not in o:
                            o = json.loads(next(iter(o.values())))
                        res["nested_json_ok"] += sc != doc and o["note"].endswith(ml)
                    except (ValueError, StopIteration, KeyError, TypeError):
                        pass
        # a multi-line value restored inside a JSON string that sits in a ```json fence or inline in prose keeps that
        # JSON valid, and JSON the user pasted (newlines written as \n) comes back byte-exact
        res["fenced_json"] = res["fenced_json_ok"] = 0
        for t, vals in listed.items():
            for v in vals:
                if " " not in v:
                    continue
                ml = v.replace(", ", "\n") if t == "ADDRESS" else v.replace(" ", "\n", 1)
                fo = c.Forms()
                sc = chk.scrub_text("Extract as JSON:\n" + ml + "\nthanks", forms=fo)
                ph = re.findall("\u00ab[A-Z_]+_\\d+\u00bb", sc)
                pasted = "Fix this:\n```json\n" + json.dumps({"note": ml}) + "\n```"
                for kind in ("fence", "inline", "pasted"):
                    res["fenced_json"] += 1
                    try:
                        if kind == "pasted":
                            fo2 = c.Forms()
                            back = chk.restore_text(chk.scrub_text(pasted, forms=fo2), forms=fo2)
                            blk = back[back.index("{"):back.rindex("}") + 1]
                            res["fenced_json_ok"] += json.loads(blk)["note"] == ml
                            continue
                        doc = json.dumps({"value": ph[0], "n": 1}, ensure_ascii=False)
                        reply = ("Here it is:\n```json\n%s\n```\nDone." % doc if kind == "fence" else
                                 "The record is %s as asked." % doc)
                        back = chk.restore_text(reply, forms=fo)
                        blk = back[back.index("{"):back.rindex("}") + 1]
                        res["fenced_json_ok"] += json.loads(blk)["value"] == ml
                    except (ValueError, IndexError, KeyError):
                        pass
        # a listed address whose lines are joined by an HTML tag plus a line break, inside a json.dumps tool result, as
        # a literal \n four JSON string levels deep, and in tool-call arguments in the history: swapped whole (not
        # blocked, not split by the address pattern)
        res["tag_joined"] = res["tag_joined_swapped"] = res["tag_joined_leaked"] = 0
        for t, vals in listed.items():
            if t != "ADDRESS":
                continue
            for v in vals:
                lines = v.split("\n") if "\n" in v else v.split(", ")
                for j in HTML_JOINS + ["\\n"]:
                    txt = "Please forward this to %s today." % j.join(lines)
                    deep = json.dumps({"data": json.dumps({"rows": [json.dumps({"v": txt})]})})
                    for doc in (json.dumps({"result": txt}), json.dumps({"result": txt}, ensure_ascii=False), deep):
                        body = {"messages": [{"role": "user", "content": "look it up"},
                                             {"role": "assistant", "content": None, "tool_calls": [{"id": "t0",
                                              "type": "function", "function": {"name": "send_note", "arguments": doc}}]},
                                             {"role": "tool", "tool_call_id": "t0", "content": doc}]}
                        res["tag_joined"] += 1
                        try:
                            chk.scrub_request(body)
                        except c.ScrubError:
                            continue
                        if leaked(body, v, t):
                            res["tag_joined_leaked"] += 1
                        else:
                            res["tag_joined_swapped"] += 1
        # a placeholder the model wrote ASCII-escaped inside a JSON string inside the tool-call arguments
        # (\\u00abADDRESS_1\\u00bb, one level deeper than \u00ab) is restored, and both JSON levels stay valid
        res["nested_args"] = res["nested_args_ok"] = 0
        for t, vals in listed.items():
            for v in vals:
                fo = c.Forms()
                ph = re.findall("\u00ab[A-Z_]+_\\d+\u00bb", chk.scrub_text("Please note: %s." % v, forms=fo))
                if not ph:
                    continue
                for ea in (True, False):
                    args = json.dumps({"to": ph[0], "nested": json.dumps({"a": ph[0]}, ensure_ascii=ea)}, ensure_ascii=ea)
                    res["nested_args"] += 1
                    try:
                        o = json.loads(chk.restore_text(args, js=True, forms=fo))
                        res["nested_args_ok"] += o["to"] == v and json.loads(o["nested"])["a"] == v
                    except (ValueError, KeyError, TypeError):
                        pass
        # review #7: a multi-line address or tab-joined name in JSON nested 1 to 8 levels deep (a tool result built by
        # json.dumps of json.dumps ...) comes back escaped once per level, so every level parses and the value is
        # byte-exact (plain and streamed); deeper than 8 levels is blocked, not sent; a dictionary-word name is not
        # joined across a sentence end or separate HTML elements; a missing or broken config is one readable line
        rv = c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=os.path.join(d, "r7")), known=list(REVIEW7_KNOWN))

        def _nest(x, k, ea=True):
            for _ in range(k):
                x = json.dumps({"v": x}, ensure_ascii=ea)
            return x

        def _unnest(x, k):
            for _ in range(k):
                x = json.loads(x)["v"]
            return x
        res["deep_json"] = res["deep_json_ok"] = res["past_cap"] = res["past_cap_blocked"] = 0
        for v in REVIEW7_DEEP:
            for k in range(1, 9):
                for ea in (True, False):
                    body = {"messages": [{"role": "user", "content": "look it up"},
                                         {"role": "tool", "tool_call_id": "t0", "content": _nest(v, k, ea)}]}
                    fo = c.Forms()
                    try:
                        rv.scrub_request(body, forms=fo)
                    except c.ScrubError:
                        res["deep_json"] += 2
                        continue
                    up = body["messages"][-1]["content"]
                    for streamed in (False, True):
                        res["deep_json"] += 1
                        if streamed:
                            sr, parts = rv.stream_restorer(forms=fo), []
                            for i in range(0, len(up), 3):
                                parts.append(sr.feed(up[i:i + 3]))
                            back = "".join(parts) + sr.flush()
                        else:
                            back = rv.restore_obj({"content": up}, forms=fo)["content"]
                        orig = _nest(v, k, ea)
                        try:                     # ASCII values come back byte for byte; any value parses at every level
                            res["deep_json_ok"] += up != orig and _unnest(back, k) == v and (back == orig or not v.isascii())
                        except (ValueError, KeyError, TypeError):
                            pass
            for k in (9, 10, 12):
                body = {"messages": [{"role": "tool", "tool_call_id": "t0", "content": _nest(v, k)}]}
                res["past_cap"] += 1
                try:
                    rv.scrub_request(body)
                except c.ScrubError:
                    res["past_cap_blocked"] += 1
        big = {"messages": [{"role": "tool", "tool_call_id": "t0", "content": _nest(
            "Contact: " + REVIEW7_DEEP[0] + " " + "status ok, nothing to report. " * 1500, 16)}]}
        tb = time.time()
        res["past_cap"] += 1
        try:
            rv.scrub_request(big)
        except c.ScrubError:
            res["past_cap_blocked"] += 1
        res["deep_body_seconds"] = round(time.time() - tb, 2)
        res["name_breaks"] = res["name_breaks_ok"] = 0
        for txt, want in REVIEW7_NAME_TEXTS:
            body = {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": txt}]}
            res["name_breaks"] += 1
            try:
                rv.scrub_request(body)
                got = "swapped" if body["messages"][-1]["content"] != txt else "untouched"
            except c.ScrubError:
                got = "blocked"
            res["name_breaks_ok"] += got == want
        res["config_errors"] = res["config_errors_ok"] = 0
        for name, txt in (("missing.yaml", None), ("broken.yaml", "listen: [\n"), ("list.yaml", "- a\n- b\n"),
                          ("timeout_word.yaml", "scrub_timeout_s: banana\n"), ("timeout_neg.yaml", "scrub_timeout_s: -1\n")):
            cp = os.path.join(d, name)
            if txt is not None:
                with open(cp, "w") as f:
                    f.write(txt)
            res["config_errors"] += 1
            try:
                c.load_cfg(cp)
            except Exception as e:           # a ConfigError: one line naming the file (a traceback is a fail)
                res["config_errors_ok"] += isinstance(e, getattr(c, "ConfigError", ())) and "\n" not in str(e) and name in str(e)
        # review #8: tool-call arguments whose outer level is ASCII-escaped (json.dumps default) around raw inner JSON
        # (or the other way round, or alternating) get the value escaped for the level it really sits at
        r8 = c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=os.path.join(d, "r8")), known=list(REVIEW8_KNOWN))
        res["mixed_args"] = res["mixed_args_ok"] = 0
        for v in (REVIEW8_ADDR, "Odile\tMarchetti"):
            fo = c.Forms()
            tb8 = {"messages": [{"role": "user", "content": "x"},
                                {"role": "tool", "tool_call_id": "t0", "content": json.dumps({"v": v})}]}
            r8.scrub_request(tb8, forms=fo)
            ph = json.loads(tb8["messages"][-1]["content"])["v"]
            for inner in range(1, 9):
                for mix in ("outer_ascii", "inner_ascii", "alternate"):
                    x = ph
                    for i in range(inner):
                        x = json.dumps({"v": x}, ensure_ascii=(mix == "inner_ascii" or (mix == "alternate" and i % 2 == 0)))
                    args = json.dumps({"path": "a.json", "content": x}, ensure_ascii=(mix != "inner_ascii"))
                    for streamed in (False, True):
                        res["mixed_args"] += 1
                        if streamed:
                            ts = c.ToolArgsStream(r8, True, None, fo, "write_file")
                            back = "".join(ts.feed(args[i:i + 3]) for i in range(0, len(args), 3)) + ts.flush()
                        else:
                            back = r8.restore_text(args, js=True, forms=fo)
                        try:
                            y = json.loads(back)["content"]
                            for _ in range(inner):
                                y = json.loads(y)["v"]
                            res["mixed_args_ok"] += y == v
                        except (ValueError, KeyError, TypeError):
                            pass
        # \u00ab-escaped placeholders in a text reply (a ```json fence, inline JSON, two levels) are restored
        res["esc_text"] = res["esc_text_ok"] = 0
        fo = c.Forms()
        tb8 = {"messages": [{"role": "tool", "tool_call_id": "t0", "content": json.dumps({"v": REVIEW8_ADDR})}]}
        r8.scrub_request(tb8, forms=fo)
        ph = json.loads(tb8["messages"][-1]["content"])["v"]
        e1 = json.dumps(ph)[1:-1]
        shapes = [("```json\n{\"address\": \"%s\"}\n```" % e1, 1), ('Saved: {"address": "%s"} to the file.' % e1, 1),
                  ("```json\n%s\n```" % json.dumps({"doc": json.dumps({"address": ph})}), 2)]
        for txt, lv in shapes:
            for streamed in (False, True):
                res["esc_text"] += 1
                if streamed:
                    sr = r8.stream_restorer(forms=fo)
                    back = "".join(sr.feed(txt[i:i + 4]) for i in range(0, len(txt), 4)) + sr.flush()
                else:
                    back = r8.restore_text(txt, forms=fo)
                try:
                    y = json.loads(back[back.index("{"):back.rindex("}") + 1])
                    y = y["address"] if lv == 1 else json.loads(y["doc"])["address"]
                    res["esc_text_ok"] += y == REVIEW8_ADDR
                except (ValueError, KeyError, TypeError):
                    pass
        # a dictionary-word name whose words each fill one HTML block (first/last-name columns, list items, divs)
        res["cells"] = res["cells_ok"] = 0
        for txt, want in REVIEW8_CELLS:
            body = {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": txt}]}
            res["cells"] += 1
            try:
                r8.scrub_request(body)
                got = "swapped" if body["messages"][-1]["content"] != txt else "untouched"
            except c.ScrubError:
                got = "blocked"
            res["cells_ok"] += got == want or (want == "not swapped" and got != "swapped")
        # review #9: the listed dictionary-word name in one-word cells/blocks wherever it sits in the row or list
        r9 = c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=os.path.join(d, "r9")), known=list(REVIEW9_KNOWN))
        res["r9_cells"] = res["r9_cells_ok"] = 0
        for lt in REVIEW9_LAYOUTS:
            for fw, lw in (("Holly", "Marsh"), ("Jasper", "Stone"), ("Thomasina", "Wexley")):
                txt = lt.format(F=fw, L=lw, f=fw.lower(), l=lw.lower(), LU=lw.upper())
                for shape in ("chat", "tool", "email"):
                    if shape == "chat":
                        content = txt
                    elif shape == "tool":
                        content = json.dumps({"body_html": txt})
                    else:
                        content = json.dumps({"messages": [{"id": "m1", "body_html": "<html><body><p>Hi,</p>" + txt
                                                            + "<p>Thanks</p></body></html>"}]})
                    body = {"messages": [{"role": "user", "content": "read"},
                                         {"role": "tool", "tool_call_id": "c", "content": content}]}
                    res["r9_cells"] += 1
                    try:
                        r9.scrub_request(body)
                    except c.ScrubError:
                        continue
                    sent = body["messages"][-1]["content"]
                    res["r9_cells_ok"] += not (re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % fw, sent, re.I)
                                               and re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % lw, sent, re.I))
        for txt in REVIEW9_ORDINARY:              # one-word blocks that do not spell a listed name, inside one row/list
            body = {"messages": [{"role": "user", "content": txt}]}
            res["r9_cells"] += 1
            try:
                r9.scrub_request(body)
                res["r9_cells_ok"] += body["messages"][-1]["content"] == txt
            except c.ScrubError:
                pass
        # the note's example placeholders are slot 0, never assigned, so a model copying them gets no real value back
        res["r9_note"] = res["r9_note_ok"] = 0
        fo9 = c.Forms()
        r9.scrub_request({"messages": [{"role": "user", "content": "Holly Marsh, Jasper Stone and Thomasina Wexley "
                                                                   "wrote to l.varga@quillfeather.example"}]}, forms=fo9)
        ex = re.findall(c.L + r"[A-Z]+_\d+" + c.R, c.DEFAULTS["system_note"])
        res["r9_note"] += 2
        res["r9_note_ok"] += bool(ex) and all(x.endswith("_0" + c.R) for x in ex)
        back9 = r9.restore_text("You wrote %s per the note." % " and ".join(ex), forms=fo9)
        res["r9_note_ok"] += back9 == "You wrote %s per the note." % " and ".join(ex)
        res["r9_note"] += 1                       # a broken known file says its YAML is broken
        bk = os.path.join(d, "broken_known.yaml")
        with open(bk, "w") as f:
            f.write("PERSON:\n  - [unclosed\n")
        try:
            c.load_known(bk)
        except c.ScrubError as e:
            res["r9_note_ok"] += "not valid YAML" in str(e) and "\n" not in str(e)
        # review #10: values whose span holds escaped quotes or escaped markup come back byte-exact (not escaped twice);
        # the cell rule also reads HTML escaped inside JSON (\u003c Go, <\/td> PHP, &lt; entities); a name swapped
        # out of cells and reused elsewhere comes back as the listed name; a capital dotted I, a "> "-quoted address
        # and many copies in one long string
        r10a = "27 Gallowgate Mews\nUnit 9\nBurlington, VT 05401"
        r10 = c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=os.path.join(d, "r10")),
                         known=[(r10a, "ADDRESS"), ("Amber Lane", "PERSON"), ("Ottoline Frobisher", "PERSON"),
                                ("\u0130pek Y\u0131ld\u0131r\u0131mc\u0131", "PERSON"), ("amber.lane@northgable.example", "EMAIL")])

        def r10_round(content):
            fo = c.Forms()
            body = {"messages": [{"role": "user", "content": "read"},
                                 {"role": "tool", "tool_call_id": "c", "content": content}]}
            try:
                r10.scrub_request(body, forms=fo)
            except c.ScrubError:
                return None, None, None
            sent = body["messages"][-1]["content"]
            return sent, r10.restore_text(sent, forms=fo), fo

        def r10_leak(sent):
            return any(re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % w, sent) for w in ("Gallowgate", "Ottoline", "Frobisher")) \
                or ("Amber" in sent and "Lane" in sent)
        res["r10_exact"] = res["r10_exact_ok"] = 0
        for html in REVIEW10_ATTR:
            for lv, ea in ((1, True), (1, False), (2, True), (2, False)):
                content = html
                for _ in range(lv):
                    content = json.dumps({"html": content}, ensure_ascii=ea)
                sent, back, _fo = r10_round(content)
                res["r10_exact"] += 1
                res["r10_exact_ok"] += sent is not None and not r10_leak(sent) and back == content
        res["r10_escaped"] = res["r10_escaped_ok"] = 0
        for lt in REVIEW9_LAYOUTS[:12]:
            row = lt.format(F="Amber", L="Lane", f="amber", l="lane", LU="LANE")
            plain = json.dumps({"html": row})
            for content in (plain.replace("<", "\\u003c").replace(">", "\\u003e"), plain.replace("/", "\\/"),
                            json.dumps({"html": row.replace("<", "&lt;").replace(">", "&gt;")})):
                sent, back, _fo = r10_round(content)
                res["r10_escaped"] += 1
                res["r10_escaped_ok"] += sent is not None and not r10_leak(sent) and back == content
        res["r10_more"] = res["r10_more_ok"] = 0
        for content in ("<table><tr><td>Amber</td><td>Lane</td><td>Tucson</td></tr></table>",
                        json.dumps({"html": '<td class="a">Amber</td><td class="b">Lane</td><td>Tucson</td>'})):
            sent, back, fo10 = r10_round(content)
            ph = c.PH_RE.search(sent or "")
            res["r10_more"] += 3
            if ph:
                res["r10_more_ok"] += back == content
                res["r10_more_ok"] += r10.restore_text(sent + "\nDear " + ph.group(0) + ",", forms=fo10).endswith("Dear Amber Lane,")
                res["r10_more_ok"] += r10.restore_text(json.dumps({"to_name": ph.group(0)}, ensure_ascii=False),
                                                       js=True, forms=fo10) == '{"to_name": "Amber Lane"}'
        for content in ("\u0130pek Y\u0131ld\u0131r\u0131mc\u0131 signed it.", json.dumps({"n": "\u0130pek Y\u0131ld\u0131r\u0131mc\u0131"}),
                        "On Monday she wrote:\n> " + r10a.replace("\n", "\n> ") + "\n\nthanks",
                        json.dumps({"body": "On Monday she wrote:\n> " + r10a.replace("\n", "\n> ")})):
            sent, back, _fo = r10_round(content)
            res["r10_more"] += 1
            res["r10_more_ok"] += sent is not None and "\u0130pek" not in sent and "Gallowgate" not in sent and back == content
        t10 = time.time()
        big = json.dumps({"v": ("lorem ipsum amber.lane@northgable.example ") * 9000})
        sent, back, _fo = r10_round(big)
        res["r10_many_seconds"] = round(time.time() - t10, 2)
        res["r10_more"] += 1
        res["r10_more_ok"] += sent is not None and "northgable" not in sent and back == big
        # review #11: the cell rule 7-8 JSON levels down (quoted attributes there are hundreds of backslashes long),
        # cells reused after another tag come back as the listed name, a name split by one comma or across CSV
        # columns, "i" + a combining dot, "| "-quoted lines, many copies 8 levels deep, and the per-request time limit
        r11a = "27 Gallowgate Mews\nUnit 9\nBurlington, VT 05401"
        r11 = c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=os.path.join(d, "r11")),
                         known=[(r11a, "ADDRESS"), ("Heath Cliff", "PERSON"), ("Faith Ridge", "PERSON"),
                                ("\u0130rem Kaya", "PERSON")])

        def r11_round(content, scr=r11):
            fo = c.Forms()
            body = {"messages": [{"role": "user", "content": "read"},
                                 {"role": "tool", "tool_call_id": "c", "content": content}]}
            try:
                scr.scrub_request(body, forms=fo)
            except c.ScrubError as e:
                return None, str(e), None
            sent = body["messages"][-1]["content"]
            return sent, scr.restore_text(sent, forms=fo), fo

        def r11_leak(sent):
            return any(w in sent for w in ("Heath", "Gallowgate", "Kaya")) or ("Faith" in sent and "Ridge" in sent)
        res["r11_deep"] = res["r11_deep_ok"] = 0
        for html in REVIEW11_DEEP:
            for lv in (7, 8):
                for enc in ("raw", "go", "php", "entities"):
                    content = html.replace("<", "&lt;").replace(">", "&gt;") if enc == "entities" else html
                    for i in range(lv):
                        content = json.dumps({"v": content})
                        if i == 0 and enc == "go":
                            content = content.replace("<", "\\u003c").replace(">", "\\u003e")
                        if i == 0 and enc == "php":
                            content = content.replace("/", "\\/")
                    sent, back, fo = r11_round(content)
                    res["r11_deep"] += 1
                    ok = sent is not None and not r11_leak(sent) and back == content
                    if ok:                          # streamed too, in 37-character chunks
                        rs = c.StreamRestorer(r11, forms=fo)
                        ok = "".join(rs.feed(sent[i:i + 37]) for i in range(0, len(sent), 37)) + rs.flush() == content
                    res["r11_deep_ok"] += ok
        res["r11_more"] = res["r11_more_ok"] = 0
        row = '<tr><td class="id">3</td><td class="first">Heath</td><td class="last">Cliff</td></tr>'
        sent, back, fo11 = r11_round(row)
        ph = c.PH_RE.search(sent or "")
        res["r11_more"] += 4
        if ph:
            p = ph.group(0)
            res["r11_more_ok"] += back == row
            res["r11_more_ok"] += r11.restore_text("<li>%s</li>" % p, forms=fo11) == "<li>Heath Cliff</li>"
            res["r11_more_ok"] += r11.restore_text("<p>%s</p>" % p, forms=fo11) == "<p>Heath Cliff</p>"
            res["r11_more_ok"] += r11.restore_text('<td class="first">%s</td>' % p, forms=fo11) == \
                '<td class="first">Heath</td><td class="last">Cliff</td>'
        for content in REVIEW11_TEXTS + [json.dumps({"v": x}) for x in REVIEW11_TEXTS] + [
                "| " + r11a.replace("\n", "\n| "), json.dumps({"body": "she wrote:\n| " + r11a.replace("\n", "\n| ")}),
                "| 3 | Faith | Ridge | Reno |", json.dumps({"md": "| id | first | last |\n|---|---|---|\n| 1 | Heath | Cliff |"})]:
            sent, back, _fo = r11_round(content)
            res["r11_more"] += 1
            res["r11_more_ok"] += sent is not None and not r11_leak(sent) and back == content
        deep = "\n---\n".join([r11a] * 400)                    # about 20 KB of copies, 8 JSON levels down
        for _ in range(8):
            deep = json.dumps({"v": deep})
        t11 = time.time()
        sent, back, _fo = r11_round(deep)
        res["r11_deep_copies_seconds"] = round(time.time() - t11, 2)
        res["r11_more"] += 1
        res["r11_more_ok"] += sent is not None and not r11_leak(sent) and back == deep
        tl = c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=os.path.join(d, "r11t"), scrub_timeout_s=0.05),
                        known=[(r11a, "ADDRESS")])
        sent, err, _fo = r11_round(deep * 3, tl)
        res["r11_more"] += 1
        res["r11_more_ok"] += sent is None and "scrub_timeout_s" in (err or "") and "\n" not in (err or "")
        # re-check #13 F1: no pattern hit starts inside a backslash escape. Go writes <, > and & as \\u003c, \\u003e,
        # \\u0026; an email or phone right after one is swapped whole, restored exactly, and the JSON stays valid
        r13 = c.Scrubber(dict(c.DEFAULTS, mode="on", state_dir=os.path.join(d, "r13")),
                         known=[("wpool@fenwright-mail.example", "EMAIL"), ("+1 415 555 0142", "PHONE")])
        res["r13_esc"] = res["r13_esc_ok"] = 0
        for val in ("wpool@fenwright-mail.example", "kdrum@larchmoor-post.example", "+1 415 555 0142",
                    "+1 206 555 0199"):
            for shape in ("<td>%s</td>", "<b>x</b>&%s", "a&amp;<i>%s</i>", "<%s>"):
                for lv in (1, 2, 3):
                    content = json.dumps({"v": shape % val}).replace("<", "\\u003c").replace(">", "\\u003e") \
                        .replace("&", "\\u0026")
                    for _ in range(lv - 1):
                        content = json.dumps({"v": content})
                    sent, back, _fo = r11_round(content, r13)
                    res["r13_esc"] += 1
                    try:
                        valid = sent is not None and json.loads(sent) is not None
                    except ValueError:
                        valid = False
                    res["r13_esc_ok"] += valid and val.split("@")[0][-6:] not in sent and back == content
        # a bad listen value or a port already in use: one readable line (exit 2), not a traceback
        import socket, subprocess
        res["listen_errors"] = res["listen_errors_ok"] = 0
        sk = socket.socket()
        sk.bind(("127.0.0.1", 0))
        sk.listen(1)
        try:
            for i, lst in enumerate(("127.0.0.1", "127.0.0.1:notaport", "127.0.0.1:%d" % sk.getsockname()[1])):
                lp = os.path.join(d, "listen%d.yaml" % i)
                with open(lp, "w") as f:
                    f.write("mode: on\nknown_file: %s\nstate_dir: %s\nlisten: '%s'\n"
                            % (os.path.join(d, "known.yaml"), os.path.join(d, "ls%d" % i), lst))
                res["listen_errors"] += 1
                try:
                    pr = subprocess.run([sys.executable, "-m", "swapback.proxy"], capture_output=True, text=True,
                                        timeout=30, env=dict(os.environ, SWAPBACK_CONFIG=lp))
                    res["listen_errors_ok"] += (pr.returncode == 2 and "Traceback" not in pr.stderr
                                                and len(pr.stderr.strip().splitlines()) == 1 and "listen" in pr.stderr)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            sk.close()
        # a large encoded body (200 KB of %XX) is checked in well under the client's patience, not in minutes
        big = {"messages": [{"role": "user", "content": "%41" * 66000}]}
        tb = time.time()
        try:
            chk.scrub_request(big)
        except c.ScrubError:
            pass
        res["large_body_seconds"] = round(time.time() - tb, 2)
        # health while running: the list file deleted, then broken, must read blocked (ok false), then ok again
        kp = os.path.join(d, "known.yaml")
        good = open(kp, encoding="utf-8").read()

        def hstat():
            with urllib.request.urlopen(base + "/scrubber/health", timeout=10) as r:
                h = json.loads(r.read().decode())
            return h.get("ok"), h.get("status")
        res["health_checks"], res["health_ok"] = 5, 0
        res["health_ok"] += hstat() == (True, "ok")
        with urllib.request.urlopen(base + "/scrubber/health", timeout=10) as r:     # "known": unique listed values
            res["health_ok"] += json.loads(r.read().decode()).get("known") == sum(len(x) for x in listed.values())
        os.remove(kp)
        res["health_ok"] += hstat() == (False, "blocked")
        with open(kp, "w", encoding="utf-8") as f:
            f.write("PERSON: [unclosed\n")
        res["health_ok"] += hstat() == (False, "blocked")
        with open(kp, "w", encoding="utf-8") as f:
            f.write(good)
        res["health_ok"] += hstat() == (True, "ok")
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
                       and res["decoded_round_trip_same"] == res["decoded_round_trips"]
                       and res["egress_caught"] == res["egress_forms"]
                       and res["nested_json_ok"] == res["nested_json"] and res["health_ok"] == res["health_checks"]
                       and res["fenced_json_ok"] == res["fenced_json"] and res["large_body_seconds"] < 10
                       and res["tag_joined_swapped"] == res["tag_joined"] and res["nested_args_ok"] == res["nested_args"]
                       and res["deep_json_ok"] == res["deep_json"] and res["past_cap_blocked"] == res["past_cap"]
                       and res["deep_body_seconds"] < 5 and res["name_breaks_ok"] == res["name_breaks"]
                       and res["config_errors_ok"] == res["config_errors"]
                       and res["mixed_args_ok"] == res["mixed_args"] and res["esc_text_ok"] == res["esc_text"]
                       and res["cells_ok"] == res["cells"] and res["listen_errors_ok"] == res["listen_errors"]
                       and res["r9_cells_ok"] == res["r9_cells"] and res["r9_note_ok"] == res["r9_note"]
                       and res["r10_exact_ok"] == res["r10_exact"] and res["r10_escaped_ok"] == res["r10_escaped"]
                       and res["r10_more_ok"] == res["r10_more"] and res["r10_many_seconds"] < 10
                       and res["r11_deep_ok"] == res["r11_deep"] and res["r11_more_ok"] == res["r11_more"]
                       and res["r13_esc_ok"] == res["r13_esc"]
                       and res["r11_deep_copies_seconds"] < 5
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
            print("  egress re-check alone (swap off) sees the form:   " + pct(res["egress_caught"], res["egress_forms"]), file=out)
            print("  skeleton layer alone (no matcher rules) sees it:  " + pct(res["skeleton_caught"], res["skeleton_forms"])
                  + "  (reported; ordinary sentences it flags: %d)" % res["skeleton_false"], file=out)
            print("  ordinary sentences left untouched:                " + pct(res["negatives_untouched"], res["negatives"]), file=out)
            print("  exact round trips:                                " + pct(res["round_trip_exact"], res["round_trips"]), file=out)
            print("  JSON-in-JSON multi-line restores, valid + exact:  " + pct(res["nested_json_ok"], res["nested_json"]), file=out)
            print("  JSON in a ```json fence / inline / pasted, valid:  " + pct(res["fenced_json_ok"], res["fenced_json"]), file=out)
            print("  addresses joined by <br>\\n etc. in JSON, swapped:  " + pct(res["tag_joined_swapped"], res["tag_joined"])
                  + "  (leaked: %d)" % res["tag_joined_leaked"], file=out)
            print("  doubly escaped placeholders in tool args restored: " + pct(res["nested_args_ok"], res["nested_args"]), file=out)
            print("  JSON nested 1-8 levels, every level valid + exact: " + pct(res["deep_json_ok"], res["deep_json"]), file=out)
            print("  nested 9+ levels (256+ backslashes), blocked:      " + pct(res["past_cap_blocked"], res["past_cap"])
                  + "  (16-level body: %.2f s)" % res["deep_body_seconds"], file=out)
            print("  dictionary-word names across sentences/HTML blocks: " + pct(res["name_breaks_ok"], res["name_breaks"]), file=out)
            print("  missing/broken config file, one readable line:     " + pct(res["config_errors_ok"], res["config_errors"]), file=out)
            print("  tool args, mixed escaping, 1-8 inner levels valid: " + pct(res["mixed_args_ok"], res["mixed_args"]), file=out)
            print("  \\u00ab-escaped placeholders in text JSON restored: " + pct(res["esc_text_ok"], res["esc_text"]), file=out)
            print("  dictionary-word names in one-word HTML cells:      " + pct(res["cells_ok"], res["cells"]), file=out)
            print("  bad listen / port in use, one readable line:       " + pct(res["listen_errors_ok"], res["listen_errors"]), file=out)
            print("  names in one-word cells anywhere in a row/list:    " + pct(res["r9_cells_ok"], res["r9_cells"]), file=out)
            print("  example placeholders unassigned, broken YAML said: " + pct(res["r9_note_ok"], res["r9_note"]), file=out)
            print("  quoted attributes between a value's parts, exact:  " + pct(res["r10_exact_ok"], res["r10_exact"]), file=out)
            print("  cell names in escaped HTML (\\u003c, <\\/, &lt;):    " + pct(res["r10_escaped_ok"], res["r10_escaped"]), file=out)
            print("  cell names reused, dotted I, quoted, many copies:  " + pct(res["r10_more_ok"], res["r10_more"])
                  + "  (9,000 copies: %.2f s)" % res["r10_many_seconds"], file=out)
            print("  cell names 7-8 JSON levels down, raw/Go/PHP/&lt;:  " + pct(res["r11_deep_ok"], res["r11_deep"]), file=out)
            print("  cells reused, comma/CSV/| names, dotted i, | quotes: " + pct(res["r11_more_ok"], res["r11_more"])
                  + "  (20 KB of copies 8 levels down: %.2f s)" % res["r11_deep_copies_seconds"], file=out)
            print("  emails/phones right after \\\\u003e, \\\\u0026, \\\\u003c:      " + pct(res["r13_esc_ok"], res["r13_esc"]), file=out)
            print("  200 KB %%XX body checked in:                       %.2f s" % res["large_body_seconds"], file=out)
            print("  health: blocked when the list breaks, known count: " + pct(res["health_ok"], res["health_checks"]), file=out)
            if res["decoded_round_trips"]:
                print("  %XX-encoded forms back as the same value, decoded: " + pct(res["decoded_round_trip_same"],
                                                                                 res["decoded_round_trips"]), file=out)
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
