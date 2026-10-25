"""The Research Desk (spec §7): tool-calling loop, SSE event stream, and ScriptedLLM seam."""

from vibedesk.desk.loop import (
    EVENT_TYPES,
    MAX_TOOL_CALLS,
    DeskLoop,
    Event,
    OmniRoute,
    OmniRouteLLM,
    ScriptedLLM,
    ScriptedLlm,
    Tool,
    ToolCall,
    ToolRegistry,
    Turn,
    desk_loop,
    parse_sse,
    sse_stream,
    starter_tools,
)

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
