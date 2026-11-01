"""The Block Library: every Block the Research Desk may compose a Strategy Spec from."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel


class Kind(StrEnum):
    ENTRY = "entry"
    FILTER = "filter"
    STOP = "stop"
    TARGET = "target"
    TRAILING = "trailing"
    TIME = "time"
    OPPOSITE = "opposite"
    SIZING = "sizing"


@dataclass(frozen=True)
class BlockDef:
    name: str
    kind: Kind
    params: type[BaseModel]
    fn: Callable
    doc: str
    approved: bool = True


class BlockRegistry:
    def __init__(self) -> None:
        self._defs: dict[str, BlockDef] = {}

    def register(
        self, name: str, kind: Kind, params: type[BaseModel], approved: bool = True
    ) -> Callable:
        def decorator(fn: Callable) -> Callable:
            if name in self._defs:
                raise ValueError(f"block {name!r} is already registered")
            doc = (fn.__doc__ or "").strip()
            self._defs[name] = BlockDef(name, kind, params, fn, doc, approved=approved)
            return fn

        return decorator

    def get(self, name: str) -> BlockDef | None:
        return self._defs.get(name)

    def all(self) -> list[BlockDef]:
        return list(self._defs.values())


LIBRARY = BlockRegistry()
