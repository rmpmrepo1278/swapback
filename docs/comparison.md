# Comparison with similar tools

Back to the [README](../README.md).

## Comparison

Other open-source projects do the same job. This table reflects each project's own README and docs as read on
2026-11-09; "unclear" means we could not confirm it there. Corrections are welcome.

| | **swapback** | [PasteGuard][pg] | [Maskit][mk] | [Veil][vl] | [piighost][ph] | [Kiji Privacy Proxy][kj] |
|---|---|---|---|---|---|---|
| License | MIT | Apache-2.0 | AGPL-3.0 | Apache-2.0 | MIT | Apache-2.0 |
| Stack | Python 3.11+, about 5,900 lines (302 KB), 3 deps, stdlib HTTP server | TypeScript on Bun + GLiNER detector service; Docker | Python engine + desktop app; Docker | Python 3.11+ (PyPI, Homebrew), about 630 KB of Python; httpx, cryptography, pyahocorasick | Python library (PyPI); HTTP proxy in a separate repo, piighost-api | Go server + Electron desktop app; Docker, systemd |
| Needs an ML model | No | Yes: GLiNER for names and places ([docs][pg-sem]) | Ships a local ONNX NER model | Optional: 46 MB ONNX NER | Optional: pluggable regex, NER or LLM detectors; exact-match needs none | Yes: local ONNX model, 26 PII types |
| Your own list of values | Yes: a YAML/JSON file you write (or build from a vCard/CSV export), typed, reloaded live | Yes: `denylist` patterns ([docs][pg-mask]) | Yes: custom words and regexes | Yes: seeded from your databases, fixtures, git history and schemas | Yes: `ExactMatchDetector` | Custom regex rules |
| Stand-ins | Typed placeholders (`«EMAIL_1»`) | Placeholders | Unclear | Pseudonyms | Typed placeholders (`<<EMAIL:1>>`) | Realistic dummy values |
| Restores values in replies | Yes, byte-exact as written | Yes ("supported placeholders") | Yes | Yes | Yes | Yes |
| Streaming restore | OpenAI Chat and Responses, Anthropic Messages SSE | Yes | Yes (SSE) | Yes for the clients it lists (Claude Code, Codex on Responses, Bedrock event streams) | Unclear | Unclear |
| Tool-call arguments | Real values for local tools; placeholders kept for web, shell and send-style tools and any call with a URL, by default; `tool_restore` allow/deny lists | Unclear | Unclear | Per-tool policy: web tools, unknown tools and network commands get pseudonyms (stricter default than swapback's); restored values escaped for shells and file types | Restored for tools (LangChain middleware); per-tool policy unclear | Unclear |
| On a scrub error | Fails closed (`503`) + egress re-check | Unclear | Unclear; documents a passthrough that forwards plaintext when masking is off or the app exits | Fails closed (`502`) + a second guard on the outgoing bytes (plain, encoded, transformed, truncated) | Unclear | Unclear |
| Tool-call egress policy (a prompt-injected `web_fetch`/`send_email` can't carry real values out) | Yes, default on, by tool-name globs for any agent; streamed tool arguments held until the call is complete | Unclear | Unclear | Yes, built around coding-agent tools and shell commands | Unclear | Unclear |
| OpenAI Responses API streams restored | Yes (`/v1/responses`, Codex) | Unclear | Unclear | Yes (Codex CLI) | Unclear | Unclear |
| Encoded spellings checked before sending | `\uXXXX`/`\n` escapes, nested JSON, HTML entities, `%XX`, `+` for space, NFD, zero-width, fullwidth; not base64 or shell transforms | Unclear | Unclear | Yes, wider: a 21-transform bypass matrix (byte dumps, base64, rot13, ...) in its regression suite | Unclear | Unclear |
| Fails closed on an empty or shrunk list, or a listed value left after the swap | Yes: blocks (empty), keeps old values + warns (shrunk), egress re-check in every spelling above | Unclear | Unclear | Fails closed on errors and leftovers; list-shrink guard unclear | Unclear | Unclear |
| Leak test you can re-run | `swapback selftest`: one command after `pip install`, made-up data in the package, about 10 s, prints the numbers above | No | No | Yes: bypass matrix and leak checks in its test suite, with real-traffic numbers in its README | No | No |
| APIs | OpenAI Chat and Responses, Anthropic Messages (JSON and streaming) | OpenAI Chat + Responses, Anthropic, Codex | OpenAI, DeepSeek, Anthropic | Anthropic Messages, OpenAI Chat + Responses, Bedrock, Vertex, more | OpenAI and Anthropic connectors; LangChain, Pydantic AI, LlamaIndex, Claude Code integrations | OpenAI; Anthropic named in its diagram |
| Beyond text | No; [hooks](guide.md#hooks) for spend caps and local fallback | Browser extension, dashboard, route-to-local-LLM mode | Image/PDF OCR redaction, browser extension, dashboard | Encrypted vault, DB seeding, shell-safe escaping, MCP DB tool | Agent-framework middleware, chat UI example | Desktop UI, Chrome extension |

**Where swapback fits:** a personal or business agent of any kind (not only a coding agent) whose sensitive data is a
list you can write down: your family, your contacts, your clients, your IDs. You want one small proxy you can read in
an afternoon, that works with whatever client or framework you use as long as it speaks one of the three APIs, never
needs a model, restores the exact original text where your tools need it, keeps placeholders where a tool could send
it out, and blocks rather than leaks. **Where others are stronger:** names you never listed (PasteGuard, Maskit and
Kiji, and the optional NER in Veil and piighost); coding agents on production data (Veil: database seeding, shell-safe
escaping, a wider transform-bypass suite, a stricter tool default, Bedrock and Vertex); images and documents (Maskit);
framework middleware (piighost); a desktop app (Kiji).

[pg]: https://github.com/sgasser/pasteguard
[pg-mask]: https://pasteguard.com/docs/concepts/mask-mode
[pg-sem]: https://github.com/sgasser/pasteguard#how-detection-works
[mk]: https://github.com/xiaYuTian11/maskit
[vl]: https://github.com/lemonadesaltbagel/veil
[ph]: https://github.com/Athroniaeth/piighost
[kj]: https://github.com/dataiku/kiji-proxy
