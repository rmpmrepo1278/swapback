# Examples

All values here are invented.

| file | what it is |
|---|---|
| `quickstart.yaml` | minimal config: proxy on 127.0.0.1:8787 in front of the mock upstream on :8009 |
| `known.demo.yaml` | three made-up known values (a name, an email, a phone number) |
| `mock_upstream.py` | a fake OpenAI-compatible server that echoes the last user message and records what it received |
| `curl_demo.sh` | one plain and one streamed request through the proxy, then what the upstream saw |
| `smoke.py` | the same check as a self-contained script (used in CI) |

```bash
python examples/mock_upstream.py 8009 &
PII_SCRUBBER_CONFIG=examples/quickstart.yaml pii-scrubber-proxy &
sh examples/curl_demo.sh
kill %1 %2
```
