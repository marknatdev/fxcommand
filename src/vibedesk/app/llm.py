"""The LLM seam (spec §3.3): `OmniRoute` for the real OpenAI-compatible chat API, and
`ScriptedLLM` for deterministic tests."""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

BUSY = {429, 502, 503, 504}  # OmniRoute or the model behind it is overloaded: worth a retry
RETRY_WAITS = (2.0, 5.0, 10.0)  # seconds before each retry


class LlmError(Exception):
    pass


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] | str = field(default_factory=dict)
    id: str = ""

    def with_id(self, call_id: str) -> ToolCall:
        return self if self.id else ToolCall(self.name, self.arguments, call_id)

    def to_openai(self) -> dict[str, Any]:
        args = (
            json.dumps(self.arguments) if isinstance(self.arguments, dict) else str(self.arguments)
        )
        return {
            "id": self.id or "call_1",
            "type": "function",
            "function": {"name": self.name, "arguments": args},
        }

    @classmethod
    def from_openai(cls, raw: dict[str, Any], default_id: str = "call_1") -> ToolCall:
        fn = raw.get("function") or {}
        name = str(fn.get("name") or raw.get("name") or "")
        raw_args = fn.get("arguments", raw.get("arguments", {}))
        if isinstance(raw_args, str):
            text = raw_args.strip()
            if not text:
                args: dict[str, Any] | str = {}
            else:
                try:
                    parsed = json.loads(text)
                    args = parsed if isinstance(parsed, dict) else raw_args
                except ValueError:
                    args = raw_args
        elif isinstance(raw_args, dict):
            args = dict(raw_args)
        else:
            args = str(raw_args)
        return cls(name=name, arguments=args, id=str(raw.get("id") or default_id))


@dataclass(frozen=True)
class Turn:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: dict[str, int] | None = None

    def to_message(self, tool_calls: Sequence[ToolCall] | None = None) -> dict[str, Any]:
        calls = self.tool_calls if tool_calls is None else tuple(tool_calls)
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if calls:
            message["tool_calls"] = [c.to_openai() for c in calls]
        return message

    @classmethod
    def from_openai(cls, message: dict[str, Any], usage: dict[str, int] | None = None) -> Turn:
        content = message.get("content") or ""
        raw_calls = message.get("tool_calls") or []
        calls = tuple(
            c.with_id(f"call_{i}")
            if isinstance(c, ToolCall)
            else ToolCall.from_openai(c, f"call_{i}")
            for i, c in enumerate(raw_calls, start=1)
        )
        return cls(content=str(content), tool_calls=calls, usage=usage)


class Llm(Protocol):
    def chat(self, messages: list[dict]) -> str: ...


class OmniRoute:
    def __init__(self, url: str, key: str, model: str, timeout: float = 120.0) -> None:
        self.url, self.key, self.model, self.timeout = url.rstrip("/"), key, model, timeout

    def models(self) -> list[str]:
        data = self._call("GET", "/models")
        return sorted(m["id"] for m in data.get("data", []))

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> Turn:
        model = self.model or (self.models() or [""])[0]
        if not model:
            raise LlmError("OmniRoute lists no models; pick one in Settings")
        body: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": 0.2,
        }
        if tools:
            body["tools"] = tools
        data = self._call("POST", "/chat/completions", body)
        try:
            message = data["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError("message is not a dict")
        except (KeyError, IndexError, TypeError) as error:
            raise LlmError(f"unexpected reply from OmniRoute: {str(data)[:300]}") from error
        return Turn.from_openai(message, usage=data.get("usage"))

    def chat(self, messages: list[dict]) -> str:
        return self.complete(messages).content

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self.key:
            raise LlmError("no OmniRoute key yet: add it in Settings")
        headers = {"Authorization": f"Bearer {self.key}"}
        for wait in (*RETRY_WAITS, None):
            try:
                response = httpx.request(method, self.url + path, json=body, headers=headers,
                                         timeout=self.timeout)  # fmt: skip
            except httpx.HTTPError as error:
                raise LlmError(
                    f"can't reach OmniRoute at {self.url} ({error.__class__.__name__}). Is the "
                    "SSH tunnel up? The start script opens it."
                ) from error
            if response.status_code in BUSY and wait is not None:
                time.sleep(wait)  # the shared server is busy: try again shortly
                continue
            if response.status_code >= 400:
                raise LlmError(_explain_error(response))
            return response.json()
        raise AssertionError("unreachable")


def _explain_error(response: httpx.Response) -> str:
    text = response.text[:300]
    low = text.lower()
    if "resource_pressure" in low or "resource pressure" in low or "heap_pressure" in low:
        return (
            "OmniRoute is out of memory on the shared server (503 resource_pressure, even after "
            "retrying). Free memory or restart OmniRoute on the server, then try again."
        )
    return f"OmniRoute said {response.status_code}: {text}"


class ScriptedLLM:
    """Deterministic fake behind the same seam as `OmniRoute` (spec §3.3).

    Each scripted step may be:
    - a `str` (an assistant prose reply),
    - a `ToolCall` or sequence of `ToolCall`s (a tool-calling turn),
    - a `Turn` or OpenAI-shaped message `dict`,
    - an `Exception` to raise when reached.
    """

    def __init__(self, *steps: Any) -> None:
        self.steps = list(steps)
        self.calls: list[list[dict]] = []
        self.tools_seen: list[list[dict] | None] = []
        self._next_id = 1

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> Turn:
        self.calls.append(copy.deepcopy(messages))
        self.tools_seen.append(copy.deepcopy(tools))
        if not self.steps:
            raise LlmError("ScriptedLLM ran out of scripted turns")
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, type) and issubclass(step, BaseException):
            raise step("scripted error")
        return self._to_turn(step)

    def chat(self, messages: list[dict]) -> str:
        return self.complete(messages).content

    def _to_turn(self, step: Any) -> Turn:
        if isinstance(step, str):
            return Turn(content=step)
        if isinstance(step, Turn):
            return Turn(
                content=step.content,
                tool_calls=tuple(self._assign_id(c) for c in step.tool_calls),
                usage=step.usage,
            )
        if isinstance(step, ToolCall):
            return Turn(tool_calls=(self._assign_id(step),))
        if isinstance(step, list | tuple):
            return Turn(tool_calls=tuple(self._assign_id(c) for c in step))
        if isinstance(step, dict):
            turn = Turn.from_openai(step, usage=step.get("usage"))
            return Turn(
                content=turn.content,
                tool_calls=tuple(self._assign_id(c) for c in turn.tool_calls),
                usage=turn.usage,
            )
        raise TypeError(f"unsupported ScriptedLLM step: {step!r}")

    def _assign_id(self, call: ToolCall | dict) -> ToolCall:
        if isinstance(call, dict):
            call = ToolCall.from_openai(call, default_id="")
        if call.id:
            return call
        call_id = f"call_{self._next_id}"
        self._next_id += 1
        return call.with_id(call_id)


OmniRouteLLM = OmniRoute
ScriptedLlm = ScriptedLLM
