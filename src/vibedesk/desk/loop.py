"""The Research Desk loop (spec §7.1): takes an owner message, calls the LLM with tool
definitions, executes tool calls, feeds results back, and repeats until the LLM answers in
prose, capped at 12 tool calls per message. Streams Server-Sent Events (`message`,
`tool_started`, `trial_progress`, `verdict`)."""

from __future__ import annotations

import copy
import inspect
import json
import math
import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from vibedesk.app.llm import (
    Llm,
    OmniRoute,
    OmniRouteLLM,
    ScriptedLLM,
    ScriptedLlm,
    ToolCall,
    Turn,
)
from vibedesk.app.research import system_prompt
from vibedesk.desk.budget import TokenBudget
from vibedesk.desk.grounding import GroundingError, render, validate

MAX_TOOL_CALLS = 12
EVENT_TYPES = ("message", "tool_started", "trial_progress", "verdict")


def _json_safe(value: Any) -> Any:
    """Make values JSON-encodable: NaN -> None, inf -> 'inf'/'-inf',
    numpy/collections/datetimes -> primitives/strings."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_json_safe(v) for v in value]
    if hasattr(value, "tolist") and callable(value.tolist):
        return _json_safe(value.tolist())
    if hasattr(value, "item") and callable(value.item):
        return _json_safe(value.item())
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return value
    if isinstance(value, int | str | bool | type(None)):
        return value
    if isinstance(value, os.PathLike) or (hasattr(value, "as_posix") and callable(value.as_posix)):
        return value.as_posix()
    return str(value)


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
        payload = json.dumps(_json_safe(self.data), allow_nan=False, default=str)
        return f"event: {self.event}\ndata: {payload}\n\n"


def sse_stream(events: Iterable[Event]) -> Iterator[str]:
    """Stream `Event` objects as Server-Sent Event wire frames."""
    try:
        for event in events:
            yield event.to_sse()
    except Exception as error:
        text = str(error)
        yield Event(
            "message",
            {
                "role": "error",
                "content": text,
                "text": text,
                "tool_calls": 0,
                "capped": False,
            },
        ).to_sse()


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
    """Registry of tools for the Research Desk loop."""

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

    def add(self, tool: Tool, *, replace: bool = False) -> Tool:
        if tool.name in self._tools and not replace:
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
        replace: bool = True,
    ) -> Any:
        def bind(func: Callable[..., Any]) -> Callable[..., Any]:
            doc = description or " ".join((func.__doc__ or "").strip().split())
            schema = parameters if parameters is not None else _infer_parameters(func)
            self.add(Tool(name=name, fn=func, description=doc, parameters=schema), replace=replace)
            return func

        return bind(fn) if fn is not None else bind

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]


def starter_tools(ctx: Any = None) -> ToolRegistry:
    """Default tool registry delegating to build_desk_tools (spec §7.2, CCC-44)."""
    from vibedesk.desk.tools import build_desk_tools

    return build_desk_tools(ctx)


def _record_and_check_budget(
    budget: TokenBudget | None,
    notifier: Any | None,
    usage: Any,
) -> bool:
    """Record token consumption, send warning at 80%, and check if paused at 100%."""
    if budget is None or not usage:
        return False
    budget.record_usage(usage)
    if budget.needs_warning and notifier is not None:
        from vibedesk.notify import Notice, NoticeKind

        notifier.send(
            Notice(
                kind=NoticeKind.TOKEN_WARNING,
                text=(
                    f"Daily token budget has reached 80% ({budget.used}/{budget.daily_cap} tokens)."
                ),
                data={"used": budget.used, "cap": budget.daily_cap, "state": budget.state().value},
            )
        )
        budget.mark_warned()
    return budget.is_paused


def desk_loop(
    llm: Llm | Any,
    message: str,
    *,
    tools: ToolRegistry | Mapping[str, Callable[..., Any] | Tool] | Iterable[Tool] | None = None,
    history: list[dict[str, Any]] | None = None,
    system: str | None = None,
    max_tool_calls: int = MAX_TOOL_CALLS,
    budget: TokenBudget | None = None,
    notifier: Any | None = None,
    lookup: Callable[[str, str, str], str | None] | None = None,
    _sink: list[dict[str, Any]] | None = None,
) -> Iterator[Event]:
    """Run one owner message through the Research Desk loop and yield SSE `Event`s.

    Repeats LLM call -> tool execution -> tool result feedback until the LLM answers in prose,
    capped at `max_tool_calls` (12) tool calls per message.

    Enforces:
    - Grounding Rule: validates assistant prose against raw digits, retries up to 2 times
      with correction messages, renders structured placeholders from stored data, or sends
      the verdict alone with an explanation withheld note (spec §7.3, ADR 0005).
    - Token Budget: counts tokens against daily cap, warns at 80% via Notifier, and pauses
      Desk loop at 100% (spec §7.4, CCC-46).
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

    ctx = getattr(registry, "ctx", None)
    if budget is None and ctx is not None:
        budget = getattr(ctx, "budget", None)
    if notifier is None and ctx is not None:
        notifier = getattr(ctx, "notifier", None)

    # Spec §7.4: at 100% of token budget, pause Desk loop explaining clearly to owner.
    # Manual Trials remain permitted.
    if budget is not None and budget.is_paused:
        pause_msg = (
            "The Research Desk is paused because the daily token budget has been reached (100%). "
            "Manual Trials are still permitted."
        )
        yield Event(
            "message",
            {
                "role": "assistant",
                "content": pause_msg,
                "text": pause_msg,
                "tool_calls": 0,
                "capped": False,
                "paused": True,
            },
        )
        return

    prompt = system_prompt() if system is None else system
    prior = copy.deepcopy(history) if history else []
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompt},
        *prior,
        {"role": "user", "content": message},
    ]
    schemas = registry.schemas()
    calls_made = 0
    executed_trials: dict[int, dict[str, Any]] = {}

    def get_lookup() -> Callable[[str, str, str], str | None]:
        if lookup is not None:
            return lookup
        ledger = getattr(ctx, "ledger", None) if ctx else None

        def _format_lookup_value(field: str, val: Any) -> str | None:
            if val is None:
                return None
            if field in ("pf", "profit_factor", "net_r", "expectancy_r", "max_drawdown_r"):
                try:
                    return f"{float(val):.2f}"
                except (ValueError, TypeError):
                    return str(val)
            if field in ("trades", "trade_count"):
                try:
                    return str(int(val))
                except (ValueError, TypeError):
                    return str(val)
            if isinstance(val, dict | list):
                return json.dumps(val)
            res = str(val)
            return None if res == "None" else res

        def default_lookup(kind: str, id_: str, field_name: str) -> str | None:
            if kind == "date":
                return id_
            if kind == "trial":
                record: dict[str, Any] | None = None
                try:
                    tid = int(id_)
                    if ledger is not None:
                        try:
                            tr = ledger.trial(tid)
                            record = json.loads(tr.result_json)
                            record["verdict"] = tr.verdict
                            record["closest_miss"] = tr.closest_miss
                        except Exception:
                            record = executed_trials.get(tid)
                    else:
                        record = executed_trials.get(tid)
                except Exception:
                    return None
                if record is None:
                    return None
                summary = record.get("summary", {})
                wf = record.get("walk_forward", {})
                metrics = wf.get("metrics", {})
                for source in (record, summary, wf, metrics):
                    if field_name in source:
                        return _format_lookup_value(field_name, source[field_name])
                if field_name in ("pf", "profit_factor"):
                    pf_val = summary.get(
                        "profit_factor", metrics.get("profit_factor", wf.get("profit_factor"))
                    )
                    return _format_lookup_value(field_name, pf_val)
                if field_name in ("trades", "trade_count"):
                    tr_val = summary.get("trades", metrics.get("trades", wf.get("trades")))
                    return _format_lookup_value(field_name, tr_val)
                if field_name == "net_r":
                    r_val = summary.get("net_r", metrics.get("net_r", wf.get("net_r")))
                    return _format_lookup_value(field_name, r_val)
                return None
            if kind == "hyp":
                if ledger is None:
                    return None
                try:
                    h = ledger.hypothesis(int(id_))
                    if field_name == "symbol":
                        return h.symbol
                    if field_name == "timeframe":
                        return h.timeframe
                    if field_name == "entry_block":
                        return h.entry_block
                    if field_name == "family_id":
                        return str(h.family_id)
                except Exception:
                    return None
            if kind == "spec":
                if ledger is None:
                    return None
                try:
                    h = ledger.hypothesis(int(id_))
                    s = json.loads(h.spec_json)
                    if field_name in s:
                        return _format_lookup_value(field_name, s[field_name])
                    if "." in field_name:
                        curr: Any = s
                        for part in field_name.split("."):
                            if isinstance(curr, dict) and part in curr:
                                curr = curr[part]
                            elif (
                                isinstance(curr, dict)
                                and "params" in curr
                                and part in curr["params"]
                            ):
                                curr = curr["params"][part]
                            else:
                                curr = None
                                break
                        if curr is not None:
                            return _format_lookup_value(field_name.split(".")[-1], curr)

                    def _find_param(data: Any, key: str) -> Any:
                        if isinstance(data, dict):
                            if key in data:
                                return data[key]
                            if (
                                "params" in data
                                and isinstance(data["params"], dict)
                                and key in data["params"]
                            ):
                                return data["params"][key]
                            for v in data.values():
                                found = _find_param(v, key)
                                if found is not None:
                                    return found
                        elif isinstance(data, list):
                            for item in data:
                                found = _find_param(item, key)
                                if found is not None:
                                    return found
                        return None

                    found_val = _find_param(s, field_name)
                    if found_val is not None:
                        return _format_lookup_value(field_name, found_val)
                except Exception:
                    return None
            return None

        return default_lookup

    try:
        while calls_made < max_tool_calls:
            turn = _call_llm(llm, messages, schemas)
            is_paused = _record_and_check_budget(budget, notifier, turn.usage)

            if not turn.tool_calls:
                # Assistant answered in prose. Enforce Grounding Rule (spec §7.3, ADR 0005).
                grounding_retries = 0
                active_lookup = get_lookup()
                current_content = turn.content

                while True:
                    problems = validate(current_content)
                    rendered_content: str | None = None
                    if not problems:
                        try:
                            rendered_content = render(current_content, active_lookup)
                        except GroundingError as g_err:
                            problems = [str(g_err)]

                    if not problems and rendered_content is not None:
                        messages.append({"role": "assistant", "content": rendered_content})
                        if _sink is not None:
                            _sink[:] = [m for m in messages if m.get("role") != "system"]
                        yield Event(
                            "message",
                            {
                                "role": "assistant",
                                "content": rendered_content,
                                "text": rendered_content,
                                "tool_calls": calls_made,
                                "capped": False,
                            },
                        )
                        if is_paused:
                            pause_msg = (
                                "The Research Desk is paused because the daily token budget has "
                                "been reached (100%). Manual Trials are still permitted."
                            )
                            yield Event(
                                "message",
                                {
                                    "role": "assistant",
                                    "content": pause_msg,
                                    "text": pause_msg,
                                    "tool_calls": calls_made,
                                    "capped": False,
                                    "paused": True,
                                },
                            )
                        return

                    # Validation or rendering failed
                    if grounding_retries < 2 and not is_paused:
                        grounding_retries += 1
                        messages.append({"role": "assistant", "content": current_content})
                        correction_msg = (
                            "Grounding Rule violation: Assistant prose may not contain raw figures "
                            "or digits. Every figure must use structured placeholders "
                            "like {{trial:ID.metric}}, {{spec:ID.param}}, {{hyp:ID.field}}, "
                            "{{date:ISO}}, or whitelisted tokens. "
                            f"Problems: {'; '.join(problems)}. "
                            "Please regenerate your response following the Grounding Rule."
                        )
                        messages.append({"role": "user", "content": correction_msg})
                        retry_turn = _call_llm(llm, messages, schemas)
                        is_paused = _record_and_check_budget(budget, notifier, retry_turn.usage)

                        if retry_turn.tool_calls:
                            turn = retry_turn
                            break
                        current_content = retry_turn.content
                    else:
                        # Retries exhausted or budget paused: show verdict alone with withheld note
                        withheld_msg = (
                            "The explanation was withheld because it did not adhere to the "
                            "Grounding Rule."
                        )
                        messages.append({"role": "assistant", "content": withheld_msg})
                        if _sink is not None:
                            _sink[:] = [m for m in messages if m.get("role") != "system"]
                        yield Event(
                            "message",
                            {
                                "role": "assistant",
                                "content": withheld_msg,
                                "text": withheld_msg,
                                "tool_calls": calls_made,
                                "capped": False,
                                "grounding_withheld": True,
                            },
                        )
                        if is_paused:
                            pause_msg = (
                                "The Research Desk is paused because the daily token budget has "
                                "been reached (100%). Manual Trials are still permitted."
                            )
                            yield Event(
                                "message",
                                {
                                    "role": "assistant",
                                    "content": pause_msg,
                                    "text": pause_msg,
                                    "tool_calls": calls_made,
                                    "capped": False,
                                    "paused": True,
                                },
                            )
                        return

            if is_paused:
                # Crossed 100% budget mid-message while requesting more tool calls.
                # Update _sink to preserve history up to this point and report pause.
                if _sink is not None:
                    _sink[:] = [m for m in messages if m.get("role") != "system"]
                pause_msg = (
                    "The Research Desk is paused because the daily token budget has been "
                    "reached (100%). Manual Trials are still permitted."
                )
                yield Event(
                    "message",
                    {
                        "role": "assistant",
                        "content": pause_msg,
                        "text": pause_msg,
                        "tool_calls": calls_made,
                        "capped": False,
                        "paused": True,
                    },
                )
                return

            batch = turn.tool_calls[: max_tool_calls - calls_made]
            messages.append(turn.to_message(batch))
            for call in batch:
                calls_made += 1
                args = (
                    call.arguments if isinstance(call.arguments, dict) else {"raw": call.arguments}
                )
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
                sub_events, content = _execute_tool(registry, call)
                for sub_event in sub_events:
                    if sub_event.event == "verdict":
                        tid = sub_event.data.get("trial_id")
                        if tid is not None:
                            rec = sub_event.data.get("record", sub_event.data)
                            executed_trials[tid] = dict(rec)
                    yield sub_event

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
    except Exception as error:
        text = str(error)
        yield Event(
            "message",
            {
                "role": "error",
                "content": text,
                "text": text,
                "tool_calls": calls_made,
                "capped": False,
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
        budget: TokenBudget | None = None,
        notifier: Any | None = None,
        lookup: Callable[[str, str, str], str | None] | None = None,
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
        self.budget = budget
        self.notifier = notifier
        self.lookup = lookup
        self.history: list[dict[str, Any]] = []

    def stream(
        self,
        message: str,
        *,
        history: list[dict[str, Any]] | None = None,
        budget: TokenBudget | None = None,
        notifier: Any | None = None,
        lookup: Callable[[str, str, str], str | None] | None = None,
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
            budget=budget or self.budget,
            notifier=notifier or self.notifier,
            lookup=lookup or self.lookup,
            _sink=sink,
        )

    chat = stream

    def sse(
        self,
        message: str,
        *,
        history: list[dict[str, Any]] | None = None,
        budget: TokenBudget | None = None,
        notifier: Any | None = None,
        lookup: Callable[[str, str, str], str | None] | None = None,
    ) -> Iterator[str]:
        return sse_stream(
            self.stream(message, history=history, budget=budget, notifier=notifier, lookup=lookup)
        )

    def run(
        self,
        message: str,
        *,
        history: list[dict[str, Any]] | None = None,
        budget: TokenBudget | None = None,
        notifier: Any | None = None,
        lookup: Callable[[str, str, str], str | None] | None = None,
    ) -> list[Event]:
        return list(
            self.stream(message, history=history, budget=budget, notifier=notifier, lookup=lookup)
        )


def _call_llm(llm: Any, messages: list[dict[str, Any]], schemas: list[dict[str, Any]]) -> Turn:
    if hasattr(llm, "complete"):
        return llm.complete(messages, schemas)
    return Turn(content=str(llm.chat(messages)))


def _execute_tool(registry: ToolRegistry, call: ToolCall) -> tuple[list[Event], str]:
    tool = registry.get(call.name)
    if tool is None:
        return [], json.dumps({"error": f"unknown tool: {call.name}"})
    if not isinstance(call.arguments, dict):
        return [], json.dumps({"error": f"invalid arguments for {call.name}: {call.arguments}"})

    emitted: list[Event] = []

    def emit(event_type: str, data: dict[str, Any]) -> None:
        emitted.append(Event(event_type, dict(_json_safe(data))))

    kwargs = dict(call.arguments)
    params = inspect.signature(tool.fn).parameters
    if "emit" in params and "emit" not in kwargs:
        kwargs["emit"] = emit

    try:
        result = tool.fn(**kwargs)
        safe_result = _json_safe(result)
        content = (
            safe_result
            if isinstance(safe_result, str)
            else json.dumps(safe_result, allow_nan=False, default=str)
        )
    except Exception as error:
        return [], json.dumps({"error": f"{error.__class__.__name__}: {error}"})

    has_progress = any(e.event == "trial_progress" for e in emitted)
    has_verdict = any(e.event == "verdict" for e in emitted)
    is_trial = call.name == "run_trial" or (isinstance(result, dict) and "verdict" in result)

    if is_trial and not has_progress:
        progress = _json_safe({"tool": call.name, **call.arguments, "status": "completed"})
        verdict_idx = next((i for i, e in enumerate(emitted) if e.event == "verdict"), len(emitted))
        emitted.insert(verdict_idx, Event("trial_progress", progress))

    if isinstance(result, dict) and "verdict" in result and not has_verdict:
        emitted.append(Event("verdict", _json_safe(dict(result))))

    safe_emitted = [Event(e.event, _json_safe(e.data)) for e in emitted]
    return safe_emitted, content


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
