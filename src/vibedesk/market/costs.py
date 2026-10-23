"""What a symbol costs to trade today, measured read-only from MT5 (ADR 0004, plan "Spread Floor").

`measure_cost` reads the last 14 days of ticks and the symbol's swaps; `record_cost` appends the
measurement to {root}/costs/{symbol}.jsonl; `current_cost` turns the latest line into the
CostSnapshot a Trial runs with. Only spreads leave this module: the ticks fall in the Holdout
period, so no price level is stored, returned or put in a message (ADR 0002).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from vibedesk.engine.costs import CostSnapshot
from vibedesk.market.mt5_api import SWAP_IN_POINTS, Mt5Api, SymbolInfo
from vibedesk.market.server_time import server_bound
from vibedesk.market.universe import SYMBOLS

FLOOR_WINDOW = timedelta(days=14)
SLIPPAGE_POINTS = 1.0  # charged on every fill


class CostError(Exception):
    """A cost that can't be measured or found; the message names the symbol and the reason."""


@dataclass(frozen=True, slots=True)
class SpreadFloor:
    """Spreads of the ticks that have both a bid and an ask, in points."""

    median_points: float  # the Spread Floor
    p90_points: float
    ticks: int


@dataclass(frozen=True, slots=True)
class CostMeasurement:
    """One measurement: a line of {root}/costs/{symbol}.jsonl. It holds no price."""

    symbol: str
    measured_at: str  # ISO UTC time the 14-day tick window ends
    point: float
    spread_floor_points: float
    spread_p90_points: float
    ticks: int
    swap_long_points: float  # per lot per night; negative means the position pays
    swap_short_points: float
    triple_swap_day: int  # MT5 numbering, 0 = Sunday, as in CostSnapshot

    def snapshot(self) -> CostSnapshot:
        """The Cost Model a Trial runs with. A swap the position would earn counts as zero:
        today's rates are charged on every night back to 2014, and nothing records what XM paid
        then, so a credit could flatter a strategy that merely holds the paying side (ADR 0004).
        A swap the position pays is charged in full. The measured rates stay in the log line."""
        return CostSnapshot(
            point=self.point,
            spread_floor_points=self.spread_floor_points,
            slippage_points=SLIPPAGE_POINTS,
            swap_long_points=min(self.swap_long_points, 0.0),
            swap_short_points=min(self.swap_short_points, 0.0),
            triple_swap_day=self.triple_swap_day,
            as_of=datetime.fromisoformat(self.measured_at).date().isoformat(),
        )


def measure_floor(api: Mt5Api, symbol: str, now: datetime) -> SpreadFloor:
    """The Spread Floor: the median spread of the ticks in the 14 days before `now`."""
    return _floor(api, symbol, _utc(now), _symbol_info(api, symbol).point)


def measure_cost(api: Mt5Api, symbol: str, now: datetime) -> CostMeasurement:
    """The Spread Floor, plus the swaps and the triple-swap day from `symbol_info`.

    A symbol whose swaps are not quoted in points per lot is refused before any tick is read:
    the Cost Model charges swap in points and has no conversion for MT5's other swap modes.
    """
    now = _utc(now)
    info = _symbol_info(api, symbol)
    if info.swap_mode != SWAP_IN_POINTS:
        raise CostError(
            f"{symbol}: swap_mode {info.swap_mode} is not {SWAP_IN_POINTS} (points per lot); "
            "the Cost Model charges swap only in points"
        )
    if info.swap_rollover3days not in range(7):
        raise CostError(f"{symbol}: swap_rollover3days {info.swap_rollover3days} is no weekday")
    floor = _floor(api, symbol, now, info.point)
    return CostMeasurement(
        symbol=symbol,
        measured_at=now.isoformat(),
        point=float(info.point),
        spread_floor_points=floor.median_points,
        spread_p90_points=floor.p90_points,
        ticks=floor.ticks,
        swap_long_points=float(info.swap_long),
        swap_short_points=float(info.swap_short),
        # MT5 and CostSnapshot both number the days from Sunday = 0, so the day carries over.
        triple_swap_day=int(info.swap_rollover3days),
    )


def record_cost(root: Path | str, measurement: CostMeasurement) -> Path:
    """Append a measurement to {root}/costs/{symbol}.jsonl. Earlier lines are never rewritten."""
    path = _path(root, measurement.symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as file:
        file.write(json.dumps(asdict(measurement), sort_keys=True) + "\n")
    return path


def current_cost(root: Path | str, symbol: str) -> CostSnapshot:
    """The latest measurement of `symbol` as a CostSnapshot, with 1 point of slippage."""
    path = _path(root, symbol)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        raise CostError(f"{symbol}: no cost measured yet ({path} does not exist)") from None
    latest = next((line for line in reversed(lines) if line.strip()), None)
    if latest is None:
        raise CostError(f"{symbol}: {path} holds no measurement")
    return CostMeasurement(**json.loads(latest)).snapshot()


def _floor(api: Mt5Api, symbol: str, now: datetime, point: float) -> SpreadFloor:
    date_from = server_bound(int((now - FLOOR_WINDOW).timestamp()))
    date_to = server_bound(int(now.timestamp()))
    ticks = api.copy_ticks_range(symbol, date_from, date_to, api.COPY_TICKS_INFO)
    if ticks is None:
        raise CostError(f"{symbol}: copy_ticks_range failed: {api.last_error()}")
    both = (ticks["bid"] > 0) & (ticks["ask"] > 0)
    if not both.any():
        raise CostError(
            f"{symbol}: no tick with both a bid and an ask in the "
            f"{FLOOR_WINDOW.days} days to {now:%Y-%m-%d %H:%M} UTC"
        )
    # Prices sit on the point grid, so a spread is a whole number of points up to float error.
    spreads = np.rint((ticks["ask"][both] - ticks["bid"][both]) / point)
    return SpreadFloor(
        float(np.median(spreads)), float(np.percentile(spreads, 90)), int(both.sum())
    )


def _symbol_info(api: Mt5Api, symbol: str) -> SymbolInfo:
    info = api.symbol_info(symbol)
    if info is None:
        raise CostError(f"{symbol}: symbol_info failed: {api.last_error()}")
    return info


def _utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware: a naive time is read in the laptop's zone")
    return now.astimezone(UTC)


def _path(root: Path | str, symbol: str) -> Path:
    # The name becomes part of a file path, so only a symbol Vibe Desk trades may reach it.
    if symbol not in SYMBOLS:
        raise CostError(f"{symbol!r}: not a symbol Vibe Desk trades ({', '.join(SYMBOLS)})")
    return Path(root) / "costs" / f"{symbol}.jsonl"
