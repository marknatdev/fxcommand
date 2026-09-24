"""Event bus, Journal and the in-memory application log buffer."""

from __future__ import annotations

import asyncio
import collections
import logging
import threading
import time
from typing import Any

from ..store import JournalRow, Store


class EventBus:
    """Fan-out of events to WebSocket subscribers. ``publish`` is safe from any thread."""

    def __init__(self, maxsize: int = 1000):
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._maxsize = maxsize

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(self._maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _deliver(self, event: dict) -> None:
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()  # drop oldest for slow consumers
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(event)

    def publish(self, type_: str, payload: Any) -> None:
        event = {"type": type_, "data": payload}
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._deliver(event)
        else:
            loop.call_soon_threadsafe(self._deliver, event)


def journal_dict(row: JournalRow) -> dict:
    return {
        "id": row.id,
        "ts": row.ts,
        "wall": row.wall,
        "session_id": row.session_id,
        "symbol": row.symbol,
        "kind": row.kind,
        "level": row.level,
        "alert": row.alert,
        "message": row.message,
        "data": row.data or {},
    }


class Journal:
    """The permanent record of what every Session did and why."""

    def __init__(self, store: Store, bus: EventBus):
        self.store = store
        self.bus = bus
        self._log = logging.getLogger("fxcommand.journal")

    def record(
        self,
        kind: str,
        message: str,
        *,
        ts: int,
        session_id: int | None = None,
        symbol: str | None = None,
        level: str = "info",
        alert: bool = False,
        data: dict | None = None,
    ) -> dict:
        row = self.store.add_journal(
            JournalRow(
                ts=int(ts),
                wall=time.time(),
                session_id=session_id,
                symbol=symbol,
                kind=kind,
                level=level,
                alert=alert,
                message=message,
                data=_jsonable(data or {}),
            )
        )
        d = journal_dict(row)
        self.bus.publish("journal", d)
        if alert:
            self.bus.publish("alert", d)
        self._log.log(
            {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}.get(level, logging.INFO),
            "[%s]%s%s %s",
            kind,
            f" s{session_id}" if session_id else "",
            f" {symbol}" if symbol else "",
            message,
        )
        return d


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    if hasattr(v, "item"):  # numpy scalar
        return _jsonable(v.item())
    return v


class LogBuffer(logging.Handler):
    """Keeps the last N application log lines for the Logs page and streams new ones."""

    def __init__(self, bus: EventBus, capacity: int = 2000):
        super().__init__(level=logging.INFO)
        self.bus = bus
        self._lines: collections.deque[dict] = collections.deque(maxlen=capacity)
        self._seq = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self._lock:
                self._seq += 1
                line = {
                    "seq": self._seq,
                    "time": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage() + (f"\n{self.formatException(record.exc_info)}" if record.exc_info else ""),
                }
                self._lines.append(line)
            self.bus.publish("log", line)
        except Exception:  # pragma: no cover
            self.handleError(record)

    def formatException(self, ei) -> str:  # noqa: N802
        return logging.Formatter().formatException(ei)

    def lines(self, level: str | None = None, after_seq: int = 0, limit: int = 500) -> list[dict]:
        order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        min_level = order.get((level or "").upper(), 0)
        with self._lock:
            out = [l for l in self._lines if l["seq"] > after_seq and order.get(l["level"], 0) >= min_level]
        return out[-limit:]
