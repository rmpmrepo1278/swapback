"""A tiny fake OpenAI-compatible upstream for trying the proxy without a real provider. It answers every chat request
by echoing the last user message ("You said: ..."), plain or streamed, and appends what it received to the file named
by MOCK_SEEN (default ./mock_seen.jsonl) so you can check what a provider would have seen.
usage: python examples/mock_upstream.py [port]   (default 8009)"""
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEEN = os.environ.get("MOCK_SEEN", "mock_seen.jsonl")


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        b = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with open(SEEN, "a", encoding="utf-8") as f:
            f.write(b.decode("utf-8") + "\n")
        j = json.loads(b or b"{}")
        last = next((m.get("content") for m in reversed(j.get("messages") or []) if m.get("role") == "user"), "")
        reply = "You said: %s" % last
        if j.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            parts = [reply[i:i + 7] for i in range(0, len(reply), 7)] or [""]
            for p in parts:
                d = "data: " + json.dumps({"id": "m", "object": "chat.completion.chunk", "model": j.get("model"),
                                           "choices": [{"index": 0, "delta": {"content": p}}]}) + "\n\n"
                self._chunk(d.encode())
            self._chunk(b"data: [DONE]\n\n")
            self._chunk(b"")
        else:
            o = json.dumps({"id": "m", "object": "chat.completion", "model": j.get("model"),
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                                         "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(o)))
            self.end_headers()
            self.wfile.write(o)

    def _chunk(self, b):
        self.wfile.write(b"%x\r\n%s\r\n" % (len(b), b))
        self.wfile.flush()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8009
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
