#!/bin/sh
# Send one plain and one streamed chat request through the proxy, then show what the upstream received.
# Run from the repo root after starting examples/mock_upstream.py and the proxy (see README "Quickstart").
set -eu
PROXY=${PROXY:-http://127.0.0.1:8787}
MSG='Draft a reply to Jane Q Doe (jdoe@corp.example) and ask her to call (425) 555-0182.'

echo "== reply your app gets back (values restored)"
curl -s "$PROXY/v1/chat/completions" -H "Content-Type: application/json" \
  -d "{\"model\":\"demo\",\"messages\":[{\"role\":\"user\",\"content\":\"$MSG\"}]}"
echo; echo

echo "== same request, streamed (SSE), restored chunk by chunk"
curl -sN "$PROXY/v1/chat/completions" -H "Content-Type: application/json" \
  -d "{\"model\":\"demo\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"$MSG\"}]}" \
  | sed -n 's/^data: //p' | python3 -c 'import sys,json
out=""
for l in sys.stdin:
    l=l.strip()
    if l and l!="[DONE]":
        for c in json.loads(l).get("choices",[]): out+=(c.get("delta") or {}).get("content") or ""
print(out)'
echo

echo "== what the upstream (the \"provider\") actually received"
tail -n 1 "${MOCK_SEEN:-mock_seen.jsonl}" | python3 -c 'import sys,json; print(json.loads(sys.stdin.read())["messages"][-1]["content"])'
