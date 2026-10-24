"""The Research Desk loop (spec §7.1): takes an owner message, calls the LLM with tool
definitions, executes tool calls, feeds results back, and repeats until the LLM answers in
prose, capped at 12 tool calls per message. Streams Server-Sent Events (`message`,
`tool_started`, `trial_progress`, `verdict`)."""

from __future__ import annotations

import copy
import inspect
import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from vibedesk.app.llm import Llm, OmniRoute, OmniRouteLLM, ScriptedLLM, ScriptedLlm, ToolCall, Turn
from vibedesk.app.research import normalize, system_prompt
from vibedesk.blocks.registry import LIBRARY
from vibedesk.blocks.spec import expand_grid, validate

MAX_TOOL_CALLS = 12
EVENT_TYPES = ("message", "tool_started", "trial_progress", "verdict")


@dataclass(frozen=True)
class Event:
    """One Server-Sent Event emitted by the Research Desk loop."""

    event: str
    data: dict[str, Any]

    def __post_init__(self) -> None:
        if self.event not in EVENT_TYPES:
            raise ValueError(f"unknown event type {self.event!r}; expected one of {EVENT_TYPES}")

    @property
    def type(self) -> str:
        return self.event

    def to_sse(self) -> str:
        """Render as a Server-Sent Event wire frame."""
        payload = json.dumps(self.data, allow_nan=False)
        return f"event: {self.event}\ndata: {payload}\n\n"


def sse_stream(events: Iterable[Event]) -> Iterator[str]:
    """Stream `Event` objects as Server-Sent Event wire frames."""
    for event in events:
        yield event.to_sse()


def parse_sse(raw: str | Iterable[str]) -> list[Event]:
    """Parse SSE frames (`event: ...\\ndata: ...\\n\\n`) back into `Event` objects."""
    text = raw if isinstance(raw, str) else "".join(raw)
    events: list[Event] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        event_name = "message"
        data_lines: list[str] = []
        for line in lines:
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if data_lines:
            events.append(Event(event=event_name, data=json.loads("\n".join(data_lines))))
    return events


@dataclass(frozen=True)
class Tool:
    """One callable tool exposed to the Research Desk LLM."""

    name: str
    fn: Callable[..., Any]
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Small registry of tools for the Research Desk loop (full tool set is CCC-44)."""

    def __init__(
        self,
        tools: Mapping[str, Callable[..., Any] | Tool] | Iterable[Tool] | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        if isinstance(tools, Mapping):
            for name, item in tools.items():
                if isinstance(item, Tool):
                    self.add(item)
                else:
                    self.register(name, item)
        elif tools is not None:
            for item in tools:
                self.add(item)

    def add(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool
        return tool

    def register(
        self,
        name: str,
        fn: Callable[..., Any] | None = None,
        *,
        description: str = "",
        parameters: dict[str, Any] | None = None,
    ) -> Any:
        def bind(func: Callable[..., Any]) -> Callable[..., Any]:
            doc = description or " ".join((func.__doc__ or "").strip().split())
            schema = parameters if parameters is not None else _infer_parameters(func)
            self.add(Tool(name=name, fn=func, description=doc, parameters=schema))
            return func

        return bind(fn) if fn is not None else bind

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]


def starter_tools() -> ToolRegistry:
    """Minimal default tool registry (full tool set is CCC-44)."""
    registry = ToolRegistry()

    @registry.register("list_blocks")
    def list_blocks() -> dict[str, Any]:
        """Return the Block Library."""
        return {"blocks": [{"name": b.name, "kind": b.kind.value} for b in LIBRARY.all()]}

    @registry.register("draft_spec")
    def check_spec(spec: dict[str, Any]) -> dict[str, Any]:
        """Validate a Strategy Spec and return its parameter grid size."""
        validated = validate(normalize(spec))
        return {
            "valid": True,
            "spec": validated.model_dump(mode="json"),
            "combinations": len(expand_grid(validated)),
        }

    return registry


def desk_loop(
    llm: Llm | Any,
    message: str,
    *,
    tools: ToolRegistry | Mapping[str, Callable[..., Any] | Tool] | Iterable[Tool] | None = None,
    history: list[dict[str, Any]] | None = None,
    system: str | None = None,
    max_tool_calls: int = MAX_TOOL_CALLS,
    _sink: list[dict[str, Any]] | None = None,
) -> Iterator[Event]:
    """Run one owner message through the Research Desk loop and yield SSE `Event`s.

    Repeats LLM call -> tool execution -> tool result feedback until the LLM answers in prose,
    capped at `max_tool_calls` (12) tool calls per message. When the cap is hit, stops and
    tells the owner plainly.
    """
    if max_tool_calls < 1:
        raise ValueError("max_tool_calls must be at least 1")
    registry = (
        starter_tools()
        if tools is None
        else tools
        if isinstance(tools, ToolRegistry)
        else ToolRegistry(tools)
    )
    prompt = system_prompt() if system is None else system
    prior = copy.deepcopy(history) if history else []
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompt},
        *prior,
        {"role": "user", "content": message},
    ]
    schemas = registry.schemas()
    calls_made = 0

    while calls_made < max_tool_calls:
        turn = _call_llm(llm, messages, schemas)
        if not turn.tool_calls:
            messages.append({"role": "assistant", "content": turn.content})
            if _sink is not None:
                _sink[:] = messages[1:]
            yield Event(
                "message",
                {
                    "role": "assistant",
                    "content": turn.content,
                    "text": turn.content,
                    "tool_calls": calls_made,
                    "capped": False,
                },
            )
            return

        batch = turn.tool_calls[: max_tool_calls - calls_made]
        messages.append(turn.to_message(batch))
        for call in batch:
            calls_made += 1
            args = call.arguments if isinstance(call.arguments, dict) else {"raw": call.arguments}
            yield Event(
                "tool_started",
                {
                    "id": call.id,
                    "tool": call.name,
                    "name": call.name,
                    "arguments": args,
                    "step": calls_made,
                },
            )
            sub_events, output = _execute_tool(registry, call)
            yield from sub_events
            content = output if isinstance(output, str) else json.dumps(output, allow_nan=False)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": content,
                }
            )

    notice = (
        f"Stopped after {max_tool_calls} tool calls (the limit per message). "
        "Tell me how you'd like to carry on."
    )
    messages.append({"role": "assistant", "content": notice})
    if _sink is not None:
        _sink[:] = messages[1:]
    yield Event(
        "message",
        {
            "role": "assistant",
            "content": notice,
            "text": notice,
            "tool_calls": calls_made,
            "capped": True,
        },
    )


class DeskLoop:
    """Stateful Research Desk loop that tracks conversation history across owner messages."""

    def __init__(
        self,
        llm: Llm | Any,
        tools: ToolRegistry
        | Mapping[str, Callable[..., Any] | Tool]
        | Iterable[Tool]
        | None = None,
        *,
        system: str | None = None,
        max_tool_calls: int = MAX_TOOL_CALLS,
    ) -> None:
        self.llm = llm
        self.tools = (
            starter_tools()
            if tools is None
            else tools
            if isinstance(tools, ToolRegistry)
            else ToolRegistry(tools)
        )
        self.system = system
        self.max_tool_calls = max_tool_calls
        self.history: list[dict[str, Any]] = []

    def stream(
        self, message: str, *, history: list[dict[str, Any]] | None = None
    ) -> Iterator[Event]:
        active = self.history if history is None else history
        sink = self.history if history is None else None
        return desk_loop(
            self.llm,
            message,
            tools=self.tools,
            history=active,
            system=self.system,
            max_tool_calls=self.max_tool_calls,
            _sink=sink,
        )

    chat = stream

    def sse(self, message: str, *, history: list[dict[str, Any]] | None = None) -> Iterator[str]:
        return sse_stream(self.stream(message, history=history))

    def run(self, message: str, *, history: list[dict[str, Any]] | None = None) -> list[Event]:
        return list(self.stream(message, history=history))


def _call_llm(llm: Any, messages: list[dict[str, Any]], schemas: list[dict[str, Any]]) -> Turn:
    if hasattr(llm, "complete"):
        return llm.complete(messages, schemas)
    return Turn(content=str(llm.chat(messages)))


def _execute_tool(registry: ToolRegistry, call: ToolCall) -> tuple[list[Event], Any]:
    tool = registry.get(call.name)
    if tool is None:
        return [], {"error": f"unknown tool: {call.name}"}
    if not isinstance(call.arguments, dict):
        return [], {"error": f"invalid arguments for {call.name}: {call.arguments}"}

    emitted: list[Event] = []

    def emit(event_type: str, data: dict[str, Any]) -> None:
        emitted.append(Event(event_type, dict(data)))

    kwargs = dict(call.arguments)
    params = inspect.signature(tool.fn).parameters
    if "emit" in params and "emit" not in kwargs:
        kwargs["emit"] = emit

    try:
        result = tool.fn(**kwargs)
    except Exception as error:
        return [], {"error": f"{error.__class__.__name__}: {error}"}

    has_progress = any(e.event == "trial_progress" for e in emitted)
    has_verdict = any(e.event == "verdict" for e in emitted)
    is_trial = call.name == "run_trial" or (isinstance(result, dict) and "verdict" in result)

    if is_trial and not has_progress:
        progress = {"tool": call.name, **call.arguments, "status": "completed"}
        verdict_idx = next((i for i, e in enumerate(emitted) if e.event == "verdict"), len(emitted))
        emitted.insert(verdict_idx, Event("trial_progress", progress))

    if isinstance(result, dict) and "verdict" in result and not has_verdict:
        emitted.append(Event("verdict", dict(result)))

    return emitted, result


def _infer_parameters(fn: Callable[..., Any]) -> dict[str, Any]:
    sig = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name == "emit" or param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        prop: dict[str, Any] = {"type": _json_type(param.annotation)}
        if param.default is inspect.Parameter.empty:
            required.append(name)
        else:
            prop["default"] = param.default
        properties[name] = prop
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _json_type(annotation: Any) -> str:
    if annotation in (int, "int"):
        return "integer"
    if annotation in (float, "float"):
        return "number"
    if annotation in (bool, "bool"):
        return "boolean"
    if annotation in (dict, "dict", dict[str, Any]):
        return "object"
    if annotation in (list, "list"):
        return "array"
    return "string"


__all__ = [
    "DeskLoop",
    "EVENT_TYPES",
    "Event",
    "MAX_TOOL_CALLS",
    "OmniRoute",
    "OmniRouteLLM",
    "ScriptedLLM",
    "ScriptedLlm",
    "Tool",
    "ToolCall",
    "ToolRegistry",
    "Turn",
    "desk_loop",
    "parse_sse",
    "sse_stream",
    "starter_tools",
]
