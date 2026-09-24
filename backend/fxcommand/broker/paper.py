"""Paper Execution Mode (ADR 0007): a Broker adapter that routes Paper Sessions' orders to a local book.

``PaperRouter`` wraps the real Broker. Reads pass straight through; ``market_order`` for a Paper
Session's Magic Number, and ``modify`` / ``close`` for Paper tickets (always negative), go to the
``PaperBook``; everything else goes to the real Broker. ``positions`` and ``history`` merge both, so
the engine treats Paper and Broker Sessions alike.

The book only ever sees the real Broker through ``ReadOnlyBroker``, whose order methods raise:
whatever goes wrong in the book, it cannot send an order.

Fill rules: entries fill at the current ask (long) / bid (short) plus ``SLIPPAGE_POINTS`` against
the trade. Stops are checked on every closed M1 bar since the last check — stop-loss first when a bar
touches both, a gap fills at the bar's open — and then against the current tick. That same catch-up
settles Paper Positions after the application was down.
"""

from __future__ import annotations

from typing import Protocol

from .base import Broker
from .types import ClosedTrade, OrderResult, Position, Side, SymbolInfo, Timeframe

SLIPPAGE_POINTS = 1
MAX_CATCHUP_BARS = 5000

RET_DONE = 10009
RET_INVALID_VOLUME = 10014
RET_INVALID_STOPS = 10016
RET_NOT_FOUND = 10036


class PaperViolation(RuntimeError):
    """The Paper book tried to reach an order method of the real Broker."""


class ReadOnlyBroker:
    """A view of a Broker without its order methods."""

    def __init__(self, inner: Broker):
        self._inner = inner

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def market_order(self, *a, **k):
        raise PaperViolation("the Paper book must never send an order")

    def modify(self, *a, **k):
        raise PaperViolation("the Paper book must never modify a real position")

    def close(self, *a, **k):
        raise PaperViolation("the Paper book must never close a real position")


class PaperRepo(Protocol):
    """Persistence the book needs (implemented by ``Store``)."""

    def paper_open(self) -> list: ...
    def paper_add(self, row) -> None: ...
    def paper_update(self, ticket: int, **fields) -> None: ...
    def paper_closed_since(self, since: int) -> list: ...
    def paper_next_ticket(self) -> int: ...


class PaperBook:
    def __init__(self, repo: PaperRepo):
        self.repo = repo

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _money(info: SymbolInfo, side: str, price_open: float, price_close: float, volume: float) -> float:
        diff = price_close - price_open if side == "long" else price_open - price_close
        return diff / info.trade_tick_size * info.trade_tick_value * volume

    def _settle(self, ro, row, price: float, reason: str, when: int) -> None:
        info = ro.symbol_info(row.symbol)
        price = round(price, info.digits)
        profit = round(self._money(info, row.side, row.price_open, price, row.volume), 2)
        self.repo.paper_update(row.ticket, status="closed", price_close=price, time_close=int(when), profit=profit, reason=reason)

    # ---------------------------------------------------------------- sync
    def sync(self, ro) -> None:
        """Settle SL/TP hits since the last check: closed M1 bars first (SL before TP, gaps at the
        open), then the current tick."""
        now = ro.server_time()
        for row in self.repo.paper_open():
            info = ro.symbol_info(row.symbol)
            tick = ro.tick(row.symbol)
            spread = tick.ask - tick.bid
            slip = SLIPPAGE_POINTS * info.point
            long = row.side == "long"
            checked = row.checked_to or row.time
            n = min(MAX_CATCHUP_BARS, max(3, (now - checked) // 60 + 3))
            bars = ro.closed_bars(row.symbol, Timeframe.M1, int(n))
            done = False
            for t, o, h, l in zip(bars["time"].to_numpy(), bars["open"].to_numpy(), bars["high"].to_numpy(), bars["low"].to_numpy()):
                t = int(t)
                if t < checked:
                    continue
                if long:
                    if row.sl and l <= row.sl:
                        self._settle(ro, row, min(row.sl, o) - slip, "sl", t + 60)
                        done = True
                    elif row.tp and h >= row.tp:
                        self._settle(ro, row, max(row.tp, o), "tp", t + 60)
                        done = True
                else:
                    if row.sl and h + spread >= row.sl:
                        self._settle(ro, row, max(row.sl, o + spread) + slip, "sl", t + 60)
                        done = True
                    elif row.tp and l + spread <= row.tp:
                        self._settle(ro, row, min(row.tp, o + spread), "tp", t + 60)
                        done = True
                if done:
                    break
                checked = t + 60
            if done:
                continue
            if long:
                if row.sl and tick.bid <= row.sl:
                    self._settle(ro, row, tick.bid - slip, "sl", tick.time)
                    continue
                if row.tp and tick.bid >= row.tp:
                    self._settle(ro, row, tick.bid, "tp", tick.time)
                    continue
            else:
                if row.sl and tick.ask >= row.sl:
                    self._settle(ro, row, tick.ask + slip, "sl", tick.time)
                    continue
                if row.tp and tick.ask <= row.tp:
                    self._settle(ro, row, tick.ask, "tp", tick.time)
                    continue
            if checked != row.checked_to:
                self.repo.paper_update(row.ticket, checked_to=int(checked))

    # -------------------------------------------------------------- orders
    def open(self, ro, symbol: str, side: Side, volume: float, sl: float, tp: float, magic: int, comment: str) -> OrderResult:
        info = ro.symbol_info(symbol)
        steps = volume / info.volume_step
        if volume < info.volume_min - 1e-12 or volume > info.volume_max + 1e-12 or abs(steps - round(steps)) > 1e-6:
            return OrderResult(False, RET_INVALID_VOLUME, f"invalid volume {volume}")
        tick = ro.tick(symbol)
        min_dist = info.stops_level * info.point
        long = side == "long"
        if not sl or (long and sl > tick.bid - min_dist) or (not long and sl < tick.ask + min_dist):
            return OrderResult(False, RET_INVALID_STOPS, f"invalid stop-loss {sl}")
        if tp and ((long and tp < tick.bid + min_dist) or (not long and tp > tick.ask - min_dist)):
            return OrderResult(False, RET_INVALID_STOPS, f"invalid take-profit {tp}")
        slip = SLIPPAGE_POINTS * info.point
        price = round(tick.ask + slip if long else tick.bid - slip, info.digits)
        ticket = self.repo.paper_next_ticket()
        from ..store.models import PaperPositionRow  # local import: the broker package stays store-free at import time

        now = ro.server_time()
        self.repo.paper_add(
            PaperPositionRow(
                ticket=ticket, symbol=symbol, side=side, volume=round(volume, 2), price_open=price, sl=sl, tp=tp or 0.0,
                magic=magic, time=now, comment=comment[:31], checked_to=now,
            )
        )
        return OrderResult(True, RET_DONE, "paper fill", ticket=ticket, price=price, volume=round(volume, 2))

    def _find(self, ticket: int):
        return next((r for r in self.repo.paper_open() if r.ticket == ticket), None)

    def modify(self, ro, ticket: int, sl: float, tp: float) -> OrderResult:
        row = self._find(ticket)
        if row is None:
            return OrderResult(False, RET_NOT_FOUND, f"paper position {ticket} not found")
        self.repo.paper_update(ticket, sl=sl, tp=tp or 0.0)
        return OrderResult(True, RET_DONE, "paper modify", ticket=ticket)

    def close(self, ro, ticket: int, comment: str = "") -> OrderResult:
        row = self._find(ticket)
        if row is None:
            return OrderResult(False, RET_NOT_FOUND, f"paper position {ticket} not found")
        tick = ro.tick(row.symbol)
        slip = SLIPPAGE_POINTS * ro.symbol_info(row.symbol).point
        price = tick.bid - slip if row.side == "long" else tick.ask + slip
        self._settle(ro, row, price, "manual" if comment == "manual" else "expert", ro.server_time())
        return OrderResult(True, RET_DONE, "paper close", ticket=ticket, price=price, volume=row.volume)

    # --------------------------------------------------------------- reads
    def positions(self, ro, magic: int | None = None) -> list[Position]:
        out = []
        for r in self.repo.paper_open():
            if magic is not None and r.magic != magic:
                continue
            info = ro.symbol_info(r.symbol)
            tick = ro.tick(r.symbol)
            cur = tick.bid if r.side == "long" else tick.ask
            out.append(
                Position(
                    ticket=r.ticket, symbol=r.symbol, side=r.side, volume=r.volume, price_open=r.price_open, price_current=cur,
                    sl=r.sl, tp=r.tp, profit=round(self._money(info, r.side, r.price_open, cur, r.volume), 2),
                    magic=r.magic, time=r.time, comment=r.comment,
                )
            )
        return out

    def history(self, since: int) -> list[ClosedTrade]:
        return [
            ClosedTrade(
                ticket=r.ticket, symbol=r.symbol, side=r.side, volume=r.volume, price_open=r.price_open,
                price_close=r.price_close, profit=r.profit, magic=r.magic, time_open=r.time, time_close=r.time_close, reason=r.reason,
            )
            for r in self.repo.paper_closed_since(since)
        ]


class PaperRouter:
    """The Broker the engine talks to: real Broker plus the Paper book, routed by Magic Number / ticket."""

    def __init__(self, inner: Broker, book: PaperBook):
        self.inner = inner
        self.book = book
        self.ro = ReadOnlyBroker(inner)
        self.paper_magics: set[int] = set()  # kept current by the engine

    @property
    def mode(self) -> str:
        return self.inner.mode

    def __getattr__(self, name: str):  # server_time, account, tick, sim controls, ...
        return getattr(self.inner, name)

    def positions(self, magic: int | None = None) -> list[Position]:
        real = [] if magic is not None and magic in self.paper_magics else self.inner.positions(magic)
        if magic is not None and magic not in self.paper_magics:
            return real
        self.book.sync(self.ro)
        return real + self.book.positions(self.ro, magic)

    def history(self, since: int) -> list[ClosedTrade]:
        return self.inner.history(since) + self.book.history(since)

    def market_order(self, symbol: str, side: Side, volume: float, sl: float, tp: float, magic: int, comment: str = "") -> OrderResult:
        if magic in self.paper_magics:
            return self.book.open(self.ro, symbol, side, volume, sl, tp, magic, comment)
        return self.inner.market_order(symbol, side, volume, sl, tp, magic, comment)

    def modify(self, ticket: int, sl: float, tp: float) -> OrderResult:
        if ticket < 0:
            return self.book.modify(self.ro, ticket, sl, tp)
        return self.inner.modify(ticket, sl, tp)

    def close(self, ticket: int, comment: str = "") -> OrderResult:
        if ticket < 0:
            return self.book.close(self.ro, ticket, comment)
        return self.inner.close(ticket, comment)
