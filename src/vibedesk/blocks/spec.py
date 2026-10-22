"""Strategy Spec: the only form a strategy exists in (ADR 0001).

`validate` turns JSON from the Research Desk into a StrategySpec, or raises SpecInvalid with every
problem it found as a (path, reason) pair the Research Desk can read. `expand_grid` turns a valid
Spec into its concrete parameter Combinations, in an order that does not depend on how the JSON
was written.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
)

from vibedesk.blocks.registry import LIBRARY, BlockRegistry, Kind

MAX_COMBINATIONS = 50

Symbol = Literal["GOLD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"]
SYMBOLS = get_args(Symbol)
Direction = Literal["long", "short", "both"]

Scalar = StrictBool | StrictInt | StrictFloat | StrictStr
ParamValue = Scalar | list[Scalar]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BlockRef(_Strict):
    block: str
    params: dict[str, ParamValue] = Field(default_factory=dict)


class ExitSpec(_Strict):
    stop: BlockRef
    target: BlockRef | None = None
    trailing: BlockRef | None = None
    time: BlockRef | None = None
    opposite: BlockRef | None = None


class StrategySpec(_Strict):
    name: str = Field(min_length=1, max_length=80)
    symbol: Symbol
    timeframe: Literal["M15", "H1", "H4", "D1"]
    direction: Direction
    entry: BlockRef
    filters: list[BlockRef] = Field(default_factory=list, max_length=4)
    exit: ExitSpec
    sizing: BlockRef


@dataclass(frozen=True)
class SpecError:
    path: str
    reason: str


class SpecInvalid(ValueError):
    def __init__(self, errors: list[SpecError]) -> None:
        super().__init__("; ".join(f"{e.path}: {e.reason}" for e in errors))
        self.errors = errors


@dataclass(frozen=True)
class Bound:
    """One Block with concrete parameter values."""

    block: str
    params: BaseModel


@dataclass(frozen=True)
class Combination:
    """One point of a Spec's parameter grid: everything needed to compile signals."""

    direction: Direction
    entry: Bound
    filters: tuple[Bound, ...]
    stop: Bound
    target: Bound | None
    trailing: Bound | None
    time: Bound | None
    opposite: Bound | None
    sizing: Bound

    def label(self) -> str:
        """A canonical JSON string that identifies this Combination."""

        def dump(bound: Bound | None):
            return None if bound is None else [bound.block, bound.params.model_dump(mode="json")]

        return json.dumps(
            {
                "direction": self.direction,
                "entry": dump(self.entry),
                "filters": [dump(f) for f in self.filters],
                "stop": dump(self.stop),
                "target": dump(self.target),
                "trailing": dump(self.trailing),
                "time": dump(self.time),
                "opposite": dump(self.opposite),
                "sizing": dump(self.sizing),
            },
            sort_keys=True,
        )


def validate(data: Mapping | str, registry: BlockRegistry = LIBRARY) -> StrategySpec:
    try:
        if isinstance(data, str):
            spec = StrategySpec.model_validate_json(data)
        else:
            spec = StrategySpec.model_validate(data)
    except ValidationError as exc:
        raise SpecInvalid(_from_pydantic("", exc)) from None

    errors: list[SpecError] = []
    size = 1
    for path, kind, ref in _slots(spec):
        combos, slot_errors = _slot_grid(path, kind, ref, registry)
        errors += slot_errors
        size *= len(combos)
    if not errors and size > MAX_COMBINATIONS:
        errors.append(
            SpecError("grid", f"{size} parameter combinations; the limit is {MAX_COMBINATIONS}")
        )
    if errors:
        raise SpecInvalid(errors)
    return spec


def expand_grid(spec: StrategySpec, registry: BlockRegistry = LIBRARY) -> list[Combination]:
    slots = _slots(spec)
    grids = []
    for path, kind, ref in slots:
        combos, errors = _slot_grid(path, kind, ref, registry)
        if errors:
            raise SpecInvalid(errors)
        grids.append([Bound(ref.block, params) for params in combos])

    n_filters = len(spec.filters)
    result = []
    for point in itertools.product(*grids):
        entry, filters, rest = point[0], point[1 : 1 + n_filters], point[1 + n_filters :]
        exits = dict(zip([path for path, _, _ in slots[1 + n_filters :]], rest, strict=True))
        result.append(
            Combination(
                direction=spec.direction,
                entry=entry,
                filters=tuple(filters),
                stop=exits["exit.stop"],
                target=exits.get("exit.target"),
                trailing=exits.get("exit.trailing"),
                time=exits.get("exit.time"),
                opposite=exits.get("exit.opposite"),
                sizing=exits["sizing"],
            )
        )
    return result


def _slots(spec: StrategySpec) -> list[tuple[str, Kind, BlockRef]]:
    slots = [("entry", Kind.ENTRY, spec.entry)]
    slots += [(f"filters[{i}]", Kind.FILTER, ref) for i, ref in enumerate(spec.filters)]
    for kind in (Kind.STOP, Kind.TARGET, Kind.TRAILING, Kind.TIME, Kind.OPPOSITE):
        ref = getattr(spec.exit, kind.value)
        if ref is not None:
            slots.append((f"exit.{kind.value}", kind, ref))
    slots.append(("sizing", Kind.SIZING, spec.sizing))
    return slots


def _slot_grid(
    path: str, kind: Kind, ref: BlockRef, registry: BlockRegistry
) -> tuple[list[BaseModel], list[SpecError]]:
    block = registry.get(ref.block)
    if block is None:
        known = ", ".join(sorted(b.name for b in registry.all() if b.kind is kind))
        return [], [SpecError(f"{path}.block", f"unknown block; known {kind} blocks: {known}")]
    if block.kind is not kind:
        return [], [SpecError(f"{path}.block", f"{ref.block} is a {block.kind} block, not {kind}")]

    errors: list[SpecError] = []
    # Declared order first, so the grid order never depends on the JSON's key order. Unknown
    # names go last; the param model rejects them below with a path the Research Desk can fix.
    declared = [name for name in block.params.model_fields if name in ref.params]
    names = declared + sorted(set(ref.params) - set(declared))
    axes = []
    for name in names:
        values = ref.params[name]
        values = values if isinstance(values, list) else [values]
        problem = _list_problem(values)
        if problem:
            errors.append(SpecError(f"{path}.params.{name}", problem))
            continue
        axes.append(sorted(values))
    if errors:
        return [], errors

    combos, seen = [], set()
    for point in itertools.product(*axes):
        try:
            combos.append(block.params.model_validate(dict(zip(names, point, strict=True))))
        except ValidationError as exc:
            for error in _from_pydantic(f"{path}.params", exc):
                if error not in seen:
                    seen.add(error)
                    errors.append(error)
    return ([] if errors else combos), errors


def _list_problem(values: list) -> str | None:
    if not values:
        return "an empty list; give at least one value"
    if len({type(v) for v in values}) > 1:
        return "values in one list must all have the same type"
    if len(set(values)) != len(values):
        return "a list must not repeat a value"
    return None


def _from_pydantic(prefix: str, exc: ValidationError) -> list[SpecError]:
    errors = []
    for error in exc.errors():
        path = prefix
        for part in error["loc"]:
            if isinstance(part, int):
                path += f"[{part}]"
            else:
                path = f"{path}.{part}" if path else str(part)
        errors.append(SpecError(path, error["msg"]))
    return errors
