# Using Swapback with LangChain

Swapback provides a reversible PII middleware (`SwapbackMiddleware`) for LangChain 1.0+ agents built with `create_agent`.

## Why reversible PII middleware?

LangChain's built-in `PIIMiddleware` is **one-way**: it redacts, masks, hashes, or blocks PII before the model sees it, and the original values are never put back. That works for logging or compliance filters, but breaks agents that need to draft replies with real names or pass real emails, phone numbers, and IDs into local tools.

`SwapbackMiddleware` is **reversible** and **fail-closed**:

| Capability | One-way `PIIMiddleware` | `SwapbackMiddleware` |
|---|---|---|
| Hides PII from the LLM provider | Yes | Yes |
| Keeps original values in graph state | No (mutates state) | Yes (swaps only on the wire to the model) |
| Restores real values in model replies | No | Yes (consistent typed stand-ins like `«PERSON_1»`, `«EMAIL_1»`) |
| Restores real values in tool-call arguments | No | Yes, with egress-tool protection (`web_fetch`, `curl`, `bash` keep placeholders by default) |
| Fail-closed egress re-check | Strategy-dependent | Yes (`ScrubError` before the model call if any known value remains) |
| Optional local multilingual NER | External detector | Built-in `swapback[newmean]` extra (2.6–15.5 MB, 54 languages, Apache-2.0) |

## Install

```bash
pip install 'swapback[langchain]'
```

To also catch unlisted person names, freeform addresses, organizations, and multilingual PII on-device without PyTorch or spaCy, install the optional detector powered by [newmean.ai](https://newmean.ai) `newmean-pii` (Apache-2.0):

```bash
pip install 'swapback[langchain,newmean]'
```

## Quickstart

```python
from langchain.agents import create_agent
from langchain.tools import tool
from swapback.langchain import SwapbackMiddleware


@tool
def lookup_order(customer_email: str) -> str:
    """Look up the latest order for a customer email."""
    return f"Order 123-4567890-1234567 for {customer_email}: ships tomorrow to 742 Evergreen Terrace."


agent = create_agent(
    "openai:gpt-5",
    tools=[lookup_order],
    middleware=[
        SwapbackMiddleware(
            known=[("Priya Nair", "PERSON")],
        )
    ],
)

result = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": "Check the order for Priya Nair (priya.nair@acme.io, 415-555-0192).",
            }
        ]
    }
)
print(result["messages"][-1].content)
```

What happens on each model call:

1. **Before the model runs (`wrap_model_call` / `awrap_model_call`)**: Swapback swaps detected PII in user messages, tool results, prior assistant turns, and system prompts for consistent typed placeholders (`«PERSON_1»`, `«EMAIL_1»`, `«PHONE_1»`, `«ADDRESS_1»`) on a copy of the request. The model never sees the raw values, and the original messages in graph state are not mutated.
2. **When the model emits a tool call**: `lookup_order(customer_email="«EMAIL_1»")` is restored to `lookup_order(customer_email="priya.nair@acme.io")` before the tool executes.
3. **When the tool returns PII**: On the next model turn, the `ToolMessage` (and prior restored `AIMessage`) are scrubbed before reaching the LLM again.
4. **Final reply**: Placeholders in the assistant's reply (and `structured_response`, if used) are restored byte-exact to the original values.

Async invocation (`await agent.ainvoke(...)`) works automatically via `awrap_model_call`.

## Pairing with `newmean-pii` (unlisted names & 54 languages)

By default, Swapback runs deterministic known-value matching plus pattern detectors (emails, phones, cards, IBANs, SSNs, IPs, street addresses, and API keys/secrets). To also catch names and PII you did not list upfront, pass `newmean`:

```python
from swapback.langchain import SwapbackMiddleware

middleware = SwapbackMiddleware(
    newmean="medium",  # "nano" (2.6 MB), "small" (7.1 MB), "medium" (11.3 MB), or "large" (15.5 MB)
)
```

Or pass a dictionary for fine-grained control over `newmean-pii` options (`sensitivity`, `labels`, `allow`, `block`, `strict_short_names`, `model_dir`, `cache_dir`):

```python
middleware = SwapbackMiddleware(
    known=[("Priya Nair", "PERSON")],  # deterministic + fail-closed egress re-check
    newmean={
        "size": "medium",
        "sensitivity": 0.0,
        "strict_short_names": True,
        "allow": ["Acme Corp"],
    },
)
```

*Optional local NER detector powered by [newmean.ai](https://newmean.ai) `newmean-pii` (Apache-2.0).*

## Protecting egress tools from prompt injection

If a prompt-injected model tries to pass a restored email or secret into a network or shell tool, Swapback's `ToolPolicy` keeps the placeholder in that tool's arguments instead of restoring the real value.

By default:
- Local/safe tools (e.g., `lookup_order`, `read_calendar`) receive **restored** real arguments.
- Egress-risk tool names (`web_fetch`, `http_*`, `curl`, `bash`, `exec`, `send_email`, `message`, `webhook`, etc.) and any tool call whose arguments contain a URL **keep placeholders**.

Customize the policy with `tool_restore`:

```python
middleware = SwapbackMiddleware(
    tool_restore={
        "default": "restore",
        "keep_if_url": True,
        "allow": ["send_email"],       # explicitly allow real values for this tool
        "deny": ["export_csv"],        # never restore real values for this tool
    }
)
```

Pass `tool_restore=False` to restore every tool call unconditionally.

## Vault lifecycle and observability

- **In-memory by default**: `SwapbackMiddleware()` uses `vault_path=":memory:"` so each instance keeps its stand-in mapping in RAM and writes nothing to disk. Call `middleware.reset()` between unrelated user sessions, or create one middleware instance per session.
- **Persistent vault**: Pass `vault_path="/path/to/vault.json"` (mode `0600`) or reuse a `swapback.Scrubber` loaded from `swapback.yaml` (`SwapbackMiddleware(cfg="swapback.yaml")`).
- **Value-free stats**: `middleware.stats` returns counts and entity types only (`swapped`, `restored`, `tool_kept`, `vault_size`), never raw PII values.

## Configuration reference

| Parameter | Default | Description |
|---|---|---|
| `scrubber` | `None` | Pre-configured `swapback.Scrubber` instance (mutually exclusive with inline scrubber options). |
| `cfg` | `None` | Swapback config dict or path to `swapback.yaml`. |
| `known` | `None` | List of `(value, TYPE)` pairs or path to `known.yaml`. |
| `newmean` | `False` | `True`, `"nano"|"small"|"medium"|"large"`, or dict of `NewmeanDetector` options. |
| `extra_detectors` | `None` | Additional detector objects (`.find(text)`) or callables returning `[(start, end, TYPE, value)]`. |
| `vault_path` | `":memory:"` | `":memory:"` for RAM-only session vault, or path to a local `vault.json`. |
| `tool_restore` | `None` | Override Swapback's `ToolPolicy` (`bool` or `dict`). |
| `apply_to_input` | `True` | Scrub `HumanMessage` content before model calls. |
| `apply_to_tool_results` | `True` | Scrub `ToolMessage` content before model calls. |
| `apply_to_system` | `True` | Scrub `SystemMessage` content before model calls. |
| `restore_output` | `True` | Restore placeholders in `AIMessage` content and `structured_response`. |
| `restore_tool_calls` | `True` | Restore placeholders in `AIMessage.tool_calls` (subject to `ToolPolicy`). |
| `add_system_note` | `True` | Append Swapback's brief stand-in note to the system prompt when PII is swapped. |
| `fail_closed` | `True` | Re-check scrubbed strings for known values and raise `ScrubError` before calling the model if any remain. |
| `name` | `"SwapbackMiddleware"` | Middleware name registered with LangChain. |

You can also use the `swapback_middleware(...)` convenience helper, which validates that `langchain >= 1.0` is installed and returns a `SwapbackMiddleware`.

## Streaming note and proxy alternative

`SwapbackMiddleware` hooks `wrap_model_call` / `awrap_model_call`, so `invoke()`, `ainvoke()`, and `stream(..., stream_mode="updates" | "values")` receive fully restored messages and tool calls.

If you stream individual LLM tokens directly to users with `stream_mode="messages"`, raw token chunks are emitted by the model callback before `wrap_model_call` sees the completed response, so mid-stream chunks still contain placeholders until the message finishes. For **byte-exact token-by-token streaming restoration**, run `swapback-proxy` locally and point your LangChain chat model's `base_url` at it instead:

```python
from langchain_openai import ChatOpenAI

model = ChatOpenAI(model="gpt-5", base_url="http://127.0.0.1:8787/v1")
```

Both modes share the same detection engine, optional `newmean-pii` backend, and `ToolPolicy` rules.
