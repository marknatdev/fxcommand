"""The Research Desk: the owner's Idea, in chat, becomes one Strategy Spec built from the Block
Library. The AI only writes the Spec; deterministic code tests it."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import vibedesk.blocks.compile  # noqa: F401  (registers every Block)
from vibedesk.app.llm import Llm, LlmError
from vibedesk.blocks.registry import LIBRARY, Kind
from vibedesk.blocks.spec import MAX_COMBINATIONS, SpecInvalid, expand_grid, validate
from vibedesk.market.universe import SYMBOLS

ATTEMPTS = 3

EXAMPLE = {
    "name": "GOLD H1 Donchian breakout, ATR stop, 2R target",
    "symbol": "GOLD",
    "timeframe": "H1",
    "direction": "both",
    "entry": {"block": "donchian_breakout", "params": {"period": [20, 55]}},
    "filters": [{"block": "adx", "params": {"min": [0, 20]}}],
    "exit": {
        "stop": {"block": "atr_stop", "params": {"mult": 2.0}},
        "target": {"block": "r_target", "params": {"r": [2.0, 3.0]}},
    },
    "sizing": {"block": "fixed_risk", "params": {}},
}


def catalog() -> str:
    """Every Block, by kind, with its parameters, defaults and ranges."""
    lines = []
    for kind in Kind:
        lines.append(f"{kind.value.upper()} blocks:")
        for block in (b for b in LIBRARY.all() if b.kind is kind):
            props = block.params.model_json_schema().get("properties", {})
            params = ", ".join(_param(name, p) for name, p in props.items()) or "no params"
            doc = " ".join(block.doc.split("\n\n")[0].split())
            lines.append(f"- {block.name}({params}): {doc}")
    return "\n".join(lines)


def system_prompt() -> str:
    return f"""You are the Research Desk of Vibe Desk, a strategy-testing desk for one trader who \
trades GOLD and FX majors on MT5 (broker XM). The owner describes a trading idea in chat, in any \
language. You turn it into ONE Strategy Spec, built only from the Blocks below. Deterministic code \
then tests the Spec walk-forward on real XM data from 2014 to 2024, with real spreads and swaps: \
for each year from 2017 it picks the best parameter values on the 3 years before and trades the \
year after blind. You never trade and never promise profit.

Symbols: {", ".join(SYMBOLS)}. Timeframes: M15, H1, H4, D1. Direction: long, short or both.

{catalog()}

Spec rules:
- exactly one entry block; up to 4 filters (all must agree); exit.stop is required; \
exit.target, exit.trailing, exit.time and exit.opposite are optional; sizing is always \
{{"block": "fixed_risk", "params": {{}}}}.
- a parameter left out takes its default. A parameter may be a list of values to try, e.g. \
"period": [20, 55]; keep the whole grid at 12 combinations or fewer (hard limit \
{MAX_COMBINATIONS}).
- name: a short title, at most 80 characters.
- if the idea is vague, pick sensible textbook values and say what you assumed; if part of it \
can't be expressed with these Blocks, say so plainly and build the closest Spec.

Example Spec:
```json
{json.dumps(EXAMPLE, indent=1)}
```

Reply with 1 to 4 short sentences in the owner's language explaining the Spec and anything you \
assumed, then the Spec in one ```json block. Nothing after the block."""


@dataclass(frozen=True)
class Draft:
    spec: dict
    explanation: str
    combinations: int
    attempts: int


class DraftFailed(Exception):
    pass


def draft_spec(llm: Llm, idea: str, history: list[dict] | None = None) -> Draft:
    """Ask the AI for a Spec, feeding back validation errors until it is valid."""
    messages = [{"role": "system", "content": system_prompt()}]
    for message in [*(history or []), {"role": "user", "content": idea}]:
        role, content = message.get("role"), str(message.get("content", ""))
        if role not in ("user", "assistant"):
            continue
        if messages[-1]["role"] == role:  # a test result, then the owner's next words
            messages[-1] = {"role": role, "content": messages[-1]["content"] + "\n\n" + content}
        else:
            messages.append({"role": role, "content": content})
    problem = ""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            reply = llm.chat(messages)
        except LlmError as error:
            raise DraftFailed(str(error)) from error
        explanation, raw = split_reply(reply)
        try:
            if raw is None:
                raise ValueError("the reply has no ```json block")
            spec = validate(normalize(json.loads(raw)))
            data = spec.model_dump(mode="json")
            return Draft(data, explanation, len(expand_grid(spec)), attempt)
        except (ValueError, SpecInvalid) as error:  # JSONDecodeError is a ValueError
            problem = str(error)
            messages += [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": f"That Spec is not valid: {problem}. Fix it and "
                 "reply again in the same format."},
            ]  # fmt: skip
    raise DraftFailed(f"the AI couldn't write a valid Spec in {ATTEMPTS} tries: {problem}")


def normalize(spec: dict) -> dict:
    """Make each parameter list one number type: JSON writers (the browser's, the AI's) turn
    2.0 into 2, and the Spec refuses a list that mixes 1.5 and 2."""

    def fix(value):
        if isinstance(value, list):
            numbers = [v for v in value if isinstance(v, int | float) and not isinstance(v, bool)]
            if numbers and len(numbers) == len(value) and any(isinstance(v, float) for v in value):
                return [float(v) for v in value]
            return value
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        return value

    return fix(spec)


def split_reply(reply: str) -> tuple[str, str | None]:
    """(the explanation, the last ```json block's text or None)."""
    blocks = list(re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", reply, re.DOTALL))
    if not blocks:
        start, end = reply.find("{"), reply.rfind("}")
        if start != -1 and end > start:
            return reply[:start].strip(), reply[start : end + 1]
        return reply.strip(), None
    last = blocks[-1]
    return reply[: blocks[0].start()].strip(), last.group(1)


def _param(name: str, schema: dict) -> str:
    kind = schema.get("type", "")
    if "enum" in schema:
        kind = "|".join(str(v) for v in schema["enum"])
    low = schema.get("minimum", schema.get("exclusiveMinimum"))
    high = schema.get("maximum", schema.get("exclusiveMaximum"))
    span = f" {low}..{high}" if low is not None or high is not None else ""
    return f"{name}: {kind}{span} = {json.dumps(schema.get('default'))}"
