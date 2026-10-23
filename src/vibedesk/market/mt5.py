"""Mt5BarSource: bars read from the MT5 terminal, in UTC. Only the importer uses it.

It never imports the MetaTrader5 module. The caller hands in the API, the real one from
`real_api()` (behind the read-only allowlist) or FakeMt5, and owns the connection: it calls
`initialize` before the first read and `shutdown` after the last.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from vibedesk.market.bars import Bars, Timeframe
from vibedesk.market.grant import RESEARCH_START
from vibedesk.market.mt5_api import TIMEFRAME_CONSTANTS, Mt5Api
from vibedesk.market.server_time import DAY, server_bound, server_to_utc


class Mt5Error(RuntimeError):
    """The terminal refused a read; the message carries its `last_error()`."""


def _now() -> datetime:
    return datetime.now(UTC)


class Mt5BarSource:
    """A BarSource over the MT5 terminal: bars whose UTC open time is in [start, end).

    MT5 reads and returns server wall-clock times, so the UTC range is widened by a day on each
    side, converted to server-clock bounds, read with `copy_rates_range`, converted back with
    `server_to_utc` and trimmed (tasks/plan.md, "Server clock to UTC"). MT5's extra columns
    (tick_volume, real_volume) are dropped; point and digits come from `symbol_info`.

    MT5 has no unbounded read, so a missing bound means the edge of what Vibe Desk imports: a
    missing start is RESEARCH_START, a missing end is `clock()` (now, by default). The bars
    that opened before the end include one that may still be forming; keeping closed bars only
    is the importer's job.
    """

    def __init__(self, api: Mt5Api, *, clock: Callable[[], datetime] = _now) -> None:
        self._api = api
        self._clock = clock
        self._symbols: dict[str, tuple[float, int]] = {}

    def fetch(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Bars:
        timeframe = Timeframe(timeframe)
        start = RESEARCH_START if start is None else start
        end = self._clock() if end is None else end
        for name, value in (("start", start), ("end", end)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be a timezone-aware UTC datetime")
        if start > end:
            raise ValueError("start must not be after end")
        lo, hi = int(start.timestamp()), int(end.timestamp())

        point, digits = self._select(symbol)
        rates = self._api.copy_rates_range(
            symbol,
            getattr(self._api, TIMEFRAME_CONSTANTS[timeframe]),
            server_bound(lo - DAY),
            server_bound(hi + DAY),
        )
        if rates is None:
            raise Mt5Error(
                f"copy_rates_range failed for {symbol} {timeframe} from {start:%Y-%m-%d %H:%M} "
                f"to {end:%Y-%m-%d %H:%M} UTC: {self._api.last_error()}"
            )
        time = server_to_utc(rates["time"])
        keep = (time >= lo) & (time < hi)
        return Bars(
            symbol=symbol,
            timeframe=timeframe,
            point=point,
            digits=digits,
            time=time[keep],
            **{name: rates[name][keep] for name in ("open", "high", "low", "close", "spread")},
        )

    def _select(self, symbol: str) -> tuple[float, int]:
        """Add the symbol to Market Watch, so its history can be read, and take its point and
        digits; once per symbol."""
        if symbol not in self._symbols:
            if not self._api.symbol_select(symbol, True):
                raise Mt5Error(f"symbol_select failed for {symbol}: {self._api.last_error()}")
            info = self._api.symbol_info(symbol)
            if info is None:
                raise Mt5Error(f"symbol_info failed for {symbol}: {self._api.last_error()}")
            self._symbols[symbol] = (float(info.point), int(info.digits))
        return self._symbols[symbol]
