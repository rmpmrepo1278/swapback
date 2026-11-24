"""LangChain 1.0+ reversible PII middleware backed by Swapback.

Install the optional extra to use with LangChain:
    pip install 'swapback[langchain]'

Optional local multilingual NER (2.6-15.5 MB, Apache-2.0, powered by newmean.ai):
    pip install 'swapback[langchain,newmean]'

Unlike one-way redaction middleware, Swapback swaps PII for consistent typed stand-ins
(``«PERSON_1»``, ``«EMAIL_1»``, ...) only on the wire to the LLM, then restores original
values in model replies and allowed tool-call arguments while keeping placeholders in
egress-risk tool calls (``web_fetch``, ``curl``, ``bash``, etc.) according to Swapback's
``ToolPolicy``.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import os
import threading
from typing import Any, Callable, Iterable, Mapping

from .core import (
    DEFAULTS,
    Forms,
    ScrubError,
    Scrubber,
    ToolPolicy,
    load_cfg,
    load_known,
)

__all__ = [
    "SwapbackMiddleware",
    "swapback_middleware",
]

_INSTALL_HINT = (
    "swapback[langchain] requires langchain >= 1.0. "
    "Install it with: pip install 'swapback[langchain]'"
)

try:
    from langchain.agents.middleware import AgentMiddleware as _AgentMiddlewareBase  # type: ignore[import-not-found]

    _HAS_LANGCHAIN = True
except ImportError:  # pragma: no cover - covered via monkeypatch in tests
    _AgentMiddlewareBase = object
    _HAS_LANGCHAIN = False


def _require_langchain() -> Any:
    try:
        from langchain.agents.middleware import AgentMiddleware  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(_INSTALL_HINT) from exc
    return AgentMiddleware


def _make_system_message(text: str, template: Any = None) -> Any:
    """Create a SystemMessage matching LangChain if installed, else template/dict fallback."""
    if template is not None:
        return _clone_message(template, content=text)
    try:
        from langchain_core.messages import SystemMessage  # type: ignore[import-not-found]

        return SystemMessage(content=text)
    except ImportError:
        return {"role": "system", "content": text}


def _clone_message(msg: Any, **updates: Any) -> Any:
    if isinstance(msg, dict):
        out = dict(msg)
        out.update(updates)
        return out
    model_copy = getattr(msg, "model_copy", None)
    if callable(model_copy):
        return model_copy(update=updates)
    if dataclasses.is_dataclass(msg) and not isinstance(msg, type):
        return dataclasses.replace(msg, **updates)
    cloned = copy.copy(msg)
    for k, v in updates.items():
        setattr(cloned, k, v)
    return cloned


def _message_role(msg: Any) -> str:
    if isinstance(msg, dict):
        raw = msg.get("role") or msg.get("type") or ""
    else:
        raw = getattr(msg, "type", None) or getattr(msg, "role", None) or ""
    r = str(raw).lower()
    if r in ("user", "human"):
        return "human"
    if r in ("assistant", "ai"):
        return "ai"
    if r == "tool":
        return "tool"
    if r == "system":
        return "system"
    return r


def _get_attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _append_note_to_content(content: Any, note: str) -> Any:
    if content is None or content == "":
        return note
    if isinstance(content, str):
        if note in content:
            return content
        return content + "\n\n" + note
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str) and note in item:
                return content
            if isinstance(item, dict) and isinstance(item.get("text"), str) and note in item["text"]:
                return content
        return list(content) + [{"type": "text", "text": note}]
    return content


def _build_scrubber(
    *,
    scrubber: Scrubber | None = None,
    cfg: Mapping[str, Any] | str | os.PathLike[str] | None = None,
    known: Iterable[tuple[str, str]] | str | os.PathLike[str] | None = None,
    private: Any = None,
    newmean: Any = False,
    extra_detectors: Iterable[Any] | None = None,
    vault_path: str | os.PathLike[str] | None = ":memory:",
    tool_restore: Any = None,
    patterns: Mapping[str, bool] | None = None,
    phone_regions: Iterable[str] | None = None,
    system_note: str | None = None,
) -> Scrubber:
    if scrubber is not None:
        conflict = any(
            x is not None and x is not False
            for x in (cfg, known, private, newmean, extra_detectors, tool_restore, patterns, phone_regions, system_note)
        )
        if conflict or vault_path != ":memory:":
            raise TypeError("pass either scrubber or scrubber configuration options, not both")
        return scrubber

    if isinstance(cfg, (str, os.PathLike)):
        base_cfg = load_cfg(str(cfg))
    elif cfg is not None:
        base_cfg = dict(DEFAULTS)
        base_cfg.update(cfg)
    else:
        base_cfg = dict(DEFAULTS)

    base_cfg["log_requests"] = False
    if newmean is not False and newmean is not None:
        base_cfg["newmean"] = newmean
    if tool_restore is not None:
        ToolPolicy(tool_restore)  # fail fast on bad policy
        base_cfg["tool_restore"] = tool_restore
    if patterns is not None:
        merged_pat = dict(base_cfg.get("patterns") or {})
        merged_pat.update(patterns)
        base_cfg["patterns"] = merged_pat
    if phone_regions is not None:
        base_cfg["phone_regions"] = list(phone_regions)
    if system_note is not None:
        base_cfg["system_note"] = system_note

    known_pairs: list[tuple[str, str]] | None
    if known is None:
        known_pairs = None if base_cfg.get("known_file") else []
    elif isinstance(known, (str, os.PathLike)):
        known_pairs = load_known(str(known))
    else:
        known_pairs = list(known)

    vp = None if vault_path is None else str(vault_path)
    return Scrubber(
        cfg=base_cfg,
        known=known_pairs,
        vault_path=vp,
        private=private,
        extra_detectors=list(extra_detectors or ()),
    )


class SwapbackMiddleware(_AgentMiddlewareBase):  # type: ignore[misc]
    """LangChain 1.0+ ``AgentMiddleware`` that reversibly swaps and restores PII around model calls.

    Works by intercepting ``wrap_model_call`` / ``awrap_model_call``:
    - Before the model runs: swaps PII in user inputs, tool results, prior assistant turns,
      and system prompts for consistent typed stand-ins (``«PERSON_1»``, ``«EMAIL_1»``, ...),
      without mutating the caller's message objects in graph state.
    - After the model returns: restores original values in the AI response, structured output,
      and allowed tool-call arguments while keeping placeholders in egress-risk tool calls
      (``web_fetch``, ``curl``, ``bash``, etc.) per Swapback's ``ToolPolicy``.
    """

    tools: tuple[Any, ...] = ()

    def __init__(
        self,
        *,
        scrubber: Scrubber | None = None,
        cfg: Mapping[str, Any] | str | os.PathLike[str] | None = None,
        known: Iterable[tuple[str, str]] | str | os.PathLike[str] | None = None,
        private: Any = None,
        newmean: Any = False,
        extra_detectors: Iterable[Any] | None = None,
        vault_path: str | os.PathLike[str] | None = ":memory:",
        tool_restore: Any = None,
        patterns: Mapping[str, bool] | None = None,
        phone_regions: Iterable[str] | None = None,
        system_note: str | None = None,
        apply_to_input: bool = True,
        apply_to_tool_results: bool = True,
        apply_to_system: bool = True,
        restore_output: bool = True,
        restore_tool_calls: bool = True,
        add_system_note: bool = True,
        fail_closed: bool = True,
        name: str = "SwapbackMiddleware",
    ) -> None:
        self.scrubber = _build_scrubber(
            scrubber=scrubber,
            cfg=cfg,
            known=known,
            private=private,
            newmean=newmean,
            extra_detectors=extra_detectors,
            vault_path=vault_path,
            tool_restore=tool_restore,
            patterns=patterns,
            phone_regions=phone_regions,
            system_note=system_note,
        )
        self.apply_to_input = bool(apply_to_input)
        self.apply_to_tool_results = bool(apply_to_tool_results)
        self.apply_to_system = bool(apply_to_system)
        self.restore_output = bool(restore_output)
        self.restore_tool_calls = bool(restore_tool_calls)
        self.add_system_note = bool(add_system_note)
        self.fail_closed = bool(fail_closed)
        self._name = str(name)
        self._stats_lock = threading.Lock()
        self._swapped: dict[str, int] = {}
        self._restored: int = 0
        self._tool_kept: int = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def stats(self) -> dict[str, Any]:
        """Counts and types only (never real values)."""
        with self._stats_lock:
            return {
                "swapped": dict(self._swapped),
                "restored": self._restored,
                "tool_kept": self._tool_kept,
                "vault_size": len(self.scrubber.rev),
            }

    def reset(self) -> None:
        """Clear the session vault and stats counters."""
        with self.scrubber.lock:
            self.scrubber.fwd.clear()
            self.scrubber.rev.clear()
            self.scrubber.ctr.clear()
            self.scrubber.cache.clear()
            self.scrubber.verified.clear()
            self.scrubber.dirty = False
        with self._stats_lock:
            self._swapped.clear()
            self._restored = 0
            self._tool_kept = 0

    def scrub_text(self, text: str) -> str:
        """Scrub a standalone string using this middleware's session vault."""
        counts: dict[str, int] = {}
        out = self.scrubber.scrub_text(text, counts=counts)
        self._record_counts(counts)
        return out

    def restore_text(self, text: str) -> str:
        """Restore placeholders in a standalone string using this middleware's session vault."""
        call_stats: dict[str, int] = {}
        out = self.scrubber.restore_text(text, stats=call_stats)
        self._record_restore(call_stats)
        return out

    def _record_counts(self, counts: Mapping[str, int]) -> None:
        if not counts:
            return
        with self._stats_lock:
            for k, v in counts.items():
                if isinstance(v, int) and v:
                    self._swapped[k] = self._swapped.get(k, 0) + v

    def _record_restore(self, call_stats: Mapping[str, int]) -> None:
        if not call_stats:
            return
        with self._stats_lock:
            self._restored += int(call_stats.get("restored", 0))
            self._tool_kept += int(call_stats.get("tool_kept", 0))

    # ------------------------------------------------------------ scrub side
    def _scrub_str(self, text: str, counts: dict[str, int], forms: Forms) -> str:
        if not text:
            return text
        s, u = self.scrubber._scrub(text, counts=counts, forms=forms)
        if self.fail_closed:
            h = hashlib.blake2b(
                (s if u is None else s + "\x00" + u).encode("utf-8", "surrogatepass"),
                digest_size=16,
            ).digest()
            if h not in self.scrubber.verified:
                left = self.scrubber.leftover_known(s)
                if not left and u is not None:
                    left = len(self.scrubber.skeleton_hits(u))
                if left:
                    raise ScrubError(
                        "egress check: %d known value(s) left after the swap" % left
                    )
                self.scrubber.verified.add(h)
        return s

    def _scrub_value(self, value: Any, counts: dict[str, int], forms: Forms) -> Any:
        if isinstance(value, str):
            return self._scrub_str(value, counts, forms)
        if isinstance(value, list):
            return [self._scrub_value(x, counts, forms) for x in value]
        if isinstance(value, tuple):
            return tuple(self._scrub_value(x, counts, forms) for x in value)
        if isinstance(value, dict):
            return {k: self._scrub_value(v, counts, forms) for k, v in value.items()}
        return value

    def _scrub_content(self, content: Any, counts: dict[str, int], forms: Forms) -> Any:
        if isinstance(content, str):
            return self._scrub_str(content, counts, forms)
        if isinstance(content, list):
            out = []
            for block in content:
                if isinstance(block, str):
                    out.append(self._scrub_str(block, counts, forms))
                elif isinstance(block, dict):
                    if isinstance(block.get("text"), str):
                        nb = dict(block)
                        nb["text"] = self._scrub_str(block["text"], counts, forms)
                        out.append(nb)
                    else:
                        out.append(block)
                else:
                    out.append(block)
            return out
        return content

    def _scrub_message(self, msg: Any, counts: dict[str, int], forms: Forms) -> Any:
        role = _message_role(msg)
        if role == "human" and not self.apply_to_input:
            return msg
        if role == "tool" and not self.apply_to_tool_results:
            return msg
        if role == "system" and not self.apply_to_system:
            return msg

        updates: dict[str, Any] = {}
        content = _get_attr(msg, "content")
        new_content = self._scrub_content(content, counts, forms)
        if new_content is not content:
            updates["content"] = new_content

        # Prior AI messages in graph state carry restored values from earlier turns;
        # re-scrub their tool_calls so those values do not leak on subsequent turns.
        if role == "ai":
            tool_calls = _get_attr(msg, "tool_calls")
            if tool_calls:
                new_tcs = []
                tc_changed = False
                for tc in tool_calls:
                    if isinstance(tc, dict) and "args" in tc and tc["args"] is not None:
                        new_args = self._scrub_value(tc["args"], counts, forms)
                        if new_args != tc["args"]:
                            ntc = dict(tc)
                            ntc["args"] = new_args
                            new_tcs.append(ntc)
                            tc_changed = True
                            continue
                    new_tcs.append(tc)
                if tc_changed:
                    updates["tool_calls"] = new_tcs

            add_kw = _get_attr(msg, "additional_kwargs")
            if isinstance(add_kw, dict) and add_kw.get("tool_calls"):
                new_raw_tcs = []
                raw_changed = False
                for tc in add_kw["tool_calls"]:
                    if isinstance(tc, dict) and isinstance(tc.get("function"), dict):
                        fn = tc["function"]
                        if isinstance(fn.get("arguments"), str):
                            scrubbed_args = self._scrub_str(fn["arguments"], counts, forms)
                            if scrubbed_args != fn["arguments"]:
                                ntc = dict(tc)
                                nfn = dict(fn)
                                nfn["arguments"] = scrubbed_args
                                ntc["function"] = nfn
                                new_raw_tcs.append(ntc)
                                raw_changed = True
                                continue
                    new_raw_tcs.append(tc)
                if raw_changed:
                    new_add_kw = dict(add_kw)
                    new_add_kw["tool_calls"] = new_raw_tcs
                    updates["additional_kwargs"] = new_add_kw

        return _clone_message(msg, **updates) if updates else msg

    def _prepare_request(self, request: Any) -> tuple[Any, Forms]:
        counts: dict[str, int] = {}
        forms = Forms()
        try:
            self.scrubber.reload_known()
            if self.scrubber.known_block:
                raise ScrubError(self.scrubber.known_block)

            overrides: dict[str, Any] = {}
            messages = getattr(request, "messages", None)
            if messages is not None:
                overrides["messages"] = [self._scrub_message(m, counts, forms) for m in messages]

            sys_msg = getattr(request, "system_message", None)
            if sys_msg is not None and self.apply_to_system:
                sys_msg = self._scrub_message(sys_msg, counts, forms)

            swapped = any(v for v in counts.values() if isinstance(v, int)) or bool(self.scrubber.rev)
            note = self.scrubber.cfg.get("system_note")
            if self.add_system_note and note and swapped:
                if sys_msg is not None:
                    new_sys_content = _append_note_to_content(_get_attr(sys_msg, "content"), note)
                    sys_msg = _clone_message(sys_msg, content=new_sys_content)
                else:
                    sys_msg = _make_system_message(note)

            if sys_msg is not getattr(request, "system_message", None):
                overrides["system_message"] = sys_msg

            self.scrubber.save_vault()
        except ScrubError:
            raise
        except Exception as exc:
            if self.fail_closed:
                raise ScrubError("scrub failed: %s" % type(exc).__name__) from exc
            raise

        self._record_counts(counts)
        if not overrides:
            return request, forms
        override_fn = getattr(request, "override", None)
        if callable(override_fn):
            return request.override(**overrides), forms
        cloned = copy.copy(request)
        for k, v in overrides.items():
            object.__setattr__(cloned, k, v)
        return cloned, forms

    # ---------------------------------------------------------- restore side
    def _restore_value(
        self,
        value: Any,
        forms: Forms,
        stats: dict[str, int],
        *,
        js: bool = False,
    ) -> Any:
        if isinstance(value, str):
            return self.scrubber.restore_text(value, js=js, stats=stats, forms=forms)
        if isinstance(value, list):
            return [self._restore_value(x, forms, stats, js=js) for x in value]
        if isinstance(value, tuple):
            return tuple(self._restore_value(x, forms, stats, js=js) for x in value)
        if isinstance(value, dict):
            return {k: self._restore_value(v, forms, stats, js=js) for k, v in value.items()}
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            updates = {}
            for f in dataclasses.fields(value):
                old_v = getattr(value, f.name)
                new_v = self._restore_value(old_v, forms, stats, js=js)
                if new_v != old_v:
                    updates[f.name] = new_v
            return dataclasses.replace(value, **updates) if updates else value
        model_dump = getattr(value, "model_dump", None)
        model_copy = getattr(value, "model_copy", None)
        if callable(model_dump) and callable(model_copy):
            dumped = model_dump()
            if isinstance(dumped, dict):
                restored_fields = {}
                for k in dumped:
                    if hasattr(value, k):
                        old_v = getattr(value, k)
                        new_v = self._restore_value(old_v, forms, stats, js=js)
                        if new_v != old_v:
                            restored_fields[k] = new_v
                return model_copy(update=restored_fields) if restored_fields else value
        return value

    def _restore_content(self, content: Any, forms: Forms, stats: dict[str, int]) -> Any:
        if isinstance(content, str):
            return self.scrubber.restore_text(content, stats=stats, forms=forms)
        if isinstance(content, list):
            out = []
            changed = False
            for block in content:
                if isinstance(block, str):
                    nb = self.scrubber.restore_text(block, stats=stats, forms=forms)
                    out.append(nb)
                    changed = changed or (nb != block)
                elif isinstance(block, dict) and isinstance(block.get("text"), str):
                    nt = self.scrubber.restore_text(block["text"], stats=stats, forms=forms)
                    if nt != block["text"]:
                        nb = dict(block)
                        nb["text"] = nt
                        out.append(nb)
                        changed = True
                    else:
                        out.append(block)
                else:
                    out.append(block)
            return out if changed else content
        return content

    def _restore_message(self, msg: Any, forms: Forms, stats: dict[str, int]) -> Any:
        updates: dict[str, Any] = {}
        if self.restore_output:
            content = _get_attr(msg, "content")
            new_content = self._restore_content(content, forms, stats)
            if new_content is not content and new_content != content:
                updates["content"] = new_content

        if self.restore_tool_calls:
            tool_calls = _get_attr(msg, "tool_calls")
            if tool_calls:
                new_tcs = []
                tc_changed = False
                for tc in tool_calls:
                    if isinstance(tc, dict) and "args" in tc and tc["args"] is not None:
                        tool_name = str(tc.get("name") or "")
                        raw_args = tc["args"]
                        if self.scrubber.tool_keep(tool_name, raw_args, stats=stats):
                            new_tcs.append(tc)
                            continue
                        restored_args = self._restore_value(
                            raw_args, forms, stats, js=isinstance(raw_args, str)
                        )
                        if restored_args != raw_args:
                            ntc = dict(tc)
                            ntc["args"] = restored_args
                            new_tcs.append(ntc)
                            tc_changed = True
                            continue
                    new_tcs.append(tc)
                if tc_changed:
                    updates["tool_calls"] = new_tcs

            add_kw = _get_attr(msg, "additional_kwargs")
            if isinstance(add_kw, dict) and add_kw.get("tool_calls"):
                new_raw_tcs = []
                raw_changed = False
                for tc in add_kw["tool_calls"]:
                    if isinstance(tc, dict) and isinstance(tc.get("function"), dict):
                        fn = tc["function"]
                        tool_name = str(fn.get("name") or "")
                        raw_args = fn.get("arguments")
                        if isinstance(raw_args, str):
                            if self.scrubber.tool_keep(tool_name, raw_args, stats=stats):
                                new_raw_tcs.append(tc)
                                continue
                            restored_args = self.scrubber.restore_text(
                                raw_args, js=True, stats=stats, forms=forms
                            )
                            if restored_args != raw_args:
                                ntc = dict(tc)
                                nfn = dict(fn)
                                nfn["arguments"] = restored_args
                                ntc["function"] = nfn
                                new_raw_tcs.append(ntc)
                                raw_changed = True
                                continue
                    new_raw_tcs.append(tc)
                if raw_changed:
                    new_add_kw = dict(add_kw)
                    new_add_kw["tool_calls"] = new_raw_tcs
                    updates["additional_kwargs"] = new_add_kw

        return _clone_message(msg, **updates) if updates else msg

    def _restore_response(self, response: Any, forms: Forms) -> Any:
        if not self.restore_output and not self.restore_tool_calls:
            return response
        if not self.scrubber.rev:
            return response

        call_stats: dict[str, int] = {}
        out = self._restore_response_inner(response, forms, call_stats)
        self._record_restore(call_stats)
        return out

    def _restore_response_inner(
        self, response: Any, forms: Forms, stats: dict[str, int]
    ) -> Any:
        # ExtendedModelResponse: carries model_response + command
        if hasattr(response, "model_response") and getattr(response, "model_response") is not None:
            inner = self._restore_response_inner(response.model_response, forms, stats)
            if inner is response.model_response:
                return response
            if dataclasses.is_dataclass(response) and not isinstance(response, type):
                return dataclasses.replace(response, model_response=inner)
            cloned = copy.copy(response)
            cloned.model_response = inner
            return cloned

        # ModelResponse: carries result: list[BaseMessage] + optional structured_response
        if hasattr(response, "result") and isinstance(getattr(response, "result"), list):
            orig_result = response.result
            new_result = [self._restore_message(m, forms, stats) for m in orig_result]
            result_changed = any(a is not b for a, b in zip(orig_result, new_result))

            orig_sr = getattr(response, "structured_response", None)
            new_sr = (
                self._restore_value(orig_sr, forms, stats)
                if (self.restore_output and orig_sr is not None)
                else orig_sr
            )
            sr_changed = new_sr is not orig_sr and new_sr != orig_sr

            if not result_changed and not sr_changed:
                return response
            if dataclasses.is_dataclass(response) and not isinstance(response, type):
                return dataclasses.replace(response, result=new_result, structured_response=new_sr)
            cloned = copy.copy(response)
            cloned.result = new_result
            if hasattr(cloned, "structured_response"):
                cloned.structured_response = new_sr
            return cloned

        # Direct AIMessage return
        return self._restore_message(response, forms, stats)

    # ---------------------------------------------------- AgentMiddleware API
    def wrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """Intercept a synchronous LangChain model call: swap PII out, restore on return."""
        scrubbed_request, forms = self._prepare_request(request)
        response = handler(scrubbed_request)
        return self._restore_response(response, forms)

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """Intercept an async LangChain model call: swap PII out, restore on return."""
        scrubbed_request, forms = self._prepare_request(request)
        response = await handler(scrubbed_request)
        return self._restore_response(response, forms)


def swapback_middleware(**kwargs: Any) -> SwapbackMiddleware:
    """Create a ``SwapbackMiddleware`` for LangChain 1.0+ ``create_agent``.

    Verifies that ``langchain >= 1.0`` is installed (raising a helpful ``ImportError`` if not)
    and returns a ``SwapbackMiddleware`` instance configured with ``kwargs``.
    """
    _require_langchain()
    return SwapbackMiddleware(**kwargs)
